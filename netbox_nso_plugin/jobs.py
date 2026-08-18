# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Periodic system jobs for the NSO plugin."""

import logging
import time

from core.choices import JobIntervalChoices
from netbox.jobs import JobRunner, system_job

logger = logging.getLogger(__name__)

# Minutes between last-sync mirror refreshes. The adapter polls devices far more often
# than this; the window only bounds how stale a NetBox page can be with nobody on it.
SYNC_CACHE_REFRESH_MINUTES = 5


@system_job(interval=JobIntervalChoices.INTERVAL_HOURLY)
class AdvanceStaleOnboardingJob(JobRunner):
    """Hourly backstop that advances async-onboarding rows stranded in 'provisioning'.

    The onboarding dashboard and the device NSO tab advance a row the moment its provision
    job finishes — but only while an operator has that page open. A job that completes with
    no such page open would otherwise strand the row in 'provisioning' forever: NSO has
    onboarded the node, yet the plugin never maps/scopes/syncs it (the adapter-push signal is
    gated on that status). This sweep polls each stale row's job and advances it, self-healing
    with nobody watching.
    """

    class Meta:
        name = "Advance stale NSO onboarding rows"

    def run(self, *args, **kwargs):
        """Advance every NSODeviceManagement row still in 'provisioning'."""
        from .onboarding import advance_stale_onboarding_rows

        advance_stale_onboarding_rows()


@system_job(interval=SYNC_CACHE_REFRESH_MINUTES)
class RefreshDeviceSyncCacheJob(JobRunner):
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
    """

    class Meta:
        name = "Refresh NSO device sync cache"

    def run(self, *args, **kwargs):
        """Refresh the last-sync mirror, repair mappings, drain the outbox, sweep settlements."""
        from .drain import compact_intent_outbox, drain_intent_outbox
        from .models import NSODeviceManagement
        from .settlement import sweep_static_route_settlements
        from .sync_cache import _snapshot, reconcile_device_links, refresh_sync_caches

        rows = list(NSODeviceManagement.objects.select_related("nso_instance"))
        # One adapter snapshot for both passes: they ask the same question of the same data,
        # and a second call could disagree with the first mid-sweep.
        snapshot = _snapshot(rows)
        checked, updated = refresh_sync_caches(rows, snapshot=snapshot)
        # A row whose mapping is broken can't be mirrored at all — repair it so the next pass
        # has something to mirror.
        broken, attempted = reconcile_device_links(rows, snapshot=snapshot)
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
        if by_id is None:
            compact_intent_outbox()
            settle_started = time.monotonic()
            logger.warning("RefreshDeviceSyncCacheJob: adapter snapshot unavailable, sends and sweep skipped")
        else:
            # Same rule as the sweep, and for the same reason: a proven global outage is not
            # a per-key failure, and every candidate would wait out its own read timeout.
            drained, drain_failed = drain_intent_outbox()
            settle_started = time.monotonic()
            polled, settle_failed = sweep_static_route_settlements()
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
