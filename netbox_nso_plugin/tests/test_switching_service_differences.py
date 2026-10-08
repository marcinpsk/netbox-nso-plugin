# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Compare each switching and service scope through real NetBox bindings."""

import copy
from uuid import uuid4

from dcim.models import Interface, Platform
from django.contrib.auth import get_user_model
from django.test import TestCase
from ipam.models import VLAN, VLANGroup

from netbox_nso_plugin.device_differences import NOT_VISIBLE, differences
from netbox_nso_plugin.models import (
    NSOFamilyObservation,
    NSOFamilyReadState,
    NSOInterfaceMtuState,
    NSOL2SapState,
    NSOLACPBundleState,
    NSOLACPMemberState,
    NSOLoggingHostState,
    NSOLoggingLevelState,
    NSOPlatformNedMapping,
    NSOSnmpCommunityState,
    NSOSnmpHostState,
    NSOSnmpSystemInfoState,
    NSOSnmpV3UserState,
    NSOSubinterfaceState,
    NSOSVIState,
    NSOVLANState,
)
from netbox_nso_plugin.observations import observation_defaults

from ._scope_observation_case import DOCUMENTS, entry, scope_observation
from .test_gated_reconcile import _make


class _ScopeDifferences:
    def setUp(self):
        self.device, self.management = _make(f"scope{uuid4().hex[:8]}", manage_interfaces=True)
        self.user = get_user_model().objects.create_superuser(username=f"scope{uuid4().hex[:8]}")
        self.interface = Interface.objects.get(device=self.device)
        self.interface.name = "Ethernet1"
        self.interface.type = "1000base-t"
        self.interface.save()
        self.document = copy.deepcopy(DOCUMENTS[self.family])
        self.make_native()

    def vlan(self, vid, name=None):
        group, _ = VLANGroup.objects.get_or_create(slug=f"nso-{self.device.pk}", defaults={"name": "Device VLANs"})
        return VLAN.objects.create(group=group, vid=vid, name=name or f"example-{vid}")

    def use_ned(self, ned_id):
        platform = Platform.objects.create(name=f"example-{self.device.pk}", slug=f"example-{self.device.pk}")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id=ned_id)
        self.device.platform = platform
        self.device.save(update_fields=["platform"])

    def publish(self, document=None, coverage=None):
        observed = scope_observation(
            self.family, document=self.document if document is None else document, coverage=coverage
        )
        state, _ = NSOFamilyReadState.objects.get_or_create(management=self.management, family=self.family)
        NSOFamilyObservation.objects.update_or_create(
            read_state=state, defaults=observation_defaults(self.family, 1, 1, observed)
        )

    def rows(self):
        return [row for row in differences(self.management, user=self.user) if row.scope == self.scope]

    def test_match(self):
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])
        self.assertTrue(all(row.kind == "unavailable" for row in self.rows()))

    def test_mismatch(self):
        self.changed_entry()[self.changed_attribute] = self.changed_value
        self.publish()
        self.assertIn(("mismatch", self.changed_attribute), [(row.kind, row.attribute) for row in self.rows()])

    def test_netbox_only(self):
        document = copy.deepcopy(self.document)
        for key in document["present"]:
            document[key] = entry() if key == "system" else (None if key == "local_levels" else [])
        self.publish(document)
        self.assertIn("netbox_only", [row.kind for row in self.rows()])

    def test_device_only(self):
        self.remove_native()
        self.publish()
        self.assertIn("device_only", [row.kind for row in self.rows()])

    def test_unprojectable_is_ambiguous(self):
        self.document["unprojectable"] = [{"index": 0, "reason": "invalid identity"}]
        self.publish()
        self.assertIn(("ambiguous", "invalid identity"), [(row.kind, row.reason) for row in self.rows()])

    def test_no_read_is_unavailable(self):
        self.assertEqual([(row.kind, row.reason) for row in self.rows()], [("unavailable", "no successful read yet")])

    def test_partial_coverage_is_unavailable(self):
        self.publish(coverage={"attributes": [], "not_comparable": [self.changed_attribute]})
        self.assertIn(("unavailable", self.changed_attribute), [(row.kind, row.attribute) for row in self.rows()])
        self.assertFalse([row for row in self.rows() if row.kind == "mismatch"])

    def changed_entry(self):
        return self.document[self.collection][0]

    def remove_native(self):
        self.native.delete()


class TestVlanDifferences(_ScopeDifferences, TestCase):
    scope = family = "vlan"
    collection = "vlans"
    changed_attribute, changed_value = "name", "different"

    def make_native(self):
        self.native = self.vlan(10, "MGMT")

    def test_hidden_vlan_does_not_leak_native_values(self):
        self.native.name = "hidden name"
        self.native.save()
        self.user = get_user_model().objects.create_user(username=f"hidden{uuid4().hex[:8]}")
        self.publish()
        rows = self.rows()
        self.assertEqual([(row.kind, row.reason) for row in rows], [("ambiguous", NOT_VISIBLE)])
        self.assertNotIn("hidden name", repr(rows))

    def test_duplicate_native_vid_is_ambiguous(self):
        other = VLAN.objects.create(vid=10, name="duplicate")
        NSOVLANState.objects.create(management=self.management, vlan=other)
        self.publish()
        self.assertEqual([row.kind for row in self.rows()], ["ambiguous"])


class TestSwitchportDifferences(_ScopeDifferences, TestCase):
    scope = family = "switchport"
    collection = "interfaces"
    changed_attribute, changed_value = "mode", "access"

    def make_native(self):
        self.native = self.interface
        self.native.mode = "tagged"
        self.native.untagged_vlan = self.vlan(10)
        self.native.save()
        self.native.tagged_vlans.set([self.native.untagged_vlan, self.vlan(20)])

    def test_hidden_related_vlan_is_redacted(self):
        from core.models import ObjectType
        from users.models import ObjectPermission

        self.user = get_user_model().objects.create_user(username=f"related{uuid4().hex[:8]}")
        permission = ObjectPermission.objects.create(name="Visible interface", actions=["view"])
        permission.object_types.add(ObjectType.objects.get_for_model(Interface))
        permission.users.add(self.user)
        self.publish()
        self.assertTrue(all(row.reason == NOT_VISIBLE for row in self.rows()))

    def test_default_untagged_vlan_uses_reconciler_equivalence(self):
        self.native.untagged_vlan = None
        self.native.save()
        self.changed_entry()["untagged_vlan"] = 1
        self.publish()
        self.assertEqual(self.rows(), [])


class TestMtuDifferences(_ScopeDifferences, TestCase):
    scope = family = "interface_mtu"
    collection = "interfaces"
    changed_attribute, changed_value = "mtu", 1500

    def make_native(self):
        self.native = self.interface
        self.native.mtu = 9216
        self.native.save()
        NSOInterfaceMtuState.objects.create(
            management=self.management, interface=self.native, l2_mtu=9216, ip_mtu=9000, mpls_mtu=None
        )

    def test_overlay_anchor_with_null_native_mtu_matches(self):
        self.native.mtu = None
        self.native.save()
        self.changed_entry()["mtu"] = None
        self.publish()
        self.assertEqual(self.rows(), [])

    def test_overlay_anchor_does_not_qualify_for_ownership_without_native_mtu(self):
        from netbox_nso_plugin.ownership_planner import _interface_mtu_bindings

        self.assertEqual(
            [row.pk for _scope, row, _model, _key in _interface_mtu_bindings(self.management)], [self.native.pk]
        )
        self.native.mtu = None
        self.native.save()
        self.changed_entry()["mtu"] = None
        self.publish()
        self.assertEqual(_interface_mtu_bindings(self.management), ())
        self.assertEqual(self.rows(), [])

    def test_overlay_anchor_with_null_native_mtu_survives_empty_observation(self):
        self.native.mtu = None
        self.native.save()
        document = copy.deepcopy(self.document)
        document["interfaces"] = []
        self.publish(document)
        self.assertEqual([(row.kind, row.identity) for row in self.rows()], [("netbox_only", "Ethernet1")])

    def test_zero_is_distinct_from_null_and_missing(self):
        for value in (0, None):
            with self.subTest(value=value):
                self.changed_entry()["mtu"] = value
                self.publish()
                self.assertIn(("mismatch", "mtu"), [(row.kind, row.attribute) for row in self.rows()])
        self.changed_entry().pop("mtu")
        self.changed_entry()["present"].remove("mtu")
        self.publish()
        self.assertIn(("mismatch", "mtu"), [(row.kind, row.attribute) for row in self.rows()])


class TestSviDifferences(_ScopeDifferences, TestCase):
    scope = family = "svi"
    collection = "interfaces"
    changed_attribute, changed_value = "vrf", "OTHER"

    def make_native(self):
        self.native = Interface.objects.create(device=self.device, name="Vlan100", type="virtual")
        vlan = self.vlan(100)
        NSOVLANState.objects.create(management=self.management, vlan=vlan)
        NSOSVIState.objects.create(
            management=self.management, interface=self.native, vlan=vlan, svi_type="svi", vrf="MGMT"
        )


class TestSubinterfaceDifferences(_ScopeDifferences, TestCase):
    scope = family = "subinterface"
    collection = "interfaces"
    changed_attribute, changed_value = "dot1q_vlan", 200

    def make_native(self):
        self.native = Interface.objects.create(
            device=self.device, name="Ethernet1.100", type="virtual", parent=self.interface
        )
        NSOSubinterfaceState.objects.create(
            management=self.management,
            interface=self.native,
            parent_interface=self.interface,
            dot1q_vlan=100,
            vrf="TENANT_A",
        )

    def conflicting_parent(self):
        parent = Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t")
        NSOSubinterfaceState.objects.filter(interface=self.native).update(parent_interface=parent)
        return parent

    def test_inconsistent_parents_are_ambiguous_even_when_native_parent_matches(self):
        self.conflicting_parent()
        self.publish()
        rows = self.rows()
        self.assertEqual(
            [(row.kind, row.reason) for row in rows],
            [("ambiguous", "native and overlay parent interfaces are inconsistent")],
        )
        self.assertNotIn("Ethernet2", repr(rows))

    def test_missing_native_parent_is_inconsistent_with_overlay_parent(self):
        self.native.parent = None
        self.native.save()
        self.publish()
        self.assertEqual(
            [(row.kind, row.reason) for row in self.rows()],
            [("ambiguous", "native and overlay parent interfaces are inconsistent")],
        )

    def test_missing_overlay_parent_is_inconsistent_with_native_parent(self):
        NSOSubinterfaceState.objects.filter(interface=self.native).update(parent_interface=None)
        self.publish()
        self.assertEqual(
            [(row.kind, row.reason) for row in self.rows()],
            [("ambiguous", "native and overlay parent interfaces are inconsistent")],
        )

    def test_visibility_includes_each_inconsistent_parent(self):
        from core.models import ObjectType
        from users.models import ObjectPermission

        parent = self.conflicting_parent()
        overlay = NSOSubinterfaceState.objects.get(interface=self.native)
        self.publish()
        for hidden_parent in (self.interface, parent):
            with self.subTest(hidden_parent=hidden_parent.pk):
                self.user = get_user_model().objects.create_user(username=f"parent{uuid4().hex[:8]}")
                for obj in (self.native, overlay, self.interface, parent):
                    if obj == hidden_parent:
                        continue
                    permission = ObjectPermission.objects.create(
                        name=f"View {obj._meta.model_name} {obj.pk}", actions=["view"], constraints={"pk": obj.pk}
                    )
                    permission.object_types.add(ObjectType.objects.get_for_model(type(obj)))
                    permission.users.add(self.user)
                rows = self.rows()
                self.assertEqual([(row.kind, row.reason) for row in rows], [("ambiguous", NOT_VISIBLE)])
                self.assertNotIn("Ethernet2", repr(rows))
                self.assertNotIn("TENANT_A", repr(rows))


class TestBfdDifferences(_ScopeDifferences, TestCase):
    scope = family = "bfd"
    collection = "interfaces"
    changed_attribute, changed_value = "multiplier", 5

    def make_native(self):
        from netbox_routing.models import BFDInterface, BFDProfile

        profile = BFDProfile.objects.create(
            name=f"example-{self.device.pk}", min_tx_int=300, min_rx_int=300, multiplier=3
        )
        self.native = BFDInterface.objects.create(
            interface=self.interface, bfd_profile=profile, micro_bfd=True, enabled=True
        )


class TestLacpDifferences(_ScopeDifferences, TestCase):
    scope = "lacp"
    family = "lag_config"
    collection = "bundles"
    changed_attribute, changed_value = "min_links", 3

    def make_native(self):
        self.native = Interface.objects.create(device=self.device, name="Port-channel1", type="lag")
        self.interface.lag = self.native
        self.interface.save()
        NSOLACPBundleState.objects.create(
            management=self.management, interface=self.native, lag_id=1, min_links=2, system_priority=100, timer="fast"
        )
        NSOLACPMemberState.objects.create(
            management=self.management, interface=self.interface, mode="active", port_priority=200
        )

    def test_membership_uses_native_topology(self):
        self.interface.lag = None
        self.interface.save()
        self.publish()
        self.assertIn("device_only", [row.kind for row in self.rows()])

    def test_config_snapshot_is_sufficient(self):
        self.publish()
        NSOFamilyObservation.objects.filter(read_state__family="lag").delete()
        self.assertEqual(self.rows(), [])

    def test_null_member_mode_uses_reconciler_normalization(self):
        NSOLACPMemberState.objects.filter(interface=self.interface).update(mode="")
        self.document["bundles"][0]["member"][0]["mode"] = None
        self.publish()
        self.assertEqual(self.rows(), [])

    def test_omitted_member_mode_stays_missing(self):
        member = self.document["bundles"][0]["member"][0]
        member.pop("mode")
        member["present"].remove("mode")
        self.publish()
        self.assertEqual([(row.kind, row.attribute) for row in self.rows()], [("mismatch", "mode")])

    def test_partial_membership_is_unavailable(self):
        self.document["bundles"][0].pop("member")
        self.document["bundles"][0]["present"].remove("member")
        self.publish()
        self.assertEqual([(row.kind, row.attribute) for row in self.rows()], [("unavailable", "member")])


class TestL2SapDifferences(_ScopeDifferences, TestCase):
    scope = "l2_sap"
    family = "l2_service"
    collection = "services"
    changed_attribute, changed_value = "outer_tag", 200

    def make_native(self):
        self.native = NSOL2SapState.objects.create(
            management=self.management,
            service_name="example-service",
            service_type="epipe",
            service_id=100,
            sap_id="Ethernet1:100",
            port="Ethernet1",
            outer_tag=100,
        )

    def changed_entry(self):
        return self.document["services"][0]["saps"][0]

    def test_partial_sap_coverage_cannot_claim_absence(self):
        self.document["services"][0].pop("saps")
        self.document["services"][0]["present"].remove("saps")
        self.publish()
        self.assertEqual([(row.kind, row.attribute) for row in self.rows()], [("unavailable", "saps")])

    def test_unknown_service_type_is_ambiguous(self):
        self.document["services"][0]["service_type"] = "unsupported"
        self.publish()
        self.assertIn("ambiguous", [row.kind for row in self.rows()])
        self.assertNotIn("mismatch", [row.kind for row in self.rows()])


class TestLoggingDifferences(_ScopeDifferences, TestCase):
    scope = family = "logging"
    collection = "hosts"
    changed_attribute, changed_value = "severity", "debugging"

    def make_native(self):
        self.native = NSOLoggingHostState.objects.create(
            management=self.management,
            address="198.18.0.1",
            port=514,
            severity="errors",
            facility="local6",
            transport="udp",
        )
        NSOLoggingLevelState.objects.create(
            management=self.management, console_severity="CRITICAL", monitor_severity="NOTICE", module_severity="NOTICE"
        )

    def test_omitted_default_port_uses_timos_equivalence(self):
        self.use_ned("timos-test")
        host = self.document["hosts"][0]
        host.pop("port")
        host["present"].remove("port")
        self.publish()
        self.assertFalse([row for row in self.rows() if row.attribute == "port"])
        self.native.port = 1514
        self.native.save()
        self.assertIn(("mismatch", "port"), [(row.kind, row.attribute) for row in self.rows()])


class TestSnmpDifferences(_ScopeDifferences, TestCase):
    scope = family = "snmp"
    collection = "communities"
    changed_attribute, changed_value = "access", "RW"

    def make_native(self):
        self.native = NSOSnmpCommunityState.objects.create(
            management=self.management,
            community_hash="abc123def456abcd",
            vault_secret_hash="abc123def456abcd",
            access="RO",
            acl="20",
        )
        NSOSnmpV3UserState.objects.create(management=self.management, username="placeholder-user", has_auth_secret=True)
        NSOSnmpHostState.objects.create(
            management=self.management,
            address="198.18.0.2",
            version="v3",
            notify_type="inform",
            port=162,
            username="placeholder-user",
        )
        NSOSnmpSystemInfoState.objects.create(management=self.management, location="example-lab", contact="example")

    def test_omitted_default_port_uses_timos_equivalence(self):
        self.use_ned("timos-test")
        host = self.document["hosts"][0]
        host.pop("port")
        host["present"].remove("port")
        self.publish()
        self.assertFalse([row for row in self.rows() if row.attribute == "port"])
        native = NSOSnmpHostState.objects.get(management=self.management)
        native.port = 1162
        native.save()
        self.assertIn(("mismatch", "port"), [(row.kind, row.attribute) for row in self.rows()])

    def test_secret_fingerprint_mismatch_is_visible(self):
        self.native.vault_secret_hash = "def456abc123def4"
        self.native.save()
        self.publish()
        self.assertIn(("mismatch", "secret"), [(row.kind, row.attribute) for row in self.rows()])

    def test_missing_secret_fingerprint_is_unavailable(self):
        self.native.vault_secret_hash = ""
        self.native.save()
        self.publish()
        self.assertIn(("unavailable", "secret"), [(row.kind, row.attribute) for row in self.rows()])
        self.assertFalse([row for row in self.rows() if row.attribute == "secret" and row.kind == "mismatch"])


class TestStaticRouteDifferences(_ScopeDifferences, TestCase):
    scope = family = "static_route"
    collection = "routes"
    changed_attribute, changed_value = "metric", 4

    def make_native(self):
        from netbox_routing.models import StaticRoute

        self.native = StaticRoute.objects.create(prefix="198.18.0.0/24", next_hop="198.18.1.1", metric=3, name="")
        self.native.devices.add(self.device)

    def test_present_null_permanence_matches_native_false(self):
        self.native.permanent = False
        self.native.save()
        self.changed_entry()["permanent"] = None
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])

    def test_present_null_permanence_differs_from_native_true(self):
        self.native.permanent = True
        self.native.save()
        self.changed_entry()["permanent"] = None
        self.publish()
        rows = [row for row in self.rows() if row.attribute == "permanent"]
        self.assertEqual([(row.kind, row.attribute) for row in rows], [("mismatch", "permanent")])
        self.assertIs(rows[0].device_value, False)

    def test_omitted_permanence_stays_missing(self):
        from netbox_nso_plugin.device_differences import MISSING

        self.native.permanent = False
        self.native.save()
        self.changed_entry()["present"].remove("permanent")
        for value in (None, False, MISSING):
            with self.subTest(value=value):
                if value is MISSING:
                    self.changed_entry().pop("permanent")
                else:
                    self.changed_entry()["permanent"] = value
                self.publish()
                rows = [row for row in self.rows() if row.attribute == "permanent"]
                self.assertEqual([(row.kind, row.attribute) for row in rows], [("mismatch", "permanent")])
                self.assertIs(rows[0].device_value, MISSING)

    def test_invalid_prefix_is_ambiguous(self):
        self.changed_entry()["prefix"] = "invalid"
        self.publish()
        self.assertIn("ambiguous", [row.kind for row in self.rows()])

    def test_interface_next_hop_uses_the_broader_binding(self):
        self.native.next_hop = None
        self.native.interface_next_hop = "Ethernet1"
        self.native.save()
        self.changed_entry()["next_hop"] = None
        self.changed_entry()["interface_next_hop"] = "Ethernet1"
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])

    def test_empty_ip_next_hop_uses_the_reconciler_identity(self):
        self.native.next_hop = None
        self.native.interface_next_hop = "Ethernet1"
        self.native.save()
        self.changed_entry()["next_hop"] = ""
        self.changed_entry()["interface_next_hop"] = "Ethernet1"
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])


class TestComparisonQueryCount(TestCase):
    def setUp(self):
        self.device, self.management = _make(f"queries{uuid4().hex[:8]}", manage_interfaces=True)
        self.user = get_user_model().objects.create_superuser(username=f"queries{uuid4().hex[:8]}")
        self.bundle = Interface.objects.create(device=self.device, name="Port-channel1", type="lag")
        NSOLACPBundleState.objects.create(management=self.management, interface=self.bundle, lag_id=1)
        self.vlan = VLAN.objects.create(vid=10, name="example-query-vlan")
        self.add_interfaces(1, 1)
        self.snapshots = {}
        for family, collection in (
            ("lag_config", "bundles"),
            ("switchport", "interfaces"),
            ("interface_mtu", "interfaces"),
        ):
            from types import SimpleNamespace

            document = copy.deepcopy(DOCUMENTS[family])
            document[collection] = []
            self.snapshots[family] = SimpleNamespace(document=document, coverage=scope_observation(family)["coverage"])

    def add_interfaces(self, start, count):
        for index in range(start, start + count):
            interface = Interface.objects.create(
                device=self.device,
                name=f"Ethernet{index + 10}",
                type="1000base-t",
                mtu=1500,
                lag=self.bundle,
                mode="tagged",
                untagged_vlan=self.vlan,
            )
            interface.tagged_vlans.add(self.vlan)
            NSOInterfaceMtuState.objects.create(management=self.management, interface=interface, ip_mtu=1500)
            NSOLACPMemberState.objects.create(management=self.management, interface=interface, mode="active")

    def test_query_count_stays_flat_when_native_rows_grow(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def render():
            return differences(self.management, user=self.user, snapshots=self.snapshots)

        render()
        with CaptureQueriesContext(connection) as queries:
            small = render()
        baseline = len(queries)
        self.add_interfaces(2, 10)
        with self.assertNumQueries(baseline):
            large = render()
        self.assertGreater(len(large), len(small))
