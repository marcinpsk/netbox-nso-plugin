# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""SNMP secret entry / verify / harvest flows (Vault-ref UX).

The adapter transport is the one true external boundary — faked per
``_adapter_http`` convention (spec'd session + REAL requests.Response). Forms,
models, signals and views run for real against the test DB.
"""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.test import TestCase

from netbox_nso_plugin.vault_refs import secret_fingerprint

from ._adapter_http import make_session
from .mixins import IntentPushDeliveryMixin

_BASE_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


def _superuser():
    User = get_user_model()
    return User.objects.create_superuser(username="vault-admin", password="pw", email="vault@test.x")  # noqa: S106


class _SecretBase(IntentPushDeliveryMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="VaultMfg", slug="vaultmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="VaultDev", slug="vaultdev")
        role = DeviceRole.objects.create(name="VaultRole", slug="vaultrole")
        site = Site.objects.create(name="VaultSite", slug="vaultsite")
        cls.device = Device.objects.create(name="vault-rtr", device_type=dt, role=role, site=site)

    def setUp(self):
        super().setUp()
        from netbox_nso_plugin.models import NSOVaultSettings

        NSOVaultSettings.objects.create(kv_mount="network", base_path="netbox/snmp", enabled=True)

    def _make_mgmt(self, adapter_device_id=42):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="vault-inst", defaults={"adapter_instance_id": "vault-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "nso-vault-rtr",
                "adapter_device_id": adapter_device_id,
                "manage_snmp": True,
            },
        )[0]

    def _community(self, mgmt, community_hash="oldhash1234567890", **kwargs):
        from netbox_nso_plugin.models import NSOSnmpCommunityState

        defaults = {"access": "RO", "status": "imported"}
        defaults.update(kwargs)
        return NSOSnmpCommunityState.objects.create(management=mgmt, community_hash=community_hash, **defaults)


class TestVaultSettingsSingleton(TestCase):
    def test_second_save_reuses_the_singleton_pk(self):
        from netbox_nso_plugin.models import NSOVaultSettings

        first = NSOVaultSettings.objects.create(kv_mount="network", base_path="netbox/snmp")
        second = NSOVaultSettings(kv_mount="kv2", base_path="other/path")
        second.save()
        self.assertEqual(NSOVaultSettings.objects.count(), 1)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(NSOVaultSettings.objects.get().kv_mount, "kv2")

    def test_edit_view_renders_with_tab(self):
        self.client.force_login(_superuser())
        resp = self.client.get("/plugins/nso/vault-settings/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "vault-settings")


class TestCommunitySecretSet(_SecretBase):
    def _form(self, instance, data):
        from netbox_nso_plugin.forms import NSOSnmpCommunityStateForm

        base = {"access": instance.access, "acl": instance.acl, "vault_ref": instance.vault_ref}
        base.update(data)
        return NSOSnmpCommunityStateForm(data=base, instance=instance)

    def test_set_secret_derives_ref_rekeys_and_repoints_hosts(self):
        from netbox_nso_plugin.models import NSOSnmpHostState

        mgmt = self._make_mgmt()
        row = self._community(mgmt)
        host = NSOSnmpHostState.objects.create(
            management=mgmt, address="10.0.0.9", version="v2c", notify_type="trap", community_hash=row.community_hash
        )
        new_fp = secret_fingerprint("new-c0mmunity")
        expected_ref = f"network/netbox/snmp/community/{new_fp}#community"
        session = make_session(json_data={"vault_ref": expected_ref, "version": 3, "hashes": {"community": new_fp}})

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
            patch("netbox_nso_plugin.adapter_client.put_snmp_intent"),
        ):
            form = self._form(row, {"secret_value": "new-c0mmunity"})
            self.assertTrue(form.is_valid(), form.errors)
            form.save()

        row.refresh_from_db()
        self.assertEqual(row.community_hash, new_fp)  # rekeyed to the new value's fingerprint
        self.assertEqual(row.vault_secret_hash, new_fp)
        self.assertEqual(row.vault_secret_version, 3)
        self.assertEqual(row.vault_ref, expected_ref)
        self.assertEqual(row.status, "accepted")
        host.refresh_from_db()
        self.assertEqual(host.community_hash, new_fp)  # sibling trap host re-pointed
        # the one adapter call carried the plaintext to /api/v1/secrets and nowhere else
        method, url = session.request.call_args[0][:2]
        self.assertEqual((method, url), ("POST", "http://adapter.local/api/v1/secrets"))
        sent = session.request.call_args.kwargs["json"]
        self.assertEqual(sent, {"vault_ref": expected_ref, "values": {"community": "new-c0mmunity"}})

    def test_collision_with_existing_community_fails_before_vault(self):
        mgmt = self._make_mgmt()
        row = self._community(mgmt)
        self._community(mgmt, community_hash=secret_fingerprint("dup-value"))
        session = make_session(json_data={})

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
        ):
            form = self._form(row, {"secret_value": "dup-value"})
            self.assertFalse(form.is_valid())
        self.assertIn("secret_value", form.errors)
        session.request.assert_not_called()  # guard fires BEFORE any Vault write

    def test_adapter_error_becomes_form_error_and_nothing_persists(self):
        mgmt = self._make_mgmt()
        row = self._community(mgmt)
        old_hash = row.community_hash
        session = make_session(status_code=502, json_data={"error": {"code": "vault_error", "message": "denied"}})

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
        ):
            form = self._form(row, {"secret_value": "s3cr3t-value"})
            self.assertFalse(form.is_valid())

        row.refresh_from_db()
        self.assertEqual(row.community_hash, old_hash)  # nothing saved
        self.assertEqual(row.vault_ref, "")
        # the error text never carries the submitted value
        self.assertNotIn("s3cr3t-value", str(form.errors))

    def test_short_ref_is_qualified_from_settings(self):
        mgmt = self._make_mgmt()
        row = self._community(mgmt)
        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent"):
            form = self._form(row, {"vault_ref": "ro-corp"})
            self.assertTrue(form.is_valid(), form.errors)
            form.save()
        row.refresh_from_db()
        self.assertEqual(row.vault_ref, "network/netbox/snmp/community/ro-corp#community")

    def test_secret_without_settings_or_ref_is_rejected(self):
        from netbox_nso_plugin.models import NSOVaultSettings

        NSOVaultSettings.objects.all().delete()
        mgmt = self._make_mgmt()
        row = self._community(mgmt)
        form = self._form(row, {"secret_value": "v"})
        self.assertFalse(form.is_valid())
        self.assertIn("secret_value", form.errors)


class TestV3SecretSet(_SecretBase):
    def _v3(self, mgmt, **kwargs):
        from netbox_nso_plugin.models import NSOSnmpV3UserState

        defaults = {"status": "imported"}
        defaults.update(kwargs)
        return NSOSnmpV3UserState.objects.create(management=mgmt, username="monitor", **defaults)

    def _form(self, instance, data):
        from netbox_nso_plugin.forms import NSOSnmpV3UserStateForm

        base = {
            "group_name": instance.group_name,
            "auth_protocol": instance.auth_protocol,
            "priv_protocol": instance.priv_protocol,
            "vault_ref": instance.vault_ref,
        }
        base.update(data)
        return NSOSnmpV3UserStateForm(data=base, instance=instance)

    def test_set_both_secrets_derives_path_ref_and_flags(self):
        mgmt = self._make_mgmt()
        row = self._v3(mgmt)
        session = make_session(
            json_data={
                "vault_ref": "network/netbox/snmp/v3/monitor",
                "version": 1,
                "hashes": {"auth": "x", "priv": "y"},
            }
        )

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
            patch("netbox_nso_plugin.adapter_client.put_snmp_intent"),
        ):
            form = self._form(
                row,
                {
                    "auth_protocol": "sha-256",
                    "priv_protocol": "aes-128",
                    "auth_secret_value": "auth-pw",
                    "priv_secret_value": "priv-pw",
                },
            )
            self.assertTrue(form.is_valid(), form.errors)
            form.save()

        row.refresh_from_db()
        self.assertEqual(row.vault_ref, "network/netbox/snmp/v3/monitor")  # PATH ref, no #key
        self.assertTrue(row.vault_has_auth)
        self.assertTrue(row.vault_has_priv)
        self.assertEqual(row.status, "accepted")
        sent = session.request.call_args.kwargs["json"]
        self.assertEqual(sent["values"], {"auth": "auth-pw", "priv": "priv-pw"})

    def test_auth_only_set_does_not_claim_priv(self):
        mgmt = self._make_mgmt()
        row = self._v3(mgmt)
        session = make_session(
            json_data={"vault_ref": "network/netbox/snmp/v3/monitor", "version": 2, "hashes": {"auth": "x"}}
        )

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
            patch("netbox_nso_plugin.adapter_client.put_snmp_intent"),
        ):
            form = self._form(row, {"auth_protocol": "sha", "auth_secret_value": "auth-pw"})
            self.assertTrue(form.is_valid(), form.errors)
            form.save()

        row.refresh_from_db()
        self.assertTrue(row.vault_has_auth)
        self.assertFalse(row.vault_has_priv)
        # merge semantics live in the adapter; only 'auth' was sent
        self.assertEqual(session.request.call_args.kwargs["json"]["values"], {"auth": "auth-pw"})

    def test_secret_requires_protocol_and_priv_requires_auth(self):
        mgmt = self._make_mgmt()
        row = self._v3(mgmt)
        form = self._form(row, {"auth_secret_value": "pw"})  # no auth_protocol
        self.assertFalse(form.is_valid())
        self.assertIn("auth_protocol", form.errors)

        form = self._form(row, {"priv_protocol": "aes-128"})  # priv without auth
        self.assertFalse(form.is_valid())
        self.assertIn("priv_protocol", form.errors)


class TestVerifyAndHarvestViews(_SecretBase):
    def test_verify_community_stores_fingerprint(self):
        mgmt = self._make_mgmt()
        row = self._community(
            mgmt, vault_ref="network/netbox/snmp/community/oldhash1234567890#community", status="accepted"
        )
        session = make_session(
            json_data={
                "vault_ref": row.vault_ref,
                "exists": True,
                "fields": ["community"],
                "hashes": {"community": row.community_hash},
                "version": 4,
            }
        )
        self.client.force_login(_superuser())
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
        ):
            resp = self.client.post(f"/plugins/nso/snmp/community-state/{row.pk}/verify-secret/")
        self.assertEqual(resp.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.vault_secret_hash, row.community_hash)
        self.assertEqual(row.vault_secret_version, 4)

    def test_verify_v3_records_field_presence(self):
        from netbox_nso_plugin.models import NSOSnmpV3UserState

        mgmt = self._make_mgmt()
        row = NSOSnmpV3UserState.objects.create(
            management=mgmt, username="monitor", vault_ref="network/netbox/snmp/v3/monitor"
        )
        session = make_session(
            json_data={
                "vault_ref": row.vault_ref,
                "exists": True,
                "fields": ["auth"],
                "hashes": {"auth": "x"},
                "version": 1,
            }
        )
        self.client.force_login(_superuser())
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
        ):
            resp = self.client.post(f"/plugins/nso/snmp/v3-user-state/{row.pk}/verify-secret/")
        self.assertEqual(resp.status_code, 302)
        row.refresh_from_db()
        self.assertTrue(row.vault_has_auth)
        self.assertFalse(row.vault_has_priv)

    def test_harvest_derives_ref_and_stores_result(self):
        mgmt = self._make_mgmt(adapter_device_id=4202)
        row = self._community(mgmt)
        expected_ref = f"network/netbox/snmp/community/{row.community_hash}#community"
        session = make_session(
            json_data={
                "vault_ref": expected_ref,
                "secret_hash": row.community_hash,
                "version": 1,
                "access": "RO",
                "acl": None,
            }
        )
        self.client.force_login(_superuser())
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
        ):
            resp = self.client.post(f"/plugins/nso/snmp/community-state/{row.pk}/harvest-secret/")
        self.assertEqual(resp.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.vault_ref, expected_ref)
        self.assertEqual(row.vault_secret_hash, row.community_hash)
        method, url = session.request.call_args[0][:2]
        self.assertEqual(
            (method, url),
            ("POST", f"http://adapter.local/api/v1/devices/{mgmt.adapter_device_id}/secrets/harvest-community"),
        )

    def test_harvest_without_adapter_link_errors_cleanly(self):
        mgmt = self._make_mgmt(adapter_device_id=None)
        mgmt.adapter_device_id = None
        mgmt.save()
        row = self._community(mgmt)
        self.client.force_login(_superuser())
        resp = self.client.post(f"/plugins/nso/snmp/community-state/{row.pk}/harvest-secret/", follow=True)
        self.assertEqual(resp.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.vault_ref, "")


class TestDeletePropagation(_SecretBase):
    def test_deleting_owned_community_pushes_reduced_intent(self):
        """WP7 live finding: SNMP states only wired post_save — deleting an owned
        community never re-pushed the intent, so the adapter kept applying the
        deleted community until some unrelated SNMP row was saved. Deleting must
        push the REDUCED snapshot (the adapter's removal propagation then
        replace-applies it off the device)."""
        from netbox_nso_plugin.signals import reset_intent_push_state

        mgmt = self._make_mgmt()
        row = self._community(
            mgmt, status="accepted", vault_ref="network/netbox/snmp/community/oldhash1234567890#community"
        )
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                row.delete()
        mock_put.assert_called_once()
        communities = mock_put.call_args[0][1]
        self.assertEqual(communities, [])


class TestV3PushDerivation(_SecretBase):
    def test_push_derives_auth_priv_refs_from_protocols_and_skips_v3_hosts(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOSnmpHostState, NSOSnmpV3UserState
        from netbox_nso_plugin.signals import reset_intent_push_state

        mgmt = self._make_mgmt(adapter_device_id=4203)
        NSOSnmpV3UserState.objects.create(
            management=mgmt,
            username="monitor",
            status="accepted",
            vault_ref="network/netbox/snmp/v3/monitor",
            group_name="v3-test-group",
            auth_protocol="sha-256",
            priv_protocol="",  # no priv protocol → priv ref must be withheld
        )
        NSOSnmpHostState.objects.create(
            management=mgmt, address="10.0.0.5", version="v3", notify_type="trap", status="accepted"
        )
        NSOSnmpHostState.objects.create(
            management=mgmt,
            address="10.0.0.6",
            version="v2c",
            notify_type="trap",
            status="accepted",
            community_hash="oldhash1234567890",
            port=1162,
        )
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            deliver("snmp", self.device.pk, mgmt.adapter_device_id)
        mock_put.assert_called_once()
        self.assertEqual(mock_put.call_args.args[0], mgmt.adapter_device_id)
        _, communities, v3_users, hosts, _ = mock_put.call_args[0]
        self.assertEqual(
            v3_users,
            [
                {
                    "username": "monitor",
                    "group": "v3-test-group",
                    "auth_protocol": "sha-256",
                    "priv_protocol": None,
                    "auth_vault_ref": "network/netbox/snmp/v3/monitor#auth",
                    "priv_vault_ref": None,
                }
            ],
        )
        # CR-P16: a v3 host with NO user name is still refused — both NSO writers key the receiver
        # on that field, so pushing it would key the host on an empty user. (A v3 host WITH a user
        # name now pushes; see test_a_v3_host_WITH_a_user_name_is_pushed below.)
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0]["address"], "10.0.0.6")
        self.assertEqual(hosts[0]["port"], 1162)
        self.assertEqual(hosts[0]["community_or_user"], "oldhash1234567890")
