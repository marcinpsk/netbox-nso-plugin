# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared test mixins."""


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
    """Reset signals.py module-global intent-push caches before each test.

    ``signals._push_changed`` skips a PUT when the snapshot hash equals the last
    one cached in the module-global ``_last_pushed_hashes`` (a per-process
    change-detection optimisation). ``reset_intent_push_state()`` clears it, but
    it is wired only as a pytest autouse fixture (``conftest.py``); the Django
    ``manage.py test`` runner — which CI uses — ignores conftest, so push-asserting
    tests would see a leaked hash from an earlier test and the push gets skipped
    (``assert_called_once`` → "Called 0 times"). Mix this in BEFORE the TestCase
    base so its ``setUp`` runs and chains via ``super()``.
    """

    def setUp(self):
        super().setUp()
        from netbox_nso_plugin.signals import reset_intent_push_state

        reset_intent_push_state()
