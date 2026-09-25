# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""switchport intent push + accept->apply round-trip."""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase, TransactionTestCase
from ipam.models import VLAN, VLANGroup

from ._outbox_case import ReceiptAdapter, make_managed, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class _SwBase(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="SwSigMfg", slug="swsigmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SwSigDev", slug="swsigdev")
        role = DeviceRole.objects.create(name="SwSigRole", slug="swsigrole")
        site = Site.objects.create(name="SwSigSite", slug="swsigsite")
        cls.device = Device.objects.create(name="sw-sig", device_type=dt, role=role, site=site)
        cls.group = VLANGroup.objects.create(name="g", slug=f"nso-{cls.device.pk}")
        cls.v10 = VLAN.objects.create(group=cls.group, vid=10, name="MGMT")
        # `mode` and `untagged_vlan` are the native anchor the switchport binding reads:
        # without them the overlay owns nothing the device carries and the audit demotes it.
        cls.iface = Interface.objects.create(
            device=cls.device,
            name="GigabitEthernet0/1",
            type="1000base-t",
            mode="access",
            untagged_vlan=cls.v10,
        )

    def _make_mgmt(self, adapter_device_id=42, auto_apply=False):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="sw-sig-inst", defaults={"adapter_instance_id": "sw-sig-inst"})
        mgmt = NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "nso-sw-sig",
                "adapter_device_id": adapter_device_id,
                "manage_interfaces": True,
            },
        )[0]
        if mgmt.auto_apply != auto_apply:
            mgmt.auto_apply = auto_apply
            mgmt.save(update_fields=["auto_apply"])
        return mgmt

    def _state(self, mgmt, mode="access", status="changed"):
        from netbox_nso_plugin.models import NSOSwitchportState

        return NSOSwitchportState.objects.create(
            management=mgmt,
            interface=self.iface,
            mode=mode,
            untagged_vlan=self.v10 if mode == "access" else None,
            status=status,
        )


class TestPushSwitchportIntent(_SwBase):
    def test_pushes_owned_switchports_mapped_to_nso_mode(self):
        from netbox_nso_plugin.delivery import render

        mgmt = self._make_mgmt()
        self._state(mgmt, mode="access", status="accepted")
        ifaces = render("switchport", self.device.pk, mgmt.adapter_device_id).payload
        assert ifaces[0]["interface_name"] == "GigabitEthernet0/1"
        assert ifaces[0]["mode"] == "access"  # NetBox access -> NSO access
        assert ifaces[0]["untagged_vlan"] == 10

    def test_excludes_non_owned(self):
        from netbox_nso_plugin.delivery import render

        mgmt = self._make_mgmt()
        self._state(mgmt, status="changed")  # not owned
        assert render("switchport", self.device.pk, mgmt.adapter_device_id).payload == []

    def test_save_no_push_without_auto_apply(self):
        """Deferred flow: a switchport save does not commit to the device unless
        auto-apply is on — the single device Apply commits it."""
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.signals import _on_switchport_state_save

        mgmt = self._make_mgmt(auto_apply=False)
        st = NSOSwitchportState(management=mgmt, interface=self.iface, mode="access", status="accepted")
        with patch("netbox_nso_plugin.adapter_client.apply_switchport_config") as mock_apply:
            with self.captureOnCommitCallbacks(execute=True):
                _on_switchport_state_save(sender=NSOSwitchportState, instance=st)
            mock_apply.assert_not_called()

    def test_foreign_overlay_save_does_not_schedule_switchport_behavior(self):
        mgmt = self._make_mgmt(auto_apply=True)
        state = self._state(mgmt, status="accepted")

        with patch("netbox_nso_plugin.signals._schedule_intent_push") as schedule:
            state.mode = "tagged-all"
            state.save(update_fields=("mode",))

        schedule.assert_not_called()


class TestSwitchportWriterScheduling(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        self.device, self.mgmt = make_managed("switching-writer", 4217)
        self.mgmt.auto_apply = True
        self.mgmt.save(update_fields=["auto_apply"])
        with without_commit_drain():
            group = VLANGroup.objects.create(name="switching-writer", slug="switching-writer")
            self.vlan = VLAN.objects.create(group=group, vid=10, name="MGMT")
            self.other_vlan = VLAN.objects.create(group=group, vid=20, name="DATA")
            self.iface = Interface.objects.create(
                device=self.device,
                name="Ethernet1",
                type="1000base-t",
                mode="access",
                untagged_vlan=self.vlan,
            )

    def test_tagged_vlan_writer_change_schedules_switchport_preparation(self):
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_m2m_set, renderer_writes

        with without_commit_drain():
            state = NSOSwitchportState.objects.create(
                management=self.mgmt, interface=self.iface, mode="tagged", status="accepted"
            )
        plan = RendererMutationPlan.build(m2m_writes=(planned_m2m_set(state, "tagged_vlans", (self.other_vlan,)),))
        config, session = self.adapter.patches()
        with config, session, renderer_writes(plan) as writer:
            writer.m2m_set(state, "tagged_vlans", (self.other_vlan,))

        assert len(self.adapter.requests) == 1
        assert self.adapter.requests[0]["body"]["deleted_roots"] == []
        assert self.adapter.requests[0]["body"]["interfaces"][0]["tagged_vlans"] == [20]

    def test_unown_prepares_detach_without_root_deletion(self):
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion, NSOSwitchportState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        with without_commit_drain():
            state = NSOSwitchportState.objects.create(
                management=self.mgmt, interface=self.iface, mode="access", status="accepted"
            )
        state.status = "changed"
        plan = RendererMutationPlan.build(saves=(planned_save(state, update_fields=("status",)),))
        config, session = self.adapter.patches()
        with config, session, renderer_writes(plan) as writer:
            writer.save(state, update_fields=("status",))

        assert len(self.adapter.requests) == 1
        assert self.adapter.requests[0]["body"]["interfaces"] == []
        assert self.adapter.requests[0]["body"]["deleted_roots"] == []
        assert not NSOSwitchingRootDeletion.objects.filter(management=self.mgmt, scope="switchport").exists()

    def test_tagged_vlan_removal_is_owned_content_without_root_deletion(self):
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion, NSOSwitchportState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_m2m_set, renderer_writes

        with without_commit_drain():
            state = NSOSwitchportState.objects.create(
                management=self.mgmt, interface=self.iface, mode="tagged", status="accepted"
            )
            plan = RendererMutationPlan.build(m2m_writes=(planned_m2m_set(state, "tagged_vlans", (self.other_vlan,)),))
            with renderer_writes(plan) as writer:
                writer.m2m_set(state, "tagged_vlans", (self.other_vlan,))
        plan = RendererMutationPlan.build(m2m_writes=(planned_m2m_set(state, "tagged_vlans", ()),))
        config, session = self.adapter.patches()
        with config, session, renderer_writes(plan) as writer:
            writer.m2m_set(state, "tagged_vlans", ())

        assert self.adapter.requests[-1]["body"]["deleted_roots"] == []
        assert self.adapter.requests[-1]["body"]["interfaces"][0]["tagged_vlans"] == []
        assert not NSOSwitchingRootDeletion.objects.filter(management=self.mgmt, scope="switchport").exists()


class TestSwitchportAcceptView(_SwBase):
    def test_accept_writes_native_and_marks_owned(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision, NSOOwnershipManifest

        mgmt = self._make_mgmt()
        st = self._state(mgmt, mode="access", status="changed")
        self.client.force_login(_superuser())
        with patch("netbox_nso_plugin.adapter_client.apply_switchport_config"):
            resp = self.client.post(f"/plugins/nso/switchport/state/{st.pk}/accept/")
        assert resp.status_code == 302
        st.refresh_from_db()
        assert st.status == "accepted"
        assert st.accepted_at is not None
        self.iface.refresh_from_db()
        assert self.iface.mode == "access"
        assert self.iface.untagged_vlan_id == self.v10.pk
        revision = NSOIntentRevision.objects.get(device=self.device, scope="switchport")
        assert revision.verified_revision == revision.revision
        assert revision.verified_fingerprint == delivery.canonical_fingerprint(
            delivery.render("switchport", self.device.pk, mgmt.adapter_device_id).payload
        )
        assert NSOOwnershipManifest.objects.filter(
            device_id=self.device.pk,
            scope="switchport",
            native_model_label="dcim.interface",
            native_key={"device_id": self.iface.device_id, "name": self.iface.name},
            ownership_state="owned",
        ).exists()


def _superuser():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    u = User.objects.filter(username="sw-admin").first()
    return u or User.objects.create_superuser(username="sw-admin", password="pw", email="sw@test.x")  # noqa: S106
