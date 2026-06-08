# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Phase 1: SNMP overlay accept + edit + deferred intent push (operator write path)."""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.test import TestCase

from .mixins import IntentPushResetMixin


def _superuser():
    User = get_user_model()
    return User.objects.create_superuser(username="snmp-admin", password="pw", email="snmp@test.x")  # noqa: S106


class _SnmpBase(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="SnmpAccMfg", slug="snmpaccmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SnmpAccDev", slug="snmpaccdev")
        role = DeviceRole.objects.create(name="SnmpAccRole", slug="snmpaccrole")
        site = Site.objects.create(name="SnmpAccSite", slug="snmpaccsite")
        cls.device = Device.objects.create(name="snmp-acc-rtr", device_type=dt, role=role, site=site)

    def _make_mgmt(self, adapter_device_id=42):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(
            name="snmp-acc-inst", defaults={"adapter_instance_id": "snmp-acc-inst"}
        )
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "nso-snmp-acc",
                "adapter_device_id": adapter_device_id,
                "manage_snmp": True,
            },
        )[0]

    def _community(self, mgmt, status="imported", vault_ref=""):
        from netbox_nso_plugin.models import NSOSnmpCommunityState

        return NSOSnmpCommunityState.objects.create(
            management=mgmt, community_hash="abcd1234abcd1234", access="RO", status=status, vault_ref=vault_ref
        )


class TestSnmpAcceptView(_SnmpBase):
    def test_accept_differing_marks_accepted(self):
        """Accepting a differing (conflict) row creates intent → 'accepted' (pending apply)."""
        mgmt = self._make_mgmt()
        c = self._community(mgmt, status="conflict")
        self.client.force_login(_superuser())
        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent"):
            resp = self.client.post(f"/plugins/nso/snmp/community-state/{c.pk}/accept/")
        assert resp.status_code == 302
        c.refresh_from_db()
        assert c.status == "accepted"
        assert c.accepted_at is not None

    def test_accept_matching_marks_in_sync_owned(self):
        """Accepting an imported (already-matching) row just marks it owned → in_sync."""
        mgmt = self._make_mgmt()
        c = self._community(mgmt, status="imported")
        self.client.force_login(_superuser())
        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent"):
            resp = self.client.post(f"/plugins/nso/snmp/community-state/{c.pk}/accept/")
        assert resp.status_code == 302
        c.refresh_from_db()
        assert c.status == "in_sync"
        assert c.accepted_at is not None

    def test_accept_with_vault_ref_pushes_intent(self):
        """Accepting a community that has a Vault ref stores it in the SNMP intent
        mirror (deferred); the device Apply later commits it."""
        from netbox_nso_plugin.models import NSOSnmpCommunityState
        from netbox_nso_plugin.signals import _on_snmp_state_save, reset_intent_push_state

        mgmt = self._make_mgmt()
        c = self._community(mgmt, status="accepted", vault_ref="secret/snmp#community")
        # Creating the row already fired the real signal (coalesced on the rolled-back
        # test txn); clear that stale coalescing state before the assertion run.
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                _on_snmp_state_save(sender=NSOSnmpCommunityState, instance=c)
            mock_put.assert_called_once()
            # communities arg carries the vault_ref-bearing row
            communities = mock_put.call_args[0][1]
            assert communities and communities[0]["vault_ref"] == "secret/snmp#community"


class TestSnmpEditView(_SnmpBase):
    def test_edit_updates_vault_ref(self):
        mgmt = self._make_mgmt()
        c = self._community(mgmt, status="imported")
        self.client.force_login(_superuser())
        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent"):
            resp = self.client.post(
                f"/plugins/nso/snmp/community-state/{c.pk}/edit/",
                data={"access": "RW", "acl": "MGMT", "vault_ref": "secret/snmp#c1"},
            )
        assert resp.status_code in (200, 302)
        c.refresh_from_db()
        assert c.vault_ref == "secret/snmp#c1"
        assert c.access == "RW"


class TestSnmpApplyPreview(_SnmpBase):
    def test_accepted_snmp_counts_as_pending(self):
        mgmt = self._make_mgmt()
        self._community(mgmt, status="accepted")
        self.client.force_login(_superuser())
        resp = self.client.get(f"/plugins/nso/devices/{self.device.pk}/apply-preview/")
        assert resp.status_code == 200
        assert resp.json()["routing"] >= 1
