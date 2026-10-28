# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""An admitted family read still needs explicit document collection coverage."""

import copy
from uuid import uuid4

from dcim.models import Interface
from django.test import TestCase

from netbox_nso_plugin.adapter_client import bound_session
from netbox_nso_plugin.models import NSOFamilyObservation, NSOFamilyReadState, NSOSnmpCommunityState, NSOSwitchportState
from netbox_nso_plugin.reconcile import reconcile_category

from ._observation_case import ObservationTransport, observation
from ._scope_observation_case import DOCUMENTS, entry, scope_observation
from .test_gated_reconcile import _make
from .test_read_gate import _rs


class TestCollectionRefreshAdmission(TestCase):
    def _switchport_refresh(self, *, covered):
        device, management = _make(f"collection{uuid4().hex[:8]}")
        port = Interface.objects.get(device=device)
        port.name, port.type = "Ethernet1", "1000base-t"
        port.save()
        document = {
            "interfaces": [entry(interface_name=port.name, mode="access", untagged_vlan=10)],
            "present": ["interfaces"] if covered else [],
            "unprojectable": [],
        }
        transport = ObservationTransport(
            management.adapter_device_id, observation("interface_attributes"), observation("interface_ip")
        )
        transport.documents["vlan-database"] = {
            "vlans": [],
            "read_state": _rs(),
            "observation": scope_observation("vlan", document={"vlans": [], "present": ["vlans"], "unprojectable": []}),
        }
        transport.documents["switchport"] = {
            "interfaces": [{"interface_name": port.name, "mode": "access", "untagged_vlan": 10, "tagged_vlans": []}],
            "read_state": _rs(),
            "observation": scope_observation("switchport", document=document),
        }
        with bound_session(transport.session()):
            context = reconcile_category(device, management, "switchport")
        assert any(request.url.endswith("/switchport") for request in transport.requests)
        return management, port, document, context

    def test_access_port_without_tagged_vlan_value_refreshes_overlay(self):
        management, port, document, context = self._switchport_refresh(covered=True)
        self.assertEqual(context["_gate"]["switchport"], "ran")
        state = NSOSwitchportState.objects.get(management=management, interface=port)
        self.assertEqual(state.mode, "access")
        self.assertEqual(state.untagged_vlan.vid, 10)
        self.assertFalse(state.tagged_vlans.exists())
        snapshot = NSOFamilyObservation.objects.get(read_state__management=management, read_state__family="switchport")
        self.assertEqual(snapshot.document, document)
        self.assertEqual(snapshot.read_state.applied_payload_revision, 1)

    def test_unmarked_switchport_interfaces_still_refuse_refresh(self):
        management, _port, _document, context = self._switchport_refresh(covered=False)
        self.assertEqual(context["_gate"]["switchport"], "skipped_unavailable")
        self.assertFalse(NSOSwitchportState.objects.filter(management=management).exists())
        self.assertFalse(
            NSOFamilyObservation.objects.filter(
                read_state__management=management, read_state__family="switchport"
            ).exists()
        )
        state = NSOFamilyReadState.objects.get(management=management, family="switchport")
        self.assertIsNone(state.applied_payload_revision)

    def test_unmarked_snmp_collection_keeps_rows_and_certified_observation(self):
        device, management = _make(f"collection{uuid4().hex[:8]}", manage_snmp=True)
        transport = ObservationTransport(
            management.adapter_device_id, observation("interface_attributes"), observation("interface_ip")
        )
        document = copy.deepcopy(DOCUMENTS["snmp"])
        document.update(users=[], hosts=[], system=entry())
        payload = {
            "communities": [{"community_hash": "abc123def456abcd", "access": "RO", "acl": "20", "has_secret": True}],
            "v3_users": [],
            "hosts": [],
            "system_info": {},
            "read_state": _rs(),
            "observation": scope_observation("snmp", document=document),
        }
        transport.documents["snmp-config"] = payload
        with bound_session(transport.session()):
            reconcile_category(device, management, "snmp")
            before = list(NSOSnmpCommunityState.objects.filter(management=management).values())
            assert before
            snapshot = NSOFamilyObservation.objects.get(read_state__management=management, read_state__family="snmp")
            certified = (snapshot.document, snapshot.digest, snapshot.revision)
            payload["communities"] = []
            document["communities"] = []
            document["present"].remove("communities")
            payload["read_state"] = _rs(attempt_id=2)
            payload["observation"] = scope_observation("snmp", document=document, revision=2)
            context = reconcile_category(device, management, "snmp")
        self.assertEqual(context["_gate"]["snmp"], "skipped_unavailable")
        self.assertEqual(list(NSOSnmpCommunityState.objects.filter(management=management).values()), before)
        snapshot.refresh_from_db()
        self.assertEqual((snapshot.document, snapshot.digest, snapshot.revision), certified)
