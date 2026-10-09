# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Publish switching, service and routing observations through every plugin reconcile seam."""

from uuid import uuid4

from django.test import TestCase

from netbox_nso_plugin.adapter_client import AdapterError, bound_session
from netbox_nso_plugin.models import NSOFamilyObservation
from netbox_nso_plugin.read_gate import gated_family_run
from netbox_nso_plugin.reconcile import reconcile_category, reconcile_device

from ._observation_case import ObservationTransport
from ._routing_observation_case import DOCUMENTS as ROUTING_DOCUMENTS
from ._routing_observation_case import routing_observation
from ._scope_observation_case import DOCUMENTS, scope_observation
from .test_gated_reconcile import _make
from .test_read_gate import _rs

DOCUMENTS = {**DOCUMENTS, **ROUTING_DOCUMENTS}


def family_observation(family, **kwargs):
    if family in ROUTING_DOCUMENTS:
        return routing_observation(family, **kwargs)
    return scope_observation(family, **kwargs)


FAMILY_ENDPOINTS = {
    "interface_attributes": ("interfaces-doc", "interfaces", "interfaces"),
    "interface_ip": ("interface-ips", "interfaces", "interface_ips"),
    "lag_config": ("lag-config", "bundles", "lacp"),
    "vlan": ("vlan-database", "vlans", "vlan"),
    "switchport": ("switchport", "interfaces", "switchport"),
    "interface_mtu": ("interface-mtu", "interfaces", "interface_mtu"),
    "svi": ("svi", "interfaces", "svi"),
    "subinterface": ("subinterface", "interfaces", "subinterface"),
    "bfd": ("bfd", "interfaces", "bfd"),
    "l2_service": ("l2-services", "services", "l2_services"),
    "logging": ("logging-config", "hosts", "logging"),
    "snmp": ("snmp-config", "communities", "snmp"),
    "static_route": ("static-routes", "routes", "static"),
    "bgp": ("bgp-config", "routers", "bgp"),
    "isis": ("isis-interfaces", "processes", "isis"),
    "ospf": ("ospf", "instances", "ospf"),
    "route_policy": ("route-policy", "prefix_lists", "route_policy"),
    "redistribution": ("redistribution", "entries", "redistribution"),
}


class TestScopeObservationPublication(TestCase):
    def setUp(self):
        self.device, self.management = _make(
            f"pub{uuid4().hex[:8]}",
            manage_interfaces=True,
            manage_snmp=True,
            manage_logging=True,
            manage_l2=True,
            manage_routing=True,
            manage_static=True,
            manage_bgp=True,
            manage_isis=True,
            manage_ospf=True,
            manage_route_policy=True,
            manage_redistribution=True,
        )
        self.transport = ObservationTransport(
            self.management.adapter_device_id,
            scope_observation("interface_attributes"),
            scope_observation("interface_ip"),
        )
        for family, (endpoint, collection, _category) in FAMILY_ENDPOINTS.items():
            self.transport.documents[endpoint] = {
                collection: [],
                "observation": family_observation(family),
                "read_state": _rs(),
            }
        self.transport.documents["isis-interfaces"]["interfaces"] = []
        self.transport.documents["ospf"]["interfaces"] = []
        self.transport.documents["route-policy"].update(community_lists=[], as_paths=[], route_maps=[])

    def assert_snapshots(self, families):
        for family in families:
            with self.subTest(family=family):
                snapshot = NSOFamilyObservation.objects.get(
                    read_state__management=self.management, read_state__family=family
                )
                self.assertEqual(snapshot.document, DOCUMENTS[family])
                self.assertEqual(snapshot.revision, snapshot.read_state.applied_payload_revision)
                self.assertEqual(snapshot.source_epoch, snapshot.read_state.applied_source_epoch)

    def test_category_reconciliation_copies_all_scope_observations(self):
        with bound_session(self.transport.session()):
            for category in sorted({row[2] for row in FAMILY_ENDPOINTS.values()}):
                reconcile_category(self.device, self.management, category)
        self.assert_snapshots(FAMILY_ENDPOINTS)
        self.assertFalse(any(request.url.endswith("/lag-topology") for request in self.transport.requests))

    def test_full_device_reconciliation_copies_all_scope_observations(self):
        with bound_session(self.transport.session()):
            reconcile_device(self.device, self.management)
        self.assert_snapshots(FAMILY_ENDPOINTS)
        self.assertTrue(any(request.url.endswith("/bgp-config") for request in self.transport.requests))
        self.assertFalse(any(request.url.endswith("/lag-topology") for request in self.transport.requests))

    def test_failed_family_publication_retains_each_previous_document(self):
        def fail():
            raise RuntimeError("body failed")

        for family in DOCUMENTS:
            with self.subTest(family=family):
                gated_family_run(
                    self.management,
                    family,
                    _rs(),
                    lambda: None,
                    epoch=self.management.adapter_device_id,
                    observation=family_observation(family),
                )
                with self.assertRaisesRegex(RuntimeError, "body failed"):
                    gated_family_run(
                        self.management,
                        family,
                        _rs(attempt_id=2),
                        fail,
                        epoch=self.management.adapter_device_id,
                        observation=family_observation(family, revision=2),
                    )
        self.assert_snapshots(DOCUMENTS)

    def test_missing_observation_fails_closed_for_each_family(self):
        for family in DOCUMENTS:
            with self.subTest(family=family), self.assertRaises(AdapterError):
                gated_family_run(
                    self.management,
                    family,
                    _rs(),
                    lambda: None,
                    epoch=self.management.adapter_device_id,
                )
        self.assertFalse(NSOFamilyObservation.objects.exists())
