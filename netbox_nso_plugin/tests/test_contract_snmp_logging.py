# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Contract tests — consumer side of GET /snmp-config and /logging-config.

Mirrors the producer contract. SNMP emits a fixed key set per level; logging hosts
omit optional keys when unset. Consumed by template_content._reconcile_snmp_config /
_reconcile_logging_config.

Canonical contract: ``nso-adapter/docs/api-contract.md`` (SNMP §; logging §).
Mirror (producer side): ``nso-adapter/tests/api/test_contract_snmp_logging.py`` — the
``*_KEYS`` sets MUST stay identical across both files.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOInstance,
    NSOLoggingHostState,
    NSOSnmpCommunityState,
    NSOSnmpHostState,
    NSOSnmpSystemInfoState,
    NSOSnmpV3UserState,
)
from netbox_nso_plugin.template_content import _reconcile_logging_config, _reconcile_snmp_config

SNMP_TOP_KEYS = {"device_id", "last_refreshed_at", "refresh_source", "communities", "v3_users", "hosts", "system_info"}
SNMP_COMMUNITY_KEYS = {"community_hash", "access", "acl"}
SNMP_V3USER_KEYS = {"username", "has_auth_secret", "has_priv_secret"}
SNMP_HOST_KEYS = {"address", "version", "notify_type", "port"}
SNMP_SYSINFO_KEYS = {"location", "contact"}
LOGGING_TOP_KEYS = {"device_id", "last_refreshed_at", "refresh_source", "hosts"}
LOGGING_HOST_REQUIRED_KEYS = {"address"}
LOGGING_HOST_OPTIONAL_KEYS = {"port", "severity", "facility", "transport", "vrf", "source"}

SNMP_PAYLOAD = {
    "device_id": 7960,
    "last_refreshed_at": "2026-06-01T10:00:00Z",
    "refresh_source": "poll",
    "communities": [{"community_hash": "abc", "access": "RO", "acl": "ACL-1"}],
    "v3_users": [{"username": "netops", "has_auth_secret": True, "has_priv_secret": False}],
    "hosts": [{"address": "10.0.0.9", "version": "2c", "notify_type": "trap", "port": 162}],
    "system_info": {"location": "DC1", "contact": "noc@x"},
}
LOGGING_PAYLOAD = {
    "device_id": 7961,
    "last_refreshed_at": "2026-06-01T10:00:00Z",
    "refresh_source": "poll",
    "hosts": [
        {
            "address": "10.0.0.5",
            "port": 514,
            "severity": "informational",
            "facility": "local7",
            "transport": "udp",
            "vrf": "MGMT",
            "source": "Loopback0",
        },
        {"address": "10.0.0.6"},
    ],
}


class TestSnmpLoggingContractConsumer(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="SlCt", slug="slct")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SlCtDev", slug="slctdev")
        role = DeviceRole.objects.create(name="SlCtRole", slug="slctrole")
        site = Site.objects.create(name="SlCtSite", slug="slctsite")
        cls.device = Device.objects.create(name="sl-ct-rtr", device_type=dt, role=role, site=site)
        inst = NSOInstance.objects.create(name="sl-ct-inst", adapter_instance_id="sl-ct-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="sl-ct", adapter_device_id=7960
        )

    def test_payload_keys_match_the_pinned_contract(self):
        """The examples mirror the producer's pinned key sets (keeps the mirror honest)."""
        self.assertEqual(set(SNMP_PAYLOAD.keys()), SNMP_TOP_KEYS)
        self.assertEqual(set(SNMP_PAYLOAD["communities"][0].keys()), SNMP_COMMUNITY_KEYS)
        self.assertEqual(set(SNMP_PAYLOAD["v3_users"][0].keys()), SNMP_V3USER_KEYS)
        self.assertEqual(set(SNMP_PAYLOAD["hosts"][0].keys()), SNMP_HOST_KEYS)
        self.assertEqual(set(SNMP_PAYLOAD["system_info"].keys()), SNMP_SYSINFO_KEYS)
        self.assertEqual(set(LOGGING_PAYLOAD.keys()), LOGGING_TOP_KEYS)
        hosts = {h["address"]: h for h in LOGGING_PAYLOAD["hosts"]}
        self.assertEqual(set(hosts["10.0.0.5"].keys()), LOGGING_HOST_REQUIRED_KEYS | LOGGING_HOST_OPTIONAL_KEYS)
        self.assertEqual(set(hosts["10.0.0.6"].keys()), LOGGING_HOST_REQUIRED_KEYS)

    def test_snmp_consumer(self):
        _reconcile_snmp_config(self.device, SNMP_PAYLOAD)
        self.assertEqual(NSOSnmpCommunityState.objects.get(management=self.mgmt, community_hash="abc").access, "RO")
        self.assertTrue(NSOSnmpV3UserState.objects.filter(management=self.mgmt, username="netops").exists())
        self.assertTrue(NSOSnmpHostState.objects.filter(management=self.mgmt, address="10.0.0.9").exists())
        self.assertEqual(NSOSnmpSystemInfoState.objects.get(management=self.mgmt).location, "DC1")

    def test_logging_consumer(self):
        _reconcile_logging_config(self.device, LOGGING_PAYLOAD)
        self.assertEqual(NSOLoggingHostState.objects.filter(management=self.mgmt).count(), 2)
        maximal = NSOLoggingHostState.objects.get(management=self.mgmt, address="10.0.0.5")
        self.assertEqual(maximal.severity, "informational")
        self.assertEqual(maximal.facility, "local7")
