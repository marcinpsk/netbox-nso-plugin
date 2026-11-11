# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Native Intend through real models, writer locks and ordinary delivery."""

from copy import deepcopy

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.test import TestCase

from netbox_nso_plugin import delivery
from netbox_nso_plugin.intent_acquisition import confirm_intend, preview_intend
from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOInterfaceMtuState, NSOOwnershipManifest

from ._adapter_http import make_apply_preflight, make_response
from ._outbox_case import ReceiptAdapter, make_managed, without_commit_drain
from .mixins import IntentPushResetMixin


class IntendAdapter(ReceiptAdapter):
    """Replace adapter HTTP with complete candidate responses and real receipts."""

    def __init__(self):
        super().__init__()
        self.candidates = []
        self.promotes = False
        self.identity = "a" * 64
        self.plan_digest = "b" * 64
        self.before_response = None
        self.error_response = None

    def _handle(self, method, url, **kwargs):
        if url.endswith("/actions/apply-preflight"):
            assert method == "POST"
            if self.error_response is not None:
                return self.error_response
            candidates = deepcopy(kwargs["json"]["candidates"])
            self.candidates.append(candidates)
            wire = make_apply_preflight(dict.fromkeys(candidates, 1))
            wire.pop("preflight_identity")
            wire.update(
                candidate_identity=self.identity,
                plan_digest=self.plan_digest,
                delivery_effect={"promotes": self.promotes},
            )
            if self.promotes:
                wire["delivery_effect"]["effects"] = [{"stream": next(iter(candidates)), "kind": "removal"}]
            if self.before_response is not None:
                self.before_response()
            return make_response(200, wire)
        return super()._handle(method, url, **kwargs)


class TestIntendAcquisition(IntentPushResetMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("intend", 1788)
        self.interface = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t", mtu=9000)
        self.user = get_user_model().objects.create_user(username="intend-operator", is_superuser=True)
        self.adapter = IntendAdapter()
        for patcher in self.adapter.patches():
            patcher.start()
            self.addCleanup(patcher.stop)
        self.selection = [
            {"scope": "interface_mtu", "native_model": "dcim.interface", "native_id": self.interface.pk, "options": {}}
        ]

    def test_native_mtu_wins_without_native_mutation(self):
        row = NSOInterfaceMtuState.objects.create(
            management=self.management, interface=self.interface, status="error", l2_mtu=1500, ip_mtu=1400
        )
        preview = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(preview.status, "ready", preview.message)
        row.refresh_from_db()
        self.assertEqual((row.status, row.l2_mtu), ("error", 1500))
        self.assertEqual(self.adapter.candidates[0]["interface_mtu"]["interfaces"][0]["mtu"], 9000)
        with without_commit_drain():
            result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "acquired", result.message)
        self.assertEqual(result.message, "acquired; delivery pending")
        row.refresh_from_db()
        self.interface.refresh_from_db()
        self.assertEqual((row.status, row.l2_mtu, row.ip_mtu), ("accepted", 9000, 1400))
        self.assertEqual(self.interface.mtu, 9000)
        self.assertIsNotNone(row.accepted_at)
        self.assertEqual(NSOOwnershipManifest.objects.get(device_id=self.device.pk).grant_kind, "intend")
        self.assertTrue(
            NSOIntentOutboxEntry.objects.filter(device=self.device, scope="interface_mtu", kind="ordinary").exists()
        )
        self.assertEqual(delivery.render("interface_mtu", self.device.pk, 1788).payload[0]["mtu"], 9000)

    def snapshot(self):
        from netbox_nso_plugin.models import NSOIntentRevision, NSOOwnershipAcquisition

        return [
            list(model.objects.order_by("pk").values())
            for model in (
                NSOInterfaceMtuState,
                NSOOwnershipManifest,
                NSOOwnershipAcquisition,
                NSOIntentRevision,
                NSOIntentOutboxEntry,
            )
        ]

    def test_preview_refuses_adapter_promotion_without_writes(self):
        self.adapter.promotes = True
        before = self.snapshot()
        result = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(result.status, "refused")
        self.assertIn("interface_mtu: removal", result.message)
        self.assertIn("No acquisition occurred", result.message)
        self.assertIn("ordinary delivery", result.message)
        self.assertNotIn("manual Apply", result.message)
        self.assertEqual(self.snapshot(), before)

    def test_confirm_refuses_changed_delivery_effect_without_writes(self):
        preview = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(preview.status, "ready", preview.message)
        before = self.snapshot()
        self.adapter.promotes = True
        result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "refused")
        self.assertIn("interface_mtu: removal", result.message)
        self.assertEqual(self.snapshot(), before)

    def stale_confirmation(self, change):
        row = NSOInterfaceMtuState.objects.create(
            management=self.management, interface=self.interface, status="imported", l2_mtu=1500
        )
        preview = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(preview.status, "ready", preview.message)
        change(row)
        before = self.snapshot()
        result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "refused", result.message)
        self.assertNotIn("ordinary delivery", result.message)
        self.assertIn("Preview again", result.message)
        self.assertEqual(self.snapshot(), before)

    def test_confirm_rejects_changed_native_projection(self):
        def change(_row):
            self.interface.mtu = 8500
            self.interface.save(update_fields=("mtu",))

        self.stale_confirmation(change)

    def test_confirm_rejects_changed_ownership(self):
        from ._outbox_case import content_update

        self.stale_confirmation(lambda row: content_update(row, status="accepted"))

    def test_confirm_rejects_changed_mapping(self):
        from netbox_nso_plugin.models import NSODeviceManagement

        self.stale_confirmation(
            lambda _row: NSODeviceManagement.objects.filter(pk=self.management.pk).update(adapter_device_id=1789)
        )

    def test_confirm_rejects_changed_baseline(self):
        from netbox_nso_plugin.models import NSOIntentRevision

        def change(_row):
            revision, _created = NSOIntentRevision.objects.get_or_create(device=self.device, scope="interface_mtu")
            revision.revision += 1
            revision.save(update_fields=("revision",))

        self.stale_confirmation(change)

    def test_confirm_rejects_changed_execution_inputs(self):
        self.stale_confirmation(lambda _row: setattr(self.adapter, "plan_digest", "c" * 64))

    def test_confirm_requires_both_adapter_hashes(self):
        self.stale_confirmation(lambda _row: setattr(self.adapter, "identity", "c" * 64))

    def test_confirm_ignores_management_delivery_and_settlement_records(self):
        from netbox_nso_plugin.models import NSODeviceManagement

        NSOInterfaceMtuState.objects.create(
            management=self.management, interface=self.interface, status="imported", l2_mtu=1500
        )
        preview = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(preview.status, "ready", preview.message)
        NSODeviceManagement.objects.filter(pk=self.management.pk).update(
            intent_push_attempts={"vlan": 7}, intent_push_errors={"vlan": "failed"}, settle_cursor_seq=7
        )
        with without_commit_drain():
            result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "acquired", result.message)

    def test_confirm_ignores_management_records_written_during_its_preflight(self):
        from netbox_nso_plugin.models import NSODeviceManagement

        preview = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(preview.status, "ready", preview.message)
        self.adapter.before_response = lambda: NSODeviceManagement.objects.filter(pk=self.management.pk).update(
            intent_push_attempts={"vlan": 8}, settle_cursor_seq=8
        )
        with without_commit_drain():
            result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "acquired", result.message)

    def test_adapter_error_text_does_not_reach_the_refusal(self):
        from netbox_nso_plugin.adapter_client import _PUBLIC_ERROR_DEFAULT

        marker = "intermediary-body-marker"
        error = make_response(502, content=f"<html>{marker}</html>".encode())
        self.adapter.error_response = error
        refused = preview_intend(self.management, self.user, self.selection)
        self.adapter.error_response = None
        preview = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(preview.status, "ready", preview.message)
        self.adapter.error_response = error
        confirmed = confirm_intend(self.management, self.user, preview.confirmation)
        for result in (refused, confirmed):
            self.assertEqual(result.status, "refused")
            self.assertNotIn(marker, result.message)
            self.assertIn(_PUBLIC_ERROR_DEFAULT, result.message)

    def test_busy_device_refusal_names_the_adapter_job_and_logs_the_failure(self):
        busy = {"code": "conflict", "message": "A job is already queued or running", "detail": {"job_id": 4711}}
        self.adapter.error_response = make_response(409, {"error": busy})
        before = self.snapshot()
        with self.assertLogs("netbox_nso_plugin.intent_acquisition", "WARNING") as logs:
            result = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(result.status, "refused")
        self.assertIn("Adapter job 4711 is queued or running for this device.", result.message)
        self.assertIn("No acquisition occurred", result.message)
        self.assertIn("conflict", logs.output[0])
        self.assertEqual(self.snapshot(), before)

    def test_confirmation_is_signed_and_bound_to_operator_and_management(self):
        from django.core import signing

        preview = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(preview.status, "ready", preview.message)
        evidence = signing.loads(preview.confirmation, salt="netbox_nso_plugin.intent_acquisition")
        self.assertEqual(evidence["operator"], self.user.pk)
        self.assertEqual(evidence["management"], self.management.pk)
        self.assertEqual(evidence["selection"][0]["native_id"], self.interface.pk)
        self.assertEqual(evidence["candidate_identity"], "a" * 64)
        self.assertEqual(evidence["plan_digest"], "b" * 64)
        self.assertIn("timestamp", evidence)
        self.assertIn("correlations", evidence)
        self.assertNotIn("bodies", evidence)
        other = get_user_model().objects.create_user(username="another-operator", is_superuser=True)
        before = self.snapshot()
        result = confirm_intend(self.management, other, preview.confirmation)
        self.assertEqual(result.status, "refused")
        result = confirm_intend(self.management, self.user, preview.confirmation + "tampered")
        self.assertEqual(result.status, "refused")
        self.assertEqual(self.snapshot(), before)

    def test_expired_confirmation_changes_nothing(self):
        import time
        from unittest.mock import patch

        from netbox_nso_plugin.intent_acquisition import CONFIRMATION_MAX_AGE

        with patch("django.core.signing.time.time", return_value=time.time() - CONFIRMATION_MAX_AGE - 10):
            preview = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(preview.status, "ready", preview.message)
        before = self.snapshot()
        result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "refused")
        self.assertIn("Preview again", result.message)
        self.assertIn("No acquisition occurred.", result.message)
        self.assertNotIn("ordinary delivery", result.message)
        self.assertEqual(self.snapshot(), before)

    def test_unexpected_value_error_propagates_from_preview_and_confirm(self):
        preview = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(preview.status, "ready", preview.message)

        def fail_unexpectedly():
            raise ValueError("unexpected candidate invariant")

        self.adapter.before_response = fail_unexpectedly
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "unexpected candidate invariant"):
            preview_intend(self.management, self.user, self.selection)
        with self.assertRaisesRegex(ValueError, "unexpected candidate invariant"):
            confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(self.snapshot(), before)

    def test_input_and_permission_refusals_give_relevant_recovery(self):
        before = self.snapshot()
        result = preview_intend(self.management, self.user, [])
        self.assertEqual(result.status, "refused")
        self.assertIn("Select", result.message)
        self.assertNotIn("ordinary delivery", result.message)
        self.assertIn("No acquisition occurred.", result.message)
        user = get_user_model().objects.create_user(username="intend-without-permissions")
        result = preview_intend(self.management, user, self.selection)
        self.assertEqual(result.status, "refused")
        self.assertIn("Permission denied", result.message)
        self.assertNotIn("ordinary delivery", result.message)
        self.assertIn("permissions", result.message)
        self.assertEqual(self.snapshot(), before)

    def test_ownership_refusals_name_the_overlay_for_operators(self):
        preview = preview_intend(self.management, self.user, self.selection)
        with without_commit_drain():
            self.assertEqual(confirm_intend(self.management, self.user, preview.confirmation).status, "acquired")
        owned = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(owned.status, "refused")
        self.assertIn("NSO Interface MTU State row", owned.message)
        NSOOwnershipManifest.objects.filter(device_id=self.device.pk).update(ownership_state="retired")
        ended = preview_intend(self.management, self.user, self.selection)
        self.assertIn("Explicit restart is required for NSO Interface MTU State on Ethernet1.", ended.message)
        for result in (owned, ended):
            self.assertNotIn("netbox_nso_plugin.", result.message)

    def test_duplicate_confirmation_does_not_create_another_episode(self):
        preview = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(preview.status, "ready", preview.message)
        with without_commit_drain():
            first = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(first.status, "acquired", first.message)
        before = self.snapshot()
        second = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(second.status, "refused")
        self.assertEqual(self.snapshot(), before)

    def test_deferred_scopes_and_auto_apply_refuse(self):
        from netbox_nso_plugin.models import NSODeviceManagement

        for scope in ("svi", "subinterface", "bfd", "snmp", "logging", "l2_sap"):
            selection = [{**self.selection[0], "scope": scope}]
            result = preview_intend(self.management, self.user, selection)
            self.assertEqual(result.status, "refused")
        NSODeviceManagement.objects.filter(pk=self.management.pk).update(auto_apply=True)
        result = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(result.status, "refused")
        self.assertFalse(self.adapter.candidates)

    def test_retained_owner_without_acceptance_timestamp_refuses_before_http(self):
        from django.db import connection

        from ._ownership_case import acquire_overlay

        retained_interface = Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t", mtu=1500)
        with without_commit_drain():
            retained = acquire_overlay(
                NSOInterfaceMtuState,
                management=self.management,
                interface=retained_interface,
                status="accepted",
                l2_mtu=1500,
            )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE netbox_nso_plugin_nsointerfacemtustate SET accepted_at = NULL WHERE id = %s", [retained.pk]
            )
        before = self.snapshot()
        result = preview_intend(self.management, self.user, self.selection)
        self.assertEqual(result.status, "refused")
        self.assertIn(f"row {retained.pk}", result.message)
        self.assertIn("acceptance timestamp", result.message)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.adapter.candidates)

    def test_every_enabled_native_family_acquires_with_render_parity(self):
        from netbox_nso_plugin.ownership_planner import manifest_binding

        from ._candidate_render_case import render_candidates

        rows, _relations = render_candidates(self.device, self.management, 3)
        selection = []
        for row in rows:
            binding = manifest_binding(row)
            self.assertIsNotNone(binding, row)
            _rule, scope, _device_id, label, native_id, _native_key, _state_label, state_key = binding
            item = {
                "scope": scope,
                "native_model": label,
                "native_id": native_id,
                "state_key": state_key,
                "options": {},
            }
            if row._meta.model_name == "nsolacpbundlestate":
                item["options"]["lag_id"] = 1
            selection.append(item)
        preview = preview_intend(self.management, self.user, selection)
        self.assertEqual(preview.status, "ready", preview.message)
        with without_commit_drain():
            result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "acquired", result.message)
        self.assertEqual(
            set(result.keys),
            {
                "interface",
                "ip",
                "interface_mtu",
                "vlan",
                "static_route",
                "isis",
                "isis_flex_algo",
                "ospf",
                "bgp",
                "route_policy",
                "lacp",
                "switchport",
            },
        )
        self.assertEqual(
            NSOOwnershipManifest.objects.filter(device_id=self.device.pk, grant_kind="intend").count(), len(rows)
        )
        scheduled = set(
            NSOIntentOutboxEntry.objects.filter(device=self.device, kind="ordinary").values_list("scope", flat=True)
        )
        self.assertEqual(scheduled, set(result.keys))

    def test_unlinked_policy_observation_acquires_the_selected_native_root(self):
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState

        policy = PrefixList.objects.create(name="INTEND-PREFIXES", family=4)
        row = NSORoutePolicyState.objects.create(
            management=self.management, family="prefix_list", object_name=policy.name, status="conflict"
        )
        selection = [
            {
                "scope": "route_policy",
                "native_model": "netbox_routing.prefixlist",
                "native_id": policy.pk,
                "options": {},
            }
        ]
        preview = preview_intend(self.management, self.user, selection)
        self.assertEqual(preview.status, "ready", preview.message)
        with without_commit_drain():
            result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "acquired", result.message)
        row.refresh_from_db()
        self.assertEqual(row.assigned_object, policy)
        self.assertEqual(row.status, "accepted")

    def test_native_switching_mode_does_not_become_lacp_member_mode(self):
        from netbox_nso_plugin.models import NSOLACPMemberState

        bundle = Interface.objects.create(device=self.device, name="Port-channel9", type="lag")
        member = Interface.objects.create(
            device=self.device, name="Ethernet9", type="1000base-t", lag=bundle, mode="tagged-all"
        )
        selection = [
            {"scope": "lacp", "native_model": "dcim.interface", "native_id": member.pk, "options": {}},
            {"scope": "lacp", "native_model": "dcim.interface", "native_id": bundle.pk, "options": {"lag_id": 9}},
        ]
        preview = preview_intend(self.management, self.user, selection)
        self.assertEqual(preview.status, "ready", preview.message)
        with without_commit_drain():
            result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "acquired", result.message)
        self.assertEqual(NSOLACPMemberState.objects.get(interface=member).mode, "")
        member.refresh_from_db()
        self.assertEqual(member.mode, "tagged-all")
