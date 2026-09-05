# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Phase 1: SNMP overlay accept + edit + deferred intent push (operator write path)."""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase

from netbox_nso_plugin.vault_refs import secret_fingerprint

from ._adapter_http import make_session
from ._outbox_case import without_commit_drain
from .mixins import IntentPushDeliveryMixin, IntentPushResetMixin, _CascadeFlushMixin

_BASE_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


def _superuser():
    User = get_user_model()
    return User.objects.create_superuser(username="snmp-admin", password="pw", email="snmp@test.x")  # noqa: S106


class _SnmpBase(IntentPushDeliveryMixin, TestCase):
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
        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSOSnmpCommunityState
        from netbox_nso_plugin.signals import _on_snmp_state_save, reset_intent_push_state

        mgmt = self._make_mgmt()
        c = self._community(mgmt, status="accepted", vault_ref="secret/snmp#community")
        # Creating the row already fired the real signal (coalesced on the rolled-back
        # test txn); clear that stale coalescing state before the assertion run.
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                with intent_transaction(footprint_for_instance(c)):
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


class TestSnmpUnpushableRowsAreRefusedNotDowngraded(_SnmpBase):
    """A FULL-REPLACE snapshot has no way to say "leave this one alone": a row the push
    skips is a shrink the adapter detaches, and a row pushed with missing fields REWRITES
    the device with those fields absent. So a row that cannot be faithfully pushed must be
    refused at accept and surfaced — never accepted-then-silently-dropped, and never
    pushed in a degraded form.
    """

    def _v3_user(self, mgmt, **kwargs):
        from netbox_nso_plugin.models import NSOSnmpV3UserState

        fields = {
            "management": mgmt,
            "username": "nms-ro",
            "has_auth_secret": True,
            "has_priv_secret": True,
            "vault_ref": "network/netbox/snmp/v3/nms",
            "status": "imported",
        }
        fields.update(kwargs)
        return NSOSnmpV3UserState.objects.create(**fields)

    def _host(self, mgmt, **kwargs):
        from netbox_nso_plugin.models import NSOSnmpHostState

        fields = {
            "management": mgmt,
            "address": "198.18.9.9",
            "version": "v3",
            "notify_type": "trap",
            "status": "imported",
        }
        fields.update(kwargs)
        return NSOSnmpHostState.objects.create(**fields)

    def test_accepting_a_v3_user_without_its_protocols_is_refused(self):
        """The read mirror reports only THAT the device holds auth/priv secrets, never which
        protocols — so an imported v3 user always arrives with auth_protocol=''. Accepting it
        as-is pushed auth_protocol=null and the apply rewrote an authPriv user as
        noAuthNoPriv: a silent security downgrade of the live device."""
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.signals import reset_intent_push_state

        mgmt = self._make_mgmt()
        user = self._v3_user(mgmt)
        revision, _created = NSOIntentRevision.objects.get_or_create(device=mgmt.device, scope="snmp")
        before = revision.revision
        self.client.force_login(_superuser())
        reset_intent_push_state()

        # Drain on_commit, or "no push happened" would be true no matter what the view did.
        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(f"/plugins/nso/snmp/v3-user-state/{user.pk}/accept/")

        assert resp.status_code == 302
        user.refresh_from_db()
        revision.refresh_from_db()
        assert user.status == "imported", f"the accept must be refused, not recorded (status={user.status})"
        assert revision.revision == before, "a refused accept committed an intent revision"
        mock_put.assert_not_called()

    def test_refused_v3_user_accept_does_not_create_an_intent_revision(self):
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.signals import reset_intent_push_state

        mgmt = self._make_mgmt()
        user = self._v3_user(mgmt)
        # Model creation initializes the scope. Remove that setup artifact to exercise
        # a refused accept against an imported row whose revision has not been created.
        NSOIntentRevision.objects.filter(device=mgmt.device, scope="snmp").delete()
        assert not NSOIntentRevision.objects.filter(device=mgmt.device, scope="snmp").exists()
        self.client.force_login(_superuser())
        reset_intent_push_state()

        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(f"/plugins/nso/snmp/v3-user-state/{user.pk}/accept/")

        assert resp.status_code == 302
        user.refresh_from_db()
        assert user.status == "imported"
        assert not NSOIntentRevision.objects.filter(device=mgmt.device, scope="snmp").exists()
        mock_put.assert_not_called()

    def test_accepting_a_v3_user_with_its_protocols_declared_pushes_them(self):
        from netbox_nso_plugin.signals import reset_intent_push_state

        mgmt = self._make_mgmt()
        user = self._v3_user(mgmt, auth_protocol="sha", priv_protocol="aes-128")
        self.client.force_login(_superuser())
        # Creating the row already fired the real signal (coalesced on the rolled-back test
        # txn); clear that stale coalescing state, then drain this accept's on_commit push.
        reset_intent_push_state()

        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(f"/plugins/nso/snmp/v3-user-state/{user.pk}/accept/")

        assert resp.status_code == 302
        user.refresh_from_db()
        # Accepting a row that already MATCHES the device lands in_sync — still an owned
        # status, so it is pushed to record ownership (_status_after_accept).
        assert user.status == "in_sync"
        mock_put.assert_called_once()
        pushed = mock_put.call_args[0][2]  # (adapter_device_id, communities, v3_users, ...)
        assert pushed == [
            {
                "username": "nms-ro",
                "group": None,
                "auth_protocol": "sha",
                "priv_protocol": "aes-128",
                "auth_vault_ref": "network/netbox/snmp/v3/nms#auth",
                "priv_vault_ref": "network/netbox/snmp/v3/nms#priv",
            }
        ]

    def test_an_already_owned_v3_user_missing_protocols_is_never_pushed_degraded(self):
        """Defence in depth for rows owned before the accept-time guard existed (or via the
        API): the snapshot builder must drop them AND surface them, not emit a null protocol."""
        from netbox_nso_plugin.delivery import deliver

        mgmt = self._make_mgmt()
        user = self._v3_user(mgmt, status="accepted")  # owned, protocols never declared

        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            deliver("snmp", mgmt.device_id, mgmt.adapter_device_id)

        mock_put.assert_called_once()
        assert mock_put.call_args[0][2] == [], "a protocol-less v3 user must not reach the device"
        user.refresh_from_db()
        assert user.status == "error", f"the dropped row must be surfaced, not left green (status={user.status})"

    def test_an_owned_community_without_a_vault_reference_blocks_the_snapshot(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.delivery import deliver

        mgmt = self._make_mgmt()
        self._community(mgmt, status="accepted", vault_ref="")

        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            with self.assertRaisesRegex(AdapterError, "SNMP snapshot is blocked") as raised:
                deliver("snmp", mgmt.device_id, mgmt.adapter_device_id)

        assert raised.exception.code == "validation_error"
        mock_put.assert_not_called()

    def test_an_owned_v3_user_without_a_vault_reference_blocks_the_snapshot(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.delivery import deliver

        mgmt = self._make_mgmt()
        self._v3_user(
            mgmt,
            status="accepted",
            auth_protocol="sha",
            priv_protocol="aes-128",
            vault_ref="",
        )

        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            with self.assertRaisesRegex(AdapterError, "SNMP snapshot is blocked") as raised:
                deliver("snmp", mgmt.device_id, mgmt.adapter_device_id)

        assert raised.exception.code == "validation_error"
        mock_put.assert_not_called()

    def test_accepting_a_v3_trap_host_is_refused(self):
        """The host overlay has no v3 username source, so a v3 host can only ever be pushed
        keyed on an empty user. It used to accept, then be dropped with a server-side log
        line — leaving a row that read 'accepted' forever while nothing was applied."""
        from netbox_nso_plugin.signals import reset_intent_push_state

        mgmt = self._make_mgmt()
        host = self._host(mgmt)
        self.client.force_login(_superuser())
        reset_intent_push_state()

        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(f"/plugins/nso/snmp/host-state/{host.pk}/accept/")

        assert resp.status_code == 302
        host.refresh_from_db()
        assert host.status == "imported", f"the accept must be refused (status={host.status})"
        mock_put.assert_not_called()

    def test_a_v2c_trap_host_still_accepts_and_pushes(self):
        from netbox_nso_plugin.signals import reset_intent_push_state

        mgmt = self._make_mgmt()
        host = self._host(mgmt, version="v2c", community_hash="abcd1234abcd1234")
        self.client.force_login(_superuser())
        reset_intent_push_state()  # drop the coalescing state left by the row creation

        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(f"/plugins/nso/snmp/host-state/{host.pk}/accept/")

        assert resp.status_code == 302
        host.refresh_from_db()
        assert host.status == "in_sync"  # accepted-as-matching is still owned
        mock_put.assert_called_once()
        assert [h["address"] for h in mock_put.call_args[0][3]] == ["198.18.9.9"]


class TestCommunityRekeyReReadsTrapHostsUnderTheLock(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """A trap host committed between host discovery and lock acquisition is still re-pointed.

    Trap hosts name their community by hash. Discovering the matching hosts BEFORE
    ``intent_transaction`` froze that set, so a host the SNMP reconciler committed in the gap
    kept the rotated-away hash and the next push named a community that no longer exists.
    """

    def setUp(self):
        super().setUp()
        from django.db import transaction

        from netbox_nso_plugin.models import (
            NSODeviceManagement,
            NSOInstance,
            NSOSnmpCommunityState,
            NSOSnmpHostState,
            NSOVaultSettings,
        )

        with without_commit_drain(), transaction.atomic():
            mfg = Manufacturer.objects.create(name="SnmpRaceMfg", slug="snmpracemfg")
            dt = DeviceType.objects.create(manufacturer=mfg, model="SnmpRaceDev", slug="snmpracedev")
            role = DeviceRole.objects.create(name="SnmpRaceRole", slug="snmpracerole")
            site = Site.objects.create(name="SnmpRaceSite", slug="snmpracesite")
            self.device = Device.objects.create(name="snmp-race-rtr", device_type=dt, role=role, site=site)
            NSOVaultSettings.objects.create(kv_mount="network", base_path="netbox/snmp", enabled=True)
            inst = NSOInstance.objects.create(name="snmp-race-inst", adapter_instance_id="snmp-race-inst")
            self.mgmt = NSODeviceManagement.objects.create(
                device=self.device,
                nso_instance=inst,
                nso_device_name="nso-snmp-race",
                adapter_device_id=77,
                manage_snmp=True,
            )
            self.old_hash = "oldhash1234567890"
            self.community = NSOSnmpCommunityState.objects.create(
                management=self.mgmt,
                community_hash=self.old_hash,
                access="RO",
                status="imported",
            )
            NSOSnmpHostState.objects.create(
                management=self.mgmt,
                address="198.51.100.1",
                version="v2c",
                notify_type="trap",
                community_hash=self.old_hash,
                status="imported",
            )

    def test_a_trap_host_committed_during_the_lock_gap_is_still_repointed(self):
        import contextlib
        import threading
        from threading import Barrier, Thread

        from django.db import close_old_connections, connections, transaction

        from netbox_nso_plugin import intent_state
        from netbox_nso_plugin.forms import NSOSnmpCommunityStateForm
        from netbox_nso_plugin.models import NSOSnmpHostState

        new_hash = secret_fingerprint("rotated-c0mmunity")
        expected_ref = f"network/netbox/snmp/community/{new_hash}#community"
        session = make_session(json_data={"vault_ref": expected_ref, "version": 4, "hashes": {"community": new_hash}})
        gap_open = Barrier(2)
        gap_closed = Barrier(2)
        results = {}
        saver_thread = threading.get_ident()
        real_intent_transaction = intent_state.intent_transaction

        def hold_the_gap_open(footprint):
            """Let the racing writer commit in the window before the rekey takes its locks."""
            if threading.get_ident() == saver_thread and not results.get("gap_used"):
                results["gap_used"] = True
                gap_open.wait(timeout=30)
                gap_closed.wait(timeout=30)
            return real_intent_transaction(footprint)

        def commit_racing_host():
            close_old_connections()
            try:
                gap_open.wait(timeout=30)
                with transaction.atomic():
                    NSOSnmpHostState.objects.create(
                        management=self.mgmt,
                        address="198.51.100.2",
                        version="v2c",
                        notify_type="trap",
                        community_hash=self.old_hash,
                        status="imported",
                    )
            except Exception as exc:  # noqa: BLE001 — surfaced by the main test thread
                results["racing_error"] = exc
            finally:
                with contextlib.suppress(Exception):
                    gap_closed.wait(timeout=30)
                connections.close_all()

        racer = Thread(target=commit_racing_host)
        with (
            without_commit_drain(),
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
            patch("netbox_nso_plugin.intent_state.intent_transaction", hold_the_gap_open),
        ):
            form = NSOSnmpCommunityStateForm(
                data={"access": "RO", "acl": "", "vault_ref": "", "secret_value": "rotated-c0mmunity"},
                instance=self.community,
            )
            self.assertTrue(form.is_valid(), form.errors)
            racer.start()
            try:
                form.save()
            finally:
                racer.join(timeout=60)

        self.assertFalse(racer.is_alive(), "the racing writer did not finish")
        self.assertNotIn("racing_error", results, results.get("racing_error"))
        self.assertTrue(results.get("gap_used"), "the rekey never reached the lock-acquisition seam")
        self.community.refresh_from_db()
        self.assertEqual(self.community.community_hash, new_hash)
        self.assertEqual(
            sorted(NSOSnmpHostState.objects.filter(management=self.mgmt).values_list("address", "community_hash")),
            [("198.51.100.1", new_hash), ("198.51.100.2", new_hash)],
            "a trap host was left pointing at the rotated-away community",
        )

    def test_a_deploying_trap_host_is_settled_by_the_rekey(self):
        """Every re-pointed host gets the lifecycle reset ``_acquire`` gives the rows it names.

        ``_acquire`` skips the table sentinel, and the sentinel is all the rekey footprint can
        carry, so the rekey has to settle the hosts itself. Left ``deploying``, a re-pointed host
        is invisible to the next Apply (promotion selects only accepted/apply_failed) while the
        superseded attempt's evidence can still fail it.
        """
        from django.db import transaction

        from netbox_nso_plugin.forms import NSOSnmpCommunityStateForm
        from netbox_nso_plugin.models import NSOSnmpHostState

        with without_commit_drain(), transaction.atomic():
            NSOSnmpHostState.objects.create(
                management=self.mgmt,
                address="198.51.100.3",
                version="v2c",
                notify_type="trap",
                community_hash=self.old_hash,
                status="deploying",
            )

        new_hash = secret_fingerprint("settled-c0mmunity")
        expected_ref = f"network/netbox/snmp/community/{new_hash}#community"
        session = make_session(json_data={"vault_ref": expected_ref, "version": 5, "hashes": {"community": new_hash}})
        with (
            without_commit_drain(),
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
        ):
            form = NSOSnmpCommunityStateForm(
                data={"access": "RO", "acl": "", "vault_ref": "", "secret_value": "settled-c0mmunity"},
                instance=self.community,
            )
            self.assertTrue(form.is_valid(), form.errors)
            form.save()

        self.assertEqual(
            sorted(
                NSOSnmpHostState.objects.filter(management=self.mgmt).values_list("address", "community_hash", "status")
            ),
            [("198.51.100.1", new_hash, "imported"), ("198.51.100.3", new_hash, "accepted")],
            "a re-pointed trap host kept the superseded apply attempt's lifecycle",
        )
