# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1627: durable ownership manifest schema."""

from django.test import SimpleTestCase, TestCase


class TestOwnershipManifestSchema(SimpleTestCase):
    def test_native_and_overlay_identity_is_stored_without_relations(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest

        fields = {field.name: field for field in NSOOwnershipManifest._meta.fields}

        assert not fields["device_id"].is_relation
        assert not fields["native_model_label"].is_relation
        assert not fields["native_key"].is_relation
        assert "overlay" not in fields

    def test_one_manifest_row_identifies_each_owned_object(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest

        constraints = {
            tuple(constraint.fields)
            for constraint in NSOOwnershipManifest._meta.constraints
            if getattr(constraint, "fields", None)
        }

        assert ("device_id", "scope", "native_model_label", "native_key") in constraints

    def test_manifest_carries_ownership_and_deletion_evidence(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest

        fields = {field.name: field for field in NSOOwnershipManifest._meta.fields}

        assert fields["ownership_state"].get_default() == "owned"
        assert fields["deletion_authority"].get_default() is False
        assert fields["acknowledged_lineage"].get_default() == []


class TestOwnershipManifestDurability(TestCase):
    def test_device_deletion_retires_and_keeps_manifest_evidence(self):
        from unittest.mock import patch

        from netbox_nso_plugin import ownership_planner
        from netbox_nso_plugin.models import NSOOwnershipManifest

        from ._outbox_case import make_managed

        device, _management = make_managed("manifest-durability", 1627)
        device_id = device.pk
        manifest = NSOOwnershipManifest.objects.create(
            device_id=device_id,
            scope="interface",
            native_model_label="dcim.interface",
            native_key={"device_id": device_id, "name": "Ethernet1"},
        )

        with patch.object(
            ownership_planner,
            "retire_device_manifests",
            wraps=ownership_planner.retire_device_manifests,
        ) as retire_device:
            device.delete()

        manifest.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "retired")
        retire_device.assert_called_once_with(device_id)

    def test_device_manifest_retirement_uses_one_update(self):
        from netbox_nso_plugin import ownership_planner
        from netbox_nso_plugin.models import NSOOwnershipManifest

        from ._outbox_case import make_managed

        device, _management = make_managed("manifest-bulk-retirement", 16274)
        for scope in ("interface", "vlan"):
            NSOOwnershipManifest.objects.create(
                device_id=device.pk,
                scope=scope,
                native_model_label="dcim.interface",
                native_key={"device_id": device.pk, "scope": scope},
            )

        with self.assertNumQueries(1):
            ownership_planner.retire_device_manifests(device.pk)

        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=device.pk, ownership_state="owned").exists())


class TestOwnershipManifestMaintenance(TestCase):
    def setUp(self):
        from ._outbox_case import make_managed

        self.device, self.management = make_managed("manifest-maintenance", 1627)

    def test_retired_manifest_is_not_reactivated(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import maintain_manifest

        from ._outbox_case import own_vlan

        state = own_vlan(self.management, 1701, "manifest-retired")
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="vlan")
        manifest.ownership_state = "retired"
        manifest.deletion_authority = False
        manifest.save(update_fields=("ownership_state", "deletion_authority"))

        maintain_manifest(state)

        manifest.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertFalse(manifest.deletion_authority)

    def test_under_lock_manifest_recheck_runs_once_for_the_scope_set(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin import ownership_planner
        from netbox_nso_plugin.models import NSOOwnershipManifest

        from ._outbox_case import make_managed, own_vlan

        query_counts = []
        for count in (1, 2, 3):
            device, management = make_managed(f"manifest-scale-{count}", 1700 + count)
            states = [
                own_vlan(management, 1710 + count * 10 + index, f"manifest-scale-{count}-{index}")
                for index in range(count)
            ]
            NSOOwnershipManifest.objects.filter(device_id=device.pk, scope="vlan").delete()

            with CaptureQueriesContext(connection) as queries:
                completed = ownership_planner.reconcile_scope_ownership(device.pk, {"vlan"})

            self.assertCountEqual(completed, (("vlan", state.pk) for state in states))
            query_counts.append(len(queries))

        self.assertEqual(query_counts[2] - query_counts[1], query_counts[1] - query_counts[0])
