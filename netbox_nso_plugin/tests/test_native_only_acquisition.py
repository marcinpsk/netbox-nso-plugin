# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Passive reads and delivery never acquire NetBox-only native rows."""

from copy import deepcopy
from functools import partial

from dcim.models import Device, Interface, Platform
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase
from django.urls import reverse
from ipam.models import ASN, RIR, VLAN, IPAddress, VLANGroup
from netbox_routing.models import (
    BGPAddressFamily,
    BGPPeer,
    BGPRouter,
    BGPScope,
    ISISFlexAlgo,
    ISISInstance,
    ISISInterface,
    OSPFArea,
    OSPFInstance,
    OSPFInterface,
    Redistribution,
    StaticRoute,
)

from netbox_nso_plugin import status_machine as sm
from netbox_nso_plugin.drain import NOTHING, SUCCEEDED, drain_key
from netbox_nso_plugin.models import (
    AdapterConnection,
    NSODeviceManagement,
    NSOInterfaceIPState,
    NSOInterfaceState,
    NSOOwnershipManifest,
    NSOPlatformNedMapping,
)
from netbox_nso_plugin.ownership_planner import converted_scope_rules
from netbox_nso_plugin.reconcile import run_device_reconcile
from netbox_nso_plugin.renderer_audit import audit_renderer_fleet

from ._adapter_http import make_response
from ._outbox_case import make_managed, reset_renderer_audit_rotation, without_commit_drain
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin
from .test_apply_selector import _ApplyContractAdapter, _no_op


class NativeOnlyAdapter(_ApplyContractAdapter):
    """Serve observations and record intent through the real HTTP client."""

    def __init__(self):
        super().__init__(lambda selected: (200, {**_no_op(selected), "device_id": self.device_id}))
        self.device_id = 17580
        self.documents = {
            "interfaces-doc": {"interfaces": []},
            "state": {},
            "svi": {"interfaces": []},
            "subinterface": {"interfaces": []},
            "interface-mtu": {"interfaces": []},
            "interface-ips": {"interfaces": []},
            "lag-config": {"bundles": []},
            "vlan-database": {"vlans": []},
            "switchport": {"interfaces": []},
            "static-routes": {"routes": []},
            "isis-interfaces": {"processes": [], "interfaces": []},
            "ospf": {"instances": [], "interfaces": []},
            "bgp-config": {"routers": []},
            "route-policy": {"objects": []},
            "redistribution": {"entries": []},
            "bfd": {"interfaces": []},
            "snmp-config": {"communities": [], "v3_users": [], "hosts": []},
            "logging-config": {"hosts": []},
            "capability": {"known": True, "ned_id": "juniper-junos-nc"},
        }
        self.reads = []
        self.scope = {
            "attributes": ["description", "enabled"],
            "auto_apply": False,
            "sync_before_apply": True,
        }
        self.primary_ip = None

    def _handle(self, method, url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "scope":
            if method == "PUT":
                self.scope = deepcopy(kwargs["json"])
                self.requests.append({"url": url, "body": deepcopy(kwargs["json"])})
            return make_response(200, self.scope)
        if method == "GET" and endpoint.isdigit() and "/devices/" in url:
            return make_response(200, {"failover": {"primary_ip": self.primary_ip, "oob_ip": None}})
        if method == "GET" and endpoint in self.documents:
            self.reads.append(endpoint)
            return make_response(200, self.documents[endpoint])
        if method == "GET" and endpoint == "jobs":
            response = make_response(200, [])
            response.headers["X-Store-Incarnation"] = "native-only-store"
            return response
        if method == "GET" and endpoint != "intent-receipts":
            raise AssertionError(f"Unserved adapter read: {url}")
        return super()._handle(method, url, **kwargs)


def native_interface(device, **values):
    return Interface.objects.create(device=device, name="ge-0/0/0", type="1000base-t", **values)


def native_vlan(device):
    group = VLANGroup.objects.create(name=f"Native {device.pk}", slug=f"nso-{device.pk}")
    return VLAN.objects.create(group=group, vid=101, name="native-vlan")


def native_switchport(device):
    return native_interface(device, mode="access", untagged_vlan=native_vlan(device))


def native_bundle(device):
    return Interface.objects.create(device=device, name="ae10", type="lag")


def native_member(device):
    return native_interface(device, lag=native_bundle(device))


def native_ip(device):
    return IPAddress.objects.create(address="198.18.0.101/24", assigned_object=native_interface(device))


def native_route(device):
    route = StaticRoute.objects.create(prefix="198.18.101.0/24", next_hop="198.18.0.1", metric=1)
    route.devices.add(device)
    return route


def native_isis(device):
    return ISISInstance.objects.create(device=device, process_tag="CORE", net="49.0001.0198.0180.0001.00")


def native_isis_interface(device):
    return ISISInterface.objects.create(
        instance=native_isis(device), interface=native_interface(device), address_family="ipv4", metric=17
    )


def native_flex_algo(device):
    return ISISFlexAlgo.objects.create(instance=native_isis(device), algo_id=128, metric_type="delay-metric")


def native_ospf(device):
    return OSPFInstance.objects.create(device=device, process_id="10", name="10", router_id="198.18.0.1")


def native_ospf_interface(device):
    area, _ = OSPFArea.objects.get_or_create(area_id="0.0.0.0", defaults={"area_type": "standard"})
    return OSPFInterface.objects.create(
        instance=native_ospf(device), interface=native_interface(device), area=area, cost=17
    )


def native_bgp_scope(device):
    rir, _ = RIR.objects.get_or_create(name="Native private", slug="native-private")
    asn, _ = ASN.objects.get_or_create(asn=64520, rir=rir)
    router = BGPRouter.objects.create(
        assigned_object_type=ContentType.objects.get_for_model(Device),
        assigned_object_id=device.pk,
        asn=asn,
        name="64520",
    )
    return BGPScope.objects.create(router=router)


def native_bgp_peer(device):
    scope = native_bgp_scope(device)
    remote_as, _ = ASN.objects.get_or_create(asn=64521, rir=scope.router.asn.rir)
    return BGPPeer.objects.create(
        scope=scope, peer=IPAddress.objects.create(address="198.18.0.2/32"), remote_as=remote_as, enabled=True
    )


def native_bgp_address_family(device):
    return BGPAddressFamily.objects.create(scope=native_bgp_scope(device), address_family="ipv4-unicast")


def native_redistribution(device, protocol):
    destination = {"bgp": native_bgp_address_family, "isis": native_isis, "ospf": native_ospf}[protocol](device)
    return Redistribution.objects.create(
        destination_type=ContentType.objects.get_for_model(type(destination)),
        destination_id=destination.pk,
        source_protocol="static",
    )


NATIVE_FACTORIES = {
    "netbox_nso_plugin.nsolacpbundlestate": ("lacp", (native_bundle,), {"name": "ae10"}),
    "netbox_nso_plugin.nsolacpmemberstate": ("lacp", (native_member,), {"interface_name": "ge-0/0/0"}),
    "netbox_nso_plugin.nsovlanstate": ("vlan", (native_vlan,), {"vlan_id": 101}),
    "netbox_nso_plugin.nsoswitchportstate": ("switchport", (native_switchport,), {"interface_name": "ge-0/0/0"}),
    "netbox_nso_plugin.nsointerfacemtustate": (
        "interface_mtu",
        (partial(native_interface, mtu=9216),),
        {"interface_name": "ge-0/0/0"},
    ),
    "netbox_nso_plugin.nsointerfacestate": (
        "interface",
        (partial(native_interface, description="native uplink", enabled=False),),
        {"interface": "ge-0/0/0"},
    ),
    "netbox_nso_plugin.nsointerfaceipstate": ("ip", (native_ip,), {"address": "198.18.0.101/24"}),
    "netbox_nso_plugin.nsostaticroutestate": ("static_route", (native_route,), {"prefix": "198.18.101.0/24"}),
    "netbox_nso_plugin.nsobgppeerstate": ("bgp", (native_bgp_peer,), {"peer_address": "198.18.0.2"}),
    "netbox_nso_plugin.nsoisisinstancestate": ("isis", (native_isis,), {"process_tag": "CORE"}),
    "netbox_nso_plugin.nsoisisinterfacestate": (
        "isis",
        (native_isis_interface,),
        {"process_tag": "CORE", "interface_name": "ge-0/0/0"},
    ),
    "netbox_nso_plugin.nsoisisflexalgostate": (
        "isis_flex_algo",
        (native_flex_algo,),
        {"process_tag": "CORE", "algo_id": 128},
    ),
    "netbox_nso_plugin.nsoospfinstancestate": ("ospf", (native_ospf,), {"process_id": "10"}),
    "netbox_nso_plugin.nsoospfinterfacestate": (
        "ospf",
        (native_ospf_interface,),
        {"process_id": "10", "area_id": "0.0.0.0", "interface_name": "ge-0/0/0"},
    ),
    "netbox_nso_plugin.nsoredistributionstate": (
        "redistribution",
        tuple(partial(native_redistribution, protocol=protocol) for protocol in ("bgp", "isis", "ospf")),
        {"source_protocol": "static"},
    ),
}


def intent_bodies(requests, apply_requests):
    for request in requests:
        if request["url"].endswith(("/intent", "-intent", "/lag-config/apply", "/switchport/apply")):
            yield request["body"]
    yield from apply_requests


def contains_wire_marker(body, marker):
    if isinstance(body, dict):
        return all(body.get(field) == value for field, value in marker.items()) or any(
            contains_wire_marker(value, marker) for value in body.values()
        )
    if isinstance(body, list):
        return any(contains_wire_marker(value, marker) for value in body)
    return False


class TestNativeWireMarkers(SimpleTestCase):
    def test_registration_and_scope_are_not_intent_bodies(self):
        requests = [
            {"url": "http://adapter/api/v1/devices", "body": {"netbox_device_id": 101}},
            {"url": "http://adapter/api/v1/devices/101/scope", "body": {"attributes": ["description"]}},
        ]
        self.assertEqual(list(intent_bodies(requests, [])), [])
        self.assertFalse(contains_wire_marker({"selected": {"vlan": 101}}, {"vlan_id": 101}))

    def test_native_markers_are_found_in_nested_intent_and_apply_bodies(self):
        for label, (_scope, _factories, marker) in NATIVE_FACTORIES.items():
            for endpoint in ("intent", "ip-intent", "isis-interface-intent", "lag-config/apply", "switchport/apply"):
                with self.subTest(overlay=label, endpoint=endpoint):
                    body = {"items": [{"nested": [dict(marker, accepted_at=None)]}]}
                    requests = [{"url": f"http://adapter/api/v1/devices/101/{endpoint}", "body": body}]
                    self.assertEqual(list(intent_bodies(requests, [body])), [body, body])
                    for candidate in intent_bodies(requests, [body]):
                        self.assertTrue(contains_wire_marker(candidate, marker))
                    self.assertFalse(contains_wire_marker({"items": []}, marker))


class TestNativeOnlyAcquisition(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.adapter = NativeOnlyAdapter()
        for patcher in self.adapter.patches():
            patcher.start()
            self.addCleanup(patcher.stop)
        reset_renderer_audit_rotation(self)

    def make_scoped_device(self, tag):
        self.adapter.device_id += 1
        device, management = make_managed(tag, self.adapter.device_id)
        type(management).objects.filter(pk=management.pk).update(
            manage_interfaces=True,
            manage_description=True,
            manage_enabled=True,
            manage_routing=True,
            manage_static=True,
            manage_isis=True,
            manage_ospf=True,
            manage_bgp=True,
            manage_redistribution=True,
        )
        management.refresh_from_db()
        self.adapter.scope["attributes"] = ["description", "enabled"]
        return device, management

    def assert_reconcile_ran(self, device):
        self.adapter.reads.clear()
        with self.assertNoLogs("netbox_nso_plugin", level="ERROR"):
            result = run_device_reconcile(device.pk)
        self.assertNotIn("error", result, result)
        self.assertNotIn("skipped", result, result)
        self.assertNotIn("deferred", result, result)
        self.assertIn("interface-ips", self.adapter.reads)

    def assert_no_acquisition(self, device):
        for label in NATIVE_FACTORIES:
            model = apps.get_model(label)
            fields = {field.name for field in model._meta.fields}
            rows = model.objects.filter(
                **({"management__device": device} if "management" in fields else {"interface__device": device})
            )
            self.assertFalse(rows.filter(status__in=sm.OWNED_STATES).exists(), label)
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=device.pk).exists())

    def assert_cadence_ran(self):
        audit = audit_renderer_fleet()
        self.assertEqual((audit.failed, audit.deferred, audit.unknown), (0, 0, 0))
        self.assertEqual(audit.devices, NSODeviceManagement.objects.filter(adapter_device_id__isnull=False).count())
        self.assertGreater(audit.devices, 0)

    def assert_management_interface_unowned(self, device, me0):
        self.assertEqual(
            {
                "ip": list(NSOInterfaceIPState.objects.filter(interface=me0).values_list("address", "status")),
                "attributes": list(
                    NSOInterfaceState.objects.filter(interface=me0, status__in=sm.OWNED_STATES).values_list(
                        "attribute", "status"
                    )
                ),
                "ip_manifests": list(
                    NSOOwnershipManifest.objects.filter(device_id=device.pk, scope="ip", native_id=198).values_list(
                        "state_model_label", "ownership_state"
                    )
                ),
                "adapter_bodies": [request["url"] for request in self.adapter.requests if "me0" in str(request["body"])]
                + [body for body in self.adapter.apply_requests if "me0" in str(body)],
            },
            {"ip": [], "attributes": [], "ip_manifests": [], "adapter_bodies": []},
        )

    def test_unreported_management_interface_never_reaches_apply(self):
        with without_commit_drain(), transaction.atomic():
            device, management = self.make_scoped_device("native-only-management")
            type(management).objects.filter(pk=management.pk).update(
                manage_route_policy=True, manage_snmp=True, manage_logging=True
            )
            AdapterConnection.objects.create(interface_ip_auto_create=True)
            platform = Platform.objects.create(name="Native Junos", slug="native-junos")
            NSOPlatformNedMapping.objects.create(platform=platform, ned_id="juniper-junos-nc")
            device.platform = platform
            me0 = Interface.objects.create(device=device, name="me0", type="1000base-t", description="management")
            for name in ("lo0.0", "vme.0", "ae50.99", "ae99.99"):
                Interface.objects.create(device=device, name=name, type="virtual")
            address = IPAddress.objects.create(pk=198, address="198.18.0.101/24", assigned_object=me0)
            device.primary_ip4 = address
            device.save(update_fields=("platform", "primary_ip4"))
        self.adapter.primary_ip = "198.18.0.101"
        self.adapter.documents["interfaces-doc"] = {
            "interfaces": [{"name": name, "attrs": {}} for name in ("lo0.0", "vme.0", "ae50.99", "ae99.99")]
        }
        self.adapter.documents["interface-ips"] = {
            "interfaces": [
                {"interface": "lo0.0", "addresses": [{"address": "198.18.0.101/32"}]},
                {"interface": "vme.0", "addresses": [{"address": "198.18.0.102/24"}]},
            ]
        }
        user = get_user_model().objects.create_superuser(username="native-only-admin", password="test-password")
        self.client.force_login(user)

        self.assert_reconcile_ran(device)
        self.assertEqual(
            NSOInterfaceIPState.objects.get(interface__device=device, interface__name="lo0.0").status, "conflict"
        )
        self.assert_management_interface_unowned(device, me0)
        # The Apply runs before any cadence audit, so its pre-capture audit is the first ownership pass.
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.adapter.apply_requests)
        self.assertTrue(self.adapter.requests)
        self.assert_management_interface_unowned(device, me0)
        self.assert_cadence_ran()
        self.assertIn(drain_key(device.pk, "ip", force=True), (NOTHING, SUCCEEDED))
        self.assert_management_interface_unowned(device, me0)
        self.assert_no_acquisition(device)
        address.refresh_from_db()
        device.refresh_from_db()
        self.assertEqual(
            (str(address.address), address.assigned_object_id, device.primary_ip4_id),
            ("198.18.0.101/24", me0.pk, address.pk),
        )

    def test_native_factories_cover_every_native_binding_overlay(self):
        self.assertEqual(
            set(NATIVE_FACTORIES),
            {
                label
                for rule in converted_scope_rules().values()
                if rule.qualification == "native_binding"
                for label in rule.overlay_model_labels
            },
        )

    def test_native_only_rows_stay_unowned_through_reconcile_audit_and_drain(self):
        user = get_user_model().objects.create_superuser(username="native-matrix-admin", password="test-password")
        self.client.force_login(user)
        case = 0
        for label, (scope, factories, wire_marker) in NATIVE_FACTORIES.items():
            for index, factory in enumerate(factories):
                case += 1
                with self.subTest(overlay=label, destination=index):
                    self.adapter.requests.clear()
                    self.adapter.apply_requests.clear()
                    with without_commit_drain(), transaction.atomic():
                        device, management = self.make_scoped_device(f"native-{case}")
                        native = factory(device)
                    self.assert_reconcile_ran(device)
                    self.assert_no_acquisition(device)
                    self.assert_cadence_ran()
                    self.assert_no_acquisition(device)
                    delivery_scope = scope
                    if scope == "redistribution":
                        delivery_scope = {"bgpaddressfamily": "bgp", "isisinstance": "isis", "ospfinstance": "ospf"}[
                            native.destination._meta.model_name
                        ]
                    self.assertIn(drain_key(device.pk, delivery_scope, force=True), (NOTHING, SUCCEEDED))
                    self.assert_no_acquisition(device)
                    response = self.client.post(
                        reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(management.pk, "apply")),
                        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                    )
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertTrue(self.adapter.apply_requests)
                    self.assertTrue(self.adapter.requests)
                    for body in intent_bodies(self.adapter.requests, self.adapter.apply_requests):
                        self.assertFalse(contains_wire_marker(body, wire_marker), body)
                    for body in self.adapter.apply_requests:
                        self.assertEqual(set(body), {"apply_attempt_id", "selected"})
                        self.assertTrue(all(type(revision) is int for revision in body["selected"].values()), body)
                    self.assert_no_acquisition(device)

    def test_adapter_owned_status_never_acquires_new_or_existing_interface_attributes(self):
        for initial in (None, "imported", "changed", "conflict", "unknown", "error"):
            for reported in sm.OWNED_STATES:
                with self.subTest(initial=initial, reported=reported):
                    with without_commit_drain(), transaction.atomic():
                        device, _management = self.make_scoped_device(f"status-{initial}-{reported}")
                        interface = native_interface(device)
                        if initial is not None:
                            for attribute in ("description", "enabled"):
                                acquire_overlay(
                                    NSOInterfaceState, interface=interface, attribute=attribute, status=initial
                                )
                    self.adapter.documents["interfaces-doc"] = {
                        "interfaces": [
                            {
                                "name": interface.name,
                                "attrs": {
                                    attribute: {"nso_value": value, "status": reported}
                                    for attribute, value in (
                                        ("description", "observed description"),
                                        ("enabled", "true"),
                                    )
                                },
                            }
                        ]
                    }
                    self.assert_reconcile_ran(device)
                    rows = NSOInterfaceState.objects.filter(interface=interface)
                    self.assertEqual(rows.count(), 2)
                    self.assertEqual(set(rows.values_list("status", flat=True)), {"imported"})
                    self.assertFalse(rows.exclude(accepted_at=None).exists())
                    self.assert_no_acquisition(device)
