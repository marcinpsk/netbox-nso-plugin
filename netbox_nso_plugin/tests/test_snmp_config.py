# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for A4: adapter_client.get_snmp_config and _reconcile_snmp_config."""

import unittest
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Platform, Site
from django.test import TestCase

from ._adapter_http import make_session
from ._outbox_case import content_bulk_update
from .mixins import IntentPushResetMixin

_BASE_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}

_SAMPLE_PAYLOAD = {
    "communities": [
        {"community_hash": "abcd1234abcd1234", "access": "RO", "acl": None, "has_secret": True},
        {"community_hash": "ef012345ef012345", "access": "RW", "acl": "ACL-MGMT", "has_secret": True},
    ],
    "v3_users": [
        {"username": "nms-user", "has_auth_secret": True, "has_priv_secret": True},
    ],
    "hosts": [
        {
            "address": "10.0.0.100",
            "version": "v2c",
            "notify_type": "trap",
            "port": None,
            "community_hash": "abcd1234abcd1234",
        }
    ],
    "system_info": {"location": "DC-01 Rack A", "contact": "noc@example.com"},
    "last_refreshed_at": "2026-06-01T10:00:00Z",
    "refresh_source": "nso",
}


# ---------------------------------------------------------------------------
# adapter_client.get_snmp_config — unit tests (no Django DB)
# ---------------------------------------------------------------------------


class TestGetSnmpConfig(unittest.TestCase):
    """Tests for adapter_client.get_snmp_config()."""

    def _make_session(self, status=200, json_data=None):
        return make_session(status_code=status, json_data=json_data)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_calls_expected_endpoint(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_snmp_config

        session = self._make_session(json_data={"communities": []})
        mock_session_cls.return_value = session

        get_snmp_config(9)

        args, _ = session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[1], "http://adapter.local/api/v1/devices/9/snmp-config")

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_returns_response_unchanged(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_snmp_config

        session = self._make_session(json_data=_SAMPLE_PAYLOAD)
        mock_session_cls.return_value = session

        result = get_snmp_config(9)
        self.assertEqual(result, _SAMPLE_PAYLOAD)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_http_error_raises_adapter_error(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import AdapterError, get_snmp_config

        session = self._make_session(status=404, json_data={"error": {"code": "not_found", "message": "no device"}})
        mock_session_cls.return_value = session

        with self.assertRaises(AdapterError):
            get_snmp_config(99)


# ---------------------------------------------------------------------------
# _reconcile_snmp_config — Django DB integration tests
# ---------------------------------------------------------------------------


class TestReconcileSnmpConfig(IntentPushResetMixin, TestCase):
    """Django-DB tests for _reconcile_snmp_config in template_content.py."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="SnmpMfg", slug="snmpmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="SnmpDevice", slug="snmpdevice")
        role = DeviceRole.objects.create(name="SnmpRole", slug="snmprole")
        site = Site.objects.create(name="SnmpSite", slug="snmpsite")
        cls.device = Device.objects.create(name="snmp-router-1", device_type=device_type, role=role, site=site)

    def _create_mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        instance = NSOInstance.objects.create(
            name="nso-test",
            adapter_instance_id="nso-test",
        )
        return NSODeviceManagement.objects.create(
            device=self.device,
            nso_instance=instance,
            nso_device_name="snmp-router-1",
            adapter_device_id=9,
        )

    def _set_ned(self, ned_id):
        from netbox_nso_plugin.models import NSOPlatformNedMapping

        platform = Platform.objects.create(
            name=f"SNMP {ned_id}",
            slug=f"snmp-{ned_id}".replace(".", "-"),
            manufacturer=self.device.device_type.manufacturer,
        )
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id=ned_id)
        self.device.platform = platform
        self.device.save(update_fields=["platform"])

    def test_empty_payload_returns_empty_lists(self):
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._create_mgmt()
        result = _reconcile_snmp_config(self.device, {"communities": [], "v3_users": [], "hosts": []})
        self.assertEqual(result["communities"], [])
        self.assertEqual(result["v3_users"], [])
        self.assertEqual(result["hosts"], [])
        self.assertIsNone(result["system_info"])

    def test_full_payload_creates_all_rows_as_imported(self):
        from netbox_nso_plugin.models import (
            NSOSnmpCommunityState,
            NSOSnmpHostState,
            NSOSnmpSystemInfoState,
            NSOSnmpV3UserState,
        )
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._create_mgmt()
        result = _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)

        self.assertEqual(len(result["communities"]), 2)
        self.assertEqual(len(result["v3_users"]), 1)
        self.assertEqual(len(result["hosts"]), 1)
        self.assertIsNotNone(result["system_info"])

        c = NSOSnmpCommunityState.objects.get(community_hash="abcd1234abcd1234")
        self.assertEqual(c.access, "RO")
        self.assertEqual(c.status, "imported")

        u = NSOSnmpV3UserState.objects.get(username="nms-user")
        self.assertTrue(u.has_auth_secret)
        self.assertEqual(u.status, "imported")

        h = NSOSnmpHostState.objects.get(address="10.0.0.100")
        self.assertEqual(h.version, "v2c")
        self.assertEqual(h.status, "imported")

        si = NSOSnmpSystemInfoState.objects.get()
        self.assertEqual(si.location, "DC-01 Rack A")
        self.assertEqual(si.status, "imported")

    def test_idempotent_rerun_does_not_duplicate(self):
        from netbox_nso_plugin.models import NSOSnmpCommunityState, NSOSnmpV3UserState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._create_mgmt()
        _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)
        _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)

        self.assertEqual(NSOSnmpCommunityState.objects.count(), 2)
        self.assertEqual(NSOSnmpV3UserState.objects.count(), 1)

    def test_stale_rows_deleted_on_refresh(self):
        from netbox_nso_plugin.models import NSOSnmpCommunityState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._create_mgmt()
        _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)
        self.assertEqual(NSOSnmpCommunityState.objects.count(), 2)

        # Refresh with only one community
        reduced_payload = dict(_SAMPLE_PAYLOAD)
        reduced_payload["communities"] = [_SAMPLE_PAYLOAD["communities"][0]]
        _reconcile_snmp_config(self.device, reduced_payload)

        self.assertEqual(NSOSnmpCommunityState.objects.count(), 1)
        self.assertEqual(NSOSnmpCommunityState.objects.get().community_hash, "abcd1234abcd1234")

    def test_write_path_statuses_not_clobbered(self):
        """Rows already in accepted/deploying/in_sync must keep their status on re-import."""
        from netbox_nso_plugin.models import NSOSnmpCommunityState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        mgmt = self._create_mgmt()
        _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)

        state = NSOSnmpCommunityState.objects.get(management=mgmt, community_hash="abcd1234abcd1234")
        content_bulk_update(state, status="accepted")

        _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)

        row = NSOSnmpCommunityState.objects.get(community_hash="abcd1234abcd1234")
        self.assertEqual(row.status, "accepted")

    def test_matching_read_does_not_settle_generation_correlated_deploying_rows(self):
        from netbox_nso_plugin.models import (
            NSOSnmpCommunityState,
            NSOSnmpHostState,
            NSOSnmpSystemInfoState,
            NSOSnmpV3UserState,
        )
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        mgmt = self._create_mgmt()
        NSOSnmpCommunityState.objects.create(
            management=mgmt,
            community_hash="abcd1234abcd1234",
            vault_secret_hash="abcd1234abcd1234",
            status="deploying",
        )
        NSOSnmpV3UserState.objects.create(
            management=mgmt,
            username="nms-user",
            has_auth_secret=True,
            has_priv_secret=True,
            auth_protocol="sha",
            priv_protocol="aes-128",
            vault_ref="network/netbox/snmp/v3/nms-user",
            status="deploying",
        )
        NSOSnmpHostState.objects.create(
            management=mgmt,
            address="10.0.0.100",
            version="v2c",
            notify_type="trap",
            community_hash="abcd1234abcd1234",
            status="deploying",
        )
        NSOSnmpSystemInfoState.objects.create(
            management=mgmt,
            location="DC-01 Rack A",
            contact="noc@example.com",
            status="deploying",
        )

        _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)

        self.assertEqual(
            NSOSnmpCommunityState.objects.get(management=mgmt, community_hash="abcd1234abcd1234").status,
            "deploying",
        )
        self.assertEqual(NSOSnmpV3UserState.objects.get(management=mgmt, username="nms-user").status, "deploying")
        self.assertEqual(NSOSnmpHostState.objects.get(management=mgmt, address="10.0.0.100").status, "deploying")
        self.assertEqual(NSOSnmpSystemInfoState.objects.get(management=mgmt).status, "deploying")

    def test_system_info_deleted_after_load_returns_none(self):
        """A concurrent singleton deletion must not crash the read reconciliation."""
        from django.db.models.signals import post_init

        from netbox_nso_plugin.models import NSOSnmpSystemInfoState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        mgmt = self._create_mgmt()
        row = NSOSnmpSystemInfoState.objects.create(
            management=mgmt,
            location="Test rack",
            contact="noc@example.invalid",
            status="accepted",
        )
        deleted = []

        def delete_after_load(sender, instance, **kwargs):
            if deleted or instance.pk != row.pk:
                return
            deleted.append(True)
            NSOSnmpSystemInfoState.objects.filter(pk=instance.pk).delete()

        post_init.connect(delete_after_load, sender=NSOSnmpSystemInfoState, weak=False)
        self.addCleanup(post_init.disconnect, delete_after_load, sender=NSOSnmpSystemInfoState)

        result = _reconcile_snmp_config(
            self.device,
            {
                "communities": [],
                "v3_users": [],
                "hosts": [],
                "system_info": {"location": "Test rack", "contact": "noc@example.invalid"},
            },
        )

        self.assertEqual(deleted, [True])
        self.assertIsNone(result["system_info"])

    def test_system_info_concurrent_edit_returns_the_persisted_row(self):
        from django.db.models.signals import post_init

        from netbox_nso_plugin.models import NSOSnmpSystemInfoState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        mgmt = self._create_mgmt()
        row = NSOSnmpSystemInfoState.objects.create(
            management=mgmt,
            location="Test rack",
            contact="noc@example.invalid",
            status="accepted",
        )
        edited = []

        def edit_after_load(sender, instance, **kwargs):
            if edited or instance.pk != row.pk:
                return
            edited.append(True)
            content_bulk_update(instance, location="Operator rack")
            instance.location = "Test rack"

        post_init.connect(edit_after_load, sender=NSOSnmpSystemInfoState, weak=False)
        self.addCleanup(post_init.disconnect, edit_after_load, sender=NSOSnmpSystemInfoState)

        result = _reconcile_snmp_config(
            self.device,
            {
                "communities": [],
                "v3_users": [],
                "hosts": [],
                "system_info": {"location": "Test rack", "contact": "noc@example.invalid"},
            },
        )

        self.assertEqual(edited, [True])
        self.assertIsNotNone(result["system_info"])
        self.assertEqual(result["system_info"].location, "Operator rack")
        self.assertEqual(result["system_info"].status, "accepted")

    def test_omitted_default_trap_port_matches_owned_intent(self):
        from netbox_nso_plugin.models import NSOSnmpHostState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._set_ned("arcos-v8.1.2X-nc-1.0")
        mgmt = self._create_mgmt()
        row = NSOSnmpHostState.objects.create(
            management=mgmt,
            address="198.18.0.30",
            version="v2c",
            notify_type="trap",
            port=162,
            status="accepted",
        )
        _reconcile_snmp_config(
            self.device,
            {
                "communities": [],
                "v3_users": [],
                "hosts": [
                    {
                        "address": row.address,
                        "version": "v2c",
                        "notify_type": "trap",
                    }
                ],
            },
        )

        row.refresh_from_db()
        self.assertEqual(row.port, 162)
        self.assertEqual(row.status, "in_sync")

    def test_present_null_trap_port_matches_owned_intent(self):
        """The adapter emits the host port as present-null, not as an absent key
        (nso_adapter/api/snmp.py) — the owned-intent port must survive that shape too."""
        from netbox_nso_plugin.models import NSOSnmpHostState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._set_ned("arcos-v8.1.2X-nc-1.0")
        mgmt = self._create_mgmt()
        row = NSOSnmpHostState.objects.create(
            management=mgmt,
            address="198.18.0.32",
            version="v2c",
            notify_type="trap",
            port=162,
            status="accepted",
        )
        _reconcile_snmp_config(
            self.device,
            {
                "communities": [],
                "v3_users": [],
                "hosts": [
                    {
                        "address": row.address,
                        "version": "v2c",
                        "notify_type": "trap",
                        "port": None,
                    }
                ],
            },
        )

        row.refresh_from_db()
        self.assertEqual(row.port, 162)
        self.assertEqual(row.status, "in_sync")

    def test_owned_settle_does_not_clobber_concurrent_operator_edit(self):
        """An operator edit landing between the reconciler's row load and its write must survive
        WHOLE: its field values AND its 'accepted' status. The settle was computed against the
        pre-edit values, so writing it would green-light intent the device has never seen."""
        from django.db.models.signals import post_init

        from netbox_nso_plugin.models import NSOSnmpHostState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._set_ned("arcos-v8.1.2X-nc-1.0")
        mgmt = self._create_mgmt()
        row = NSOSnmpHostState.objects.create(
            management=mgmt,
            address="198.18.0.33",
            version="v2c",
            notify_type="trap",
            port=162,
            status="accepted",
        )

        fired = []

        def _concurrent_editor(sender, instance, **kwargs):
            # post_init = the reconciler has just SELECTed the row, so its in-memory copy is
            # already stale when the edit lands. .update() writes straight to the DB: no
            # post_init, hence no recursion.
            # Only the row under test: the loop's get_or_create instantiates others too.
            if fired or instance.pk != row.pk:
                return
            fired.append(True)
            content_bulk_update(instance, notify_type="inform")
            instance.notify_type = "trap"

        post_init.connect(_concurrent_editor, sender=NSOSnmpHostState, weak=False)
        self.addCleanup(post_init.disconnect, _concurrent_editor, sender=NSOSnmpHostState)

        _reconcile_snmp_config(
            self.device,
            {
                "communities": [],
                "v3_users": [],
                "hosts": [
                    {
                        "address": row.address,
                        "version": "v2c",
                        "notify_type": "trap",
                    }
                ],
            },
        )

        row.refresh_from_db()
        self.assertEqual(row.notify_type, "inform")
        self.assertEqual(row.port, 162)
        self.assertEqual(row.status, "accepted")

    def test_owned_settle_does_not_follow_a_concurrent_address_rename(self):
        """The CAS must guard the row's IDENTITY, not just its values: a host renamed between
        the reconciler's SELECT and its write was confirmed at the OLD address only, so the
        settle belongs to a host this row no longer is."""
        from django.db.models.signals import post_init

        from netbox_nso_plugin.models import NSOSnmpHostState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._set_ned("arcos-v8.1.2X-nc-1.0")
        mgmt = self._create_mgmt()
        row = NSOSnmpHostState.objects.create(
            management=mgmt,
            address="198.18.0.35",
            version="v2c",
            notify_type="trap",
            port=162,
            status="accepted",
        )

        fired = []

        def _concurrent_renamer(sender, instance, **kwargs):
            # Only the row under test: the loop's get_or_create instantiates others too.
            if fired or instance.pk != row.pk:
                return
            fired.append(True)
            content_bulk_update(instance, address="198.18.0.99")
            instance.address = "198.18.0.35"

        post_init.connect(_concurrent_renamer, sender=NSOSnmpHostState, weak=False)
        self.addCleanup(post_init.disconnect, _concurrent_renamer, sender=NSOSnmpHostState)

        _reconcile_snmp_config(
            self.device,
            {
                "communities": [],
                "v3_users": [],
                "hosts": [
                    {
                        "address": "198.18.0.35",
                        "version": "v2c",
                        "notify_type": "trap",
                    }
                ],
            },
        )

        row.refresh_from_db()
        self.assertEqual(row.address, "198.18.0.99")
        self.assertEqual(row.status, "accepted")

    def test_package_canonical_2c_settles_owned_v2c_alias(self):
        from netbox_nso_plugin.models import NSOSnmpHostState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._set_ned("arcos-v8.1.2X-nc-1.0")
        mgmt = self._create_mgmt()
        row = NSOSnmpHostState.objects.create(
            management=mgmt,
            address="198.18.0.34",
            version="v2c",
            notify_type="trap",
            status="accepted",
        )

        _reconcile_snmp_config(
            self.device,
            {
                "communities": [],
                "v3_users": [],
                "hosts": [
                    {
                        "address": row.address,
                        "version": "2c",
                        "notify_type": "trap",
                    }
                ],
            },
        )

        row.refresh_from_db()
        self.assertEqual(row.version, "v2c")
        self.assertEqual(row.status, "in_sync")

    def test_package_canonical_3_settles_owned_legacy_snmpv3_alias(self):
        from netbox_nso_plugin.models import NSOSnmpHostState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._set_ned("timos-nc-23.10")
        mgmt = self._create_mgmt()
        row = NSOSnmpHostState.objects.create(
            management=mgmt,
            address="198.18.0.35",
            version="snmpv3",
            notify_type="trap",
            username="netmon-v3",
            status="accepted",
        )

        _reconcile_snmp_config(
            self.device,
            {
                "communities": [],
                "v3_users": [],
                "hosts": [
                    {
                        "address": row.address,
                        "version": "3",
                        "notify_type": "trap",
                        "username": "netmon-v3",
                    }
                ],
            },
        )

        row.refresh_from_db()
        self.assertEqual(row.version, "snmpv3")
        self.assertEqual(row.status, "in_sync")

    def test_value_suppressed_default_trap_port_stays_absent_in_push_payload(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOSnmpHostState

        self._set_ned("arcos-v8.1.2X-nc-1.0")
        mgmt = self._create_mgmt()
        NSOSnmpHostState.objects.create(
            management=mgmt,
            address="198.18.0.31",
            version="v2c",
            notify_type="trap",
            port=162,
            status="accepted",
        )

        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as put:
            deliver("snmp", self.device.pk, mgmt.adapter_device_id)

        host = put.call_args.args[3][0]
        self.assertNotIn("port", host)

    def test_iosxe_default_trap_port_uses_ios_omission_semantics(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOSnmpHostState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._set_ned("cisco-iosxe-cli-6.114")
        mgmt = self._create_mgmt()
        row = NSOSnmpHostState.objects.create(
            management=mgmt,
            address="198.18.0.33",
            version="v2c",
            notify_type="trap",
            port=162,
            status="accepted",
        )

        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as put:
            deliver("snmp", self.device.pk, mgmt.adapter_device_id)
        self.assertNotIn("port", put.call_args.args[3][0])

        _reconcile_snmp_config(
            self.device,
            {
                "communities": [],
                "v3_users": [],
                "hosts": [
                    {
                        "address": row.address,
                        "version": "v2c",
                        "notify_type": "trap",
                    }
                ],
            },
        )
        row.refresh_from_db()
        self.assertEqual(row.status, "in_sync")

    def test_default_free_family_preserves_explicit_conventional_port(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOSnmpHostState

        self._set_ned("juniper-junos-nc-4.19")
        mgmt = self._create_mgmt()
        NSOSnmpHostState.objects.create(
            management=mgmt,
            address="198.18.0.32",
            version="v2c",
            notify_type="trap",
            port=162,
            status="accepted",
        )

        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as put:
            deliver("snmp", self.device.pk, mgmt.adapter_device_id)

        self.assertEqual(put.call_args.args[3][0]["port"], 162)

    def test_refresh_preserves_owned_rows_absent_from_payload(self):
        """Operator-owned rows absent from the payload must SURVIVE the refresh.

        Deleting them loses vault_ref/status mid-flight: an operator-created
        community not yet applied, or a just-rotated one whose new hash the
        device doesn't report yet, would vanish between Accept and Apply.
        Unowned (imported/conflict) stale rows are still deleted.
        """
        from netbox_nso_plugin.models import NSOSnmpCommunityState, NSOSnmpHostState, NSOSnmpV3UserState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        mgmt = self._create_mgmt()
        _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)

        community = NSOSnmpCommunityState.objects.get(management=mgmt, community_hash="ef012345ef012345")
        content_bulk_update(
            community,
            status="accepted",
            vault_ref="network/netbox/snmp/community/ef012345ef012345#community",
        )
        user = NSOSnmpV3UserState.objects.get(management=mgmt, username="nms-user")
        content_bulk_update(
            user,
            status="deploying",
            vault_ref="network/netbox/snmp/v3/nms-user",
        )
        host = NSOSnmpHostState.objects.get(management=mgmt, address="10.0.0.100")
        content_bulk_update(host, status="in_sync")

        empty = dict(_SAMPLE_PAYLOAD)
        empty["communities"], empty["v3_users"], empty["hosts"] = [], [], []
        _reconcile_snmp_config(self.device, empty)

        # owned rows survived with vault_ref intact — and accepted/deploying keep their
        # status, because that intent is not yet confirmed on the device anyway.
        row = NSOSnmpCommunityState.objects.get(community_hash="ef012345ef012345")
        self.assertEqual(row.status, "accepted")
        self.assertEqual(row.vault_ref, "network/netbox/snmp/community/ef012345ef012345#community")
        self.assertEqual(NSOSnmpV3UserState.objects.get(username="nms-user").status, "deploying")
        # ...but an APPLIED row the device has stopped reporting is drift, not "in sync".
        # Surviving the refresh is only half the contract: without the present=False leg the
        # row sat green forever while the config it describes had been removed out-of-band.
        # (This assertion used to read `in_sync`.)
        self.assertEqual(NSOSnmpHostState.objects.get(address="10.0.0.100").status, "changed")
        # the unowned imported community was still dropped
        self.assertFalse(NSOSnmpCommunityState.objects.filter(community_hash="abcd1234abcd1234").exists())

    def test_accepted_community_with_matching_fingerprint_settles_in_sync(self):
        """WP7 live finding: an accepted community whose Vault fingerprint equals the
        device-reported hash is GENUINELY device-confirmed — the reconcile must settle
        it accepted → in_sync (matches=True), not leave it 'pending apply' forever.
        Without a fingerprint (or on hash2 platforms) there is no confirmation and the
        owned status must be preserved."""
        from netbox_nso_plugin.models import NSOSnmpCommunityState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        mgmt = self._create_mgmt()
        _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)

        # confirmed: fingerprint equals the device hash → settles
        confirmed = NSOSnmpCommunityState.objects.get(management=mgmt, community_hash="abcd1234abcd1234")
        content_bulk_update(confirmed, status="accepted", vault_secret_hash="abcd1234abcd1234")
        # unconfirmed: no fingerprint recorded → preserved
        unconfirmed = NSOSnmpCommunityState.objects.get(management=mgmt, community_hash="ef012345ef012345")
        content_bulk_update(unconfirmed, status="accepted", vault_secret_hash="")

        _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)

        self.assertEqual(NSOSnmpCommunityState.objects.get(community_hash="abcd1234abcd1234").status, "in_sync")
        self.assertEqual(NSOSnmpCommunityState.objects.get(community_hash="ef012345ef012345").status, "accepted")

    def test_refresh_preserves_vault_ref_on_update_path(self):
        """A reported row's operator-set vault_ref survives the field refresh."""
        from netbox_nso_plugin.models import NSOSnmpCommunityState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        mgmt = self._create_mgmt()
        _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)
        state = NSOSnmpCommunityState.objects.get(management=mgmt, community_hash="abcd1234abcd1234")
        content_bulk_update(
            state,
            vault_ref="network/netbox/snmp/community/abcd1234abcd1234#community",
        )

        _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)

        row = NSOSnmpCommunityState.objects.get(community_hash="abcd1234abcd1234")
        self.assertEqual(row.vault_ref, "network/netbox/snmp/community/abcd1234abcd1234#community")

    def test_community_acl_and_access_updated(self):
        from netbox_nso_plugin.models import NSOSnmpCommunityState
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._create_mgmt()
        _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)

        rw_community = NSOSnmpCommunityState.objects.get(community_hash="ef012345ef012345")
        self.assertEqual(rw_community.access, "RW")
        self.assertEqual(rw_community.acl, "ACL-MGMT")

    def test_refresh_source_propagated(self):
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        self._create_mgmt()
        result = _reconcile_snmp_config(self.device, _SAMPLE_PAYLOAD)

        self.assertEqual(result["refresh_source"], "nso")
        self.assertEqual(result["last_refreshed_at"], "2026-06-01T10:00:00Z")

    def test_no_mgmt_returns_empty(self):
        """Device without NSODeviceManagement returns empty dict gracefully."""
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        # Use a fresh device without management
        device_type = DeviceType.objects.get(slug="snmpdevice")
        role = DeviceRole.objects.get(slug="snmprole")
        site = Site.objects.get(slug="snmpsite")
        unmanaged = Device.objects.create(name="unmanaged-1", device_type=device_type, role=role, site=site)

        result = _reconcile_snmp_config(unmanaged, _SAMPLE_PAYLOAD)
        self.assertEqual(result["communities"], [])
        self.assertEqual(result["v3_users"], [])
        self.assertEqual(result["hosts"], [])
        self.assertIsNone(result["system_info"])
