# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1627: durable ownership manifest schema."""

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase


class TestOwnershipManifestSchema(SimpleTestCase):
    def test_native_and_overlay_identity_is_stored_without_relations(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest

        fields = {field.name: field for field in NSOOwnershipManifest._meta.fields}

        assert not fields["device_id"].is_relation
        assert not fields["native_model_label"].is_relation
        assert not fields["native_key"].is_relation
        assert not fields["native_id"].is_relation
        assert not fields["state_model_label"].is_relation
        assert not fields["state_key"].is_relation
        assert "overlay" not in fields

    def test_one_manifest_row_identifies_each_owned_object(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest

        constraints = {
            tuple(expression.name for expression in constraint.expressions)
            for constraint in NSOOwnershipManifest._meta.constraints
        }

        assert (
            "device_id",
            "scope",
            "native_model_label",
            "native_key",
            "state_model_label",
            "state_key",
        ) in constraints

    def test_native_and_overlay_identity_fields_are_required(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest

        fields = {field.name: field for field in NSOOwnershipManifest._meta.fields}

        assert fields["native_id"].null is False
        assert fields["state_model_label"].blank is False

    def test_manifest_carries_ownership_and_deletion_evidence(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest

        fields = {field.name: field for field in NSOOwnershipManifest._meta.fields}

        assert fields["ownership_state"].get_default() == "owned"
        assert fields["deletion_authority"].get_default() is False
        assert fields["acknowledged_lineage"].get_default() == []


_MANIFEST_TABLE = "netbox_nso_plugin_nsoownershipmanifest"


class _PeerManifestInsert:
    """Land a peer audit's identical manifest row right after this audit read the identity.

    The identity read and the write are separate statements, so a second audit can land the
    row in the window between them. Reproduce that against the real unique index.
    """

    def __init__(self, identity):
        self.identity = identity
        self.fired = False

    def __call__(self, execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if not self.fired and sql.lstrip().upper().startswith("SELECT") and _MANIFEST_TABLE in sql:
            self.fired = True
            from netbox_nso_plugin.models import NSOOwnershipManifest

            NSOOwnershipManifest.objects.create(
                **self.identity,
                native_id=0,
                ownership_state="owned",
                deletion_authority=False,
            )
        return result


class TestOwnershipManifestConcurrency(TestCase):
    def test_a_peer_insert_does_not_abort_the_recording_transaction(self):
        from django.db import connection
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOOwnershipManifest, NSOVLANState
        from netbox_nso_plugin.ownership_planner import maintain_manifest, manifest_binding

        from ._outbox_case import make_managed

        device, management = make_managed("ownership-conflict", 16274)
        group = VLANGroup.objects.create(name="Ownership conflict", slug=f"nso-{device.pk}")
        vlan = VLAN.objects.create(group=group, vid=1730, name="ownership-conflict")
        state = NSOVLANState.objects.create(
            management=management,
            vlan=vlan,
            device_name=vlan.name,
            status="accepted",
        )
        (
            _rule,
            scope,
            device_id,
            native_model_label,
            _native_id,
            native_key,
            state_model_label,
            state_key,
        ) = manifest_binding(state)
        identity = {
            "device_id": device_id,
            "scope": scope,
            "native_model_label": native_model_label,
            "native_key": native_key,
            "state_model_label": state_model_label,
            "state_key": state_key,
        }
        peer = _PeerManifestInsert(identity)

        with connection.execute_wrapper(peer):
            maintain_manifest(state)

        manifest = NSOOwnershipManifest.objects.get(**identity)
        self.assertTrue(peer.fired)
        self.assertEqual(NSOOwnershipManifest.objects.filter(**identity).count(), 1)
        self.assertEqual(manifest.native_id, vlan.pk)
        self.assertEqual(manifest.ownership_state, "owned")
        self.assertTrue(manifest.deletion_authority)

    def test_a_peer_insert_does_not_abort_retired_manifest_adoption(self):
        from django.db import connection
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOOwnershipManifest, NSOVLANState
        from netbox_nso_plugin.ownership_planner import maintain_manifest, manifest_binding

        from ._outbox_case import make_managed

        device, management = make_managed("ownership-retired-conflict", 16275)
        group = VLANGroup.objects.create(name="Ownership retired conflict", slug=f"nso-{device.pk}")
        vlan = VLAN.objects.create(group=group, vid=1731, name="ownership-retired-conflict")
        state = NSOVLANState.objects.create(
            management=management,
            vlan=vlan,
            device_name=vlan.name,
            status="accepted",
        )
        binding = manifest_binding(state)
        identity = {
            "device_id": binding[2],
            "scope": binding[1],
            "native_model_label": binding[3],
            "native_key": binding[5],
            "state_model_label": binding[6],
            "state_key": binding[7],
        }
        previous = NSOOwnershipManifest.objects.create(
            **(identity | {"native_key": identity["native_key"] | {"vid": 1730}}),
            native_id=vlan.pk,
            ownership_state="retired",
        )
        peer = _PeerManifestInsert(identity)

        with connection.execute_wrapper(peer):
            maintain_manifest(state)

        previous.refresh_from_db()
        manifest = NSOOwnershipManifest.objects.get(**identity)
        self.assertTrue(peer.fired)
        self.assertEqual(previous.ownership_state, "retired")
        self.assertEqual(manifest.native_id, vlan.pk)
        self.assertTrue(manifest.deletion_authority)


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
            native_id=device_id,
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

    def test_bulk_device_deletion_offboards_the_adapter_device(self):
        from unittest.mock import patch

        from dcim.models import Device

        from ._outbox_case import make_managed

        device, management = make_managed("ownership-bulk-offboard", 16273)

        with (
            patch("netbox_nso_plugin.adapter_client.delete_device") as delete_device,
            self.captureOnCommitCallbacks(execute=True),
        ):
            Device.objects.filter(pk=device.pk).delete()

        delete_device.assert_called_once_with(management.adapter_device_id)


class TestOwnershipManifestMaintenance(TestCase):
    def setUp(self):
        from ._outbox_case import make_managed

        self.device, self.management = make_managed("manifest-maintenance", 1627)

    def test_route_policy_binding_rejects_an_invalid_generic_target(self):
        from django.contrib.contenttypes.models import ContentType

        from netbox_nso_plugin.models import NSODeviceManagement, NSOOwnershipManifest, NSORoutePolicyState
        from netbox_nso_plugin.ownership_planner import maintain_manifest, manifest_binding

        state = NSORoutePolicyState.objects.create(
            management=self.management,
            content_type=ContentType.objects.get_for_model(NSODeviceManagement),
            object_id=self.management.pk,
            family="route_map",
            object_name="invalid-manifest-target",
            status="accepted",
        )

        self.assertIsNone(manifest_binding(state))
        maintain_manifest(state)
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk).exists())

    def test_route_policy_binding_rejects_a_model_from_another_family(self):
        from django.contrib.contenttypes.models import ContentType
        from django.core.exceptions import ValidationError
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.ownership_planner import manifest_binding

        route_map = RouteMap.objects.create(name="manifest-wrong-family")
        state = NSORoutePolicyState.objects.create(
            management=self.management,
            content_type=ContentType.objects.get_for_model(RouteMap),
            object_id=route_map.pk,
            family="prefix_list",
            object_name=route_map.name,
            status="accepted",
        )

        self.assertIsNone(manifest_binding(state))

        with self.assertRaises(ValidationError) as error:
            state.full_clean()

        self.assertIn("content_type", error.exception.message_dict)

    def test_manifest_schema_drift_raises_the_model_field_error(self):
        from dataclasses import replace

        from django.core.exceptions import FieldDoesNotExist
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.ownership_planner import _validated_native_key_fields, converted_scope_rules

        route_map = RouteMap.objects.create(name="manifest-schema-drift")
        rule = replace(converted_scope_rules()["route_policy"], native_key_fields=("missing_identity",))

        with self.assertRaises(FieldDoesNotExist):
            _validated_native_key_fields(route_map, rule)

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

            with (
                patch.object(
                    ownership_planner,
                    "_manifest_record_actions",
                    wraps=ownership_planner._manifest_record_actions,
                ) as record_actions,
                patch.object(
                    ownership_planner,
                    "_manifest_states",
                    wraps=ownership_planner._manifest_states,
                ) as manifest_states,
                CaptureQueriesContext(connection) as queries,
            ):
                completed = ownership_planner.reconcile_scope_ownership(device.pk, {"vlan"})

            self.assertCountEqual(completed, (("vlan", state.pk) for state in states))
            self.assertEqual(record_actions.call_count, 1)
            self.assertEqual(manifest_states.call_count, 2)
            query_counts.append(len(queries))

        self.assertEqual(query_counts[2] - query_counts[1], query_counts[1] - query_counts[0])
