# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Greenfield VLAN attach (device NSO-tab action) + delete-propagation signals.

A shared ipam.VLAN is attached to a device via an NSOVLANState overlay (not a
per-device group), so the same VLAN can span devices and a rename/delete propagates
to all of them.
"""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from ._outbox_case import in_thread
from .mixins import IntentPushDeliveryMixin, IntentPushResetMixin, _CascadeFlushMixin


class _VlanGreenfieldBase(IntentPushDeliveryMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="VgMfg", slug="vgmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="VgDev", slug="vgdev")
        role = DeviceRole.objects.create(name="VgRole", slug="vgrole")
        site = Site.objects.create(name="VgSite", slug="vgsite")
        cls.sw3 = Device.objects.create(name="vg-sw3", device_type=dt, role=role, site=site)
        cls.sw4 = Device.objects.create(name="vg-sw4", device_type=dt, role=role, site=site)

    def _mgmt(self, device, adapter_device_id):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="vg-inst", defaults={"adapter_instance_id": "vg-inst"})
        return NSODeviceManagement.objects.create(
            device=device,
            nso_instance=inst,
            nso_device_name=f"nso-{device.name}",
            adapter_device_id=adapter_device_id,
        )

    def _shared_vlan(self, vid=3366, name="testnso"):
        from ipam.models import VLAN, VLANGroup

        group, _ = VLANGroup.objects.get_or_create(name="shared", slug="shared")
        return VLAN.objects.create(group=group, vid=vid, name=name)


class TestVlanAttachView(_VlanGreenfieldBase):
    def test_attach_creates_accepted_overlay_and_pushes(self):
        from netbox_nso_plugin.models import NSOVLANState

        mgmt = self._mgmt(self.sw3, 196)
        vlan = self._shared_vlan()
        self.client.force_login(__import__("users").models.User.objects.create_user("vgadmin", is_superuser=True))

        url = reverse("plugins:netbox_nso_plugin:vlan_attach", kwargs={"device_pk": self.sw3.pk})
        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(url, {"vlan": vlan.pk})
        assert resp.status_code == 302
        state = NSOVLANState.objects.get(management=mgmt, vlan=vlan)
        assert state.status == "accepted"
        # the same shared VLAN, attached via overlay (group is the shared group, not per-device)
        assert state.vlan.group.slug == "shared"
        mock_put.assert_called()

    def test_unavailable_vlan_does_not_advance_the_intent_revision(self):
        from netbox_nso_plugin.intent_state import deletion_footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.signals import suppress_intent_push

        mgmt = self._mgmt(self.sw3, 196)
        vlan = self._shared_vlan()
        self.client.force_login(__import__("users").models.User.objects.create_user("vgmissing", is_superuser=True))
        revision, _created = NSOIntentRevision.objects.get_or_create(device=mgmt.device, scope="vlan")
        before = revision.revision
        original_build = RendererMutationPlan.build
        deleted = False

        def plan_then_delete(**kwargs):
            nonlocal deleted
            plan = original_build(**kwargs)
            if deleted:
                return plan
            deleted = True
            doomed = type(vlan).objects.get(pk=vlan.pk)
            with suppress_intent_push(), intent_transaction(deletion_footprint_for_instance(doomed)):
                doomed.delete()
            return plan

        url = reverse("plugins:netbox_nso_plugin:vlan_attach", kwargs={"device_pk": self.sw3.pk})
        with patch.object(RendererMutationPlan, "build", side_effect=plan_then_delete):
            response = self.client.post(url, {"vlan": vlan.pk})

        assert response.status_code == 302
        assert not type(vlan).objects.filter(pk=vlan.pk).exists()
        revision.refresh_from_db()
        assert revision.revision == before, "an unavailable VLAN committed an intent revision"


class TestVlanAttachViewConcurrency(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        from django.db import transaction
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOVLANState

        manufacturer = Manufacturer.objects.create(name="VLAN race", slug="vlan-race")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="VLAN race",
            slug="vlan-race",
        )
        role = DeviceRole.objects.create(name="VLAN race", slug="vlan-race")
        site = Site.objects.create(name="VLAN race", slug="vlan-race")
        self.device = Device.objects.create(
            name="vlan-race-device",
            device_type=device_type,
            role=role,
            site=site,
        )
        nso_instance = NSOInstance.objects.create(
            name="vlan-race-instance",
            adapter_instance_id="vlan-race-instance",
        )
        self.mgmt = NSODeviceManagement(
            device=self.device,
            nso_instance=nso_instance,
            nso_device_name="vlan-race-device",
            adapter_device_id=196,
        )
        with intent_transaction(footprint_for_instance(self.mgmt)):
            NSODeviceManagement.objects.bulk_create([self.mgmt])

        group = VLANGroup.objects.create(name="VLAN race", slug="vlan-race")
        with transaction.atomic():
            self.vlan = VLAN.objects.create(group=group, vid=3366, name="test-vlan")
            self.state = NSOVLANState.objects.create(
                management=self.mgmt,
                vlan=self.vlan,
                device_name="original-name",
                status="imported",
            )
        user = __import__("users").models.User.objects.create_user("vlan-race-admin", is_superuser=True)
        self.client.force_login(user)

    def test_existing_overlay_conflict_uses_the_refresh_response(self):
        from django.contrib.messages import get_messages

        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan

        original_build = RendererMutationPlan.build
        plan_staled = False

        def change_device_name():
            current = NSOVLANState.objects.get(pk=self.state.pk)
            with intent_transaction(footprint_for_instance(current)):
                NSOVLANState.objects.filter(pk=current.pk).update(device_name="concurrent-name")

        def build_then_stale(*args, **kwargs):
            nonlocal plan_staled
            plan = original_build(*args, **kwargs)
            if not plan_staled:
                plan_staled = True
                in_thread(change_device_name)
            return plan

        url = reverse("plugins:netbox_nso_plugin:vlan_attach", kwargs={"device_pk": self.device.pk})
        self.client.raise_request_exception = False
        with patch.object(RendererMutationPlan, "build", side_effect=build_then_stale):
            response = self.client.post(url, {"vlan": self.vlan.pk})

        self.assertEqual(response.status_code, 302)
        message_text = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertIn("The selected VLAN is no longer available.", message_text)
        self.assertFalse(any(message.startswith("Attached VLAN") for message in message_text))
        self.state.refresh_from_db()
        self.assertEqual(self.state.device_name, "concurrent-name")
        self.assertEqual(self.state.status, "imported")


class TestVlanDeletePropagation(_VlanGreenfieldBase):
    def test_delete_vlan_pushes_reduced_intent_to_all_attached(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.intent_state import intent_transaction, vlan_footprint
        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes
        from netbox_nso_plugin.signals import suppress_intent_push

        m3 = self._mgmt(self.sw3, 196)
        m4 = self._mgmt(self.sw4, 197)
        vlan = self._shared_vlan()
        footprint = vlan_footprint(vlan.pk, ("vlan",), extra_device_ids=(self.sw3.pk, self.sw4.pk))
        with suppress_intent_push(), intent_transaction(footprint):
            NSOVLANState.objects.create(management=m3, vlan=vlan, status="in_sync")
            NSOVLANState.objects.create(management=m4, vlan=vlan, status="in_sync")

        pushed = []
        with patch(
            "netbox_nso_plugin.adapter_client.put_vlan_intent",
            side_effect=lambda adapter_id, vlans: pushed.append((adapter_id, vlans)),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                candidate = VLAN.objects.get(pk=vlan.pk)
                plan = RendererMutationPlan.build(deletes=(planned_delete(candidate),))
                with renderer_writes(plan) as writer:
                    writer.delete(candidate)

        # both attached devices got a reduced (empty) snapshot → removal propagates
        adapter_ids = sorted(a for a, _ in pushed)
        assert adapter_ids == [196, 197]
        assert all(v == [] for _, v in pushed)
        assert NSOVLANState.objects.filter(vlan__vid=3366).count() == 0

    def test_implicit_vlan_delete_pushes_after_acquiring_the_revision(self):
        from netbox_nso_plugin.intent_state import intent_transaction, vlan_footprint
        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push

        m3 = self._mgmt(self.sw3, 196)
        m4 = self._mgmt(self.sw4, 197)
        vlan = self._shared_vlan()
        footprint = vlan_footprint(vlan.pk, ("vlan",), extra_device_ids=(self.sw3.pk, self.sw4.pk))
        with suppress_intent_push(), intent_transaction(footprint):
            NSOVLANState.objects.create(management=m3, vlan=vlan, status="in_sync")
            NSOVLANState.objects.create(management=m4, vlan=vlan, status="in_sync")

        pushed = []
        with patch(
            "netbox_nso_plugin.adapter_client.put_vlan_intent",
            side_effect=lambda adapter_id, vlans: pushed.append((adapter_id, vlans)),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                type(vlan).objects.get(pk=vlan.pk).delete()

        self.assertEqual(sorted(adapter_id for adapter_id, _vlans in pushed), [196, 197])
        self.assertTrue(all(vlans == [] for _adapter_id, vlans in pushed))
