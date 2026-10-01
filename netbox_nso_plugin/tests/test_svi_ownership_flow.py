# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""SVI ownership through the device action, audit, and delivery paths."""

from contextlib import contextmanager
from threading import Event, Thread, current_thread
from unittest.mock import patch

import requests
from dcim.models import Interface, Platform
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import Client, TransactionTestCase
from django.urls import reverse
from ipam.models import VLAN

from netbox_nso_plugin.models import NSOPlatformNedMapping, NSOSVIState, NSOVLANState
from netbox_nso_plugin.ownership_grants import OwnershipGrant

from ._adapter_http import make_session
from ._outbox_case import (
    CFG,
    ReceiptAdapter,
    content_update,
    make_managed,
    wait_until_postgres_blocks,
    without_commit_drain,
)
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


@contextmanager
def adapter_device_ned(ned_id):
    session = make_session(json_data={"known": bool(ned_id), "ned_id": ned_id, "sw_version": "", "elements": []})
    with (
        patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG),
        patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
    ):
        yield session


class TestOwnedSviDelivery(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        self.device, self.management = make_managed("svidelivery", 17543)
        self.group = _device_vlan_group(self.device)
        self.client.force_login(get_user_model().objects.create_user("svi-delivery-admin", is_superuser=True))

    def _vlan(self, vid):
        vlan = VLAN.objects.create(group=self.group, vid=vid, name=f"SVI {vid}")
        NSOVLANState.objects.create(management=self.management, vlan=vlan, status="imported")
        return vlan

    def _ned(self, prefix):
        platform = Platform.objects.create(name=f"SVI {prefix}", slug=f"svi-{prefix}")
        self.device.platform = platform
        self.device.save(update_fields=("platform",))
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id=f"{prefix}-test")

    def test_create_uses_adapter_junos_ned_when_platform_maps_to_ios(self):
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        self._ned("cisco-ios-cli")
        vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=10, name="Adapter Junos VLAN")
        NSOVLANState.objects.create(management=self.management, vlan=vlan, status="imported")
        url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
        session = make_session(
            json_data={"known": True, "ned_id": "juniper-junos-nc-test", "sw_version": "", "elements": []}
        )
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
            without_commit_drain(),
        ):
            missing_unit = self.client.post(url, {"vlan": vlan.pk, "unit": "", "vrf": ""})
            self.assertContains(missing_unit, "Junos IRB unit is required")
            response = self.client.post(url, {"vlan": vlan.pk, "unit": 7, "vrf": ""})
        self.assertEqual(response.status_code, 302, response.content)
        self.assertTrue(NSOSVIState.objects.filter(management=self.management, interface__name="irb.7").exists())
        self.assertFalse(Interface.objects.filter(device=self.device, name="Vlan10").exists())
        self.assertTrue(
            any(
                call.args[:2] == ("GET", f"{CFG['url']}/api/v1/devices/{self.management.adapter_device_id}/capability")
                for call in session.request.call_args_list
            )
        )

    def test_create_reads_the_adapter_ned_once_per_post(self):
        vlan = self._vlan(11)
        url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
        capability = ("GET", f"{CFG['url']}/api/v1/devices/{self.management.adapter_device_id}/capability")
        with without_commit_drain(), adapter_device_ned("juniper-junos-nc-test") as session:
            response = self.client.post(url, {"vlan": vlan.pk, "unit": 11, "vrf": ""})
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual([call.args[:2] for call in session.request.call_args_list].count(capability), 1)

    def test_invalid_vlan_does_not_read_adapter_capability(self):
        url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
        capability = ("GET", f"{CFG['url']}/api/v1/devices/{self.management.adapter_device_id}/capability")
        with without_commit_drain(), adapter_device_ned("juniper-junos-nc-test") as session:
            response = self.client.post(url, {"vlan": "invalid", "unit": "", "vrf": ""})
        self.assertEqual(response.status_code, 200)
        self.assertIn("vlan", response.context["form"].errors)
        self.assertNotIn(capability, [call.args[:2] for call in session.request.call_args_list])

    def test_adapter_unreachable_refuses_create(self):
        self._ned("cisco-ios-cli")
        vlan = self._vlan(10)
        url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
        session = make_session()
        session.request.side_effect = requests.ConnectionError("adapter unavailable")
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
            without_commit_drain(),
        ):
            response = self.client.post(url, {"vlan": vlan.pk, "unit": "", "vrf": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "adapter")
        self.assertFalse(NSOSVIState.objects.filter(management=self.management).exists())
        self.assertFalse(Interface.objects.filter(device=self.device, name="Vlan10").exists())

    def test_unsupported_adapter_ned_refuses_create(self):
        self._ned("cisco-ios-cli")
        vlan = self._vlan(10)
        url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
        session = make_session(json_data={"known": True, "ned_id": "arcos-cli-test", "sw_version": "", "elements": []})
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
            without_commit_drain(),
        ):
            response = self.client.post(url, {"vlan": vlan.pk, "unit": "", "vrf": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "does not support SVI creation")
        self.assertFalse(NSOSVIState.objects.filter(management=self.management).exists())
        self.assertFalse(Interface.objects.filter(device=self.device, name="Vlan10").exists())

    def test_missing_adapter_mapping_refuses_create(self):
        self._ned("cisco-ios-cli")
        vlan = self._vlan(10)
        type(self.management).objects.filter(pk=self.management.pk).update(adapter_device_id=None)
        url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
        with without_commit_drain():
            response = self.client.post(url, {"vlan": vlan.pk, "unit": "", "vrf": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no adapter mapping")
        self.assertFalse(NSOSVIState.objects.filter(management=self.management).exists())

    def test_adapter_without_ned_refuses_create(self):
        self._ned("cisco-ios-cli")
        vlan = self._vlan(10)
        url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
        with without_commit_drain(), adapter_device_ned(""):
            response = self.client.post(url, {"vlan": vlan.pk, "unit": "", "vrf": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "does not report a device NED")
        self.assertFalse(NSOSVIState.objects.filter(management=self.management).exists())

    def test_imported_irb_zero_and_independent_unit_survive_accept_audit_and_apply(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        from .test_apply_selector import _ApplyContractAdapter, _promoted

        self._vlan(1)
        self._vlan(10)
        with without_commit_drain():
            rows = reconcile_svi(
                self.device,
                {
                    "interfaces": [
                        {"interface_name": "irb.0", "vlan_id": 1, "type": "irb", "vrf": ""},
                        {"interface_name": "irb.7", "vlan_id": 10, "type": "irb", "vrf": ""},
                    ]
                },
            )
            for row in rows:
                response = self.client.post(reverse("plugins:netbox_nso_plugin:svi_accept", args=[row.pk]))
                self.assertEqual(response.status_code, 302)
            audit_renderer_scopes(self.device.pk, ["svi"], "test")
            audit_renderer_scopes(self.device.pk, ["svi"], "test")
        self.assertEqual(
            set(NSOSVIState.objects.filter(management=self.management).values_list("interface__name", flat=True)),
            {"irb.0", "irb.7"},
        )
        self.assertTrue(
            all(row.status in {"accepted", "in_sync"} for row in NSOSVIState.objects.filter(management=self.management))
        )
        adapter = _ApplyContractAdapter(
            lambda selected: (202, {**_promoted(selected), "device_id": self.management.adapter_device_id})
        )
        config, session = adapter.patches()
        with config, session:
            drain.push_now(self.device.pk, "svi", force=True)
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[self.management.pk, "apply"]),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        snapshots = [request for request in adapter.requests if request["url"].endswith("/svi-intent")]
        self.assertEqual(
            {item["interface_name"]: item["vlan_id"] for item in snapshots[-1]["body"]["interfaces"]},
            {"irb.0": 1, "irb.7": 10},
        )

    def test_native_svi_without_overlay_does_not_acquire(self):
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        vlan = self._vlan(1723)
        with without_commit_drain(), transaction.atomic():
            Interface.objects.create(device=self.device, name="Vlan1723", type="virtual", untagged_vlan=vlan)
        audit_renderer_scopes(self.device.pk, ["svi"], "test")
        self.assertFalse(NSOSVIState.objects.filter(management=self.management).exists())

    def test_rescoped_shared_vlan_remains_acceptable_and_deliverable(self):
        from ipam.models import VLANGroup

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.vlan_reconciler import rescope_vlan

        vlan = self._vlan(1)
        state = NSOSVIState.objects.create(
            management=self.management,
            interface=Interface.objects.create(device=self.device, name="irb.0", type="virtual"),
            vlan=vlan,
            svi_type="irb",
            status="imported",
        )
        shared = VLANGroup.objects.create(name="Shared SVI VLANs", slug="shared-svi-vlans")
        with without_commit_drain():
            action, surviving = rescope_vlan(
                NSOVLANState.objects.get(management=self.management, vlan=vlan),
                shared,
                grant=OwnershipGrant("operator_edit"),
            )
            response = self.client.post(reverse("plugins:netbox_nso_plugin:svi_accept", args=[state.pk]))
        self.assertEqual(action, "moved")
        self.assertEqual(surviving.pk, vlan.pk)
        self.assertEqual(response.status_code, 302)
        self.assertIn(NSOSVIState.objects.get(pk=state.pk).status, {"accepted", "in_sync"})
        adapter = ReceiptAdapter()
        config, session = adapter.patches()
        with config, session:
            drain.push_now(self.device.pk, "svi", force=True)
        requests = [request for request in adapter.requests if request["url"].endswith("/svi-intent")]
        self.assertEqual(requests[-1]["body"]["interfaces"][0]["interface_name"], "irb.0")

    def test_reconcile_links_imported_irb_to_attached_shared_vlan(self):
        from ipam.models import VLANGroup

        from netbox_nso_plugin.svi_reconciler import reconcile_svi
        from netbox_nso_plugin.vlan_reconciler import rescope_vlan

        vlan = self._vlan(10)
        shared = VLANGroup.objects.create(name="Shared import VLANs", slug="shared-import-vlans")
        with without_commit_drain():
            rescope_vlan(
                NSOVLANState.objects.get(management=self.management, vlan=vlan),
                shared,
                grant=OwnershipGrant("operator_edit"),
            )
            rows = reconcile_svi(
                self.device, {"interfaces": [{"interface_name": "irb.8", "vlan_id": 10, "type": "irb"}]}
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].vlan_id, vlan.pk)

    def test_reconcile_keeps_imported_vlan_when_attached_vid_becomes_ambiguous(self):
        from ipam.models import VLANGroup

        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        first_vlan = self._vlan(10)
        payload = {"interfaces": [{"interface_name": "irb.7", "vlan_id": 10, "type": "irb"}]}
        with without_commit_drain():
            original = reconcile_svi(self.device, payload)[0]
        self.assertEqual(original.vlan_id, first_vlan.pk)

        shared = VLANGroup.objects.create(name="Later SVI VLANs", slug="later-svi-vlans")
        second_vlan = VLAN.objects.create(group=shared, vid=10, name="Later VID 10")
        NSOVLANState.objects.create(management=self.management, vlan=second_vlan, status="imported")
        with without_commit_drain():
            refreshed = reconcile_svi(self.device, payload)[0]
            unresolved = reconcile_svi(
                self.device,
                {"interfaces": payload["interfaces"] + [{"interface_name": "irb.8", "vlan_id": 10, "type": "irb"}]},
            )[1]
        self.assertEqual(refreshed.vlan_id, first_vlan.pk)
        self.assertEqual(NSOSVIState.objects.get(pk=original.pk).vlan_id, first_vlan.pk)
        self.assertIsNone(unresolved.vlan_id)
        self.assertEqual(unresolved.status, "conflict")

    def test_add_svi_offers_attached_shared_vlan_and_refuses_unattached_vlan(self):
        from ipam.models import VLANGroup

        shared = VLANGroup.objects.create(name="Shared choice VLANs", slug="shared-choice-vlans")
        attached = VLAN.objects.create(group=shared, vid=10, name="Attached")
        unattached = VLAN.objects.create(group=self.group, vid=11, name="Unattached")
        NSOVLANState.objects.create(management=self.management, vlan=attached, status="imported")
        url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(attached, response.context["form"].fields["vlan"].queryset)
        self.assertNotIn(unattached, response.context["form"].fields["vlan"].queryset)

    def test_two_irbs_cannot_own_one_vlan_and_conflicts_block_snapshot(self):
        from netbox_nso_plugin import drain

        self._ned("juniper-junos-nc")
        vlan = self._vlan(10)
        url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
        with without_commit_drain(), adapter_device_ned("juniper-junos-nc-test"):
            first = self.client.post(url, {"vlan": vlan.pk, "unit": 7, "vrf": ""})
            second = self.client.post(url, {"vlan": vlan.pk, "unit": 8, "vrf": ""})
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, "already", status_code=200)
        self.assertEqual(NSOSVIState.objects.filter(management=self.management).count(), 1)

        imported = NSOSVIState.objects.create(
            management=self.management,
            interface=Interface.objects.create(device=self.device, name="irb.8", type="virtual"),
            vlan=vlan,
            svi_type="irb",
            status="imported",
        )
        with without_commit_drain():
            accepted = self.client.post(reverse("plugins:netbox_nso_plugin:svi_accept", args=[imported.pk]))
            edited = self.client.post(
                reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "svi", "pk": imported.pk}),
                {"vrf": "BLUE"},
            )
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(edited.status_code, 400)
        imported.refresh_from_db()
        self.assertEqual((imported.status, imported.vrf), ("imported", ""))

        with without_commit_drain():
            content_update(imported, status="accepted")
        adapter = ReceiptAdapter()
        config, session = adapter.patches()
        with config, session:
            drain.push_now(self.device.pk, "svi", force=True)
        self.management.refresh_from_db()
        self.assertEqual(self.management.intent_push_errors["svi"]["code"], "validation_error")
        self.assertFalse(any(request["url"].endswith("/svi-intent") for request in adapter.requests))

    def test_duplicate_attached_vid_refuses_new_binding_and_blocks_owned_snapshot(self):
        from ipam.models import VLANGroup

        from netbox_nso_plugin import drain

        self._ned("juniper-junos-nc")
        first_vlan = self._vlan(10)
        url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
        with without_commit_drain(), adapter_device_ned("juniper-junos-nc-test"):
            first = self.client.post(url, {"vlan": first_vlan.pk, "unit": 7, "vrf": ""})
        self.assertEqual(first.status_code, 302)

        shared = VLANGroup.objects.create(name="Other SVI VLANs", slug="other-svi-vlans")
        second_vlan = VLAN.objects.create(group=shared, vid=10, name="Other VID 10")
        NSOVLANState.objects.create(management=self.management, vlan=second_vlan, status="imported")
        with without_commit_drain(), adapter_device_ned("juniper-junos-nc-test"):
            second = self.client.post(url, {"vlan": second_vlan.pk, "unit": 8, "vrf": ""})
        self.assertContains(second, "ambiguous", status_code=200)
        self.assertEqual(NSOSVIState.objects.filter(management=self.management).count(), 1)

        imported = NSOSVIState.objects.create(
            management=self.management,
            interface=Interface.objects.create(device=self.device, name="irb.8", type="virtual"),
            vlan=second_vlan,
            svi_type="irb",
            status="imported",
        )
        with without_commit_drain():
            accepted = self.client.post(reverse("plugins:netbox_nso_plugin:svi_accept", args=[imported.pk]))
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(NSOSVIState.objects.get(pk=imported.pk).status, "imported")

        content_update(imported, status="accepted")
        adapter = ReceiptAdapter()
        config, session = adapter.patches()
        with config, session:
            drain.push_now(self.device.pk, "svi", force=True)
        self.management.refresh_from_db()
        self.assertEqual(self.management.intent_push_errors["svi"]["code"], "validation_error")
        self.assertFalse(any(request["url"].endswith("/svi-intent") for request in adapter.requests))

    def test_bad_owned_row_blocks_the_snapshot_until_repaired(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.views import ApplyPreparationRefused, _prepare_apply

        vlan = self._vlan(10)
        other_vlan = self._vlan(1723)
        with without_commit_drain(), transaction.atomic():
            interface = Interface.objects.create(device=self.device, name="irb.7", type="virtual")
            state = acquire_overlay(
                NSOSVIState,
                management=self.management,
                interface=interface,
                vlan=vlan,
                svi_type="irb",
                status="accepted",
            )
            acquire_overlay(
                NSOSVIState,
                management=self.management,
                interface=Interface.objects.create(device=self.device, name="Vlan1723", type="virtual"),
                vlan=other_vlan,
                svi_type="svi",
                status="accepted",
            )
        adapter = ReceiptAdapter()
        config, session = adapter.patches()
        with config, session:
            drain.push_now(self.device.pk, "svi", force=True)
            acknowledged = [request for request in adapter.requests if request["url"].endswith("/svi-intent")]
            NSOSVIState.objects.filter(pk=state.pk).update(vlan=None)
            drain.push_now(self.device.pk, "svi", force=True)
            self.management.refresh_from_db()
            self.assertEqual(self.management.intent_push_errors["svi"]["code"], "validation_error")
            with self.assertRaises(ApplyPreparationRefused):
                _prepare_apply(self.management)
            self.assertEqual(
                [request for request in adapter.requests if request["url"].endswith("/svi-intent")], acknowledged
            )
            NSOSVIState.objects.filter(pk=state.pk).update(vlan=vlan)
            drain.push_now(self.device.pk, "svi", force=True)
        self.assertGreater(
            len([request for request in adapter.requests if request["url"].endswith("/svi-intent")]), len(acknowledged)
        )

    def test_cisco_svi_accepts_matching_vid_and_refuses_wrong_identity(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        self._vlan(1723)
        self._vlan(11)
        with without_commit_drain():
            rows = reconcile_svi(
                self.device,
                {
                    "interfaces": [
                        {"interface_name": "Vlan1723", "vlan_id": 1723, "type": "svi"},
                        {"interface_name": "Vlan10", "vlan_id": 11, "type": "svi"},
                    ]
                },
            )
            for row in rows:
                self.assertEqual(
                    self.client.post(reverse("plugins:netbox_nso_plugin:svi_accept", args=[row.pk])).status_code,
                    302,
                )
        self.assertIn(NSOSVIState.objects.get(interface__name="Vlan1723").status, {"accepted", "in_sync"})
        invalid = NSOSVIState.objects.get(interface__name="Vlan10")
        self.assertEqual(invalid.status, "imported")
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "svi", "pk": invalid.pk}),
            {"vrf": "BLUE"},
        )
        self.assertEqual(response.status_code, 400)
        audit_renderer_scopes(self.device.pk, ["svi"], "test")
        audit_renderer_scopes(self.device.pk, ["svi"], "test")
        self.assertIn(NSOSVIState.objects.get(interface__name="Vlan1723").status, {"accepted", "in_sync"})
        self.assertEqual(NSOSVIState.objects.get(pk=invalid.pk).status, "imported")
        adapter = ReceiptAdapter()
        config, session = adapter.patches()
        with config, session:
            drain.push_now(self.device.pk, "svi", force=True)
        requests = [request for request in adapter.requests if request["url"].endswith("/svi-intent")]
        self.assertEqual([row["interface_name"] for row in requests[-1]["body"]["interfaces"]], ["Vlan1723"])

    def test_missing_vlan_and_vid_zero_refuse_accept(self):
        from ipam.models import VLANGroup

        foreign_group = VLANGroup.objects.create(name="Foreign VLANs", slug="foreign-svi-vlans")
        with without_commit_drain(), transaction.atomic():
            missing = NSOSVIState.objects.create(
                management=self.management,
                interface=Interface.objects.create(device=self.device, name="Vlan10", type="virtual"),
                svi_type="svi",
                status="imported",
            )
            zero = NSOSVIState.objects.create(
                management=self.management,
                interface=Interface.objects.create(device=self.device, name="irb.0", type="virtual"),
                vlan=self._vlan(0),
                svi_type="irb",
                status="imported",
            )
            ungrouped = NSOSVIState.objects.create(
                management=self.management,
                interface=Interface.objects.create(device=self.device, name="Vlan12", type="virtual"),
                vlan=VLAN.objects.create(vid=12, name="Ungrouped SVI VLAN"),
                svi_type="svi",
                status="imported",
            )
            foreign = NSOSVIState.objects.create(
                management=self.management,
                interface=Interface.objects.create(device=self.device, name="Vlan13", type="virtual"),
                vlan=VLAN.objects.create(group=foreign_group, vid=13, name="Foreign SVI VLAN"),
                svi_type="svi",
                status="imported",
            )
            unattached = NSOSVIState.objects.create(
                management=self.management,
                interface=Interface.objects.create(device=self.device, name="Vlan14", type="virtual"),
                vlan=VLAN.objects.create(group=self.group, vid=14, name="Unattached SVI VLAN"),
                svi_type="svi",
                status="imported",
            )
        with without_commit_drain():
            for row in (missing, zero, ungrouped, foreign, unattached):
                self.assertEqual(
                    self.client.post(reverse("plugins:netbox_nso_plugin:svi_accept", args=[row.pk])).status_code,
                    302,
                )
        self.assertEqual(
            list(
                NSOSVIState.objects.filter(
                    pk__in=(missing.pk, zero.pk, ungrouped.pk, foreign.pk, unattached.pk)
                ).values_list("status", flat=True)
            ),
            ["imported"] * 5,
        )

    def test_junos_and_cisco_create_reach_apply(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.delivery import MODE_STORE_ONLY

        from .test_apply_selector import _ApplyContractAdapter, _promoted

        for index, (prefix, vid, unit, expected_name) in enumerate(
            (
                ("cisco-ios-cli", 1723, "", "Vlan1723"),
                ("cisco-nx-cli", 1724, "", "Vlan1724"),
                ("juniper-junos-nc", 10, 7, "irb.7"),
            )
        ):
            with self.subTest(prefix=prefix):
                from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

                self.device, self.management = make_managed(f"svi-create-{index}", 17545 + index)
                self.group = _device_vlan_group(self.device)
                self._ned(prefix)
                vlan = self._vlan(vid)
                url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
                with without_commit_drain(), adapter_device_ned(f"{prefix}-test"):
                    response = self.client.post(url, {"vlan": vlan.pk, "unit": unit, "vrf": ""})
                    duplicate = self.client.post(url, {"vlan": vlan.pk, "unit": unit, "vrf": ""})
                self.assertEqual(response.status_code, 302, response.content)
                self.assertEqual(duplicate.status_code, 200)
                self.assertContains(duplicate, "already exists")
                state = NSOSVIState.objects.get(management=self.management, interface__name=expected_name)
                self.assertEqual(state.vlan, vlan)
                adapter = _ApplyContractAdapter(
                    lambda selected: (202, {**_promoted(selected), "device_id": self.management.adapter_device_id})
                )
                config, session = adapter.patches()
                with config, session:
                    drain.push_now(self.device.pk, "svi", mode=MODE_STORE_ONLY, force=True)
                    apply_response = self.client.post(
                        reverse(
                            "plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[self.management.pk, "apply"]
                        ),
                        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                    )
                self.assertEqual(apply_response.status_code, 200)
                self.assertEqual(apply_response.json()["status"], "ok")
                requests = [request for request in adapter.requests if request["url"].endswith("/svi-intent")]
                self.assertEqual(
                    requests[-1]["body"]["interfaces"],
                    [{"interface_name": expected_name, "vlan_id": vid, "type": state.svi_type, "vrf": ""}],
                )

    def test_deleted_native_interface_retracts_with_delete_origin(self):
        from netbox_nso_plugin import drain

        self._ned("juniper-junos-nc")
        vlan = self._vlan(10)
        url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
        with without_commit_drain(), adapter_device_ned("juniper-junos-nc-test"):
            self.assertEqual(self.client.post(url, {"vlan": vlan.pk, "unit": 7, "vrf": ""}).status_code, 302)
        state = NSOSVIState.objects.get(management=self.management)
        adapter = ReceiptAdapter()
        config, session = adapter.patches()
        with config, session:
            drain.push_now(self.device.pk, "svi", force=True)
            with without_commit_drain():
                Interface.objects.filter(pk=state.interface_id).delete()
            drain.push_now(self.device.pk, "svi", force=True)
        requests = [request for request in adapter.requests if request["url"].endswith("/svi-intent")]
        self.assertGreaterEqual(len(requests), 2)
        self.assertEqual(requests[-1]["body"], {"interfaces": []})
        self.assertEqual(requests[-1]["params"].get("delete_origin"), "true")

    def test_foreign_overlay_delete_retires_manifest(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        vlan = self._vlan(10)
        with without_commit_drain(), transaction.atomic():
            state = acquire_overlay(
                NSOSVIState,
                management=self.management,
                interface=Interface.objects.create(device=self.device, name="irb.7", type="virtual"),
                vlan=vlan,
                svi_type="irb",
                status="accepted",
            )
        reconcile_scope_ownership(self.device.pk, ["svi"])
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="svi")
        with without_commit_drain():
            NSOSVIState.objects.filter(pk=state.pk).delete()
        reconcile_scope_ownership(self.device.pk, ["svi"])
        manifest.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertFalse(NSOSVIState.objects.filter(management=self.management).exists())


class TestSviCreateRaces(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        self.device, self.management = make_managed("svirace", 17544)
        platform = Platform.objects.create(name="SVI race", slug="svi-race")
        self.device.platform = platform
        self.device.save(update_fields=("platform",))
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="juniper-junos-nc-test")
        self.vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=10, name="SVI race VLAN")
        NSOVLANState.objects.create(management=self.management, vlan=self.vlan, status="imported")

    def test_new_duplicate_vid_attachment_while_create_waits_refuses_binding(self):
        from ipam.models import VLANGroup

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.svi_create import create_svi

        shared = VLANGroup.objects.create(name="Concurrent SVI VLANs", slug="concurrent-svi-vlans")
        second_vlan = VLAN.objects.create(group=shared, vid=10, name="Concurrent VID 10")
        build = RendererMutationPlan.build
        plan_ready = Event()
        resume = Event()
        result = {}

        def signal_plan(*args, **kwargs):
            plan = build(*args, **kwargs)
            if current_thread() is worker:
                plan_ready.set()
                if not resume.wait(20):
                    raise AssertionError("the attachment transaction did not finish")
            return plan

        def attempt_create():
            close_old_connections()
            try:
                with transaction.atomic():
                    result["value"] = create_svi(self.management, self.vlan, 7, "", svi_type="irb")
            except Exception as exc:
                result["error"] = exc
            finally:
                connection.close()

        worker = Thread(target=attempt_create)
        with (
            without_commit_drain(),
            adapter_device_ned("juniper-junos-nc-test"),
            patch.object(RendererMutationPlan, "build", signal_plan),
        ):
            try:
                with transaction.atomic():
                    NSOVLANState.objects.create(management=self.management, vlan=second_vlan, status="imported")
                    worker.start()
                    self.assertTrue(plan_ready.wait(20), "the writer did not finish planning")
            finally:
                resume.set()
                if worker.ident is not None:
                    worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "the writer did not finish after attachment committed")
        self.assertIsInstance(result.get("error"), ValidationError, result)
        self.assertIn("ambiguous", str(result["error"]))
        self.assertFalse(NSOSVIState.objects.filter(management=self.management).exists())

    def test_new_duplicate_vid_attachment_while_accept_waits_refuses_binding(self):
        from ipam.models import VLANGroup

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan

        state = NSOSVIState.objects.create(
            management=self.management,
            interface=Interface.objects.create(device=self.device, name="irb.7", type="virtual"),
            vlan=self.vlan,
            svi_type="irb",
            status="imported",
        )
        shared = VLANGroup.objects.create(name="Concurrent accept VLANs", slug="concurrent-accept-vlans")
        second_vlan = VLAN.objects.create(group=shared, vid=10, name="Concurrent accept VID 10")
        user = get_user_model().objects.create_user("svi-ambiguous-accept-admin", is_superuser=True)
        url = reverse("plugins:netbox_nso_plugin:svi_accept", args=[state.pk])
        plan_ready = Event()
        resume = Event()
        result = {}
        build = RendererMutationPlan.build

        def signal_plan(*args, **kwargs):
            plan = build(*args, **kwargs)
            if current_thread() is worker:
                plan_ready.set()
                if not resume.wait(20):
                    raise AssertionError("the attachment transaction did not finish")
            return plan

        def attempt_accept():
            close_old_connections()
            try:
                with transaction.atomic():
                    client = Client()
                    client.force_login(get_user_model().objects.get(pk=user.pk))
                    result["response"] = client.post(url)
            except Exception as exc:
                result["error"] = exc
            finally:
                connection.close()

        worker = Thread(target=attempt_accept)
        with without_commit_drain(), patch.object(RendererMutationPlan, "build", signal_plan):
            try:
                with transaction.atomic():
                    NSOVLANState.objects.create(management=self.management, vlan=second_vlan, status="imported")
                    worker.start()
                    self.assertTrue(plan_ready.wait(20), "the writer did not finish planning")
            finally:
                resume.set()
                if worker.ident is not None:
                    worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "the writer did not finish after attachment committed")
        self.assertNotIn("error", result, result)
        self.assertEqual(result["response"].status_code, 302)
        self.assertEqual(NSOSVIState.objects.get(pk=state.pk).status, "imported")

    def test_vlan_vid_change_while_accept_waits_refuses_ownership(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan

        user = get_user_model().objects.create_user("svi-accept-race-admin", is_superuser=True)
        interface = Interface.objects.create(device=self.device, name="Vlan10", type="virtual")
        state = NSOSVIState.objects.create(
            management=self.management, interface=interface, vlan=self.vlan, svi_type="svi", status="imported"
        )
        url = reverse("plugins:netbox_nso_plugin:svi_accept", args=[state.pk])
        ready = Event()
        result = {}
        build = RendererMutationPlan.build

        def signal_plan(*args, **kwargs):
            plan = build(*args, **kwargs)
            if current_thread() is worker:
                ready.set()
            return plan

        def accept():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    result["pid"] = cursor.fetchone()[0]
                client = Client()
                client.force_login(get_user_model().objects.get(pk=user.pk))
                result["response"] = client.post(url)
            except Exception as exc:
                result["error"] = exc
            finally:
                connection.close()

        worker = Thread(target=accept)
        with without_commit_drain(), patch.object(RendererMutationPlan, "build", signal_plan):
            try:
                with transaction.atomic():
                    VLAN.objects.filter(pk=self.vlan.pk).update(vid=11)
                    worker.start()
                    self.assertTrue(ready.wait(20))
                    wait_until_postgres_blocks(result["pid"], "SVI accept", timeout=20)
            finally:
                if worker.ident is not None:
                    worker.join(timeout=30)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", result, result)
        self.assertEqual(result["response"].status_code, 302)
        self.assertEqual(NSOSVIState.objects.get(pk=state.pk).status, "imported")

    def test_vlan_vid_change_while_inline_edit_waits_refuses_ownership(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan

        user = get_user_model().objects.create_user("svi-edit-race-admin", is_superuser=True)
        interface = Interface.objects.create(device=self.device, name="Vlan10", type="virtual")
        state = NSOSVIState.objects.create(
            management=self.management, interface=interface, vlan=self.vlan, svi_type="svi", status="imported"
        )
        url = reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "svi", "pk": state.pk})
        ready = Event()
        result = {}
        build = RendererMutationPlan.build

        def signal_plan(*args, **kwargs):
            plan = build(*args, **kwargs)
            if current_thread() is worker:
                ready.set()
            return plan

        def edit():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    result["pid"] = cursor.fetchone()[0]
                client = Client()
                client.force_login(get_user_model().objects.get(pk=user.pk))
                result["response"] = client.post(url, {"vrf": "BLUE"})
            except Exception as exc:
                result["error"] = exc
            finally:
                connection.close()

        worker = Thread(target=edit)
        with without_commit_drain(), patch.object(RendererMutationPlan, "build", signal_plan):
            try:
                with transaction.atomic():
                    VLAN.objects.filter(pk=self.vlan.pk).update(vid=11)
                    worker.start()
                    self.assertTrue(ready.wait(20))
                    wait_until_postgres_blocks(result["pid"], "SVI inline edit", timeout=20)
            finally:
                if worker.ident is not None:
                    worker.join(timeout=30)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", result, result)
        self.assertEqual(result["response"].status_code, 400)
        state.refresh_from_db()
        self.assertEqual((state.status, state.vrf), ("imported", ""))

    def test_vlan_detachment_after_validation_refuses_create(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.svi_create import create_svi

        build = RendererMutationPlan.build
        plan_ready = Event()
        result = {}

        def signal_plan(*args, **kwargs):
            plan = build(*args, **kwargs)
            if current_thread() is worker:
                plan_ready.set()
            return plan

        def attempt_create():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    result["pid"] = cursor.fetchone()[0]
                result["value"] = create_svi(self.management, self.vlan, 7, "", svi_type="irb")
            except Exception as exc:
                result["error"] = exc
            finally:
                connection.close()

        worker = Thread(target=attempt_create)
        with (
            without_commit_drain(),
            adapter_device_ned("juniper-junos-nc-test"),
            patch.object(RendererMutationPlan, "build", signal_plan),
        ):
            try:
                with transaction.atomic():
                    NSOVLANState.objects.filter(management=self.management, vlan=self.vlan).delete()
                    worker.start()
                    self.assertTrue(plan_ready.wait(20), "the writer did not finish planning")
                    wait_until_postgres_blocks(result["pid"], "SVI create", timeout=20)
            finally:
                if worker.ident is not None:
                    worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "the writer did not finish after the competitor committed")
        self.assertIsInstance(result.get("error"), ValidationError, result)
        self.assertIn("vlan", result["error"].message_dict)
        self.assertFalse(NSOSVIState.objects.filter(management=self.management).exists())

    def test_competing_irb_units_cannot_bind_same_vlan(self):
        from django.test import Client

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.svi_create import create_svi

        user = get_user_model().objects.create_user("svi-race-admin", is_superuser=True)
        url = reverse("plugins:netbox_nso_plugin:svi_add", kwargs={"device_pk": self.device.pk})
        build = RendererMutationPlan.build
        plan_ready = Event()
        result = {}

        def signal_plan(*args, **kwargs):
            plan = build(*args, **kwargs)
            if current_thread() is worker:
                plan_ready.set()
            return plan

        def attempt_create():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    result["pid"] = cursor.fetchone()[0]
                client = Client()
                client.force_login(get_user_model().objects.get(pk=user.pk))
                result["value"] = client.post(url, {"vlan": self.vlan.pk, "unit": 8, "vrf": ""})
            except Exception as exc:
                result["error"] = exc
            finally:
                connection.close()

        worker = Thread(target=attempt_create)
        with (
            without_commit_drain(),
            adapter_device_ned("juniper-junos-nc-test"),
            patch.object(RendererMutationPlan, "build", signal_plan),
        ):
            try:
                with transaction.atomic():
                    create_svi(self.management, self.vlan, 7, "", svi_type="irb")
                    worker.start()
                    self.assertTrue(plan_ready.wait(20), "the writer did not finish planning")
                    wait_until_postgres_blocks(result["pid"], "SVI create", timeout=20)
            finally:
                if worker.ident is not None:
                    worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "the writer did not finish after the competitor committed")
        self.assertNotIn("error", result, result)
        self.assertEqual(result["value"].status_code, 200)
        self.assertContains(result["value"], "already bound")
        self.assertEqual(NSOSVIState.objects.filter(management=self.management).count(), 1)
