# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Subinterface ownership through the device action, audit, and delivery paths."""

from threading import Event, Thread, current_thread
from unittest.mock import patch

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import Client, TransactionTestCase
from django.urls import reverse

from netbox_nso_plugin.models import NSOSubinterfaceState, NSOSwitchportState

from ._outbox_case import ReceiptAdapter, make_managed, wait_until_postgres_blocks, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestUnitZeroSwitchportOwnership(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def test_accept_drain_audit_and_apply_preparation_do_not_acquire_unit_zero(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.delivery import MODE_STORE_ONLY, render
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes
        from netbox_nso_plugin.views import _prepare_apply

        device, management = make_managed("unit0flow", 17540)
        with without_commit_drain(), transaction.atomic():
            parent = Interface.objects.create(device=device, name="xe-0/0/1", type="1000base-t")
            child = Interface.objects.create(device=device, name="xe-0/0/1.0", type="virtual", parent=parent)
            loopback = Interface.objects.create(device=device, name="lo0", type="virtual")
            Interface.objects.create(device=device, name="lo0.0", type="virtual", parent=loopback)
            switchport = NSOSwitchportState.objects.create(
                management=management, interface=child, mode="access", status="imported"
            )
        self.client.force_login(get_user_model().objects.create_user("unit0-admin", is_superuser=True))
        adapter = ReceiptAdapter()
        config, session = adapter.patches()
        with config, session:
            response = self.client.post(reverse("plugins:netbox_nso_plugin:switchport_accept", args=[switchport.pk]))
            self.assertEqual(response.status_code, 302)
            drain.push_now(device.pk, "subinterface", mode=MODE_STORE_ONLY, force=True)
            audit_renderer_scopes(device.pk, ["subinterface"], "test")
            self.assertEqual(render("subinterface", device.pk, management.adapter_device_id).payload, [])
            _prepare_apply(management)
        self.assertFalse(NSOSubinterfaceState.objects.filter(management=management).exists())


class TestSubinterfaceCreateRaces(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        self.device, self.management = make_managed("subifrace", 17541)
        with without_commit_drain(), transaction.atomic():
            self.parent = Interface.objects.create(device=self.device, name="ae99", type="lag")

    def _overlapping_create(self, competing, *, unit=7, tag=100, attempt=None):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.subinterface_create import create_subinterface

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
                result["value"] = (
                    attempt()
                    if attempt is not None
                    else create_subinterface(self.management, self.parent, unit, tag, "")
                )
            except Exception as exc:
                result["error"] = exc
            finally:
                connection.close()

        worker = Thread(target=attempt_create)
        with without_commit_drain(), patch.object(RendererMutationPlan, "build", signal_plan):
            try:
                with transaction.atomic():
                    competing()
                    worker.start()
                    self.assertTrue(plan_ready.wait(20), "the writer did not finish planning")
                    wait_until_postgres_blocks(result["pid"], "subinterface create", timeout=20)
            finally:
                if worker.ident is not None:
                    worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "the writer did not finish after the competitor committed")
        return result

    def test_competing_units_cannot_commit_the_same_tag(self):
        from netbox_nso_plugin.subinterface_create import create_subinterface

        result = self._overlapping_create(lambda: create_subinterface(self.management, self.parent, 8, 100, ""))
        self.assertIsInstance(result.get("error"), ValidationError, result)
        self.assertIn("dot1q_vlan", result["error"].message_dict)
        self.assertEqual(
            list(
                NSOSubinterfaceState.objects.filter(management=self.management).values_list(
                    "interface__name", flat=True
                )
            ),
            ["ae99.8"],
        )

    def test_parent_rename_after_validation_refuses_create(self):
        result = self._overlapping_create(lambda: Interface.objects.filter(pk=self.parent.pk).update(name="ae50"))
        self.assertIsInstance(result.get("error"), ValidationError, result)
        self.assertIn("parent", result["error"].message_dict)
        self.assertFalse(NSOSubinterfaceState.objects.filter(management=self.management).exists())
        self.assertFalse(Interface.objects.filter(device=self.device, name="ae99.7").exists())

    def test_parent_rename_while_accept_waits_refuses_ownership(self):
        user = get_user_model().objects.create_user("subif-accept-race-admin", is_superuser=True)
        child = Interface.objects.create(device=self.device, name="ae99.7", type="virtual", parent=self.parent)
        state = NSOSubinterfaceState.objects.create(
            management=self.management,
            interface=child,
            parent_interface=self.parent,
            dot1q_vlan=100,
            status="imported",
        )
        url = reverse("plugins:netbox_nso_plugin:subinterface_accept", args=[state.pk])

        def accept():
            client = Client()
            client.force_login(get_user_model().objects.get(pk=user.pk))
            return client.post(url)

        result = self._overlapping_create(
            lambda: Interface.objects.filter(pk=self.parent.pk).update(name="ae50"), attempt=accept
        )
        self.assertNotIn("error", result, result)
        self.assertEqual(result["value"].status_code, 302)
        self.assertEqual(NSOSubinterfaceState.objects.get(pk=state.pk).status, "imported")

    def test_parent_rename_while_inline_edit_waits_refuses_ownership(self):
        user = get_user_model().objects.create_user("subif-edit-race-admin", is_superuser=True)
        child = Interface.objects.create(device=self.device, name="ae99.7", type="virtual", parent=self.parent)
        state = NSOSubinterfaceState.objects.create(
            management=self.management,
            interface=child,
            parent_interface=self.parent,
            dot1q_vlan=100,
            status="imported",
        )
        url = reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "subinterface", "pk": state.pk})

        def edit():
            client = Client()
            client.force_login(get_user_model().objects.get(pk=user.pk))
            return client.post(url, {"vrf": "BLUE"})

        result = self._overlapping_create(
            lambda: Interface.objects.filter(pk=self.parent.pk).update(name="ae50"), attempt=edit
        )
        self.assertNotIn("error", result, result)
        self.assertEqual(result["value"].status_code, 400)
        state.refresh_from_db()
        self.assertEqual((state.status, state.vrf), ("imported", ""))

    def test_native_parent_change_while_accept_waits_refuses_ownership(self):
        user = get_user_model().objects.create_user("subif-link-race-admin", is_superuser=True)
        other_parent = Interface.objects.create(device=self.device, name="ae50", type="lag")
        child = Interface.objects.create(device=self.device, name="ae99.7", type="virtual", parent=self.parent)
        state = NSOSubinterfaceState.objects.create(
            management=self.management,
            interface=child,
            parent_interface=self.parent,
            dot1q_vlan=100,
            status="imported",
        )
        url = reverse("plugins:netbox_nso_plugin:subinterface_accept", args=[state.pk])

        def accept():
            client = Client()
            client.force_login(get_user_model().objects.get(pk=user.pk))
            return client.post(url)

        result = self._overlapping_create(
            lambda: Interface.objects.filter(pk=child.pk).update(parent=other_parent), attempt=accept
        )
        self.assertNotIn("error", result, result)
        self.assertEqual(result["value"].status_code, 302)
        self.assertEqual(NSOSubinterfaceState.objects.get(pk=state.pk).status, "imported")

    def test_parent_becomes_switchport_after_validation_refuses_create(self):
        result = self._overlapping_create(lambda: Interface.objects.filter(pk=self.parent.pk).update(mode="access"))
        self.assertIsInstance(result.get("error"), ValidationError, result)
        self.assertIn("parent", result["error"].message_dict)
        self.assertFalse(NSOSubinterfaceState.objects.filter(management=self.management).exists())
        self.assertFalse(Interface.objects.filter(device=self.device, name="ae99.7").exists())

    def test_conflicting_unit_create_returns_form_error(self):
        from netbox_nso_plugin.subinterface_create import create_subinterface

        user = get_user_model().objects.create_user("subif-race-admin", is_superuser=True)
        url = reverse("plugins:netbox_nso_plugin:subinterface_add", kwargs={"device_pk": self.device.pk})

        def post_create():
            client = Client()
            client.force_login(get_user_model().objects.get(pk=user.pk))
            return client.post(url, {"parent": self.parent.pk, "unit": 7, "dot1q_vlan": 100, "vrf": ""})

        result = self._overlapping_create(
            lambda: create_subinterface(self.management, self.parent, 7, 200, ""),
            attempt=post_create,
        )
        self.assertNotIn("error", result, result)
        self.assertEqual(result["value"].status_code, 200)
        self.assertContains(result["value"], "Refresh the page and try again")
        self.assertEqual(
            list(NSOSubinterfaceState.objects.filter(management=self.management).values_list("dot1q_vlan", flat=True)),
            [200],
        )


class TestOwnedSubinterfaceDelivery(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        self.device, self.management = make_managed("subifdelivery", 17542)
        self.client.force_login(get_user_model().objects.create_user("subif-delivery-admin", is_superuser=True))

    def _create(self, parent, unit, tag):
        url = reverse("plugins:netbox_nso_plugin:subinterface_add", kwargs={"device_pk": self.device.pk})
        with without_commit_drain():
            response = self.client.post(url, {"parent": parent.pk, "unit": unit, "dot1q_vlan": tag, "vrf": ""})
        self.assertEqual(response.status_code, 302, response.content)

    def test_imported_routed_child_with_stale_switchport_parent_accepts_and_delivers(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        from .test_apply_selector import _ApplyContractAdapter, _promoted

        with without_commit_drain(), transaction.atomic():
            parent = Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t", mode="access")
            NSOSwitchportState.objects.create(
                management=self.management, interface=parent, mode="access", status="changed"
            )
        with without_commit_drain():
            rows = reconcile_subinterface(
                self.device,
                {
                    "interfaces": [
                        {
                            "interface_name": "Ethernet2.1724",
                            "parent_interface": "Ethernet2",
                            "dot1q_vlan": 1724,
                        }
                    ]
                },
            )
            response = self.client.post(reverse("plugins:netbox_nso_plugin:subinterface_accept", args=[rows[0].pk]))
        self.assertEqual(response.status_code, 302)
        rows[0].refresh_from_db()
        self.assertIn(rows[0].status, ("accepted", "in_sync"))
        adapter = _ApplyContractAdapter(
            lambda selected: (202, {**_promoted(selected), "device_id": self.management.adapter_device_id})
        )
        config, session = adapter.patches()
        with config, session:
            drain.push_now(self.device.pk, "subinterface", force=True)
            apply_response = self.client.post(
                reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[self.management.pk, "apply"]),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(apply_response.status_code, 200)
        self.assertEqual(apply_response.json()["status"], "ok")
        self.assertEqual(len(adapter.apply_requests), 1)
        requests = [request for request in adapter.requests if request["url"].endswith("/subinterface-intent")]
        self.assertEqual(
            requests[-1]["body"]["interfaces"],
            [
                {
                    "interface_name": "Ethernet2.1724",
                    "parent_interface": "Ethernet2",
                    "dot1q_vlan": 1724,
                    "type": "subinterface",
                    "vrf": "",
                }
            ],
        )

    def test_ios_and_junos_create_reach_drain_and_apply_request(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.delivery import MODE_STORE_ONLY

        from .test_apply_selector import _ApplyContractAdapter, _promoted

        with without_commit_drain(), transaction.atomic():
            ios = Interface.objects.create(device=self.device, name="GigabitEthernet0/1", type="1000base-t")
            junos = Interface.objects.create(device=self.device, name="ae99", type="lag")
        self._create(ios, 300, 300)
        self._create(junos, 7, 100)
        adapter = _ApplyContractAdapter(
            lambda selected: (202, {**_promoted(selected), "device_id": self.management.adapter_device_id})
        )
        config, session = adapter.patches()
        with config, session:
            drain.push_now(self.device.pk, "subinterface", mode=MODE_STORE_ONLY, force=True)
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[self.management.pk, "apply"]),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(len(adapter.apply_requests), 1)
        self.assertIn("subinterface", adapter.apply_requests[0]["selected"])
        requests = [request for request in adapter.requests if request["url"].endswith("/subinterface-intent")]
        self.assertGreaterEqual(len(requests), 2)
        expected = {
            "GigabitEthernet0/1.300": ("GigabitEthernet0/1", 300),
            "ae99.7": ("ae99", 100),
        }
        for request in requests:
            carried = {
                row["interface_name"]: (row["parent_interface"], row["dot1q_vlan"])
                for row in request["body"]["interfaces"]
            }
            self.assertEqual(carried, expected)
        self.assertTrue(all(request["params"].get("store_only") == "true" for request in requests))
        receipt = next(receipt for url, receipt in adapter.receipts.items() if url.endswith("/subinterface-intent"))
        self.assertEqual(adapter.apply_requests[0]["selected"]["subinterface"], receipt["push_seq"])

    def test_bad_owned_row_stays_visible_and_blocks_apply_until_repaired(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.views import ApplyPreparationRefused, _prepare_apply

        with without_commit_drain(), transaction.atomic():
            parent = Interface.objects.create(device=self.device, name="ae99", type="lag")
        self._create(parent, 7, 100)
        state = NSOSubinterfaceState.objects.get(management=self.management, interface__name="ae99.7")
        adapter = ReceiptAdapter()
        config, session = adapter.patches()
        with config, session:
            drain.push_now(self.device.pk, "subinterface", force=True)
            acknowledged = [request for request in adapter.requests if request["url"].endswith("/subinterface-intent")]
            self.assertEqual(acknowledged[-1]["body"]["interfaces"][0]["dot1q_vlan"], 100)
            NSOSubinterfaceState.objects.filter(pk=state.pk).update(dot1q_vlan=0)
            drain.push_now(self.device.pk, "subinterface", force=True)
            self.management.refresh_from_db()
            self.assertEqual(self.management.intent_push_errors["subinterface"]["code"], "validation_error")
            category = self.client.get(
                reverse(
                    "plugins:netbox_nso_plugin:device_nso_category",
                    kwargs={"pk": self.device.pk, "key": "subinterface"},
                )
            )
            self.assertContains(category, "validation_error")
            with self.assertRaises(ApplyPreparationRefused) as raised:
                _prepare_apply(self.management)
            self.assertEqual(raised.exception.key, "subinterface")
            self.assertEqual(
                [request for request in adapter.requests if request["url"].endswith("/subinterface-intent")],
                acknowledged,
            )
            NSOSubinterfaceState.objects.filter(pk=state.pk).update(dot1q_vlan=100)
            drain.push_now(self.device.pk, "subinterface", force=True)
        delivered = [request for request in adapter.requests if request["url"].endswith("/subinterface-intent")]
        self.assertGreater(len(delivered), len(acknowledged))
        self.assertEqual(delivered[-1]["body"]["interfaces"][0]["dot1q_vlan"], 100)

    def test_deleted_native_child_reaches_adapter_with_deletion_authority(self):
        from netbox_nso_plugin import drain

        with without_commit_drain(), transaction.atomic():
            parent = Interface.objects.create(device=self.device, name="ae99", type="lag")
        self._create(parent, 7, 100)
        state = NSOSubinterfaceState.objects.get(management=self.management, interface__name="ae99.7")
        adapter = ReceiptAdapter()
        config, session = adapter.patches()
        with config, session:
            drain.push_now(self.device.pk, "subinterface", force=True)
            with without_commit_drain():
                Interface.objects.filter(pk=state.interface_id).delete()
            drain.push_now(self.device.pk, "subinterface", force=True)
        requests = [request for request in adapter.requests if request["url"].endswith("/subinterface-intent")]
        self.assertGreaterEqual(len(requests), 2)
        self.assertEqual(requests[0]["body"]["interfaces"][0]["interface_name"], "ae99.7")
        self.assertEqual(requests[-1]["body"], {"interfaces": []})
        self.assertEqual(requests[-1]["params"].get("delete_origin"), "true")
