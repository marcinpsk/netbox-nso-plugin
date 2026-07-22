# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""READSEM S4 D5 — the device-wide read mutex and the family read-state gate.

Three cooperating pieces, consumed by the reconcile paths in Slice B3:

* :class:`DeviceReadLease` — a cross-worker redis mutex per management (device-wide;
  a per-family lock cannot serialize incarnation adoption). SET NX EX with an owner
  token; renewal and release are atomic Lua compare-and-extend / compare-and-delete
  (a naive GET-then-DEL can delete a SUCCESSOR's lease after expiry). The heartbeat
  thread renews at TTL/3 and the context manager's exit always stops, joins, and
  releases; a token found gone is logged LOUDLY — the overwrite window existed.

* Per-call-class acquisition (R6-1): web reconciles fail fast (``skipped_busy`` is a
  real UI state); RQ reconciles retry with backoff+jitter, then defer via the
  marker-handoff protocol (R8-1/R9-1/R10-1 — NO delayed scheduling exists in this
  deployment): the deferrer writes a per-device pending marker holding a fresh
  NONCE, and the lease owner's release path atomically GETDELs it and enqueues a
  successor whose job id embeds that nonce — exactly one successor per consumed
  marker, immune to both the deterministic-id dedupe and a prior still-running
  handoff job. The periodic cadence poll remains the ultimate backstop.

* The gate itself — :func:`gated_family_run` (one fetched family document → ONE
  decision → ONE body) and :func:`observe_aggregate` (the tab's observed-only
  protocol, R6-3). Both order by the store incarnation (adoption by born, durable
  reset/conflict markers — R13-1..R16-1) then ``attempt_id``; both revalidate the
  epoch (``adapter_device_id``) at write time. The reconcile fence compares against
  APPLIED only; observations never adopt and never touch ``applied_*``.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

# ── Dispositions (D9 vocabulary; GateResult carries one of these) ───────────────
RAN = "ran"
SKIPPED_UNAVAILABLE = "skipped_unavailable"
SKIPPED_STALE_ATTEMPT = "skipped_stale_attempt"
SKIPPED_BUSY = "skipped_busy"
SKIPPED_LOCK_UNAVAILABLE = "skipped_lock_unavailable"
LEGACY = "legacy"


class _SkippedType:
    """Typed sentinel for a skipped body's value — never a fabricated empty."""

    def __repr__(self):
        return "<SKIPPED>"


SKIPPED = _SkippedType()


@dataclass
class GateResult:
    """Disposition + the reconciler body's return value (SKIPPED when it never ran)."""

    disposition: str
    value: Any = SKIPPED


@dataclass
class Deferred:
    """RQ contention terminal state: the job hands off via the pending marker."""

    attempts: int
    nonce: str


class LockUnavailable(Exception):
    """Redis coordination unreachable — callers fail CLOSED (no body, no row writes)."""


LEASE_TTL_S = 120
MARKER_TTL_S = 900

_AUTHORITATIVE_OUTCOMES = frozenset({"present", "absent_authoritative"})
_ADMIT_RESULTS = frozenset({"replaced", "cleared"})

#: extend only if the stored token is ours (KEYS[1]=lease, ARGV=token, ttl_s)
_EXTEND_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

#: delete only if the stored token is ours (KEYS[1]=lease, ARGV=token)
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def lease_key(mgmt_pk) -> str:
    """Return the device-wide mutex key for one NSODeviceManagement."""
    return f"nso-read-lease:{mgmt_pk}"


def marker_key(device_id) -> str:
    """Return the per-device pending-reconcile marker key (holds the handoff nonce)."""
    return f"nso-reconcile-pending:{device_id}"


def carrier_key(device_id) -> str:
    """Per-device pointer → the id of the one genuinely-queued reconcile carrier (READSEM 1334).

    The canonical single-flight slot for :func:`enqueue_reconcile_carrier`. Persistent (no TTL):
    a carrier queued behind a long backlog must not outlive its pointer (codex r1-f2), and a stale
    pointer is safe because every producer validates + overwrites it. Deliberately NOT
    :func:`marker_key` — that namespace holds lease-handoff nonces (codex r7-3 namespace hygiene).
    """
    return f"nso-reconcile-carrier:{device_id}"


def fresh_carrier_id(device_id) -> str:
    """Return a never-reused carrier job id (so a persistent pointer can never become a false positive)."""
    return f"nso-reconcile-{device_id}-carrier-{uuid.uuid4().hex}"


def _to_str(value):
    """Normalize a Redis value (``bytes``/``str``/``None``) to ``str``/``None`` (codex r1-f1)."""
    if isinstance(value, bytes):
        return value.decode()
    return value


def _redis_errors():
    import redis

    return (redis.exceptions.RedisError, OSError)


class DeviceReadLease:
    """The device-wide redis lease (R6-4 lifecycle).

    ``acquire()`` then use as a context manager; the heartbeat renews at TTL/3 and
    exit always stops → joins → releases. ``lost`` goes True (with an ERROR log)
    whenever a renewal or the release finds the token gone — a stalled holder that
    outlived its lease re-opened bounded last-writer-wins, and we say so loudly.
    When constructed with ``device_id``/``queue``, a successful release consumes the
    pending marker and enqueues the handoff successor (R9-1).
    """

    def __init__(self, conn, key: str, *, ttl_s: int = LEASE_TTL_S, device_id=None, queue=None):
        self.conn = conn
        self.key = key
        self.ttl_s = ttl_s
        self.token = uuid.uuid4().hex
        self.device_id = device_id
        self.queue = queue
        self.lost = False
        self._stop = threading.Event()
        self._hb_thread: threading.Thread | None = None

    def acquire(self) -> bool:
        return bool(self.conn.set(self.key, self.token, nx=True, ex=self.ttl_s))

    def _extend(self) -> bool:
        return bool(self.conn.eval(_EXTEND_LUA, 1, self.key, self.token, self.ttl_s))

    def release(self) -> bool:
        """Atomic compare-and-delete; on success, consume the handoff marker."""
        ok = bool(self.conn.eval(_RELEASE_LUA, 1, self.key, self.token))
        if not ok:
            self.lost = True
            logger.error(
                "device read lease %s LOST (token gone at release) — a successor may have "
                "run concurrently; bounded last-writer-wins window existed",
                self.key,
            )
            return False
        if self.device_id is not None and self.queue is not None:
            consume_marker_and_enqueue_successor(self.conn, self.device_id, self.queue)
        return True

    def _heartbeat(self):
        interval = self.ttl_s / 3.0
        while not self._stop.wait(interval):
            try:
                if not self._extend():
                    self.lost = True
                    logger.error(
                        "device read lease %s LOST (renewal found the token gone/foreign) — "
                        "a successor may be running; overwrite window existed",
                        self.key,
                    )
                    return
            except Exception:
                logger.exception("device read lease %s renewal error; heartbeat stopping", self.key)
                return

    def __enter__(self):
        self._stop.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat,
            name=f"nso-read-lease-hb-{self.key}",
            daemon=True,  # process-shutdown safety only — never a cleanup mechanism
        )
        self._hb_thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join()
        self.release()
        return False


def acquire_for_web(conn, key: str, *, ttl_s: int = LEASE_TTL_S, device_id=None, queue=None) -> DeviceReadLease | None:
    """WEB call class: one attempt, fail fast (the UI renders 'refresh in progress').

    Carries ``device_id``/``queue`` so a web-owned lease's release ALSO consumes a
    pending RQ handoff marker (codex B5-F3) — otherwise an RQ reconcile that
    deferred behind a web refresh is stranded until the cadence backstop.
    """
    lease = DeviceReadLease(conn, key, ttl_s=ttl_s, device_id=device_id, queue=queue)
    try:
        got = lease.acquire()
    except _redis_errors() as exc:
        raise LockUnavailable(str(exc)) from exc
    return lease if got else None


def acquire_for_rq(
    conn,
    key: str,
    device_id,
    queue,
    *,
    ttl_s: int = LEASE_TTL_S,
    retry_budget_s: float = 90.0,
    base_delay_s: float = 2.0,
    max_delay_s: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> DeviceReadLease | Deferred:
    """RQ call class: bounded backoff+jitter retries, then the marker handoff.

    Never completes as success-with-zero-work: the terminal states are a held lease
    or a :class:`Deferred` (marker written, successor guaranteed by the owner's
    release hook or — if the marker lapses — the periodic cadence backstop).
    """
    lease = DeviceReadLease(conn, key, ttl_s=ttl_s, device_id=device_id, queue=queue)
    attempts = 0
    started = time.monotonic()
    try:
        while True:
            attempts += 1
            if lease.acquire():
                return lease
            if time.monotonic() - started >= retry_budget_s:
                break
            delay = min(base_delay_s * (2 ** (attempts - 1)), max_delay_s)
            sleep(delay * (0.5 + random.random() / 2))
        # Marker handoff. The ONE post-marker attempt closes the release-before-marker
        # race: if the owner released before seeing the marker, we take the lease now
        # and consume our own marker.
        nonce = write_defer_marker(conn, device_id)
        attempts += 1
        if lease.acquire():
            conn.getdel(marker_key(device_id))
            return lease
    except _redis_errors() as exc:
        raise LockUnavailable(str(exc)) from exc
    logger.warning(
        "device %s reconcile: lease still contended after %d attempts — deferred via "
        "marker handoff (nonce %s); the lease owner's release will enqueue the successor",
        device_id,
        attempts,
        nonce,
    )
    return Deferred(attempts=attempts, nonce=nonce)


def write_defer_marker(conn, device_id) -> str:
    """Write the per-device pending marker with a FRESH nonce (last deferrer wins)."""
    nonce = uuid.uuid4().hex
    conn.set(marker_key(device_id), nonce, ex=MARKER_TTL_S)
    return nonce


#: Wall-clock backstop for the arbiter's optimistic-CAS loop (READSEM 1334). NOT a per-count cap: a
#: WatchError means another producer committed the pointer → the retry re-reads it and (almost always)
#: suppresses, so the loop converges in ~1-2 tries under real per-device contention. The budget only
#: guards a request thread from a pathological same-device storm (optimistic CAS is lock-free, not
#: starvation-free) — tripping it routes to the DEGRADED path (a bounded duplicate, never a lost edge).
_CAS_LIVENESS_BUDGET_S = 2.0


def _queued_carrier(queue, conn, job_id):
    """Return the ``Job`` iff *job_id* is a GENUINELY-QUEUED carrier (READSEM 1334), else ``None``.

    Genuinely queued = in the RQ queue list (i.e. not yet popped/started) AND its hash is fetchable
    (a dangling queue entry is not suppressible — codex r1-f4). Suppressing onto such a carrier is
    loss-free: a job still in the queue list has not been popped, so all its reads follow this
    producer's mirror refresh. A popped/running/deferred/dead carrier is absent from the list → not
    suppressible → the caller enqueues a trailing carrier (no StartedRegistry dependency — codex r3-3).
    """
    if not job_id:
        return None
    from rq.exceptions import NoSuchJobError
    from rq.job import Job as RqJob

    if job_id not in queue.get_job_ids():
        return None
    try:
        return RqJob.fetch(job_id, connection=conn)
    except NoSuchJobError:
        return None


def _degraded_enqueue(conn, queue, device_id, consume_marker):
    """Fallback OUTSIDE the one-queued-carrier bound (Redis down OR CAS starvation past the budget).

    At most ONE raw enqueue per invocation — a bounded duplicate, never a lost edge (codex r6-note1).
    Durable job first, then best-effort marker consume: a crash between them retains the marker, so the
    edge still survives via a later release/producer or the cadence backstop.
    """
    from .reconcile import run_device_reconcile

    logger.warning("device %s: reconcile carrier degraded-enqueue (redis/CAS unavailable)", device_id)
    job = queue.enqueue(run_device_reconcile, device_id, result_ttl=300, job_timeout=600)
    if consume_marker:
        try:
            conn.delete(marker_key(device_id))
        except _redis_errors():
            pass
    return job


def _inline_reconcile(conn, queue, device_id, consume_marker):
    """Synchronous-queue path (``is_async=False``): RQ runs the body inline, so there is no queued slot.

    A caller-owned pipeline would run the job BEFORE ``EXEC``, so the CAS is bypassed here. For a handoff
    (``consume_marker``), consume the marker FIRST and run the successor only if one was actually pending —
    otherwise the inline run's own release hook would re-enter forever (codex r3-4). This path cannot make
    marker-consume + arbitrary Python execution crash-atomic (reduced failure model, documented).
    """
    from .reconcile import run_device_reconcile

    if consume_marker:
        try:
            present = conn.delete(marker_key(device_id))
        except _redis_errors():
            present = 0
        if not present:
            return None
    return queue.enqueue(run_device_reconcile, device_id, result_ttl=300, job_timeout=600)


def enqueue_reconcile_carrier(conn, queue, device_id, *, consume_marker=False):
    """Run the atomic per-device queued-carrier arbiter (READSEM 1334) — one single-flight slot per device.

    Optimistic CAS (``WATCH``/``MULTI``/``EXEC``) on :func:`carrier_key`, reusing RQ's own ``enqueue_job``
    inside the ``MULTI`` so the job hash, queue push, and pointer commit in one transaction. Suppresses a
    new edge only onto a GENUINELY-QUEUED carrier (loss-free — see :func:`_queued_carrier`); otherwise
    enqueues exactly one trailing carrier. ``consume_marker=True`` (the handoff mode) also reads the defer
    marker UNDER WATCH and deletes it in the SAME ``EXEC`` as the ensured/suppressed carrier — never
    creating on behalf of a stale/absent nonce (codex r3-1). Returns the (existing or new) ``Job``, or
    ``None`` on a handoff with no pending marker.

    Bound (soft): under healthy Redis at most one queued carrier per device; the only duplicate-producing
    escapes are Redis failure and sustained-CAS-starvation past :data:`_CAS_LIVENESS_BUDGET_S`, both routed
    to :func:`_degraded_enqueue` and both preferring a bounded duplicate over a lost edge.
    """
    from redis.exceptions import WatchError

    from .reconcile import run_device_reconcile

    # The sync-queue path uses neither WATCH nor the version pre-warm — decide it FIRST, so a
    # transient pre-warm failure can never route a synchronous handoff through the async-oriented
    # (delete-after-run) degraded path instead of _inline_reconcile's consume-first order (codex diff-1).
    if not queue.is_async:
        return _inline_reconcile(conn, queue, device_id, consume_marker)
    try:
        queue.get_redis_server_version()  # pre-warm the lazy version lookup OUTSIDE the WATCH (codex r2-4)
    except _redis_errors():
        return _degraded_enqueue(conn, queue, device_id, consume_marker)

    ckey = carrier_key(device_id)
    mkey = marker_key(device_id)
    keys = (ckey, mkey) if consume_marker else (ckey,)
    deadline = time.monotonic() + _CAS_LIVENESS_BUDGET_S
    while True:
        try:
            with conn.pipeline() as pipe:
                pipe.watch(*keys)
                existing = _queued_carrier(queue, conn, _to_str(pipe.get(ckey)))
                if consume_marker and _to_str(pipe.get(mkey)) is None:
                    pipe.unwatch()
                    return existing  # nothing to hand off — never create on behalf of a stale/absent nonce
                if existing is not None:
                    if consume_marker:
                        pipe.multi()
                        pipe.delete(mkey)  # atomic marker-consume alongside the suppression
                        pipe.execute()
                    else:
                        pipe.unwatch()
                    return existing
                job = queue.create_job(
                    run_device_reconcile,
                    args=(device_id,),
                    job_id=fresh_carrier_id(device_id),
                    result_ttl=300,
                    timeout=600,
                )
                pipe.multi()
                queue.enqueue_job(job, pipeline=pipe)  # job hash + queue push, buffered into the MULTI
                pipe.set(ckey, job.id)  # persistent pointer, same EXEC as the durable job
                if consume_marker:
                    pipe.delete(mkey)  # consume the marker observed under WATCH, atomically
                pipe.execute()  # WatchError if carrier/marker moved → retry (a nonce bump ⇒ retry consumes it)
                return job
        except WatchError:
            if time.monotonic() >= deadline:
                break  # sustained-starvation backstop → degraded path (bounded duplicate)
            time.sleep(random.uniform(0.0, 0.05))  # jittered backoff caps spin rate
            continue
        except _redis_errors():
            break  # genuine Redis coordination failure → degraded path
    return _degraded_enqueue(conn, queue, device_id, consume_marker)


def consume_marker_and_enqueue_successor(conn, device_id, queue):
    """Consume a pending defer marker and ensure exactly one queued successor, atomically (READSEM 1334).

    Delegates to :func:`enqueue_reconcile_carrier` in handoff mode: the marker is read UNDER WATCH and
    deleted in the same ``EXEC`` as the ensured/suppressed carrier, so concurrent release hooks that
    observe the same marker converge on one carrier + one delete, a nonce bump ``N1→N2`` retries and
    consumes ``N2``, and an absent marker is a no-op (never a phantom successor). Reworks the old
    non-atomic ``GETDEL → enqueue`` (which could delete the marker then fail to enqueue → lost edge).
    Returns the successor ``Job``, or ``None`` when there was no marker.
    """
    return enqueue_reconcile_carrier(conn, queue, device_id, consume_marker=True)


# ── The gate (transition table + incarnation adoption) ─────────────────────────


def _parse_dt(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return parse_datetime(value)


def _is_authoritative(rs: dict) -> bool:
    """Apply D3's binary gate tuple.

    Authoritative iff outcome ∈ {present, absent_authoritative} AND succeeded is
    True AND result ∈ {replaced, cleared}. Unknown values fail closed.
    """
    return (
        rs.get("outcome") in _AUTHORITATIVE_OUTCOMES
        and rs.get("succeeded") is True
        and rs.get("result") in _ADMIT_RESULTS
    )


@dataclass
class _Decision:
    disposition: str
    run_body: bool


def _locked_row(mgmt, family: str):
    """Lock-or-create the (management, family) row.

    The caller already holds the management row lock, so creation is serialized;
    the IntegrityError retry is a belt-and-braces for the concurrent first-create.
    """
    from .models import NSOFamilyReadState

    locked = NSOFamilyReadState.objects.select_for_update()
    try:
        return locked.get(management=mgmt, family=family)
    except NSOFamilyReadState.DoesNotExist:
        try:
            with transaction.atomic():
                return NSOFamilyReadState.objects.create(management=mgmt, family=family)
        except IntegrityError:
            return locked.get(management=mgmt, family=family)


_RESET_FIELDS = {
    "observed_outcome": "",
    "observed_reason": "",
    "observed_freshness": "",
    "observed_result": "",
    "observed_succeeded": None,
    "observed_read_at": None,
    "observed_attempt_id": None,
    "observed_incarnation": "",
    "observed_incarnation_born": None,
    "observed_epoch": None,
    "applied_attempt_id": None,
    "applied_incarnation": "",
}

_MARKER_FIELDS = [
    "adapter_incarnation",
    "adapter_incarnation_born",
    "reset_pending_incarnation",
    "reset_pending_born",
    "reset_conflict_born",
]


def _advance_observed(row, rs: dict, inc: str, born, epoch) -> bool:
    """Advance observed_* monotonically within the adopted incarnation.

    A strictly-older attempt (None sorts below every real id) never regresses;
    an equal attempt refreshes.
    """
    incoming = rs.get("attempt_id")
    if row.observed_attempt_id is not None and (incoming is None or incoming < row.observed_attempt_id):
        return False
    row.observed_outcome = rs.get("outcome") or ""
    row.observed_reason = rs.get("reason") or ""
    row.observed_freshness = rs.get("freshness") or ""
    row.observed_result = rs.get("result") or ""
    row.observed_succeeded = rs.get("succeeded")
    row.observed_read_at = _parse_dt(rs.get("read_at"))
    row.observed_attempt_id = incoming
    row.observed_incarnation = inc
    row.observed_incarnation_born = born
    row.observed_epoch = epoch
    return True


def _register_incarnation_observation(m, inc: str, born) -> None:
    """R13-1..R16-1 marker transitions for a NON-adopted incarnation observation.

    pending(born, uuid) is monotonic (greatest born wins; equal-born different-UUID
    replaces AND records the collision); conflict_born pins the highest born at
    which two different UUIDs collided (vs adopted OR vs pending).
    """
    collision = (
        m.adapter_incarnation_born is not None and born == m.adapter_incarnation_born and inc != m.adapter_incarnation
    ) or (m.reset_pending_born is not None and born == m.reset_pending_born and inc != m.reset_pending_incarnation)
    if collision:
        if m.reset_conflict_born is None or born > m.reset_conflict_born:
            m.reset_conflict_born = born
        if m.reset_pending_born is None or born >= m.reset_pending_born:
            m.reset_pending_incarnation = inc
            m.reset_pending_born = born
    elif m.reset_pending_born is None or born > m.reset_pending_born:
        m.reset_pending_incarnation = inc
        m.reset_pending_born = born
    # born < pending_born and no collision → monotonic marker: ignore


def _adopt_incarnation(m, inc: str, born) -> None:
    """Adopt *inc* and RESET every family row for this management (→ unknown).

    Blanked rows are OLD overlay data with no read-state behind them — they must
    not render healthy, so when any exist the adoption RETARGETS the pending
    marker at the adopted pair instead of clearing it (codex B5-F1); the marker
    then clears via :func:`_maybe_clear_reset_marker` once every family has
    re-observed. Only a marker this adoption reaches (born > pending_born, or the
    exact pending pair) is touched — a NEWER pending pair stays.
    """
    from .models import NSOFamilyReadState

    blanked = NSOFamilyReadState.objects.filter(management=m).update(**_RESET_FIELDS)
    m.adapter_incarnation = inc
    m.adapter_incarnation_born = born
    if (
        m.reset_pending_born is None
        or born > m.reset_pending_born
        or (inc == m.reset_pending_incarnation and born == m.reset_pending_born)
    ):
        if blanked:
            m.reset_pending_incarnation = inc
            m.reset_pending_born = born
        else:
            m.reset_pending_incarnation = ""
            m.reset_pending_born = None
            m.reset_conflict_born = None
    m.save(update_fields=_MARKER_FIELDS)


def _maybe_clear_reset_marker(m) -> bool:
    """Clear the pending/conflict markers once NO family row is blank (B5-F1).

    Only when the pending marker points at the ADOPTED pair — a marker for a
    newer incarnation clears when its own adoption completes. Runs inside the
    caller's locked transaction; returns True when it cleared.
    """
    from .models import NSOFamilyReadState

    if m.reset_pending_born is None:
        return False
    if m.reset_pending_incarnation != m.adapter_incarnation or m.reset_pending_born != m.adapter_incarnation_born:
        return False
    if NSOFamilyReadState.objects.filter(management=m, observed_outcome="").exists():
        return False
    m.reset_pending_incarnation = ""
    m.reset_pending_born = None
    m.reset_conflict_born = None
    m.save(update_fields=_MARKER_FIELDS)
    return True


def _gate_and_record(mgmt, family: str, read_state: dict | None, *, epoch) -> _Decision:
    """Run step-1 of the D5 protocol.

    ONE short locked transaction deciding admission and persisting observed/applied
    state. Commits before any body runs.
    """
    from .models import NSODeviceManagement

    with transaction.atomic():
        m = NSODeviceManagement.objects.select_for_update().get(pk=mgmt.pk)
        if m.adapter_device_id is None or epoch is None or m.adapter_device_id != epoch:
            # a response fetched from a link this row no longer has (delayed replay)
            return _Decision(SKIPPED_STALE_ATTEMPT, False)

        row = _locked_row(m, family)

        if read_state is None:
            # pre-S4 adapter (key absent) → legacy behavior; blank outcome so the
            # UI ignores any previously-persisted state (rollback hygiene, R1-F9)
            if row.observed_outcome:
                row.observed_outcome = ""
                row.save(update_fields=["observed_outcome"])
            return _Decision(LEGACY, True)

        inc = read_state.get("incarnation") or ""
        born = _parse_dt(read_state.get("incarnation_born"))
        if not inc or born is None:
            # a block without the incarnation pair cannot participate in ordering —
            # inconsistent per D3 → fail closed, write nothing
            return _Decision(SKIPPED_UNAVAILABLE, False)

        if not (m.adapter_incarnation and inc == m.adapter_incarnation):
            # Equal-born/different-UUID vs the PENDING pair is the same ambiguity as
            # vs the adopted pair — adopting it would materialize an ambiguous
            # incarnation (codex B5-R2-2, the R15 algebra).
            pending_collision = (
                m.reset_pending_born is not None and born == m.reset_pending_born and inc != m.reset_pending_incarnation
            )
            adoptable = (
                (
                    m.adapter_incarnation == ""
                    or (m.adapter_incarnation_born is not None and born > m.adapter_incarnation_born)
                )
                and (m.reset_conflict_born is None or born > m.reset_conflict_born)
                and not pending_collision
            )
            if not adoptable:
                # equal-born/different-UUID is a durable CONFLICT in every branch (R15-1)
                if (
                    (
                        m.adapter_incarnation_born is not None
                        and born == m.adapter_incarnation_born
                        and inc != m.adapter_incarnation
                    )
                    or (m.reset_conflict_born is not None and born == m.reset_conflict_born)
                    or pending_collision
                ):
                    _register_incarnation_observation(m, inc, born)
                    m.save(update_fields=_MARKER_FIELDS)
                return _Decision(SKIPPED_STALE_ATTEMPT, False)
            _adopt_incarnation(m, inc, born)
            row.refresh_from_db()

        incoming = read_state.get("attempt_id")
        if row.applied_attempt_id is not None and (incoming is None or incoming < row.applied_attempt_id):
            # strictly older than APPLIED: nothing advances, body skipped
            return _Decision(SKIPPED_STALE_ATTEMPT, False)

        if _is_authoritative(read_state):
            _advance_observed(row, read_state, inc, born, epoch)
            row.applied_attempt_id = incoming
            row.applied_incarnation = inc
            row.save()
            _maybe_clear_reset_marker(m)
            return _Decision(RAN, True)

        # non-authoritative (unavailable / result=error / tuple-fail / unknown values):
        # observed advances monotonically, applied untouched, body skipped
        if _advance_observed(row, read_state, inc, born, epoch):
            row.save()
            _maybe_clear_reset_marker(m)
        return _Decision(SKIPPED_UNAVAILABLE, False)


def _admission_still_current(mgmt, family: str, incoming, inc: str) -> bool:
    """Body fence (codex B5-F2 + R2-1): check OUR admission is still the applied one.

    Between the admission commit and the body there is no lock; a successor may
    have admitted AND materialized a newer attempt — or ADOPTED a newer
    incarnation whose attempt ids restarted at the same numbers (attempt ids are
    NOT unique across store rebuilds), so the fence compares the incarnation too.
    One re-select refuses the stale body. The mid-body window remains the
    design's documented bounded last-writer-wins, logged loudly by the lease
    heartbeat.
    """
    from .models import NSOFamilyReadState

    row = (
        NSOFamilyReadState.objects.filter(management=mgmt, family=family)
        .values_list("applied_attempt_id", "applied_incarnation")
        .first()
    )
    return row is not None and row[0] == incoming and row[1] == inc


def gated_family_run(mgmt, family: str, read_state: dict | None, body: Callable[[], Any], *, epoch) -> GateResult:
    """ONE family document → ONE gate decision → at most ONE body run (R3-6).

    The admission transaction commits BEFORE the body runs, so a body failure after
    admission keeps ``applied_*`` advanced (deliberate — the read was real; the
    materialization failure surfaces via the caller's existing scope-error path).
    An admitted body is fenced right before it runs (B5-F2): if a successor already
    applied a newer attempt, the stale body is refused. LEGACY has no attempt
    ordering — it runs unfenced.
    """
    decision = _gate_and_record(mgmt, family, read_state, epoch=epoch)
    if not decision.run_body:
        return GateResult(decision.disposition)
    if decision.disposition == RAN and not _admission_still_current(
        mgmt, family, read_state.get("attempt_id"), read_state.get("incarnation") or ""
    ):
        return GateResult(SKIPPED_STALE_ATTEMPT)
    return GateResult(decision.disposition, body())


def observe_aggregate(mgmt, read_states: dict[str, dict | None], *, epoch) -> bool:
    """Record the tab's aggregate observation (R6-3).

    ONE short transaction: management lock first (same order as the gate — no
    deadlock), deterministic family order, observed_* only, monotonic, NEVER
    adopts and never touches ``applied_*``. A NEWER incarnation only records the
    durable reset-pending marker (R11/R12). Returns True when anything persisted.
    """
    from .models import NSODeviceManagement

    if not read_states:
        return False
    with transaction.atomic():
        m = NSODeviceManagement.objects.select_for_update().get(pk=mgmt.pk)
        if m.adapter_device_id is None or epoch is None or m.adapter_device_id != epoch:
            return False
        if not m.adapter_incarnation:
            # nothing adopted yet — observation REQUIRES the adopted incarnation
            return False
        wrote = False
        marker_dirty = False
        for family in sorted(read_states):
            rs = read_states[family]
            if rs is None:
                continue
            inc = rs.get("incarnation") or ""
            born = _parse_dt(rs.get("incarnation_born"))
            if not inc or born is None:
                continue
            if inc != m.adapter_incarnation:
                if m.adapter_incarnation_born is not None and born < m.adapter_incarnation_born:
                    continue  # pre-adoption replay: ignore entirely
                before = (m.reset_pending_incarnation, m.reset_pending_born, m.reset_conflict_born)
                _register_incarnation_observation(m, inc, born)
                marker_dirty = marker_dirty or before != (
                    m.reset_pending_incarnation,
                    m.reset_pending_born,
                    m.reset_conflict_born,
                )
                continue  # a non-adopted incarnation's payload never advances rows
            row = _locked_row(m, family)
            if _advance_observed(row, rs, inc, born, epoch):
                row.save()
                wrote = True
        if marker_dirty:
            m.save(update_fields=_MARKER_FIELDS)
        cleared = _maybe_clear_reset_marker(m) if wrote else False
        return wrote or marker_dirty or cleared
