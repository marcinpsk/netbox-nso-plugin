# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Periodic system jobs for the NSO plugin."""

import contextlib
import logging
import time
from abc import abstractmethod

from core.choices import JobIntervalChoices
from django.db import connection
from netbox.jobs import JobRunner, system_job

from .deployment import DeploymentQuiesced
from .deployment import guarded as _deployment_guarded

logger = logging.getLogger(__name__)

# Minutes between last-sync mirror refreshes. The adapter polls devices far more often
# than this; the window only bounds how stale a NetBox page can be with nobody on it.
SYNC_CACHE_REFRESH_MINUTES = 5
RENDERER_AUDIT_MINUTES = 5

#: The RQ timeout every plugin system job enqueues with, so none depends on RQ_DEFAULT_TIMEOUT.
SYSTEM_JOB_TIMEOUT_SECONDS = 600
#: When a run stops starting work. The rest of the timeout is for the work still in flight then:
#: one settlement poll or link repair makes a few adapter calls of up to the 30 s read timeout.
SYSTEM_JOB_BUDGET_SECONDS = SYSTEM_JOB_TIMEOUT_SECONDS - 360
#: The longest one lock wait in a run may take. A longer wait fails that unit of work, and a later run retries it.
SYSTEM_JOB_LOCK_TIMEOUT = "30s"
#: How long after the run deadline a started settlement poll may still page its evidence feed.
#: The rest of the timeout is for that poll's reads and writes after the feed.
SETTLEMENT_FEED_GRACE_SECONDS = 180
#: The part of the sync-cache budget kept for its last pass, the settlement sweep.
SETTLEMENT_SWEEP_SHARE = 0.25
#: The part of the sync-cache budget kept for the outbox compaction and drain, before the sweep's share.
OUTBOX_DRAIN_SHARE = 0.25


def run_deadline() -> float:
    """Return the monotonic instant a system job run that starts now must stop starting work."""
    return time.monotonic() + SYSTEM_JOB_BUDGET_SECONDS


def lock_wait_timed_out(exc) -> bool:
    """Whether a database error is a lock wait that passed :data:`SYSTEM_JOB_LOCK_TIMEOUT`."""
    return getattr(exc.__cause__, "sqlstate", None) == "55P03"


@contextlib.contextmanager
def _bounded_lock_waits():
    """Set this session's lock_timeout to :data:`SYSTEM_JOB_LOCK_TIMEOUT`, then restore the previous value."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('lock_timeout')")
        previous = cursor.fetchone()[0]
        cursor.execute("SELECT set_config('lock_timeout', %s, false)", [SYSTEM_JOB_LOCK_TIMEOUT])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('lock_timeout', %s, false)", [previous])


class _PluginSystemJob(JobRunner):
    """A system job that sets its own RQ timeout and bounds each run below it.

    RQ kills a run that passes its timeout, and NetBox then never reschedules it: the Job row
    stays ``running`` and ``enqueue_once`` keeps skipping it. So each run stops at
    :func:`run_deadline` and leaves the rest of its work to the next run. A row lock that another
    transaction holds can block a unit of work past that deadline, so every lock wait in
    :meth:`tick` is bounded by :data:`SYSTEM_JOB_LOCK_TIMEOUT`.
    """

    @classmethod
    def enqueue(cls, *args, **kwargs):
        """Enqueue with the plugin's job timeout, also on NetBox's reschedule after each run."""
        kwargs.setdefault("job_timeout", SYSTEM_JOB_TIMEOUT_SECONDS)
        return super().enqueue(*args, **kwargs)

    def run(self, *args, **kwargs):
        """Run one :meth:`tick` with bounded lock waits, restored before NetBox reschedules the job."""
        with _bounded_lock_waits():
            return self.tick()

    @abstractmethod
    def tick(self):
        """Do one run's work."""

    def carried(self, key):
        """Return what the latest earlier run of this job recorded under *key*, or None.

        RQ runs each job in a forked work horse, so process memory does not reach the next run.
        """
        from core.models import Job

        previous = (
            Job.objects.filter(name=self.name, data__has_key=key).exclude(pk=self.job.pk).order_by("-created").first()
        )
        return previous.data[key] if previous is not None else None

    def carry(self, key, value) -> None:
        """Record *value* under *key* on this run's Job row, for the next run."""
        self.job.data = {**(self.job.data or {}), key: value}
        self.job.save(update_fields=("data",))


@system_job(interval=RENDERER_AUDIT_MINUTES)
class AuditRendererScopesJob(_PluginSystemJob):
    """Repair signal-less renderer drift on the periodic five-minute cadence."""

    class Meta:
        name = "Audit NSO renderer scopes"

    def tick(self):
        """Run one bounded fleet audit, or defer it while deployment is quiesced."""
        from .renderer_audit import audit_renderer_fleet

        rotation = self.carried("fleet_rotation") or {}
        try:
            result = audit_renderer_fleet(deadline=run_deadline(), rotation=rotation)
        except DeploymentQuiesced:
            logger.info("AuditRendererScopesJob: paused for an intent deployment")
            return None
        finally:
            self.carry("fleet_rotation", rotation)
        logger.info(
            "AuditRendererScopesJob: %d devices, %d repairs, %d deferred, %d unknown, %d failed",
            result.devices,
            result.repaired,
            result.deferred,
            result.unknown,
            result.failed,
        )
        return result


@system_job(interval=JobIntervalChoices.INTERVAL_HOURLY)
class AdvanceStaleOnboardingJob(_PluginSystemJob):
    """Hourly backstop that advances every durable provision tombstone.

    The sweep polls open attempts and completes terminal attempts. It also offboards successful
    attempts that no longer have a matching management incarnation.
    """

    class Meta:
        name = "Advance stale NSO onboarding rows"

    def tick(self):
        """Advance each non-closed provision tombstone."""
        from .onboarding import advance_stale_onboarding_rows

        try:
            advance_stale_onboarding_rows(deadline=run_deadline())
        except DeploymentQuiesced:
            logger.info("AdvanceStaleOnboardingJob: paused for an intent deployment")


@system_job(interval=SYNC_CACHE_REFRESH_MINUTES)
class RefreshDeviceSyncCacheJob(_PluginSystemJob):
    """The plugin's per-device maintenance tick. The name is kept; it does more than it says.

    Four passes today, in this order and for these reasons:

    1. **the last-sync mirror.** ``NSODeviceManagement.last_sync_at``/``last_sync_status``
       are a cache of the adapter's device row, and page renders used to be the only thing
       that wrote it — so a device nobody had opened showed a blank status, and one last
       opened days ago showed a stale 'succeeded', while the adapter had been syncing both
       all along.
    2. **the adapter-mapping repair.** A row whose mapping is broken cannot be mirrored at
       all, and its adapter device id is half the settlement cursor's epoch.
    3. **the intent-outbox drain.** #1503 Appendix O's pass, and the clock that carries a
       scheduled push whose commit callback was lost. It runs after the repair because it
       needs a repaired adapter id, and it re-queries its own candidates rather than reading
       the rows this job materialized, which the repair leaves stale in two of its branches.
    4. **the static-route settlement sweep.** The retry clock for #1502's consumer, and the
       reason it is here rather than on a schedule of its own: this job runs
       plugin-to-adapter, so it survives the failure the callback channel cannot — an
       invalid adapter-to-NetBox token answers 401 on every notification while reads stay
       healthy. It must stay LAST, after the repair whose epoch it depends on.

    Renaming the class would change the system-job identity NetBox has registered, so the
    docstring carries the truth instead.

    The first two passes stop early, so that :data:`OUTBOX_DRAIN_SHARE` of the run's budget
    stays for the drain. The drain stops early, so that :data:`SETTLEMENT_SWEEP_SHARE` stays for
    the sweep. The sweep stops at the run's deadline.
    """

    class Meta:
        name = "Refresh NSO device sync cache"

    def tick(self):
        """Run a scheduled tick, or return normally while a deployment pauses it."""
        refresh_rotation = self.carried("refresh_rotation") or {}
        rotation = self.carried("settlement_rotation") or {}
        try:
            return RefreshDeviceSyncCacheJob._run(run_deadline(), refresh_rotation, rotation)
        except DeploymentQuiesced:
            logger.info("RefreshDeviceSyncCacheJob: paused for an intent deployment")
            return None
        finally:
            self.carry("refresh_rotation", refresh_rotation)
            self.carry("settlement_rotation", rotation)

    @staticmethod
    @_deployment_guarded("device maintenance tick")
    def _run(deadline, refresh_rotation, rotation):
        """Refresh the last-sync mirror, repair mappings, drain the outbox, sweep settlements."""
        from .drain import compact_intent_outbox, drain_intent_outbox
        from .models import NSODeviceManagement
        from .settlement import sweep_static_route_settlements
        from .sync_cache import _snapshot, reconcile_device_links, refresh_sync_caches

        before_sweep = deadline - SYSTEM_JOB_BUDGET_SECONDS * SETTLEMENT_SWEEP_SHARE
        before_drain = before_sweep - SYSTEM_JOB_BUDGET_SECONDS * OUTBOX_DRAIN_SHARE
        rows = list(NSODeviceManagement.objects.select_related("nso_instance"))
        # One adapter snapshot for both passes: they ask the same question of the same data,
        # and a second call could disagree with the first mid-sweep.
        snapshot = _snapshot(rows)
        checked, updated = refresh_sync_caches(
            rows, snapshot=snapshot, deadline=before_drain, rotation=refresh_rotation
        )
        # A row whose mapping is broken can't be mirrored at all — repair it so the next pass
        # has something to mirror.
        broken, attempted = reconcile_device_links(rows, snapshot=snapshot, deadline=before_drain)
        # LAST, and on its own candidate query: the repair above wrote the database while
        # leaving `rows` stale in two of its three branches, so reusing that list here would
        # skip a device repaired this tick or poll it on an id that no longer exists.
        #
        # The one thing the fresh query must NOT re-litigate is a proven global outage. The
        # snapshot's `by_id is None` means the adapter did not answer at all, and per-device
        # isolation is the wrong tool there: every candidate would wait out the full read
        # timeout in turn, so a hundred of them can hold a five-minute job for the best part
        # of an hour. Skip the pass; the next tick is five minutes away.
        _mapped, by_id, _by_identity = snapshot
        drained = drain_failed = polled = settle_failed = 0
        drain_started = settle_started = time.monotonic()
        # The drain guards each key, not its own candidate query, so an error there would
        # abort the tick before the sweep — the one pass that must always run.
        if by_id is None:
            try:
                compact_intent_outbox(deadline=before_sweep)
            except Exception:  # noqa: BLE001 — the sweep below is the retry clock
                logger.exception("RefreshDeviceSyncCacheJob: intent-outbox compaction failed")
            settle_started = time.monotonic()
            logger.warning("RefreshDeviceSyncCacheJob: adapter snapshot unavailable, sends and sweep skipped")
        else:
            # Same rule as the sweep, and for the same reason: a proven global outage is not
            # a per-key failure, and every candidate would wait out its own read timeout.
            try:
                drained, drain_failed = drain_intent_outbox(deadline=before_sweep)
            except Exception:  # noqa: BLE001 — the sweep below is the retry clock
                logger.exception("RefreshDeviceSyncCacheJob: intent-outbox drain failed")
            settle_started = time.monotonic()
            polled, settle_failed = sweep_static_route_settlements(
                deadline=deadline, feed_deadline=deadline + SETTLEMENT_FEED_GRACE_SECONDS, rotation=rotation
            )
        logger.info(
            "RefreshDeviceSyncCacheJob: %d checked, %d updated, %d broken, %d repair attempted, "
            "%d outbox drained, %d outbox failed, outbox drain %.3fs, "
            "%d settlement polled, %d settlement failed, settlement sweep %.3fs",
            checked,
            updated,
            broken,
            attempted,
            drained,
            drain_failed,
            settle_started - drain_started,
            polled,
            settle_failed,
            time.monotonic() - settle_started,
        )


@system_job(interval=JobIntervalChoices.INTERVAL_DAILY)
class PruneApplyLedgerJob(_PluginSystemJob):
    """Prune bounded batches of expired Apply history each day until the ledger ends or the run's deadline."""

    class Meta:
        name = "Prune NSO Apply ledger"

    def tick(self):
        """Use the same eligibility and deletion path as the maintenance command."""
        from .apply_retention import prune_apply_ledger

        cursor = self.carried("apply_ledger_cursor")
        deadline = run_deadline()
        try:
            while True:
                result = prune_apply_ledger(execute=True, cursor=cursor, deadline=deadline)
                cursor = result.next_cursor
                self.carry("apply_ledger_cursor", cursor)
                if result.adapter_unavailable or cursor is None or time.monotonic() >= deadline:
                    return result
        except DeploymentQuiesced:
            logger.info("PruneApplyLedgerJob: paused for an intent deployment")
            return None
