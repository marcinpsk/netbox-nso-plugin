# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Keep unknown interface collections out of comparison and reconciliation."""

import hashlib
import json
from uuid import uuid4

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from ipam.models import IPAddress

from netbox_nso_plugin.adapter_client import bound_session, reset_interfaces_doc_capability
from netbox_nso_plugin.models import NSOFamilyObservation, NSOFamilyReadState
from netbox_nso_plugin.observations import observation_defaults
from netbox_nso_plugin.reconcile import reconcile_category

from ._observation_case import ObservationTransport, interface_observation, ip_observation, observation
from .test_gated_reconcile import _make
from .test_read_gate import _rs


def _digest(snapshot):
    snapshot["digest"] = hashlib.sha256(
        json.dumps(snapshot["document"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return snapshot


class TestInterfaceCollectionComparison(TestCase):
    def setUp(self):
        self.device, self.management = _make(f"coverage{uuid4().hex[:8]}", manage_description=True)
        self.interface = Interface.objects.get(device=self.device, name="lag-60")
        self.address = IPAddress.objects.create(address="198.18.0.1/24", assigned_object=self.interface)
        self.user = get_user_model().objects.create_superuser(username=f"coverage{uuid4().hex[:8]}")
        self.client.force_login(self.user)
        self.url = reverse("plugins:netbox_nso_plugin:device_nso_differences", kwargs={"pk": self.device.pk})

    def _page(self, family, snapshot):
        state, _ = NSOFamilyReadState.objects.get_or_create(management=self.management, family=family)
        NSOFamilyObservation.objects.update_or_create(
            read_state=state, defaults=observation_defaults(family, 1, 1, _digest(snapshot))
        )
        response = self.client.get(self.url, {"scope": "ip" if family == "interface_ip" else "interface"})
        self.assertEqual(response.status_code, 200)
        return response, response.context["page_obj"].object_list

    def test_covered_empty_addresses_offer_native_address_difference(self):
        entry = ip_observation("lag-60")
        entry["addresses"] = []
        _, rows = self._page("interface_ip", observation("interface_ip", interfaces=[entry]))
        self.assertEqual([(row["kind"], row["identity"]) for row in rows], [("netbox_only", "lag-60 198.18.0.1/24")])

    def test_uncovered_addresses_report_gap_without_removal_candidate(self):
        for addresses, present in ((None, ["addresses"]), ([], []), (None, [])):
            with self.subTest(addresses=addresses, present=present):
                entry = {"interface": "lag-60", "bound_port": None, "addresses": addresses, "present": present}
                response, rows = self._page("interface_ip", observation("interface_ip", interfaces=[entry]))
                self.assertEqual([row["kind"] for row in rows], ["unavailable"])
                self.assertContains(response, "addresses")
                self.assertNotContains(response, "198.18.0.1/24")

    def test_uncovered_interfaces_report_component_gap(self):
        for family in ("interface_ip", "interface_attributes"):
            for interfaces, present in ((None, ["interfaces"]), ([], []), (None, [])):
                with self.subTest(family=family, interfaces=interfaces, present=present):
                    snapshot = observation(family)
                    snapshot["document"].update(interfaces=interfaces, present=present)
                    response, rows = self._page(family, snapshot)
                    self.assertEqual([row["kind"] for row in rows], ["unavailable"])
                    self.assertContains(response, "interfaces")
                    self.assertNotContains(response, "198.18.0.1/24")

    def test_address_gap_uses_bound_port_and_keeps_covered_interfaces_comparable(self):
        other = Interface.objects.create(device=self.device, name="Ethernet1", type="virtual")
        IPAddress.objects.create(address="198.18.0.2/24", assigned_object=other)
        entry = {"interface": "logical-port", "bound_port": "lag-60", "addresses": None, "present": []}
        _, rows = self._page("interface_ip", observation("interface_ip", interfaces=[entry]))
        self.assertEqual(sorted(row["kind"] for row in rows), ["netbox_only", "unavailable"])
        self.assertEqual(
            next(row["identity"] for row in rows if row["kind"] == "netbox_only"), "Ethernet1 198.18.0.2/24"
        )


class TestCollectionReadGate(TestCase):
    def setUp(self):
        self.device, self.management = _make(
            f"readcoverage{uuid4().hex[:8]}", manage_interfaces=True, manage_description=True, manage_l2=True
        )
        reset_interfaces_doc_capability()
        self.addCleanup(reset_interfaces_doc_capability)
        self.transport = ObservationTransport(
            self.management.adapter_device_id, observation("interface_attributes"), observation("interface_ip")
        )

    def test_not_covered_interface_refresh_keeps_overlay_native_and_observation(self):
        from netbox_nso_plugin.models import NSOInterfaceState

        doc = self.transport.documents["interfaces-doc"]
        doc["observation"] = observation(
            "interface_attributes", interfaces=[interface_observation("lag-60", "certified")]
        )
        doc["interfaces"] = [
            {"name": "lag-60", "attrs": {"description": {"nso_value": "certified", "status": "imported"}}}
        ]
        with bound_session(self.transport.session()):
            reconcile_category(self.device, self.management, "interfaces")
            before = list(NSOInterfaceState.objects.filter(interface__device=self.device).values())
            self.assertTrue(before)
            native = list(Interface.objects.filter(device=self.device).values())
            snapshot = NSOFamilyObservation.objects.get(
                read_state__management=self.management, read_state__family="interface_attributes"
            )
            certified = (snapshot.document, snapshot.digest, snapshot.observed_at)
            doc["interfaces"][0]["attrs"]["description"]["nso_value"] = "uncertified"
            doc["read_state"] = _rs(
                outcome="unavailable",
                reason="not_covered",
                freshness=None,
                result="kept",
                succeeded=False,
                attempt_id=2,
                payload_revision=1,
            )
            context = reconcile_category(self.device, self.management, "interfaces")
        self.assertEqual(context["_gate"]["interface_attributes"], "skipped_unavailable")
        self.assertEqual(list(NSOInterfaceState.objects.filter(interface__device=self.device).values()), before)
        self.assertEqual(list(Interface.objects.filter(device=self.device).values()), native)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.revision, 1)
        self.assertEqual((snapshot.document, snapshot.digest, snapshot.observed_at), certified)
        self.assertEqual(snapshot.read_state.observed_reason, "not_covered")
        self.assertIs(snapshot.read_state.observed_succeeded, False)
        self.assertEqual(snapshot.read_state.applied_payload_revision, 1)

    def test_not_covered_row_refresh_keeps_overlay_native_and_observation(self):
        from netbox_nso_plugin.models import NSOL2SapState

        from ._scope_observation_case import entry, scope_observation
        from .test_gated_reconcile import _l2_payload

        doc = _l2_payload(("certified-service",), _rs())
        document = {
            "services": [
                entry(
                    **{key: value for key, value in service.items() if key != "saps"},
                    saps=[entry(**sap) for sap in service["saps"]],
                )
                for service in doc["services"]
            ],
            "present": ["services"],
            "unprojectable": [],
        }
        doc["observation"] = scope_observation("l2_service", document=document)
        self.transport.documents["l2-services"] = doc
        with bound_session(self.transport.session()):
            reconcile_category(self.device, self.management, "l2_services")
            before = list(NSOL2SapState.objects.filter(management=self.management).values())
            self.assertTrue(before)
            snapshot = NSOFamilyObservation.objects.get(
                read_state__management=self.management, read_state__family="l2_service"
            )
            certified = (snapshot.document, snapshot.digest, snapshot.observed_at)
            native = list(Interface.objects.filter(device=self.device).values())
            self.transport.documents["l2-services"] = _l2_payload(
                ("uncertified-service",),
                _rs(
                    outcome="unavailable",
                    reason="not_covered",
                    freshness=None,
                    result="kept",
                    succeeded=True,
                    attempt_id=2,
                    payload_revision=1,
                ),
            )
            self.transport.documents["l2-services"]["observation"] = doc["observation"]
            context = reconcile_category(self.device, self.management, "l2_services")
        self.assertEqual(context["_gate"]["l2_service"], "skipped_unavailable")
        self.assertEqual(list(NSOL2SapState.objects.filter(management=self.management).values()), before)
        self.assertEqual(list(Interface.objects.filter(device=self.device).values()), native)
        state = NSOFamilyReadState.objects.get(management=self.management, family="l2_service")
        self.assertEqual(state.observed_reason, "not_covered")
        self.assertEqual(state.observation.revision, 1)
        self.assertEqual(
            (state.observation.document, state.observation.digest, state.observation.observed_at), certified
        )
        self.assertIs(state.observed_succeeded, True)
        self.assertEqual(state.applied_payload_revision, 1)
