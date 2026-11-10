# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Repair stored owned rows without changing schema or unowned acceptance times."""

from django.db import connection
from django.test import TransactionTestCase

from .mixins import _CascadeFlushMixin

APP = "netbox_nso_plugin"


class TestOwnedAcceptanceMigration(_CascadeFlushMixin, TransactionTestCase):
    def test_owned_null_times_are_backfilled_without_changing_existing_or_unowned_times(self):
        from datetime import UTC, datetime
        from uuid import uuid4

        from django.db.migrations.executor import MigrationExecutor
        from django.utils import timezone
        from ipam.models import VLAN

        from ._outbox_case import make_managed

        _device, management = make_managed("acceptance-migration", 17896)
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes(APP)
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(leaves))
        parent = (APP, "0046_attempt_prune_index_pre_ledger")
        executor.migrate([parent])
        historical = executor.loader.project_state([parent]).apps
        State = historical.get_model(APP, "NSOVLANState")
        synced = datetime(2026, 10, 1, tzinfo=UTC)
        accepted = datetime(2026, 10, 2, tzinfo=UTC)
        rows = {}
        for index, (status, stamp, sync) in enumerate(
            (
                ("accepted", None, synced),
                ("in_sync", None, None),
                ("apply_failed", None, synced),
                ("deploying", None, synced),
                ("imported", None, synced),
                ("changed", None, synced),
                ("accepted", accepted, synced),
            ),
            start=1,
        ):
            vlan = VLAN.objects.create(vid=100 + index, name=f"ACCEPTANCE-{index}")
            rows[index] = State.objects.create(
                management_id=management.pk,
                vlan_id=vlan.pk,
                status=status,
                accepted_at=stamp,
                last_sync_at=sync,
                apply_attempt_id=uuid4() if status == "deploying" else None,
            )
        InterfaceState = historical.get_model(APP, "NSOInterfaceState")
        from dcim.models import Interface

        interface = Interface.objects.create(device=management.device, name="Ethernet1", type="1000base-t")
        attribute = InterfaceState.objects.create(interface_id=interface.pk, attribute="description", status="accepted")
        before = timezone.now()
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
        after = timezone.now()
        State = executor.loader.project_state(leaves).apps.get_model(APP, "NSOVLANState")
        times = {index: State.objects.get(pk=row.pk).accepted_at for index, row in rows.items()}
        self.assertEqual(times[1], synced)
        self.assertIsNotNone(times[2])
        self.assertLessEqual(before, times[2])
        self.assertLessEqual(times[2], after)
        self.assertEqual(times[3], synced)
        self.assertEqual(times[4], synced)
        self.assertIsNone(times[5])
        self.assertIsNone(times[6])
        self.assertEqual(times[7], accepted)
        InterfaceState = executor.loader.project_state(leaves).apps.get_model(APP, "NSOInterfaceState")
        self.assertEqual(InterfaceState.objects.get(pk=attribute.pk).accepted_at, times[2])
