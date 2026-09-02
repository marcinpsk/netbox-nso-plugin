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
                native_id=device.pk,
                native_key={"device_id": device.pk, "scope": scope},
            )

        with self.assertNumQueries(1):
            ownership_planner.retire_device_manifests(device.pk)

        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=device.pk, ownership_state="owned").exists())
