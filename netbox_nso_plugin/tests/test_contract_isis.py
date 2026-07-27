# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Contract test — consumer side of GET /api/v1/devices/{id}/isis-interfaces.

The deepest read contract: a large optional scalar set on processes/interfaces plus the
four nested JSON bags (``settings``/``levels``/``segment_routing``/``flex_algos``) the
plugin reads fixed key sets out of. Consumed by
``template_content._reconcile_isis_process`` / ``_reconcile_isis_interfaces`` — note
these take the LISTs (``payload["processes"]`` / ``["interfaces"]``), not the dict.

Canonical contract: ``nso-adapter/docs/api-contract.md`` (IS-IS §).
Mirror (producer side): ``nso-adapter/tests/api/test_contract_isis.py`` — the ``*_KEYS``
sets MUST stay identical across both files.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOInstance,
    NSOISISInstanceState,
    NSOISISInterfaceState,
)
from netbox_nso_plugin.template_content import _reconcile_isis_interfaces, _reconcile_isis_process

TOP_KEYS = {"device_id", "last_refreshed_at", "refresh_source", "processes", "interfaces"}
PROC_REQUIRED_KEYS = {"process_tag"}
PROC_OPTIONAL_SCALARS = {
    "net",
    "is_type",
    "metric_style",
    "overload_bit",
    "area_auth_type",
    "area_auth_present",
    "area_auth_key",
    "domain_auth_type",
    "domain_auth_present",
    "domain_auth_key",
    "spf_initial_wait",
    "spf_max_wait",
    "lsp_initial_wait",
    "lsp_max_wait",
    "lsp_lifetime",
    "lsp_refresh_interval",
    "lsp_mtu",
    "overload_on_startup",
    "overload_timeout",
    "te_enabled",
    "sr_enabled",
    "sr_node_msd",
    "distance",
    "maximum_paths",
    "reference_bandwidth",
    "segment_routing_reported",
    "segment_routing_configured",
}
PROC_CONTAINER_KEYS = {"settings", "levels", "segment_routing", "flex_algos"}
IFACE_REQUIRED_KEYS = {"interface_name", "af", "process_tag", "passive"}
IFACE_OPTIONAL_SCALARS = {
    "circuit_type",
    "network_type",
    "metric",
    "bound_port",
    "hello_auth_type",
    "hello_auth_present",
    "bfd_enabled",
    "csnp_interval",
    "retransmit_interval",
    "lsp_interval",
    "mesh_group",
}
IFACE_CONTAINER_KEYS = {"settings", "levels"}
INSTANCE_LEVEL_KEYS = {
    "level",
    "default_metric",
    "wide_metrics_only",
    "preference",
    "labeled_preference",
    "disabled",
    "auth_type",
}
IFACE_LEVEL_KEYS = {"level", "metric", "hello_interval", "hello_multiplier", "priority", "passive"}
SR_KEYS = {
    "enabled",
    "prefix_sid_range",
    "srgb_start",
    "srgb_range",
    "node_sid_index",
    "node_sid_label",
    "node_sid_v6_index",
    "node_sid_v6_label",
    "maximum_sid_depth",
    "tunnel_table_pref",
}
FLEX_KEYS = {
    "algo_id",
    "metric_type",
    "priority",
    "admin_group_exclude",
    "admin_group_include_any",
    "admin_group_include_all",
}

CONTRACT_PAYLOAD = {
    "device_id": 7900,
    "last_refreshed_at": "2026-06-01T10:00:00Z",
    "refresh_source": "poll",
    "processes": [
        {
            "process_tag": "1",
            "net": "49.0001.0000.0000.0001.00",
            "is_type": "level-2",
            "metric_style": "wide",
            "overload_bit": False,
            "area_auth_type": "md5",
            "area_auth_present": True,
            "area_auth_key": "x",
            "domain_auth_type": "md5",
            "domain_auth_present": True,
            "domain_auth_key": "y",
            "spf_initial_wait": 50,
            "spf_max_wait": 5000,
            "lsp_initial_wait": 50,
            "lsp_max_wait": 5000,
            "lsp_lifetime": 65535,
            "lsp_refresh_interval": 65000,
            "lsp_mtu": 1492,
            "overload_on_startup": True,
            "overload_timeout": 180,
            "te_enabled": True,
            "sr_enabled": True,
            "sr_node_msd": 10,
            "distance": 115,
            "maximum_paths": 8,
            "reference_bandwidth": 100000,
            "segment_routing_reported": True,
            "segment_routing_configured": True,
            "settings": {"some_knob": "v"},
            "levels": [
                {
                    "level": "2",
                    "default_metric": 10,
                    "wide_metrics_only": True,
                    "preference": 7,
                    "labeled_preference": 7,
                    "disabled": False,
                    "auth_type": "md5",
                }
            ],
            "segment_routing": {
                "enabled": True,
                "prefix_sid_range": "global",
                "srgb_start": 100000,
                "srgb_range": 200000,
                "node_sid_index": 100,
                "node_sid_label": 100100,
                "node_sid_v6_index": 200,
                "node_sid_v6_label": 100200,
                "maximum_sid_depth": 10,
                "tunnel_table_pref": 8,
            },
            "flex_algos": [
                {
                    "algo_id": 128,
                    "metric_type": "igp",
                    "priority": 100,
                    "admin_group_exclude": ["RED"],
                    "admin_group_include_any": ["BLUE"],
                    "admin_group_include_all": [],
                }
            ],
        },
        {"process_tag": "2"},
    ],
    "interfaces": [
        {
            "interface_name": "GE0/0",
            "af": "ipv4",
            "process_tag": "1",
            "circuit_type": "level-2-only",
            "network_type": "point-to-point",
            "metric": 10,
            "passive": False,
            "bound_port": "GE0/0",
            "hello_auth_type": "md5",
            "hello_auth_present": True,
            "bfd_enabled": True,
            "csnp_interval": 10,
            "retransmit_interval": 5,
            "lsp_interval": 33,
            "mesh_group": "1",
            "settings": {"some_knob": "v"},
            "levels": [
                {
                    "level": "2",
                    "metric": 10,
                    "hello_interval": 3,
                    "hello_multiplier": 3,
                    "priority": 64,
                    "passive": False,
                }
            ],
        },
        {"interface_name": "GE0/1", "af": "ipv4", "process_tag": "", "passive": False},
    ],
}


class TestIsisContractConsumer(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="IsCt", slug="isct")
        dt = DeviceType.objects.create(manufacturer=mfg, model="IsCtDev", slug="isctdev")
        role = DeviceRole.objects.create(name="IsCtRole", slug="isctrole")
        site = Site.objects.create(name="IsCtSite", slug="isctsite")
        cls.device = Device.objects.create(name="is-ct-rtr", device_type=dt, role=role, site=site)
        for name in ("GE0/0", "GE0/1"):
            Interface.objects.create(device=cls.device, name=name, type="1000base-t")
        inst = NSOInstance.objects.create(name="is-ct-inst", adapter_instance_id="is-ct-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="is-ct", adapter_device_id=7900
        )

    def test_payload_keys_match_the_pinned_contract(self):
        """The example mirrors the producer's pinned key sets, including the nested bags."""
        self.assertEqual(set(CONTRACT_PAYLOAD.keys()), TOP_KEYS)
        procs = {p["process_tag"]: p for p in CONTRACT_PAYLOAD["processes"]}
        self.assertEqual(set(procs["1"].keys()), PROC_REQUIRED_KEYS | PROC_OPTIONAL_SCALARS | PROC_CONTAINER_KEYS)
        self.assertEqual(set(procs["2"].keys()), PROC_REQUIRED_KEYS)
        self.assertEqual(set(procs["1"]["levels"][0].keys()), INSTANCE_LEVEL_KEYS)
        self.assertEqual(set(procs["1"]["segment_routing"].keys()), SR_KEYS)
        self.assertEqual(set(procs["1"]["flex_algos"][0].keys()), FLEX_KEYS)
        ifaces = {i["interface_name"]: i for i in CONTRACT_PAYLOAD["interfaces"]}
        self.assertEqual(
            set(ifaces["GE0/0"].keys()), IFACE_REQUIRED_KEYS | IFACE_OPTIONAL_SCALARS | IFACE_CONTAINER_KEYS
        )
        self.assertEqual(set(ifaces["GE0/1"].keys()), IFACE_REQUIRED_KEYS)
        self.assertEqual(set(ifaces["GE0/0"]["levels"][0].keys()), IFACE_LEVEL_KEYS)

    def test_consumer_reads_contract_payload(self):
        """The reconcilers ingest the documented shape (incl. nested bags) into the overlays."""
        try:
            from netbox_routing.models import ISISFlexAlgo, ISISLevel, ISISSegmentRouting
        except ImportError:
            self.skipTest("netbox_routing not installed")

        proc_rows = _reconcile_isis_process(self.device, CONTRACT_PAYLOAD["processes"])
        iface_rows = _reconcile_isis_interfaces(self.device, CONTRACT_PAYLOAD["interfaces"])

        self.assertEqual(NSOISISInstanceState.objects.filter(management=self.mgmt).count(), 2)
        self.assertEqual(len(proc_rows), 2)
        self.assertTrue(NSOISISInterfaceState.objects.filter(management=self.mgmt, interface__name="GE0/0").exists())
        self.assertGreaterEqual(len(iface_rows), 1)

        # The nested JSON bags flowed through into netbox_routing child tables.
        inst_state = NSOISISInstanceState.objects.get(management=self.mgmt, process_tag="1")
        ri = inst_state.isis_instance
        self.assertIsNotNone(ri)
        self.assertTrue(ISISLevel.objects.filter(instance=ri).exists())  # levels bag
        self.assertTrue(ISISSegmentRouting.objects.filter(instance=ri, enabled=True).exists())  # SR bag
        self.assertTrue(ISISFlexAlgo.objects.filter(instance=ri).exists())  # flex_algos bag
