# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Native-write authorization through real requests (#1717 increment I2).

An operator request writes native NetBox rows only within the user's NetBox permissions: the
stored row before a change and every completed row before commit. A refusal is 403, rolls the
whole request back, schedules no delivery and never reaches the adapter. A refresh writes device
facts only; owned intent reaches native rows through the operator Accept. Onboarding needs add
on the prospective management row. Real ObjectPermission rows, real requests, the real ORM; only
the adapter transport is faked.
"""

from uuid import uuid4

from dcim.models import Device, Interface
from django.contrib.auth import get_user_model
from django.urls import reverse

from netbox_nso_plugin.models import (
    NSOBFDInterfaceState,
    NSODeviceManagement,
    NSOInterfaceMtuState,
    NSOInterfaceState,
    NSOISISInterfaceState,
    NSORedistributionState,
    NSOSwitchportState,
)

from ._authority_case import AdapterCase, FakeAdapter, grant, operator
from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay

User = get_user_model()


# ── Interface, MTU and switchport writers (A2, C1): end to end ──


class TestInterfaceWriters(AdapterCase):
    """The operator's NetBox permission on dcim.Interface gates every interface writer."""

    @classmethod
    def setUpTestData(cls):
        from ipam.models import VLAN

        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        cls.device, cls.management = make_managed("nw-iface", 17183)
        NSODeviceManagement.objects.filter(pk=cls.management.pk).update(manage_description=True, manage_enabled=True)
        cls.interface = Interface.objects.create(
            device=cls.device, name="ge-0/0/5", type="1000base-t", description="ok-1"
        )
        cls.port = Interface.objects.create(device=cls.device, name="ge-0/0/6", type="1000base-t")
        cls.mtu_port = Interface.objects.create(device=cls.device, name="ge-0/0/7", type="1000base-t")
        cls.vlan = VLAN.objects.create(group=_device_vlan_group(cls.device), vid=207, name="nw-207")
        with without_commit_drain():
            cls.description = acquire_overlay(
                NSOInterfaceState,
                interface=cls.interface,
                attribute="description",
                nso_value="device",
                status="changed",
            )
            cls.switchport = acquire_overlay(
                NSOSwitchportState,
                management=cls.management,
                interface=cls.port,
                mode="access",
                untagged_vlan=cls.vlan,
                status="imported",
            )
            cls.mtu = acquire_overlay(
                NSOInterfaceMtuState, management=cls.management, interface=cls.mtu_port, l2_mtu=9000, status="imported"
            )

    def _edit(self, user, value):
        self.client.force_login(user)
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_edit_field", kwargs={"pk": self.description.pk})
        return self.client.post(url, {"value": value}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def _set_description(self, value):
        Interface.objects.filter(pk=self.interface.pk).update(description=value)

    def assertUnchanged(self, description="ok-1", status="changed"):
        self.interface.refresh_from_db()
        self.description.refresh_from_db()
        self.assertEqual((self.interface.description, self.description.status), (description, status))

    def test_an_edit_without_the_native_permission_is_refused_whole(self):
        with self.assert_refused_whole():
            response = self._edit(operator(), "ok-2")
        self.assertEqual(response.status_code, 403)
        self.assertUnchanged()

    def test_an_edit_checks_the_stored_row(self):
        """C1: the user may change only interfaces whose description starts with ok; this one does not yet."""
        self._set_description("bad")
        user = operator((("change",), Interface, {"description__startswith": "ok"}))
        with self.assert_refused_whole():
            response = self._edit(user, "ok-new")
        self.assertEqual(response.status_code, 403)
        self.assertUnchanged(description="bad")

    def test_an_edit_checks_the_completed_row(self):
        user = operator((("change",), Interface, {"description__startswith": "ok"}))
        with self.assert_refused_whole():
            response = self._edit(user, "bad")
        self.assertEqual(response.status_code, 403)
        self.assertUnchanged()

    def test_an_edit_inside_the_restriction_succeeds(self):
        user = operator((("change",), Interface, {"description__startswith": "ok"}))
        response = self._edit(user, "ok-2")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertUnchanged(description="ok-2", status="accepted")

    def test_adopting_the_device_value_needs_the_native_permission(self):
        self.client.force_login(operator())
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept_device", kwargs={"pk": self.description.pk})
        with self.assert_refused_whole():
            response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        self.assertUnchanged()

    def test_a_switchport_accept_needs_the_native_permission(self):
        url = reverse("plugins:netbox_nso_plugin:switchport_accept", kwargs={"pk": self.switchport.pk})
        self.client.force_login(operator())
        with self.assert_refused_whole():
            response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        self.port.refresh_from_db()
        self.switchport.refresh_from_db()
        self.assertEqual((self.port.mode, self.port.untagged_vlan_id, self.switchport.status), (None, None, "imported"))

        self.client.force_login(operator((("change",), Interface, None)))
        self.assertEqual(self.client.post(url).status_code, 302)
        self.port.refresh_from_db()
        self.assertEqual((self.port.mode, self.port.untagged_vlan_id), ("access", self.vlan.pk))

    def test_an_mtu_accept_checks_the_completed_interface(self):
        """C1: the user may change interfaces only while their MTU stays at or below 1500."""
        url = reverse("plugins:netbox_nso_plugin:interface_mtu_accept", kwargs={"pk": self.mtu.pk})
        self.client.force_login(operator((("change",), Interface, {"mtu__lte": 1500})))
        Interface.objects.filter(pk=self.mtu_port.pk).update(mtu=1500)
        with self.assert_refused_whole():
            response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        self.mtu_port.refresh_from_db()
        self.mtu.refresh_from_db()
        self.assertEqual((self.mtu_port.mtu, self.mtu.status), (1500, "imported"))


# ── The three owned-intent conversions: refresh leaves the gap, Accept materializes it (C12/C14) ──


def _grid_row(test, user, device, key, state, section=None):
    """Return the persisted grid row of *state* as the NSO tab reloads it."""
    test.client.force_login(user)
    url = reverse("plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": device.pk, "key": key})
    payload = test.client.get(url, {"format": "json"}).json()
    rows = payload[section]["rows"] if section else payload["rows"]
    (row,) = [row for row in rows if row["pk"] == state.pk]
    return row


def _refresh(test, user, device, key):
    test.client.force_login(user)
    url = reverse("plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": device.pk, "key": key})
    response = test.client.get(url, {"refresh": "1"})
    test.assertEqual(response.status_code, 200)


class TestIsisConversion(AdapterCase):
    """Owned interface intends P; native P is absent; the device reports an unowned process P."""

    @classmethod
    def setUpTestData(cls):
        from netbox_routing.models import ISISInstance, ISISInterface

        cls.device, cls.management = make_managed("nw-isis", 17184)
        cls.interface = Interface.objects.create(device=cls.device, name="ge-0/0/8", type="1000base-t")
        cls.current = ISISInstance.objects.create(device=cls.device, process_tag="Q")
        cls.native = ISISInterface.objects.create(
            interface=cls.interface, address_family="ipv4", instance=cls.current, circuit_type="level-2-only"
        )
        with without_commit_drain():
            cls.overlay = acquire_overlay(
                NSOISISInterfaceState,
                management=cls.management,
                interface=cls.interface,
                af="ipv4",
                process_tag="P",
                circuit_type="level-2-only",
                isis_interface=cls.native,
                status="accepted",
            )
        cls.admin = User.objects.create_superuser(username=f"nw-isis-admin-{uuid4().hex[:6]}")

    def setUp(self):
        super().setUp()
        FakeAdapter.documents = {
            "/isis-interfaces": {
                "processes": [{"process_tag": "P", "net": "49.0001.0000.0000.0001.00"}],
                "interfaces": [
                    {"interface_name": "ge-0/0/8", "af": "ipv4", "process_tag": "P", "circuit_type": "level-2-only"}
                ],
            }
        }

    def _accept(self, user):
        self.client.force_login(user)
        url = reverse("plugins:netbox_nso_plugin:routing_accept_isis_interface", kwargs={"pk": self.overlay.pk})
        return self.client.post(url)

    def assertRefreshCreatesTheObservedProcessOnly(self, user):
        from netbox_routing.models import ISISInstance

        _refresh(self, user, self.device, "isis")
        self.assertTrue(ISISInstance.objects.filter(device=self.device, process_tag="P").exists())
        self.native.refresh_from_db()
        self.assertEqual(self.native.instance_id, self.current.pk)

    def test_refresh_creates_the_observed_process_and_leaves_the_owned_repoint(self):
        self.assertRefreshCreatesTheObservedProcessOnly(self.admin)

    def test_the_gap_stays_visible_and_offers_accept(self):
        """The device matches the intent, but the native interface is on Q: not in sync, Accept offered."""
        self.assertRefreshCreatesTheObservedProcessOnly(self.admin)
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.status, "accepted")
        row = _grid_row(self, self.admin, self.device, "isis", self.overlay, section="interfaces")
        self.assertTrue(row["native_gap"])
        self.assertEqual(
            row["accept_url"],
            reverse("plugins:netbox_nso_plugin:routing_accept_isis_interface", kwargs={"pk": self.overlay.pk}),
        )

    def test_refresh_by_a_user_without_routing_permissions_does_the_same(self):
        self.assertRefreshCreatesTheObservedProcessOnly(operator())

    def test_accept_repoints_with_the_native_permission_only(self):
        from netbox_routing.models import ISISInterface

        self.assertRefreshCreatesTheObservedProcessOnly(self.admin)
        with self.assert_refused_whole():
            self.assertEqual(self._accept(operator()).status_code, 403)
        self.native.refresh_from_db()
        self.assertEqual(self.native.instance_id, self.current.pk)

        self.assertEqual(self._accept(operator((("change",), ISISInterface, None))).status_code, 302)
        self.native.refresh_from_db()
        self.assertEqual(self.native.instance.process_tag, "P")

    def test_the_untagged_process_is_materialized_by_accept(self):
        """An empty process tag is the untagged process: the gap shows and Accept repoints to it."""
        from netbox_routing.models import ISISInstance, ISISInterface

        interface = Interface.objects.create(device=self.device, name="ge-0/0/9", type="1000base-t")
        native_row = ISISInterface.objects.create(
            interface=interface, address_family="ipv4", instance=self.current, circuit_type="level-2-only"
        )
        with without_commit_drain():
            overlay = acquire_overlay(
                NSOISISInterfaceState,
                management=self.management,
                interface=interface,
                af="ipv4",
                process_tag="",
                circuit_type="level-2-only",
                isis_interface=native_row,
                status="accepted",
            )
        self.assertTrue(_grid_row(self, self.admin, self.device, "isis", overlay, section="interfaces")["native_gap"])
        url = reverse("plugins:netbox_nso_plugin:routing_accept_isis_interface", kwargs={"pk": overlay.pk})
        self.client.force_login(operator())
        with self.assert_refused_whole():
            self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(operator((("change",), ISISInterface, None), (("add",), ISISInstance, None)))
        self.assertEqual(self.client.post(url).status_code, 302)
        native_row.refresh_from_db()
        self.assertEqual(native_row.instance.process_tag, "")


class TestBfdConversion(AdapterCase):
    """Owned BFD overlay without a native BFD interface; an unowned interface reports its own profile."""

    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("nw-bfd", 17185)
        cls.owned_port = Interface.objects.create(device=cls.device, name="ge-0/0/10", type="1000base-t")
        cls.observed_port = Interface.objects.create(device=cls.device, name="ge-0/0/11", type="1000base-t")
        with without_commit_drain():
            cls.overlay = acquire_overlay(
                NSOBFDInterfaceState,
                management=cls.management,
                interface=cls.owned_port,
                min_tx=310,
                min_rx=320,
                multiplier=3,
                micro_bfd=False,
                status="accepted",
            )
        cls.admin = User.objects.create_superuser(username=f"nw-bfd-admin-{uuid4().hex[:6]}")

    def setUp(self):
        super().setUp()
        FakeAdapter.documents = {
            "/bfd": {
                "interfaces": [
                    {"interface_name": "ge-0/0/10", "min_tx": 310, "min_rx": 320, "multiplier": 3, "micro_bfd": False},
                    {"interface_name": "ge-0/0/11", "min_tx": 710, "min_rx": 720, "multiplier": 5, "micro_bfd": False},
                ]
            }
        }

    def test_refresh_creates_no_intent_row_and_keeps_observed_rows(self):
        from netbox_routing.models import BFDInterface, BFDProfile

        _refresh(self, operator(), self.device, "bfd")
        self.assertFalse(BFDInterface.objects.filter(interface=self.owned_port).exists())
        self.assertFalse(BFDProfile.objects.filter(name="bfd-310-320-x3").exists())
        observed = BFDInterface.objects.get(interface=self.observed_port)
        self.assertEqual(observed.bfd_profile.name, "bfd-710-720-x5")
        # The device matches the intent, but the native row is missing: not in sync, Accept offered.
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.status, "accepted")
        row = _grid_row(self, self.admin, self.device, "bfd", self.overlay)
        self.assertTrue(row["native_gap"])
        self.assertEqual(
            row["accept_url"], reverse("plugins:netbox_nso_plugin:bfd_accept", kwargs={"pk": self.overlay.pk})
        )

    def test_a_deploying_native_gap_does_not_offer_accept(self):
        from netbox_nso_plugin.models import NSOApplyAttempt

        attempt = NSOApplyAttempt.objects.create(management=self.management, selected={"bfd": 1})
        NSOBFDInterfaceState.objects.filter(pk=self.overlay.pk).update(status="deploying", apply_attempt_id=attempt.pk)
        row = _grid_row(self, self.admin, self.device, "bfd", self.overlay)
        self.assertFalse(row["native_gap"])
        self.assertIsNone(row["accept_url"])
        self.overlay.refresh_from_db()
        self.assertEqual((self.overlay.status, self.overlay.apply_attempt_id), ("deploying", attempt.pk))

    def test_accept_creates_the_native_rows_with_the_native_permission_only(self):
        from netbox_routing.models import BFDInterface, BFDProfile

        url = reverse("plugins:netbox_nso_plugin:bfd_accept", kwargs={"pk": self.overlay.pk})
        self.client.force_login(operator())
        with self.assert_refused_whole():
            self.assertEqual(self.client.post(url).status_code, 403)
        self.assertFalse(BFDInterface.objects.filter(interface=self.owned_port).exists())

        self.client.force_login(operator((("add",), BFDInterface, None), (("add",), BFDProfile, None)))
        self.assertEqual(self.client.post(url).status_code, 302)
        native_row = BFDInterface.objects.get(interface=self.owned_port)
        self.assertEqual(
            (native_row.bfd_profile.name, native_row.micro_bfd, native_row.enabled), ("bfd-310-320-x3", False, True)
        )


class TestRedistributionConversion(AdapterCase):
    """Owned redistribution overlay whose native redistribution is missing."""

    @classmethod
    def setUpTestData(cls):
        from netbox_routing.models import OSPFInstance

        cls.device, cls.management = make_managed("nw-redist", 17186)
        cls.ospf = OSPFInstance.objects.create(device=cls.device, process_id="7", name="7", router_id="198.18.0.7")
        with without_commit_drain():
            cls.overlay = acquire_overlay(
                NSORedistributionState,
                management=cls.management,
                dest_protocol="ospf",
                dest_ref="7",
                source_protocol="static",
                source_ref="",
                metric=70,
                metric_type="type-1",
                status="accepted",
            )
        cls.admin = User.objects.create_superuser(username=f"nw-redist-admin-{uuid4().hex[:6]}")

    def setUp(self):
        super().setUp()
        FakeAdapter.documents = {
            "/redistribution": {
                "entries": [
                    {
                        "dest_protocol": "ospf",
                        "dest_ref": "7",
                        "source_protocol": "static",
                        "source_ref": "",
                        "metric": 30,
                    }
                ]
            }
        }

    def test_refresh_creates_nothing_and_accept_needs_the_native_permission(self):
        from netbox_routing.models import Redistribution

        _refresh(self, self.admin, self.device, "redistribution")
        self.assertFalse(Redistribution.objects.filter(destination_id=self.ospf.pk).exists())
        self.assertTrue(_grid_row(self, self.admin, self.device, "redistribution", self.overlay)["native_gap"])

        url = reverse("plugins:netbox_nso_plugin:routing_accept_redistribution", kwargs={"pk": self.overlay.pk})
        self.client.force_login(operator())
        with self.assert_refused_whole():
            self.assertEqual(self.client.post(url).status_code, 403)
        self.assertFalse(Redistribution.objects.filter(destination_id=self.ospf.pk).exists())

        self.client.force_login(operator((("add",), Redistribution, None)))
        self.assertEqual(self.client.post(url).status_code, 302)
        native_row = Redistribution.objects.get(destination_id=self.ospf.pk)
        self.assertEqual(
            (native_row.metric, native_row.metric_type, native_row.source_protocol), (70, "type-1", "static")
        )
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.redistribution_id, native_row.pk)

    def test_a_missing_destination_does_not_offer_native_gap_accept(self):
        with without_commit_drain():
            self.ospf.delete()
        row = _grid_row(self, self.admin, self.device, "redistribution", self.overlay)
        self.assertFalse(row["native_gap"])
        self.assertIsNone(row["accept_url"])

    def test_a_native_gap_needs_a_visible_route_map_before_accept(self):
        from netbox_routing.models import OSPFInstance, Redistribution, RouteMap

        from ._ownership_case import save_overlay_fixture

        self.overlay.route_map = "gap-route-map"
        save_overlay_fixture(self.overlay, update_fields=("route_map",))
        with self.subTest(route_map="missing"):
            row = _grid_row(self, self.admin, self.device, "redistribution", self.overlay)
            self.assertFalse(row["native_gap"])
            self.assertIsNone(row["accept_url"])
        route_map = RouteMap.objects.create(name=self.overlay.route_map)
        with self.subTest(route_map="hidden"):
            row = _grid_row(self, operator(), self.device, "redistribution", self.overlay)
            self.assertFalse(row["native_gap"])
            self.assertIsNone(row["accept_url"])
        user = operator((("view",), OSPFInstance, None), (("view",), RouteMap, None), (("add",), Redistribution, None))
        row = _grid_row(self, user, self.device, "redistribution", self.overlay)
        self.assertTrue(row["native_gap"])
        self.assertIsNotNone(row["accept_url"])
        self.assertEqual(self.client.post(row["accept_url"]).status_code, 302)
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.redistribution.route_map_id, route_map.pk)


# ── Adjacent holes closed in I2 ──


class TestRedistributionEditRouteMap(AdapterCase):
    """The inline redistribution edit resolves its route map through the view restriction."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import OSPFInstance, Redistribution, RouteMap

        cls.device, cls.management = make_managed("nw-rmap", 17187)
        ospf = OSPFInstance.objects.create(device=cls.device, process_id="8", name="8", router_id="198.18.0.8")
        cls.hidden = RouteMap.objects.create(name="nw-hidden-map")
        cls.native = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(OSPFInstance),
            destination_id=ospf.pk,
            source_protocol="static",
            source_ref="",
        )
        with without_commit_drain():
            cls.overlay = acquire_overlay(
                NSORedistributionState,
                management=cls.management,
                dest_protocol="ospf",
                dest_ref="8",
                source_protocol="static",
                source_ref="",
                redistribution=cls.native,
                status="imported",
            )

    def test_a_route_map_the_user_may_not_view_reads_as_missing(self):
        from netbox_routing.models import Redistribution, RouteMap

        user = operator((("change",), Redistribution, None), (("view",), RouteMap, {"name": "nw-other-map"}))
        self.client.force_login(user)
        url = reverse(
            "plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "redistribution", "pk": self.overlay.pk}
        )
        response = self.client.post(url, {"route_map": self.hidden.name})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("does not exist in NetBox", response.json()["errors"]["route_map"][0])
        self.native.refresh_from_db()
        self.assertIsNone(self.native.route_map_id)


class TestOnboardingAddConstraint(AdapterCase):
    """Onboarding creates the management row: the operator needs add on that row (NetBox semantics).

    The row is saved and checked against the add restriction before the adapter call; the
    adapter's job id is lifecycle evidence recorded on that same row afterwards.
    """

    @classmethod
    def setUpTestData(cls):
        from dcim.models import Platform
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInstance, NSOPlatformNedMapping
        from netbox_nso_plugin.onboarding import normalize_nso_device_name

        from ._outbox_case import make_device

        platform = Platform.objects.create(name="nw-onboard-os", slug="nw-onboard-os")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="cisco-ios-cli-6.114")
        cls.device = make_device("nw-onboard", 1)
        interface = Interface.objects.create(device=cls.device, name="mgmt0", type="virtual")
        address = IPAddress.objects.create(address="198.18.44.1/24", assigned_object=interface)
        Device.objects.filter(pk=cls.device.pk).update(platform=platform, primary_ip4=address)
        cls.device.refresh_from_db()
        cls.nso_name = normalize_nso_device_name(cls.device.name)
        cls.instance = NSOInstance.objects.create(name="nw-onboard-inst", adapter_instance_id="nw-onboard-inst")

    def _user(self, constraints):
        from netbox_nso_plugin.models import NSOInstance

        user = User.objects.create_user(username=f"nw-onboard-{uuid4().hex[:6]}")
        grant(user, ("view", "add"), NSODeviceManagement, constraints)
        grant(user, ("view",), Device)
        grant(user, ("view",), NSOInstance)
        return user

    def assertNothingCreated(self):
        from netbox_nso_plugin.models import NSOProvisionTombstone

        self.assertFalse(NSODeviceManagement.objects.filter(device=self.device).exists())
        self.assertFalse(NSOProvisionTombstone.objects.filter(netbox_device_id=self.device.pk, state="open").exists())

    def _onboard(self, user):
        self.client.force_login(user)
        return self.client.post(
            reverse("plugins:netbox_nso_plugin:onboard"),
            {"device": self.device.pk, "instance": self.instance.adapter_instance_id},
        )

    def test_ui_onboard_outside_the_add_constraint_is_refused_before_the_adapter(self):
        with self.assert_refused_whole():
            response = self._onboard(self._user({"nso_device_name": "nw-only-this-name"}))
        self.assertEqual(response.status_code, 403)
        self.assertNothingCreated()

    def test_ui_onboard_checks_the_persisted_row_after_the_adapter_receipt(self):
        """A constraint over the adapter's job id passes the probe; the persisted row is checked too."""
        from netbox_nso_plugin.models import NSOProvisionTombstone

        FakeAdapter.posts = {"/api/v1/devices/provision": {"job_id": "4418"}}
        response = self._onboard(self._user({"onboard_job_id": ""}))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(NSODeviceManagement.objects.filter(device=self.device).exists())
        # The admitted attempt keeps its adapter evidence; the sweep settles it without a row.
        attempt = NSOProvisionTombstone.objects.get(netbox_device_id=self.device.pk)
        self.assertEqual((attempt.state, attempt.adapter_job_id), ("open", "4418"))

    def test_ui_onboard_inside_the_add_constraint_keeps_the_authorized_row(self):
        FakeAdapter.posts = {"/api/v1/devices/provision": {"job_id": "4417"}}
        response = self._onboard(self._user({"nso_device_name": self.nso_name, "onboard_status": "provisioning"}))
        self.assertEqual(response.status_code, 302)
        (management,) = NSODeviceManagement.objects.filter(device=self.device)
        self.assertEqual((management.onboard_status, management.onboard_job_id), ("provisioning", "4417"))

    def test_quick_manage_outside_the_add_constraint_is_refused(self):
        self.client.force_login(self._user({"nso_device_name": "nw-only-this-name"}))
        with self.assert_refused_whole():
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:quick_manage"),
                {"device": self.device.pk, "instance": self.instance.adapter_instance_id, "nso_name": "nw-other"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertNothingCreated()

    def test_quick_manage_inside_the_add_constraint_creates_the_row(self):
        self.client.force_login(self._user({"nso_device_name": "nw-this-name"}))
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:quick_manage"),
            {"device": self.device.pk, "instance": self.instance.adapter_instance_id, "nso_name": "nw-this-name"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(NSODeviceManagement.objects.filter(device=self.device, nso_device_name="nw-this-name").exists())

    def _api_post(self, user):
        from rest_framework.test import APIClient
        from users.constants import TOKEN_PREFIX
        from users.models import Token

        token = Token.objects.create(user=user)
        return APIClient().post(
            reverse("plugins-api:netbox_nso_plugin-api:onboard"),
            {"netbox_device_id": self.device.pk, "instance": self.instance.adapter_instance_id},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}",
        )

    def test_api_onboard_needs_add_not_change(self):
        from netbox_nso_plugin.models import NSOInstance

        user = User.objects.create_user(username=f"nw-api-{uuid4().hex[:6]}")
        grant(user, ("view", "change"), NSODeviceManagement)
        grant(user, ("view",), Device)
        grant(user, ("view",), NSOInstance)
        with self.assert_refused_whole():
            self.assertEqual(self._api_post(user).status_code, 403)
        self.assertNothingCreated()

    def test_api_onboard_outside_the_add_constraint_is_refused_before_the_adapter(self):
        user = self._user({"nso_device_name": "nw-only-this-name"})
        grant(user, ("change",), NSODeviceManagement)
        with self.assert_refused_whole():
            self.assertEqual(self._api_post(user).status_code, 403)
        self.assertNothingCreated()


class TestInterfaceIpPrefixRefresh(AdapterCase):
    """A refresh with IP auto-create creates the covering Prefix through the system writer."""

    @classmethod
    def setUpTestData(cls):
        from ipam.models import Prefix

        cls.device, cls.management = make_managed("nw-prefix", 17188)
        Interface.objects.create(device=cls.device, name="Loopback1", type="virtual")
        # The new /24 re-parents this /26: NetBox recomputes the depth cache inside the permitted save.
        cls.child = Prefix.objects.create(prefix="198.18.61.0/26")
        cls.admin = User.objects.create_superuser(username=f"nw-prefix-admin-{uuid4().hex[:6]}")

    def test_refresh_creates_the_missing_prefix(self):
        from unittest.mock import patch

        from django.apps import apps
        from ipam.models import IPAddress, Prefix

        address = {"address": "198.18.61.200/24", "vrf": "", "family": "ipv4", "secondary": False}
        FakeAdapter.documents = {
            "/svi": {"interfaces": []},
            "/subinterface": {"interfaces": []},
            "/interface-ips": {
                "device_id": self.device.pk,
                "interfaces": [{"interface": "Loopback1", "addresses": [address]}],
            },
        }
        with patch.object(apps.get_app_config("netbox_nso_plugin"), "_interface_ip_auto_create", True):
            _refresh(self, self.admin, self.device, "interface_ips")
        self.assertTrue(IPAddress.objects.filter(address="198.18.61.200/24").exists())
        self.assertTrue(Prefix.objects.filter(prefix="198.18.61.0/24").exists())
        self.child.refresh_from_db()
        self.assertEqual(self.child._depth, 1)
