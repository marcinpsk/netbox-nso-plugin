# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared test mixins."""

import contextlib
import logging

logger = logging.getLogger(__name__)

_TEST_DELIVERY_PUSH_SEQ = 0


class _CascadeFlushMixin:
    """Make TransactionTestCase's post-test flush use ``TRUNCATE ... CASCADE``.

    NetBox's schema has tag M2M through-tables (e.g. ``netbox_map_topologysavedview_tags``)
    that are referenced by foreign keys. Django's default TransactionTestCase teardown only
    emits CASCADE when ``available_apps`` is set (``allow_cascade = available_apps is not
    None``), so on PostgreSQL the plain TRUNCATE errors with "cannot truncate a table
    referenced in a foreign key constraint". This override is a faithful copy of Django's
    ``_fixture_teardown`` with ``allow_cascade`` forced True — resetting the DB without
    having to enumerate ``available_apps`` for the whole NetBox app graph.
    """

    def _fixture_teardown(self):
        from django.core.management import call_command
        from django.db import connections

        for db_name in self._databases_names(include_mirrors=False):
            inhibit_post_migrate = self.available_apps is not None or (
                self.serialized_rollback and hasattr(connections[db_name], "_test_serialized_contents")
            )
            call_command(
                "flush",
                verbosity=0,
                interactive=False,
                database=db_name,
                reset_sequences=False,
                allow_cascade=True,
                inhibit_post_migrate=inhibit_post_migrate,
            )


class IntentPushResetMixin:
    """Clear the thread's pending intent-push keys before each test.

    ``_schedule_intent_push`` records the keys the current transaction appended to and the
    commit callback drains them. A transaction that ROLLED BACK leaves its keys in that
    cell by design (§4.2: a stale cell costs an O(1) callback, never a missing drain), so a
    test whose fixture rolled back would otherwise hand the next test a key to drain.
    ``reset_intent_push_state()`` clears it, but it is wired only as a pytest autouse
    fixture (``conftest.py``); the Django ``manage.py test`` runner — which CI uses —
    ignores conftest. Mix this in BEFORE the TestCase base so its ``setUp`` runs and chains
    via ``super()``.
    """

    def setUp(self):
        super().setUp()
        from netbox_nso_plugin.signals import reset_intent_push_state

        reset_intent_push_state()

    def tearDown(self):
        from netbox_nso_plugin.intent_state import _ACTIVE_PERMIT

        permit = _ACTIVE_PERMIT.get()
        try:
            self.assertIsNone(permit, f"renderer permit leaked after the test: {permit!r}")
        finally:
            _ACTIVE_PERMIT.set(None)
            super().tearDown()


def _deliver_scheduled_keys():
    """Deliver what the transaction scheduled, when a ``TestCase`` makes the drain impossible.

    A ``TestCase`` body runs inside one transaction that never commits, and
    ``captureOnCommitCallbacks`` executes the commit callbacks INSIDE it. The outbox drain
    refuses to run there, and rightly: it sets its own isolation level, which PostgreSQL
    accepts only before a transaction's first statement, and its send must hold no row lock.
    Neither is possible nested in a caller's block, and no amount of test scaffolding can
    give a `TestCase` a committed transaction to work from.

    So inside a transaction this does what the callback does MINUS the claim: it takes the
    keys the transaction appended to and renders and sends each one through the same choke
    point, carrying the entries' folded deletion authority and marking AND. It asserts
    nothing about durability, replay or sequencing — the claim protocol has its own
    ``TransactionTestCase`` pins for those (``test_intent_outbox_*``). Outside a transaction
    it is not used at all: the real drain runs.
    """
    from netbox_nso_plugin import delivery, outbox, signals
    from netbox_nso_plugin.models import NSODeviceManagement, NSOIntentOutboxEntry, NSOIntentOutboxState

    keys = signals._pending_intent_keys()
    claimed = sorted(keys)
    keys.clear()
    for device_id, scope in claimed:
        adapter_device_id = (
            NSODeviceManagement.objects.filter(device_id=device_id, adapter_device_id__isnull=False)
            .values_list("adapter_device_id", flat=True)
            .first()
        )
        if adapter_device_id is None:
            continue
        rows = list(
            NSOIntentOutboxEntry.objects.filter(device_id=device_id, scope=scope, consumed_by_push_seq__isnull=True)
            .order_by("id")
            .values("id", "kind", "mark_and", "transitions")
        )
        if not rows:
            continue
        entry_ids = [row["id"] for row in rows]
        state = NSOIntentOutboxState.objects.filter(device_id=device_id, scope=scope).first() or NSOIntentOutboxState()
        folded = outbox.fold_state_transitions([record for row in rows for record in row["transitions"]], state)
        ordinary = [row for row in rows if row["kind"] == outbox.CONTRIBUTION_KIND_ORDINARY]
        try:
            rendered = delivery.render(scope, device_id, adapter_device_id)
            delivery.send(
                rendered,
                rendered.payload,
                mark=all(row["mark_and"] for row in ordinary) if ordinary else False,
                deletions=list(folded.queued.values()),
            )
        except Exception:  # noqa: BLE001 (one key's failure must not abort its siblings)
            logger.exception("test delivery failed for %s/%s", device_id, scope)
            continue
        NSOIntentOutboxEntry.objects.filter(id__in=entry_ids).update(consumed_by_push_seq=_TEST_DELIVERY_PUSH_SEQ)
        NSOIntentOutboxState.objects.filter(device_id=device_id, scope=scope).delete()


class IntentPushDeliveryMixin(IntentPushResetMixin):
    """Make a ``TestCase``'s commit callbacks deliver, since they cannot drain.

    Mix this in wherever a ``TestCase`` asserts that a signal produced an adapter call. See
    :func:`_deliver_scheduled_keys` for why the real drain cannot run inside a test
    transaction and for what this substitutes in its place. With no transaction open the
    real drain runs untouched, so a ``TransactionTestCase`` mixing this in still exercises
    the production path.
    """

    def setUp(self):
        super().setUp()
        from unittest.mock import patch

        from django.db import connection

        from netbox_nso_plugin import signals

        real_drain = signals._drain_intent_pushes

        def deliver_or_drain():
            if connection.needs_rollback:
                return None
            if connection.in_atomic_block:
                return _deliver_scheduled_keys()
            return real_drain()

        patcher = patch("netbox_nso_plugin.signals._drain_intent_pushes", deliver_or_drain)
        patcher.start()
        self.addCleanup(patcher.stop)


@contextlib.contextmanager
def isolate_other_scopes(*under_test: str):
    """Let only *under_test* reach the adapter; every other delivery scope answers settled.

    Derived from ``delivery.delivery_keys()``, which is the registry the Apply itself walks,
    so registering a new scope isolates it here automatically. The three hand-maintained
    transport tuples this replaced had each drifted from that registry by a different set of
    scopes, and each had to be edited by hand for every new one.

    The claim boundary is the seam rather than the adapter transports the tuples named: the
    scope under test still renders and sends for real, so a test may assert on its transport,
    while the others are answered without rendering anything. ``drain_key`` is patched beside
    ``push_now`` because the Apply routes SNMP through it and every other scope through
    ``push_now``.
    """
    from unittest.mock import patch

    from netbox_nso_plugin import delivery, drain

    keys = delivery.delivery_keys()
    unknown = set(under_test) - set(keys)
    assert not unknown, f"not delivery scopes: {sorted(unknown)}"
    real_push_now, real_drain_key = drain.push_now, drain.drain_key
    synthetic_push_seq = iter(range(1_000_000, 1_001_000))

    def record(device_id, scope):
        from netbox_nso_plugin.models import NSOIntentRevision

        captured = drain._SUCCESSFUL_PUSHES.get()
        if captured is not None:
            revision, _created = NSOIntentRevision.objects.get_or_create(device_id=device_id, scope=scope)
            captured[scope] = drain.SuccessfulPush(
                next(synthetic_push_seq),
                f"isolated-{scope}",
                int(revision.revision),
            )

    def push_now(device_id, scope, **kwargs):
        if scope in under_test:
            return real_push_now(device_id, scope, **kwargs)
        record(device_id, scope)
        return {"status": "deployed", "count": 0}

    def drain_key(device_id, scope, **kwargs):
        if scope in under_test:
            return real_drain_key(device_id, scope, **kwargs)
        record(device_id, scope)
        return drain.SUCCEEDED

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("netbox_nso_plugin.drain.push_now", side_effect=push_now))
        stack.enter_context(patch("netbox_nso_plugin.drain.drain_key", side_effect=drain_key))
        yield stack
