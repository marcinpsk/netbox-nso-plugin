# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Contract tests — consumer side of the L2/L3-interface read-mirrors.

Mirrors the producer contract for GET /vlan-database, /switchport, /svi,
/subinterface. These four responses have NO top-level ``last_refreshed_at``/
``refresh_source`` and every level emits a fixed key set.

Canonical contract: ``nso-adapter/docs/api-contract.md`` (sections).
Mirror (producer side): ``nso-adapter/tests/api/test_contract_l2_read.py`` — the
``*_KEYS`` sets MUST stay identical across both files.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOInstance,
    NSOSubinterfaceState,
    NSOSVIState,
    NSOSwitchportState,
    NSOVLANState,
)
from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface
from netbox_nso_plugin.svi_reconciler import reconcile_svi
from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, reconcile_vlan_database

VLAN_TOP_KEYS = {"device_id", "vlans"}
VLAN_KEYS = {"vlan_id", "name", "source"}
SWITCHPORT_TOP_KEYS = {"device_id", "interfaces"}
SWITCHPORT_IFACE_KEYS = {"interface_name", "mode", "untagged_vlan", "tagged_vlans", "source"}
SVI_TOP_KEYS = {"device_id", "interfaces"}
SVI_IFACE_KEYS = {"interface_name", "vlan_id", "type", "vrf", "source"}
SUBIF_TOP_KEYS = {"device_id", "interfaces"}
SUBIF_IFACE_KEYS = {"interface_name", "parent_interface", "dot1q_vlan", "type", "vrf", "source"}

VLAN_PAYLOAD = {"device_id": 7950, "vlans": [{"vlan_id": 100, "name": "DATA", "source": "vlan-database"}]}
SWITCHPORT_PAYLOAD = {
    "device_id": 7951,
    "interfaces": [
        {"interface_name": "GE0/1", "mode": "access", "untagged_vlan": None, "tagged_vlans": [], "source": "switchport"}
    ],
}
SVI_PAYLOAD = {
    "device_id": 7952,
    "interfaces": [{"interface_name": "Vlan100", "vlan_id": 100, "type": "svi", "vrf": "MGMT", "source": "svi"}],
}
SUBIF_PAYLOAD = {
    "device_id": 7953,
    "interfaces": [
        {
            "interface_name": "GE0/0.100",
            "parent_interface": "GE0/0",
            "dot1q_vlan": 100,
            "type": "subinterface",
            "vrf": "",
            "source": "subinterface",
        }
    ],
}


class TestL2ReadContractConsumer(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="L2Ct", slug="l2ct")
        dt = DeviceType.objects.create(manufacturer=mfg, model="L2CtDev", slug="l2ctdev")
        role = DeviceRole.objects.create(name="L2CtRole", slug="l2ctrole")
        site = Site.objects.create(name="L2CtSite", slug="l2ctsite")
        cls.device = Device.objects.create(name="l2-ct-rtr", device_type=dt, role=role, site=site)
        # GE0/1 for switchport; GE0/0 is the subinterface's physical parent (never auto-created).
        Interface.objects.create(device=cls.device, name="GE0/1", type="1000base-t")
        Interface.objects.create(device=cls.device, name="GE0/0", type="1000base-t")
        inst = NSOInstance.objects.create(name="l2-ct-inst", adapter_instance_id="l2-ct-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="l2-ct", adapter_device_id=7950
        )

    def test_payload_keys_match_the_pinned_contract(self):
        """Each example mirrors the producer's pinned key sets (keeps the mirror honest)."""
        self.assertEqual(set(VLAN_PAYLOAD.keys()), VLAN_TOP_KEYS)
        self.assertEqual(set(VLAN_PAYLOAD["vlans"][0].keys()), VLAN_KEYS)
        self.assertEqual(set(SWITCHPORT_PAYLOAD.keys()), SWITCHPORT_TOP_KEYS)
        self.assertEqual(set(SWITCHPORT_PAYLOAD["interfaces"][0].keys()), SWITCHPORT_IFACE_KEYS)
        self.assertEqual(set(SVI_PAYLOAD.keys()), SVI_TOP_KEYS)
        self.assertEqual(set(SVI_PAYLOAD["interfaces"][0].keys()), SVI_IFACE_KEYS)
        self.assertEqual(set(SUBIF_PAYLOAD.keys()), SUBIF_TOP_KEYS)
        self.assertEqual(set(SUBIF_PAYLOAD["interfaces"][0].keys()), SUBIF_IFACE_KEYS)

    def test_vlan_consumer(self):
        rows = reconcile_vlan_database(self.device, VLAN_PAYLOAD)
        self.assertEqual(len(rows), 1)
        self.assertEqual(NSOVLANState.objects.filter(management=self.mgmt, vlan__vid=100).count(), 1)

    def test_switchport_consumer(self):
        reconcile_switchport(self.device, SWITCHPORT_PAYLOAD)
        state = NSOSwitchportState.objects.get(management=self.mgmt, interface__name="GE0/1")
        self.assertEqual(state.mode, "access")

    def test_svi_consumer(self):
        reconcile_svi(self.device, SVI_PAYLOAD)
        state = NSOSVIState.objects.get(management=self.mgmt, interface__name="Vlan100")
        self.assertEqual(state.svi_type, "svi")

    def test_subinterface_consumer(self):
        reconcile_subinterface(self.device, SUBIF_PAYLOAD)
        state = NSOSubinterfaceState.objects.get(management=self.mgmt, interface__name="GE0/0.100")
        self.assertEqual(state.dot1q_vlan, 100)
