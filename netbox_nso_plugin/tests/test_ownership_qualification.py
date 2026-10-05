# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""An explicit resolved acquisition must survive the ownership audit."""

import copy

from dcim.models import Interface
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import TestCase
from netbox_routing.models import PrefixList

from netbox_nso_plugin.models import NSOOwnershipAcquisition, NSOOwnershipManifest
from netbox_nso_plugin.ownership_grants import GRANTS, OwnershipGrant
from netbox_nso_plugin.ownership_planner import (
    OwnershipNotQualified,
    converted_scope_rules,
    manifest_binding,
    reconcile_scope_ownership,
)
from netbox_nso_plugin.renderer_writer import (
    RendererMutationPlan,
    planned_save,
    renderer_mirror_writes,
    renderer_writes,
)

from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay, save_overlay_fixture
from .mixins import IntentPushResetMixin
from .test_acquisition_producers import PRODUCERS
from .test_native_only_acquisition import NATIVE_FACTORIES

ANCHOR_MODELS = {
    "nsobfdinterfacestate": {},
    "nsosvistate": {},
    "nsosubinterfacestate": {},
    "nsol2sapstate": {"service_name": "test-service", "sap_id": "test-port:100"},
    "nsologginghoststate": {"address": "198.18.0.1"},
    "nsologginglevelstate": {},
    "nsosnmpcommunitystate": {"community_hash": "test-community-hash"},
    "nsosnmpv3userstate": {"username": "test-user"},
    "nsosnmphoststate": {"address": "198.18.0.2"},
    "nsosnmpsysteminfostate": {},
    "nsoroutepolicystate": {},
}


def _native_content_values(name, native, factory_index):
    values = {}
    if name == "nsointerfacestate":
        values["attribute"] = "description"
    elif name == "nsolacpbundlestate":
        values["lag_id"] = 10
    elif name == "nsolacpmemberstate":
        values["mode"] = "active"
    elif name == "nsoisisinstancestate":
        values["process_tag"] = native.process_tag
    elif name == "nsoisisinterfacestate":
        values.update(interface=native.interface, af=native.address_family, process_tag=native.instance.process_tag)
    elif name == "nsoisisflexalgostate":
        values.update(process_tag=native.instance.process_tag, algo_id=native.algo_id)
    elif name == "nsoospfinstancestate":
        values["process_id"] = str(native.process_id)
    elif name == "nsobgppeerstate":
        values.update(
            asn_str=str(native.scope.router.asn.asn), vrf_name="", peer_address_str=str(native.peer.address.ip)
        )
    elif name == "nsoredistributionstate":
        protocol = ("bgp", "isis", "ospf")[factory_index]
        reference = {"bgp": "64520//ipv4-unicast", "isis": "CORE", "ospf": "10"}[protocol]
        values.update(dest_protocol=protocol, dest_ref=reference, source_protocol="static")
    return values


def qualifying_overlay(device, management, label, factory_index=0):
    model = apps.get_model(label)
    values = {"management": management, "status": "imported"}
    field_names = {field.name for field in model._meta.concrete_fields}
    if "management" not in field_names:
        values.pop("management")
    name = label.split(".")[-1]
    if label in NATIVE_FACTORIES:
        native = NATIVE_FACTORIES[label][1][factory_index](device)
        native.refresh_from_db()
        rule = next(rule for rule in converted_scope_rules().values() if label in rule.overlay_model_labels)
        field = dict(rule.overlay_native_fields)[label]
        if field == "__ip_address__":
            values.update(interface=native.assigned_object, address=str(native.address), family="ipv4")
        elif field == "__ospf_interface__":
            values.update(
                interface=native.interface, process_id=str(native.instance.process_id), area_id=str(native.area.area_id)
            )
        else:
            values[field] = native
        values.update(_native_content_values(name, native, factory_index))
        return model.objects.create(**values)
    values.update(ANCHOR_MODELS[name])
    if name in {"nsobfdinterfacestate", "nsosvistate", "nsosubinterfacestate"}:
        interface = Interface.objects.create(device=device, name="test-interface", type="virtual")
        if name == "nsosubinterfacestate":
            parent = Interface.objects.create(device=device, name="test-parent", type="1000base-t")
            interface.parent = parent
            interface.save(update_fields=("parent",))
            values["parent_interface"] = parent
        values["interface"] = interface
    if name == "nsoroutepolicystate":
        native = PrefixList.objects.create(name="test-prefix-list")
        values.update(
            content_type=ContentType.objects.get_for_model(PrefixList),
            object_id=native.pk,
            family="prefix_list",
            object_name=native.name,
        )
    return model.objects.create(**values)


class TestAcquisitionQualification(IntentPushResetMixin, TestCase):
    def acquire(self, row, grant):
        candidate = copy.copy(row)
        candidate.status = "accepted"
        plan = RendererMutationPlan.build(
            saves=(planned_save(candidate, update_fields=("status",), expected_before=row),), grant=grant
        )
        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
        with mutation(plan) as writer:
            writer.save(candidate, update_fields=("status",))

    def test_acquisition_qualifies_after_the_last_native_save(self):
        from netbox_nso_plugin.models import NSOInterfaceMtuState

        device, management = make_managed("post-plan-qualification", None)
        interface = Interface.objects.create(device=device, name="test-port", type="1000base-t")
        row = NSOInterfaceMtuState(management=management, interface=interface, status="accepted", l2_mtu=9000)
        native = copy.copy(interface)
        native.mtu = 9000
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(row, force_insert=True, natural_key=("management", "interface")),
                planned_save(native, update_fields=("mtu",)),
            ),
            grant=OwnershipGrant("create"),
        )
        with without_commit_drain(), renderer_writes(plan) as writer:
            writer.save(row, force_insert=True)
            writer.save(native, update_fields=("mtu",))
        self.assertEqual(reconcile_scope_ownership(device.pk, ("interface_mtu",)), ())
        self.assertEqual(NSOOwnershipManifest.objects.get(device_id=device.pk).ownership_state, "owned")

    def test_nonqualifying_post_plan_state_rolls_back_every_save(self):
        from netbox_nso_plugin.models import NSOInterfaceMtuState

        device, management = make_managed("post-plan-refusal", None)
        interface = Interface.objects.create(device=device, name="test-port", type="1000base-t", mtu=9000)
        row = NSOInterfaceMtuState(management=management, interface=interface, status="accepted", l2_mtu=9000)
        native = copy.copy(interface)
        native.mtu = None
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(row, force_insert=True, natural_key=("management", "interface")),
                planned_save(native, update_fields=("mtu",)),
            ),
            grant=OwnershipGrant("create"),
        )
        with self.assertRaises(OwnershipNotQualified), without_commit_drain(), renderer_writes(plan) as writer:
            writer.save(row, force_insert=True)
            writer.save(native, update_fields=("mtu",))
        interface.refresh_from_db()
        self.assertEqual(interface.mtu, 9000)
        self.assertFalse(NSOInterfaceMtuState.objects.filter(management=management).exists())
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=device.pk).exists())

    def test_raced_owned_creation_still_requires_post_plan_qualification(self):
        from netbox_nso_plugin.models import NSOInterfaceMtuState

        device, management = make_managed("raced-ownership-qualification", None)
        interface = Interface.objects.create(device=device, name="test-port", type="1000base-t", mtu=9000)
        candidate = NSOInterfaceMtuState(management=management, interface=interface, status="accepted", l2_mtu=9000)
        plan = RendererMutationPlan.build(
            saves=(planned_save(candidate, force_insert=True, natural_key=("management", "interface")),),
            grant=OwnershipGrant("create"),
        )
        raced = acquire_overlay(
            NSOInterfaceMtuState,
            management=management,
            interface=interface,
            status="accepted",
            l2_mtu=9000,
        )
        manifests_before = list(NSOOwnershipManifest.objects.filter(device_id=device.pk).order_by("pk").values())
        Interface.objects.filter(pk=interface.pk).update(mtu=None)
        with self.assertRaises(OwnershipNotQualified), without_commit_drain(), renderer_writes(plan) as writer:
            self.assertTrue(writer.consume_existing_creation(raced))
        self.assertEqual(
            list(NSOOwnershipManifest.objects.filter(device_id=device.pk).order_by("pk").values()), manifests_before
        )
        self.assertFalse(
            NSOOwnershipAcquisition.objects.filter(
                state_model_label=raced._meta.label_lower, state_id=raced.pk
            ).exists()
        )

    def test_raced_owned_update_still_requires_post_plan_qualification(self):
        from netbox_nso_plugin.models import NSOInterfaceMtuState

        device, management = make_managed("raced-ownership-update", None)
        interface = Interface.objects.create(device=device, name="test-port", type="1000base-t", mtu=9000)
        row = NSOInterfaceMtuState.objects.create(
            management=management, interface=interface, status="imported", l2_mtu=9000
        )
        candidate = copy.copy(row)
        candidate.status = "accepted"
        plan = RendererMutationPlan.build(
            saves=(planned_save(candidate, update_fields=("status",), expected_before=row),),
            grant=OwnershipGrant("accept"),
        )
        save_overlay_fixture(candidate, update_fields=("status",))
        manifests_before = list(NSOOwnershipManifest.objects.filter(device_id=device.pk).order_by("pk").values())
        Interface.objects.filter(pk=interface.pk).update(mtu=None)
        with self.assertRaises(OwnershipNotQualified), without_commit_drain(), renderer_writes(plan) as writer:
            self.assertTrue(writer.consume_applied_save(candidate))
        self.assertEqual(
            list(NSOOwnershipManifest.objects.filter(device_id=device.pk).order_by("pk").values()), manifests_before
        )
        self.assertFalse(
            NSOOwnershipAcquisition.objects.filter(state_model_label=row._meta.label_lower, state_id=row.pk).exists()
        )

    def test_factory_matrix_covers_every_converted_overlay(self):
        expected = {label for rule in converted_scope_rules().values() for label in rule.overlay_model_labels}
        actual = set(NATIVE_FACTORIES) | {f"netbox_nso_plugin.{name}" for name in ANCHOR_MODELS}
        self.assertEqual(actual, expected)

    def test_classified_producer_grants_are_in_the_qualification_matrix(self):
        inherited = {
            "enclosing_accept_or_create": {"accept", "create"},
            "operator_edit_from_view": {"operator_edit"},
        }
        exercised = set()
        for producer, (kind, _count) in PRODUCERS.items():
            with self.subTest(producer=producer):
                kinds = inherited.get(kind, {kind})
                self.assertLessEqual(kinds, GRANTS)
                exercised.update(kinds)
        self.assertEqual(exercised, GRANTS - {"intend"})

    def test_qualifying_accept_survives_audit_for_every_scope_and_grant(self):
        for rule in converted_scope_rules().values():
            for label in rule.overlay_model_labels:
                factories = NATIVE_FACTORIES.get(label, (None, (None,), None))[1]
                for index in range(len(factories)):
                    for kind in sorted(GRANTS - {"manifest_reown"}):
                        with (
                            self.subTest(scope=rule.scope, model=label, destination=index, grant=kind),
                            transaction.atomic(),
                            without_commit_drain(),
                        ):
                            device, management = make_managed(
                                f"qualification-{label.split('.')[-1]}-{index}-{kind}", None
                            )
                            management.manage_description = True
                            management.save(update_fields=("manage_description",))
                            row = qualifying_overlay(device, management, label, index)
                            self.acquire(row, OwnershipGrant(kind))
                            manifests = NSOOwnershipManifest.objects.filter(
                                device_id=device.pk, ownership_state="owned"
                            )
                            self.assertEqual(manifests.count(), 1)
                            scope = manifests.get().scope
                            self.assertEqual(reconcile_scope_ownership(device.pk, (scope,)), ())
                            row.refresh_from_db()
                            self.assertEqual(row.status, "accepted")
                            self.assertEqual(manifests.get().grant_kind, kind)
                            transaction.set_rollback(True)

    def test_resolved_nonqualifying_acquisition_rolls_back_status_and_manifest(self):
        cases = {
            "nsolacpbundlestate": ("type", "other"),
            "nsolacpmemberstate": ("lag", None),
            "nsoswitchportstate": ("mode", ""),
            "nsointerfacemtustate": ("mtu", None),
            "nsointerfacestate": ("attribute", "enabled"),
            "nsosubinterfacestate": ("parent", None),
            "nsosvistate": ("device", "foreign"),
            "nsostaticroutestate": ("devices", None),
            "nsobgppeerstate": ("asn_str", "64522"),
            "nsoisisinstancestate": ("process_tag", "OTHER"),
            "nsoisisinterfacestate": ("af", "ipv6"),
            "nsoisisflexalgostate": ("algo_id", 129),
            "nsoospfinstancestate": ("process_id", "20"),
            "nsoospfinterfacestate": ("device", "foreign"),
            "nsoredistributionstate": ("source_protocol", "ospf"),
        }
        for name, (field, value) in cases.items():
            with self.subTest(model=name), transaction.atomic(), without_commit_drain():
                device, management = make_managed(f"unqualified-{name}", None)
                management.manage_description = True
                management.manage_enabled = False
                management.save(update_fields=("manage_description", "manage_enabled"))
                row = qualifying_overlay(device, management, f"netbox_nso_plugin.{name}")
                if field in {"attribute", "asn_str", "process_tag", "af", "algo_id", "process_id", "source_protocol"}:
                    setattr(row, field, value)
                    row.save(update_fields=(field,))
                elif field == "devices":
                    row.static_route.devices.remove(device)
                else:
                    native = row.interface
                    if field == "device":
                        value = make_managed("foreign-anchor", None)[0]
                    setattr(native, field, value)
                    if name == "nsoswitchportstate":
                        native.untagged_vlan = None
                        native.save(update_fields=("mode", "untagged_vlan"))
                    else:
                        native.save(update_fields=(field,))
                row.refresh_from_db()
                if hasattr(row, "interface_id") and row.interface_id is not None:
                    row.interface.refresh_from_db()
                self.assertIsNotNone(manifest_binding(row))
                with self.assertRaises(OwnershipNotQualified), transaction.atomic():
                    self.acquire(row, OwnershipGrant("accept"))
                row.refresh_from_db()
                self.assertEqual(row.status, "imported")
                self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=device.pk).exists())
                transaction.set_rollback(True)

    def test_link_role_refuses_a_resolved_binding_for_another_device(self):
        from netbox_nso_plugin.link_role import enable_igp_for_role
        from netbox_nso_plugin.models import NSOLinkRole, NSOOSPFInterfaceState

        device, management = make_managed("link-role-native-anchor", None)
        row = qualifying_overlay(device, management, "netbox_nso_plugin.nsoospfinterfacestate")
        _other_device, other_management = make_managed("link-role-other-device", None)
        role = NSOLinkRole.objects.create(
            name="test-ospf-role", slug="test-ospf-role", igp="ospf", ospf_process_id="10", ospf_area="0.0.0.0"
        )
        with without_commit_drain():
            result = enable_igp_for_role(row.interface, role, push=False, mgmt=other_management)
        self.assertIn("does not qualify", result["error"])
        self.assertFalse(result["enabled"])
        self.assertFalse(NSOOSPFInterfaceState.objects.filter(management=other_management).exists())
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=other_management.device_id).exists())

    def test_link_role_unresolved_routing_binding_keeps_explicit_exception(self):
        from netbox_nso_plugin.link_role import enable_igp_for_role
        from netbox_nso_plugin.models import NSOLinkRole, NSOOSPFInterfaceState

        device, management = make_managed("unbound-link-role", None)
        interface = Interface.objects.create(device=device, name="test-port", type="1000base-t")
        role = NSOLinkRole.objects.create(
            name="test-role",
            slug="test-role",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            igp="ospf",
            ospf_process_id="1",
            ospf_area="0",
        )
        with without_commit_drain():
            self.assertTrue(enable_igp_for_role(interface, role, push=False, mgmt=management)["enabled"])
            self.assertEqual(reconcile_scope_ownership(device.pk, ("ospf",)), ())
        row = NSOOSPFInterfaceState.objects.get(management=management, interface=interface)
        self.assertEqual(row.status, "accepted")
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=device.pk).exists())

    def test_manifest_reown_requires_a_qualifying_continuing_binding(self):
        from netbox_nso_plugin.models import NSOInterfaceMtuState

        device, management = make_managed("qualifying-reown", None)
        interface = Interface.objects.create(device=device, name="test-port", type="1000base-t", mtu=9000)
        row = NSOInterfaceMtuState.objects.create(management=management, interface=interface, status="imported")
        with without_commit_drain():
            self.acquire(row, OwnershipGrant("accept"))
            row.refresh_from_db()
            type(row).objects.filter(pk=row.pk).update(status="imported")
            row.refresh_from_db()
            manifest = NSOOwnershipManifest.objects.get(device_id=device.pk)
            self.acquire(row, OwnershipGrant("manifest_reown", manifest_pk=manifest.pk))
            self.assertEqual(reconcile_scope_ownership(device.pk, ("interface_mtu",)), ())
        manifest.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "owned")
