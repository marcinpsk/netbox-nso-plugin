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
                    {"interface_name": "ge-0/0/10", "min_tx": 510, "min_rx": 520, "multiplier": 4, "micro_bfd": False},
                    {"interface_name": "ge-0/0/11", "min_tx": 710, "min_rx": 720, "multiplier": 5, "micro_bfd": False},
                ]
            }
        }

    def test_refresh_creates_no_intent_row_and_keeps_observed_rows(self):
        from netbox_routing.models import BFDInterface, BFDProfile

        _refresh(self, operator(), self.device, "bfd")
        self.assertFalse(BFDInterface.objects.filter(interface=self.owned_port).exists())
        self.assertFalse(BFDProfile.objects.filter(name__in=("bfd-310-320-x3", "bfd-510-520-x4")).exists())
        observed = BFDInterface.objects.get(interface=self.observed_port)
        self.assertEqual(observed.bfd_profile.name, "bfd-710-720-x5")

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
    """Onboarding creates the management row: the user needs add on that prospective row (NetBox semantics)."""

    @classmethod
    def setUpTestData(cls):
        from netbox_nso_plugin.models import NSOInstance

        from ._outbox_case import make_device

        cls.device = make_device("nw-onboard", 1)
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
        self.assertFalse(NSOProvisionTombstone.objects.filter(netbox_device_id=self.device.pk).exists())

    def test_ui_onboard_outside_the_add_constraint_is_refused_before_the_adapter(self):
        self.client.force_login(self._user({"nso_device_name": "nw-only-this-name"}))
        with self.assert_refused_whole():
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:onboard"),
                {"device": self.device.pk, "instance": self.instance.adapter_instance_id, "ned_id": "test-ned"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertNothingCreated()

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
