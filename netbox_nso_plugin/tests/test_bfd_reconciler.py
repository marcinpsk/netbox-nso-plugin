# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for bfd_reconciler.reconcile_bfd — shared-profile dedup + BFDInterface."""

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Platform, Site
from django.test import TestCase

from ._outbox_case import content_update
from .mixins import IntentPushResetMixin


def _make_device(suffix="bfd"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"BfdMfg{suffix}", slug=f"bfdmfg{suffix}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"BfdDev{suffix}", slug=f"bfddev{suffix}")
    role, _ = DeviceRole.objects.get_or_create(name=f"BfdRole{suffix}", slug=f"bfdrole{suffix}")
    site, _ = Site.objects.get_or_create(name=f"BfdSite{suffix}", slug=f"bfdsite{suffix}")
    return Device.objects.create(name=f"bfd-router-{suffix}", device_type=dt, role=role, site=site)


class TestReconcileBfd(TestCase):
    """Integration tests for reconcile_bfd() — real Django DB + netbox_routing."""

    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("main")
        cls.ae1 = Interface.objects.create(device=cls.device, name="ae1", type="lag")
        cls.ae2 = Interface.objects.create(device=cls.device, name="ae2", type="lag")
        cls.ge0 = Interface.objects.create(device=cls.device, name="GigabitEthernet0/0", type="1000base-t")

    def _entry(self, name, micro=True, tx=300, rx=300, mult=3, **kw):
        e = {"interface_name": name, "micro_bfd": micro, "enabled": True}
        if tx is not None:
            e["min_tx"] = tx
        if rx is not None:
            e["min_rx"] = rx
        if mult is not None:
            e["multiplier"] = mult
        e.update(kw)
        return e

    def test_shared_profile_deduped(self):
        """Two interfaces with the same timer-set link ONE shared BFDProfile."""
        from netbox_routing.models import BFDInterface, BFDProfile

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd

        reconcile_bfd(self.device, [self._entry("ae1"), self._entry("ae2")])
        self.assertEqual(BFDProfile.objects.filter(name="bfd-300-300-x3").count(), 1)
        self.assertEqual(BFDInterface.objects.filter(interface__device=self.device).count(), 2)
        profiles = {b.bfd_profile_id for b in BFDInterface.objects.filter(interface__device=self.device)}
        self.assertEqual(len(profiles), 1)  # both share the one profile

    def test_micro_vs_normal_recorded(self):
        from netbox_routing.models import BFDInterface

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd

        reconcile_bfd(
            self.device,
            [self._entry("ae1", micro=True), self._entry("GigabitEthernet0/0", micro=False, tx=750, rx=750, mult=4)],
        )
        self.assertTrue(BFDInterface.objects.get(interface=self.ae1).micro_bfd)
        self.assertFalse(BFDInterface.objects.get(interface=self.ge0).micro_bfd)

    def test_missing_timers_no_profile(self):
        """An entry without full timers still records the interface, with no profile."""
        from netbox_routing.models import BFDInterface

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd

        reconcile_bfd(self.device, [self._entry("ae1", tx=None, rx=None, mult=None)])
        bi = BFDInterface.objects.get(interface=self.ae1)
        self.assertIsNone(bi.bfd_profile_id)
        self.assertTrue(bi.micro_bfd)

    def test_stale_pruned(self):
        """An interface that stops reporting BFD has its BFDInterface removed."""
        from netbox_routing.models import BFDInterface

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd

        reconcile_bfd(self.device, [self._entry("ae1"), self._entry("ae2")])
        reconcile_bfd(self.device, [self._entry("ae1")])
        names = set(
            BFDInterface.objects.filter(interface__device=self.device).values_list("interface__name", flat=True)
        )
        self.assertEqual(names, {"ae1"})

    def test_unmodelled_interface_skipped(self):
        from netbox_routing.models import BFDInterface

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd

        reconcile_bfd(self.device, [self._entry("ae99")])  # not a NetBox interface
        self.assertEqual(BFDInterface.objects.filter(interface__device=self.device).count(), 0)


class TestBfdWritePath(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        cls.device = _make_device("wp")
        cls.iface = Interface.objects.create(device=cls.device, name="Port-channel1", type="lag")
        cls.instance = NSOInstance.objects.create(name="nso-bwp", adapter_instance_id="nso-bwp")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="bfd-router-wp", adapter_device_id=88
        )

    def test_reconcile_creates_overlay_imported(self):
        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd
        from netbox_nso_plugin.models import NSOBFDInterfaceState

        reconcile_bfd(
            self.device,
            [
                {
                    "interface_name": "Port-channel1",
                    "micro_bfd": True,
                    "enabled": True,
                    "min_tx": 300,
                    "min_rx": 300,
                    "multiplier": 3,
                },
            ],
        )
        st = NSOBFDInterfaceState.objects.get(management=self.management, interface=self.iface)
        assert st.status == "imported" and st.min_tx == 300 and st.multiplier == 3 and st.micro_bfd is True

    def test_reconcile_preserves_owned_status(self):
        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd
        from netbox_nso_plugin.models import NSOBFDInterfaceState

        NSOBFDInterfaceState.objects.create(
            management=self.management, interface=self.iface, min_tx=300, min_rx=300, multiplier=3, status="accepted"
        )
        reconcile_bfd(
            self.device,
            [
                {"interface_name": "Port-channel1", "micro_bfd": True, "min_tx": 300, "min_rx": 300, "multiplier": 3},
            ],
        )
        assert NSOBFDInterfaceState.objects.get(interface=self.iface).status == "accepted"

    def test_empty_interface_name_does_not_use_bound_port_only_in_the_plan(self):
        from netbox_nso_plugin.bfd_reconciler import bfd_reconcile_plan, reconcile_bfd
        from netbox_nso_plugin.models import NSOBFDInterfaceState

        full = {
            "interface_name": "Port-channel1",
            "bound_port": "Port-channel1",
            "micro_bfd": True,
            "min_tx": 300,
            "min_rx": 300,
            "multiplier": 3,
        }
        reconcile_bfd(self.device, [full])
        state = NSOBFDInterfaceState.objects.get(management=self.management, interface=self.iface)
        state.status = "in_sync"
        state.save(update_fields=["status"])

        empty_name = [{**full, "interface_name": ""}]
        self.assertTrue(bfd_reconcile_plan(self.device, empty_name).changes_content)
        reconcile_bfd(self.device, empty_name)

        state.refresh_from_db()
        self.assertEqual(state.status, "changed")

    def test_nokia_default_timer_omission_preserves_owned_profile_without_false_settle(self):
        """Omission preserves intent but cannot prove an explicit-default push landed."""
        from netbox_routing.models import BFDInterface

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd
        from netbox_nso_plugin.models import NSOBFDInterfaceState, NSOPlatformNedMapping

        platform = Platform.objects.create(name="BFD Nokia", slug="bfd-nokia")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")

        reconcile_bfd(
            self.device,
            [
                {
                    "interface_name": "Port-channel1",
                    "micro_bfd": True,
                    "enabled": True,
                    "min_tx": 100,
                    "min_rx": 100,
                    "multiplier": 3,
                }
            ],
        )
        state = NSOBFDInterfaceState.objects.get(management=self.management, interface=self.iface)
        state.status = "accepted"
        state.save(update_fields=["status"])
        profile_id = BFDInterface.objects.get(interface=self.iface).bfd_profile_id

        reconcile_bfd(
            self.device,
            [{"interface_name": "Port-channel1", "micro_bfd": True, "enabled": True}],
        )

        state.refresh_from_db()
        native = BFDInterface.objects.get(interface=self.iface)
        self.assertEqual((state.min_tx, state.min_rx, state.multiplier), (100, 100, 3))
        self.assertEqual(native.bfd_profile_id, profile_id)
        self.assertEqual(state.status, "accepted")

    def test_junos_omitted_default_multiplier_preserves_owned_profile_without_false_settle(self):
        from netbox_routing.models import BFDInterface

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd
        from netbox_nso_plugin.models import NSOBFDInterfaceState, NSOPlatformNedMapping

        platform = Platform.objects.create(name="BFD Junos", slug="bfd-junos")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="juniper-junos-nc-4.19")
        full = {
            "interface_name": "Port-channel1",
            "micro_bfd": True,
            "enabled": True,
            "min_tx": 300,
            "min_rx": 300,
            "multiplier": 3,
        }
        reconcile_bfd(self.device, [full])
        state = NSOBFDInterfaceState.objects.get(management=self.management, interface=self.iface)
        state.status = "accepted"
        state.save(update_fields=["status"])
        profile_id = BFDInterface.objects.get(interface=self.iface).bfd_profile_id
        corrected = dict(full)
        corrected.pop("multiplier")

        reconcile_bfd(self.device, [corrected])

        state.refresh_from_db()
        self.assertEqual(state.multiplier, 3)
        self.assertEqual(BFDInterface.objects.get(interface=self.iface).bfd_profile_id, profile_id)
        self.assertEqual(state.status, "accepted")

    def test_device_refresh_cannot_clobber_owned_native_bfd_fields(self):
        """The native routing row follows owned intent, not stale device state."""
        from netbox_routing.models import BFDInterface, BFDProfile

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd
        from netbox_nso_plugin.models import NSOBFDInterfaceState

        reconcile_bfd(
            self.device,
            [
                {
                    "interface_name": "Port-channel1",
                    "micro_bfd": False,
                    "enabled": False,
                    "min_tx": 300,
                    "min_rx": 300,
                    "multiplier": 3,
                }
            ],
        )
        intended_profile = BFDProfile.objects.create(
            name="bfd-100-100-x5",
            min_tx_int=100,
            min_rx_int=100,
            multiplier=5,
        )
        native = BFDInterface.objects.get(interface=self.iface)
        native.bfd_profile = intended_profile
        native.micro_bfd = True
        native.enabled = True
        native.save(update_fields=["bfd_profile", "micro_bfd", "enabled"])
        state = NSOBFDInterfaceState.objects.get(management=self.management, interface=self.iface)
        state = content_update(
            state,
            min_tx=100,
            min_rx=100,
            multiplier=5,
            micro_bfd=True,
            status="accepted",
        )

        reconcile_bfd(
            self.device,
            [
                {
                    "interface_name": "Port-channel1",
                    "micro_bfd": False,
                    "enabled": False,
                    "min_tx": 300,
                    "min_rx": 300,
                    "multiplier": 3,
                }
            ],
        )

        native.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(native.bfd_profile_id, intended_profile.pk)
        self.assertTrue(native.micro_bfd)
        self.assertTrue(native.enabled)
        self.assertEqual(state.status, "accepted")

    def test_owned_state_survives_when_interface_drops_from_payload(self):
        """An owned BFD overlay must NOT be hard-deleted when the device stops reporting it.

        The scope is in ``_prepare_apply``/`_APPLY_DEPLOYING_SCOPES``, so a bulk delete of
        stale rows destroys the in-flight Apply marker + operator ownership. Intent-pending
        rows (deploying) are kept; a confirmed row (in_sync) that vanishes surfaces as drift
        (``changed``), never data-loss.
        """
        from uuid import uuid4

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd
        from netbox_nso_plugin.models import NSOBFDInterfaceState

        deploying = NSOBFDInterfaceState.objects.create(
            management=self.management,
            interface=self.iface,
            min_tx=300,
            min_rx=300,
            multiplier=3,
            status="deploying",
            apply_attempt_id=uuid4(),
        )
        ge = Interface.objects.create(device=self.device, name="Gi7/7", type="1000base-t")
        confirmed = NSOBFDInterfaceState.objects.create(
            management=self.management, interface=ge, min_tx=300, min_rx=300, multiplier=3, status="in_sync"
        )
        reconcile_bfd(self.device, [])  # device no longer reports BFD on any interface
        assert NSOBFDInterfaceState.objects.filter(pk=deploying.pk).exists(), (
            "deploying (apply-in-flight) overlay deleted"
        )
        assert NSOBFDInterfaceState.objects.filter(pk=confirmed.pk).exists(), "in_sync overlay deleted"
        assert NSOBFDInterfaceState.objects.get(pk=deploying.pk).status == "deploying"
        assert NSOBFDInterfaceState.objects.get(pk=confirmed.pk).status == "changed"

    def test_matching_timers_keep_a_deploying_row_in_flight(self):
        """Re-reading the intended timers is not apply evidence for an in-flight BFD row."""
        from uuid import uuid4

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd
        from netbox_nso_plugin.models import NSOBFDInterfaceState

        attempt = uuid4()
        deploying = NSOBFDInterfaceState.objects.create(
            management=self.management,
            interface=self.iface,
            min_tx=300,
            min_rx=300,
            multiplier=3,
            micro_bfd=True,
            status="deploying",
            apply_attempt_id=attempt,
        )

        reconcile_bfd(
            self.device,
            [
                {
                    "interface_name": "Port-channel1",
                    "micro_bfd": True,
                    "enabled": True,
                    "min_tx": 300,
                    "min_rx": 300,
                    "multiplier": 3,
                }
            ],
        )

        deploying.refresh_from_db()
        assert deploying.status == "deploying"
        assert deploying.apply_attempt_id == attempt

    def test_push_builds_owned_snapshot(self):
        from unittest.mock import patch

        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOBFDInterfaceState
        from netbox_nso_plugin.signals import reset_intent_push_state

        NSOBFDInterfaceState.objects.create(
            management=self.management,
            interface=self.iface,
            min_tx=300,
            min_rx=300,
            multiplier=3,
            micro_bfd=True,
            status="accepted",
        )
        ge = Interface.objects.create(device=self.device, name="Gi9/9", type="1000base-t")
        NSOBFDInterfaceState.objects.create(
            management=self.management,
            interface=ge,
            min_tx=100,
            status="imported",  # not owned → excluded
        )
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_bfd_intent") as mock_put:
            deliver("bfd", self.device.pk, 88)
        mock_put.assert_called_once()
        ifaces = mock_put.call_args[0][1]
        assert [i["interface_name"] for i in ifaces] == ["Port-channel1"]
        assert ifaces[0]["min_tx"] == 300 and ifaces[0]["multiplier"] == 3 and ifaces[0]["micro_bfd"] is True

    def test_accept_marks_owned(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model

        from netbox_nso_plugin.models import NSOBFDInterfaceState

        state = NSOBFDInterfaceState.objects.create(
            management=self.management, interface=self.iface, min_tx=300, min_rx=300, multiplier=3, status="conflict"
        )
        User = get_user_model()
        admin = User.objects.create_superuser(username="bfd-admin", password="pw", email="b@x.y")  # noqa: S106
        self.client.force_login(admin)
        with patch("netbox_nso_plugin.adapter_client.put_bfd_intent"):
            resp = self.client.post(f"/plugins/nso/bfd/state/{state.pk}/accept/")
        assert resp.status_code == 302
        state.refresh_from_db()
        assert state.status == "accepted" and state.accepted_at is not None
