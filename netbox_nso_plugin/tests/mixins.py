# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared test mixins."""

import logging

logger = logging.getLogger(__name__)


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
    point, carrying the AND of the entries' marks exactly as the fold would. It asserts
    nothing about durability, replay or sequencing — the claim protocol has its own
    ``TransactionTestCase`` pins for those (``test_intent_outbox_*``). Outside a transaction
    it is not used at all: the real drain runs.
    """
    from netbox_nso_plugin import delivery, signals
    from netbox_nso_plugin.models import NSODeviceManagement, NSOIntentOutboxEntry

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
        marks = list(
            NSOIntentOutboxEntry.objects.filter(
                device_id=device_id, scope=scope, consumed_by_push_seq__isnull=True
            ).values_list("mark_and", flat=True)
        )
        if not marks:
            continue
        try:
            delivery.deliver(scope, device_id, adapter_device_id, mark=all(marks))
        except Exception:  # noqa: BLE001 (one key's failure must not abort its siblings)
            logger.exception("test delivery failed for %s/%s", device_id, scope)


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
            if connection.in_atomic_block:
                return _deliver_scheduled_keys()
            return real_drain()

        patcher = patch("netbox_nso_plugin.signals._drain_intent_pushes", deliver_or_drain)
        patcher.start()
        self.addCleanup(patcher.stop)
