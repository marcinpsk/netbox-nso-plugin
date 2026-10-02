# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Junos LACP observations create native topology that survives Accept and Apply."""

from copy import deepcopy

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.contrib.messages import SUCCESS, get_messages
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.urls import reverse

from netbox_nso_plugin import status_machine as sm
from netbox_nso_plugin.models import (
    NSOLACPBundleState,
    NSOLACPMemberState,
    NSOOwnershipManifest,
    NSOSwitchingRootDeletion,
)
from netbox_nso_plugin.reconcile import run_device_reconcile

from ._adapter_http import _REAL_SESSION, make_response
from ._outbox_case import make_managed, reset_renderer_audit_rotation, without_commit_drain
from ._settlement_case import _evidence_generation
from .mixins import IntentPushResetMixin, _CascadeFlushMixin
from .test_apply_selector import _promoted
from .test_native_only_acquisition import NativeOnlyAdapter


class LACPNativeTopologyAdapter(NativeOnlyAdapter):
    """Serve LACP observations and settled Apply evidence at the HTTP boundary."""

    def __init__(self):
        super().__init__()
        self.attempts = {}
        self.evidence_reads = []
        self.apply_response = self.promote

    def session(self):
        adapter = self

        class AdapterSession(_REAL_SESSION):
            def request(self, method, url, **kwargs):
                return adapter._handle(method, url, **kwargs)

        return AdapterSession()

    def promote(self, selected):
        result = _promoted(selected)
        result["device_id"] = self.device_id
        result["generations"] = result["generations"][:1]
        generation = result["generations"][0]
        generation["generation_id"] += len(self.attempts)
        generation["seq"] += len(self.attempts)
        result["job_id"] += len(self.attempts)
        generation["job_id"] = result["job_id"]
        return 202, result

    def _handle(self, method, url, **kwargs):
        if method == "POST" and url.endswith("/deployment-evidence"):
            requested = kwargs["json"]["apply_attempt_ids"]
            self.evidence_reads.append(deepcopy(requested))
            return make_response(
                200,
                {
                    "device_id": self.device_id,
                    "head": None,
                    "blocked": False,
                    "write_work_pending": False,
                    "held_jobs": [],
                    "pending_generations": 0,
                    "attempts": [self.attempts[attempt_id] for attempt_id in requested if attempt_id in self.attempts],
                    "unknown_apply_attempt_ids": [
                        attempt_id for attempt_id in requested if attempt_id not in self.attempts
                    ],
                },
            )
        response = super()._handle(method, url, **kwargs)
        if method == "POST" and url.endswith("/actions/apply"):
            result = response.json()
            generations = []
            for admitted in result["generations"]:
                generation = _evidence_generation(admitted["generation_id"], result["selected"], ("lag",))
                generation.update(
                    seq=admitted["seq"],
                    stream_revisions=admitted["stream_revisions"],
                    source_push_seq=admitted["source_push_seq"],
                    carrier_job_id=admitted["job_id"],
                )
                generations.append(generation)
            self.attempts[kwargs["json"]["apply_attempt_id"]] = {
                "apply_attempt_id": kwargs["json"]["apply_attempt_id"],
                "admission_state": "admitted",
                "http_status": response.status_code,
                "response": deepcopy(result),
                "generations": generations,
            }
        return response


class TestLACPNativeTopology(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = LACPNativeTopologyAdapter()
        for patcher in self.adapter.patches():
            patcher.start()
            self.addCleanup(patcher.stop)
        reset_renderer_audit_rotation(self)
        user = get_user_model().objects.create_superuser(username="lacp-topology-admin", password="test-password")
        self.client.force_login(user)
        with without_commit_drain(), transaction.atomic():
            self.device, self.management = make_managed("lacp-native-topology", self.adapter.device_id)
            type(self.management).objects.filter(pk=self.management.pk).update(
                manage_interfaces=True, manage_description=True, manage_enabled=True
            )
            self.management.refresh_from_db()
            self.bundle_interface = Interface.objects.create(device=self.device, name="ae1", type="other")
            self.member_interfaces = [
                Interface.objects.create(device=self.device, name=name, type="100gbase-x-qsfp28")
                for name in ("et-0/0/1", "et-0/0/2")
            ]
        self.adapter.documents["interfaces-doc"] = {
            "interfaces": [
                {"name": interface.name, "attrs": {}} for interface in (self.bundle_interface, *self.member_interfaces)
            ]
        }
        self.adapter.documents["lag-config"] = {
            "bundles": [
                {
                    "name": "ae1",
                    "lag_id": 1,
                    "members": [
                        {"interface_name": "et-0/0/1", "mode": "active"},
                        {"interface_name": "et-0/0/2", "mode": "active"},
                    ],
                }
            ]
        }

    def assert_reconcile_ran(self):
        self.adapter.reads.clear()
        with self.assertNoLogs("netbox_nso_plugin", level="ERROR"):
            result = run_device_reconcile(self.device.pk)
        for key in ("error", "skipped", "deferred"):
            self.assertNotIn(key, result, result)
        self.assertIn("lag-config", self.adapter.reads)

    def assert_native_topology(self):
        self.bundle_interface.refresh_from_db()
        self.assertEqual(self.bundle_interface.type, "lag", "LACP reconcile must model the reported bundle in NetBox")
        for interface in self.member_interfaces:
            interface.refresh_from_db()
            self.assertEqual(interface.lag_id, self.bundle_interface.pk, interface.name)

    def reconcile_and_accept_bundle(self):
        self.assertEqual(self.bundle_interface.type, "other")
        self.assertTrue(all(interface.lag_id is None for interface in self.member_interfaces))
        self.assert_reconcile_ran()
        self.assert_native_topology()
        bundle = NSOLACPBundleState.objects.get(management=self.management, interface=self.bundle_interface)
        members = list(NSOLACPMemberState.objects.filter(management=self.management).order_by("interface__name"))
        self.assertEqual([row.interface_id for row in members], [interface.pk for interface in self.member_interfaces])
        self.lacp_states = [bundle, *members]
        self.assertEqual([row.status for row in self.lacp_states], ["imported"] * 3)
        response = self.client.post(reverse("plugins:netbox_nso_plugin:lacp_accept_bundle", args=(bundle.pk,)))
        self.assertIn(response.status_code, (302, 200), response.content)
        messages = list(get_messages(response.wsgi_request))
        self.assertIn((SUCCESS, "Accepted LACP bundle ae1."), [(message.level, str(message)) for message in messages])
        self.manifests = [
            NSOOwnershipManifest.objects.get(
                device_id=self.device.pk,
                scope="lacp",
                native_model_label="dcim.interface",
                native_id=row.interface_id,
                state_model_label=row._meta.label_lower,
            )
            for row in self.lacp_states
        ]
        self.assert_owned()

    def assert_owned(self):
        manifests = NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="lacp")
        self.assertEqual(set(manifests.values_list("pk", flat=True)), {manifest.pk for manifest in self.manifests})
        for row, manifest in zip(self.lacp_states, self.manifests, strict=True):
            row.refresh_from_db()
            self.assertIn(row.status, sm.OWNED_STATES, row)
            manifest.refresh_from_db()
            self.assertEqual(manifest.ownership_state, "owned", manifest)
            self.assertEqual(manifest.grant_kind, "accept", manifest)
            self.assertEqual(manifest.native_id, row.interface_id)

    def assert_in_sync(self):
        for row in self.lacp_states:
            row.refresh_from_db()
            self.assertEqual(row.status, "in_sync", row)

    def apply_device(self):
        request_start = len(self.adapter.requests)
        apply_start = len(self.adapter.apply_requests)
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "ok", response.json())
        self.assertEqual(len(self.adapter.apply_requests), apply_start + 1)
        return self.adapter.requests[request_start:]

    def assert_lacp_intent(self, requests):
        bodies = [request["body"] for request in requests if request["url"].endswith("/lag-config/apply")]
        self.assertTrue(bodies, "Apply must prepare a LACP snapshot")
        for body in bodies:
            self.assertEqual(body["deleted_roots"], [])
            self.assertEqual([(bundle["name"], bundle["lag_id"]) for bundle in body["bundles"]], [("ae1", 1)])
            self.assertEqual(
                sorted((member["interface_name"], member["mode"]) for member in body["bundles"][0]["members"]),
                [("et-0/0/1", "active"), ("et-0/0/2", "active")],
            )
        self.assertFalse(NSOSwitchingRootDeletion.objects.filter(management__device=self.device).exists())
        for request in self.adapter.requests:
            body = request["body"]
            if isinstance(body, dict) and "deleted_roots" in body:
                self.assertEqual(body["deleted_roots"], [], request["url"])

    def test_accepted_junos_bundle_survives_apply(self):
        self.reconcile_and_accept_bundle()
        requests = self.apply_device()
        self.assert_owned()
        self.assert_native_topology()
        self.assert_lacp_intent(requests)

    def test_owned_bundle_survives_two_empty_observations(self):
        self.reconcile_and_accept_bundle()
        for member in self.lacp_states[1:]:
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:overlay_field_edit", args=("lacp_member", member.pk)),
                {"port_priority": "200"},
            )
            self.assertEqual(response.status_code, 200, response.content)
        self.assert_owned()
        self.assertEqual([row.status for row in self.lacp_states], ["accepted"] * 3)
        requests = self.apply_device()
        self.assert_owned()
        self.assert_lacp_intent(requests)
        self.assertEqual([row.status for row in self.lacp_states], ["deploying"] * 3)
        attempt_id = self.adapter.apply_requests[-1]["apply_attempt_id"]
        self.assertEqual({str(row.apply_attempt_id) for row in self.lacp_states}, {attempt_id})
        self.adapter.evidence_reads.clear()
        self.assert_reconcile_ran()
        self.assertTrue(self.adapter.evidence_reads, "Reconcile must read the Apply result from the adapter")
        self.assertIn(attempt_id, self.adapter.evidence_reads[-1])
        self.assert_in_sync()
        self.assert_owned()
        self.assert_native_topology()
        self.adapter.documents["lag-config"] = {"bundles": []}
        for observation in range(2):
            with self.subTest(observation=observation + 1):
                self.assert_reconcile_ran()
                self.assert_in_sync()
                self.assert_owned()
                self.assert_native_topology()
        requests = self.apply_device()
        self.assert_owned()
        self.assert_native_topology()
        self.assert_lacp_intent(requests)

    def test_missing_reported_member_refuses_bundle_accept(self):
        self.adapter.documents["lag-config"]["bundles"][0]["members"].append(
            {"interface_name": "et-0/0/3", "mode": "active"}
        )
        self.assert_reconcile_ran()
        bundle = NSOLACPBundleState.objects.get(management=self.management, interface=self.bundle_interface)
        response = self.client.post(reverse("plugins:netbox_nso_plugin:lacp_accept_bundle", args=(bundle.pk,)))
        self.assertIn("et-0/0/3", " ".join(str(message) for message in get_messages(response.wsgi_request)))
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="lacp").exists())

    def test_grid_accept_refusal_returns_the_reason_as_json(self):
        self.adapter.documents["lag-config"]["bundles"][0]["members"].append(
            {"interface_name": "et-0/0/3", "mode": "active"}
        )
        self.assert_reconcile_ran()
        bundle = NSOLACPBundleState.objects.get(management=self.management, interface=self.bundle_interface)
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:lacp_accept_bundle", args=(bundle.pk,)),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("et-0/0/3", response.json()["message"])
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="lacp").exists())

    def test_last_member_removal_prunes_projected_topology(self):
        self.assert_reconcile_ran()
        self.adapter.documents["lag-config"] = {"bundles": []}
        self.assert_reconcile_ran()
        self.assertFalse(NSOLACPMemberState.objects.filter(management=self.management).exists())
        self.assertFalse(NSOLACPBundleState.objects.filter(management=self.management).exists())
        for interface in self.member_interfaces:
            interface.refresh_from_db()
            self.assertIsNone(interface.lag_id)

    def test_missing_owned_static_member_retires_bundle_on_audit(self):
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        for member in self.adapter.documents["lag-config"]["bundles"][0]["members"]:
            member.update(mode="on", port_priority=200)
        self.reconcile_and_accept_bundle()
        NSOLACPMemberState.objects.filter(pk=self.lacp_states[1].pk).delete()
        with without_commit_drain():
            reconcile_scope_ownership(self.device.pk, ("lacp",))
        self.assert_retired_bundle()

    def test_missing_owned_bundle_retires_episode_on_audit(self):
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        self.reconcile_and_accept_bundle()
        NSOLACPBundleState.objects.filter(pk=self.lacp_states[0].pk).delete()
        with without_commit_drain():
            reconcile_scope_ownership(self.device.pk, ("lacp",))
        self.assert_retired_bundle()

    def test_observation_retires_episode_before_replacing_lost_member(self):
        self.reconcile_and_accept_bundle()
        NSOLACPMemberState.objects.filter(pk=self.lacp_states[1].pk).delete()
        self.assert_reconcile_ran()
        replacement = NSOLACPMemberState.objects.get(interface=self.member_interfaces[0], management=self.management)
        self.assertEqual(replacement.status, "imported")
        self.assert_retired_bundle()

    def assert_retired_bundle(self):
        self.assertEqual(
            set(
                NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="lacp").values_list(
                    "ownership_state", flat=True
                )
            ),
            {"retired"},
        )
        for model in (NSOLACPBundleState, NSOLACPMemberState):
            self.assertFalse(model.objects.filter(management=self.management, status__in=sm.OWNED_STATES).exists())
        self.assertFalse(NSOSwitchingRootDeletion.objects.filter(management=self.management).exists())
        requests = self.apply_device()
        bodies = [request["body"] for request in requests if request["url"].endswith("/lag-config/apply")]
        self.assertTrue(bodies, "Apply must prepare the relinquished LACP snapshot")
        for body in bodies:
            self.assertEqual(body["bundles"], [])
            self.assertEqual(body["deleted_roots"], [])

    def bundle_state(self):
        return NSOLACPBundleState.objects.get(management=self.management, interface=self.bundle_interface)

    def assert_acquisition_refused(self):
        bundle = self.bundle_state()
        response = self.client.post(reverse("plugins:netbox_nso_plugin:lacp_accept_bundle", args=(bundle.pk,)))
        self.assertEqual(response.status_code, 302)
        text = " ".join(str(message) for message in get_messages(response.wsgi_request))
        self.assertTrue(text)
        self.assertFalse(
            NSOOwnershipManifest.objects.filter(
                device_id=self.device.pk, scope="lacp", ownership_state="owned"
            ).exists()
        )
        for model in (NSOLACPBundleState, NSOLACPMemberState):
            self.assertFalse(model.objects.filter(management=self.management, status__in=sm.OWNED_STATES).exists())
        return text

    def test_netbox_refuses_connected_bundle_without_native_write(self):
        from netbox_nso_plugin.lacp_topology import native_validation_message

        self.bundle_interface.mark_connected = True
        with without_commit_drain():
            self.bundle_interface.save(update_fields=("mark_connected",))
        reason = native_validation_message(self.bundle_interface, type="lag")
        self.assertTrue(reason)
        with self.assertLogs("netbox_nso_plugin.lacp_reconciler", level="WARNING") as logs:
            self.assert_reconcile_ran()
        warning = " ".join(logs.output)
        for expected in (self.device.name, self.bundle_interface.name, reason):
            self.assertIn(expected, warning)
        self.bundle_interface.refresh_from_db()
        self.assertEqual(self.bundle_interface.type, "other")
        self.assertIn(reason, self.assert_acquisition_refused())

    def test_netbox_refuses_cabled_bundle_without_native_write(self):
        from dcim.models import Cable, CableTermination

        from netbox_nso_plugin.lacp_topology import native_validation_message

        with without_commit_drain():
            cable = Cable.objects.create(status="connected")
            CableTermination.objects.create(cable=cable, cable_end="A", termination=self.bundle_interface)
            CableTermination.objects.create(cable=cable, cable_end="B", termination=self.member_interfaces[0])
        self.bundle_interface.refresh_from_db()
        reason = native_validation_message(self.bundle_interface, type="lag")
        self.assertTrue(reason)
        with self.assertLogs("netbox_nso_plugin.lacp_reconciler", level="WARNING") as logs:
            self.assert_reconcile_ran()
        self.assertIn(reason, " ".join(logs.output))
        self.bundle_interface.refresh_from_db()
        self.assertEqual(self.bundle_interface.type, "other")
        self.assertIn(reason, self.assert_acquisition_refused())

    def test_rejected_native_member_refuses_accept_and_timer_edit(self):
        from netbox_nso_plugin.lacp_topology import native_validation_message

        rejected = self.member_interfaces[1]
        rejected.type = "virtual"
        with without_commit_drain():
            rejected.save(update_fields=("type",))
        with self.assertLogs("netbox_nso_plugin.lacp_reconciler", level="WARNING"):
            self.assert_reconcile_ran()
        self.bundle_interface.refresh_from_db()
        reason = native_validation_message(rejected, lag=self.bundle_interface)
        self.assertTrue(reason)
        self.assertIn(reason, self.assert_acquisition_refused())
        bundle = self.bundle_state()
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", args=("lacp_bundle", bundle.pk)), {"timer": "fast"}
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn(reason, str(response.json()))
        bundle.refresh_from_db()
        self.assertEqual(bundle.timer, "")
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="lacp").exists())

    def test_native_only_membership_is_not_mirrored_acquired_or_rendered(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.lacp_reconciler import reconcile_lag_config

        self.reconcile_and_accept_bundle()
        with without_commit_drain():
            native_only = Interface.objects.create(
                device=self.device, name="et-0/0/3", type="100gbase-x-qsfp28", lag=self.bundle_interface
            )
            reconcile_lag_config(self.device, {"bundles": []})
        native_only.refresh_from_db()
        self.assertEqual(native_only.lag_id, self.bundle_interface.pk)
        self.assertFalse(NSOLACPMemberState.objects.filter(interface=native_only).exists())
        self.assertFalse(
            NSOOwnershipManifest.objects.filter(
                device_id=self.device.pk, scope="lacp", native_id=native_only.pk
            ).exists()
        )
        body = delivery.render("lacp", self.device.pk, self.management.adapter_device_id).payload
        self.assertEqual([member["interface_name"] for member in body[0]["members"]], ["et-0/0/1", "et-0/0/2"])

    def test_owned_native_membership_and_bundle_type_are_never_overwritten(self):
        from netbox_nso_plugin.lacp_reconciler import reconcile_lag_config

        self.reconcile_and_accept_bundle()
        with without_commit_drain():
            alternate = Interface.objects.create(device=self.device, name="ae2", type="other")
            self.bundle_interface.type = "other"
            self.bundle_interface.save(update_fields=("type",))
            payload = {
                "bundles": [
                    {"name": "ae1", "lag_id": 1, "members": []},
                    {
                        "name": alternate.name,
                        "lag_id": 2,
                        "members": [{"interface_name": self.member_interfaces[0].name}],
                    },
                ]
            }
            reconcile_lag_config(self.device, payload)
        self.bundle_interface.refresh_from_db()
        self.member_interfaces[0].refresh_from_db()
        self.assertEqual(self.bundle_interface.type, "other")
        self.assertEqual(self.member_interfaces[0].lag_id, self.bundle_interface.pk)

    def test_owned_member_move_changes_both_bundle_fingerprints_and_rendering(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.intent_state import canonical_fragment
        from netbox_nso_plugin.lacp_reconciler import reconcile_lag_config
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        self.reconcile_and_accept_bundle()
        with without_commit_drain():
            second = Interface.objects.create(device=self.device, name="ae2", type="other")
            reconcile_lag_config(self.device, {"bundles": [{"name": "ae2", "lag_id": 2, "members": []}]})
        second_state = NSOLACPBundleState.objects.get(interface=second, management=self.management)
        self.client.post(reverse("plugins:netbox_nso_plugin:lacp_accept_bundle", args=(second_state.pk,)))
        first_state = self.bundle_state()
        before = (canonical_fragment(first_state), canonical_fragment(second_state))
        revision_before = NSOIntentRevision.objects.get(device=self.device, scope="lacp").revision
        member = self.member_interfaces[0]
        member.refresh_from_db()
        second.refresh_from_db()
        member.lag = second
        plan = RendererMutationPlan.build(saves=(planned_save(member, update_fields=("lag",)),))
        with without_commit_drain():
            with renderer_writes(plan) as writer:
                writer.save(member, update_fields=("lag",))
            self.assertEqual(reconcile_scope_ownership(self.device.pk, ("lacp",)), ())
        first_state.refresh_from_db()
        second_state.refresh_from_db()
        after = (canonical_fragment(first_state), canonical_fragment(second_state))
        self.assertNotEqual(before[0], after[0])
        self.assertNotEqual(before[1], after[1])
        self.assertEqual(
            NSOIntentRevision.objects.get(device=self.device, scope="lacp").revision,
            revision_before + 1,
        )
        body = delivery.render("lacp", self.device.pk, self.management.adapter_device_id).payload
        self.assertEqual(
            {item["name"]: sorted(member["interface_name"] for member in item["members"]) for item in body},
            {"ae1": ["et-0/0/2"], "ae2": ["et-0/0/1"]},
        )
        self.assertTrue(
            all(
                row.ownership_state == "owned"
                for row in NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="lacp")
            )
        )

    def test_clearing_owned_native_member_retracts_only_that_member(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        self.reconcile_and_accept_bundle()
        member = self.member_interfaces[0]
        member.refresh_from_db()
        member.lag = None
        with without_commit_drain():
            member.save(update_fields=("lag",))
            reconcile_scope_ownership(self.device.pk, ("lacp",))
        self.manifests[1].refresh_from_db()
        self.assertEqual(self.manifests[1].ownership_state, "retired")
        self.manifests[0].refresh_from_db()
        self.assertEqual(self.manifests[0].ownership_state, "owned")
        body = delivery.render("lacp", self.device.pk, self.management.adapter_device_id).payload
        self.assertEqual([member["interface_name"] for member in body[0]["members"]], ["et-0/0/2"])
        self.assertFalse(NSOSwitchingRootDeletion.objects.filter(management=self.management).exists())

    def test_member_overlay_missing_before_selection_refuses_accept(self):
        self.assert_reconcile_ran()
        NSOLACPMemberState.objects.filter(interface=self.member_interfaces[0], management=self.management).delete()
        self.assertIn(self.member_interfaces[0].name, self.assert_acquisition_refused())

    def test_native_overlay_member_not_reported_refuses_accept(self):
        self.assert_reconcile_ran()
        bundle = self.bundle_state()
        bundle.observed_members = [self.member_interfaces[0].name]
        bundle.save(update_fields=("observed_members",))
        self.assertIn(self.member_interfaces[1].name, self.assert_acquisition_refused())

    def test_vpc_bundle_refuses_inline_timer_acquisition(self):
        self.adapter.documents["lag-config"]["bundles"][0]["vpc_sensitive"] = True
        self.assert_reconcile_ran()
        bundle = self.bundle_state()
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", args=("lacp_bundle", bundle.pk)), {"timer": "fast"}
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("vPC", str(response.json()))
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="lacp").exists())

    def _interleave_plan(self, action, *, before_build=False):
        from unittest.mock import patch

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan

        build = RendererMutationPlan.build
        fired = []

        def interleaved(*args, **kwargs):
            selected = not fired
            if selected:
                fired.append(True)
            if selected and before_build:
                action()
            plan = build(*args, **kwargs)
            if selected and not before_build:
                action()
            return plan

        return patch.object(RendererMutationPlan, "build", new=interleaved)

    def test_member_move_between_selection_and_lock_refuses_accept(self):
        self.assert_reconcile_ran()
        with without_commit_drain():
            alternate = Interface.objects.create(device=self.device, name="ae2", type="lag")

        def move():
            native = Interface.objects.get(pk=self.member_interfaces[0].pk)
            native.lag = alternate
            native.save(update_fields=("lag",))

        with without_commit_drain(), self._interleave_plan(move):
            self.assertIn("et-0/0/1", self.assert_acquisition_refused())

    def test_member_added_between_selection_and_lock_refuses_accept(self):
        self.assert_reconcile_ran()

        def add():
            native = Interface.objects.create(
                device=self.device, name="et-0/0/3", type="100gbase-x-qsfp28", lag=self.bundle_interface
            )
            NSOLACPMemberState.objects.create(
                management=self.management, interface=native, mode="active", status="imported"
            )

        with without_commit_drain(), self._interleave_plan(add):
            self.assertIn("et-0/0/3", self.assert_acquisition_refused())

    def test_member_overlay_deleted_between_selection_and_lock_refuses_accept(self):
        self.assert_reconcile_ran()

        def delete():
            NSOLACPMemberState.objects.filter(interface=self.member_interfaces[0], management=self.management).delete()

        with without_commit_drain(), self._interleave_plan(delete):
            self.assertIn("et-0/0/1", self.assert_acquisition_refused())

    def test_vpc_flag_flip_between_selection_and_lock_refuses_accept(self):
        self.assert_reconcile_ran()

        def protect():
            bundle = self.bundle_state()
            bundle.vpc_sensitive = True
            bundle.save(update_fields=("vpc_sensitive",))

        with without_commit_drain(), self._interleave_plan(protect):
            self.assertIn("vPC", self.assert_acquisition_refused())

    def test_inline_timer_edit_refuses_membership_changed_before_lock(self):
        self.assert_reconcile_ran()

        def unlink():
            native = Interface.objects.get(pk=self.member_interfaces[0].pk)
            native.lag = None
            native.save(update_fields=("lag",))

        bundle = self.bundle_state()
        with without_commit_drain(), self._interleave_plan(unlink):
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:overlay_field_edit", args=("lacp_bundle", bundle.pk)),
                {"timer": "fast"},
            )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("Refresh", response.json()["message"])
        bundle.refresh_from_db()
        self.assertEqual(bundle.timer, "")
        self.assertEqual(bundle.status, "imported")
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="lacp").exists())

    def test_inline_member_edit_refuses_member_moved_after_validation(self):
        from unittest.mock import patch

        from netbox_nso_plugin import lacp_topology

        self.assert_reconcile_ran()
        with without_commit_drain():
            alternate = Interface.objects.create(device=self.device, name="ae2", type="lag")
        member = NSOLACPMemberState.objects.get(management=self.management, interface=self.member_interfaces[0])
        select = lacp_topology.member_states

        def moved_then_select(bundle_state):
            # A reconcile moves the member and publishes both observations, so only the member check sees it.
            if Interface.objects.filter(pk=self.member_interfaces[0].pk).update(lag=alternate):
                NSOLACPBundleState.objects.filter(pk=self.bundle_state().pk).update(
                    observed_members=[self.member_interfaces[1].name]
                )
            return select(bundle_state)

        with without_commit_drain(), patch.object(lacp_topology, "member_states", new=moved_then_select):
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:overlay_field_edit", args=("lacp_member", member.pk)),
                {"port_priority": "200"},
            )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("no longer a member", response.json()["message"])
        member.refresh_from_db()
        self.assertIsNone(member.port_priority)
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="lacp").exists())

    def test_inline_member_edit_refuses_bundle_deleted_after_validation(self):
        self.assert_reconcile_ran()
        bundle = self.bundle_state()
        member = NSOLACPMemberState.objects.get(management=self.management, interface=self.member_interfaces[0])
        deleted = []

        def delete_before_lookup(execute, sql, params, many, context):
            # Delete the real row when the save resolves its previously validated bundle.
            if not deleted and 'FROM "netbox_nso_plugin_nsolacpbundlestate"' in sql and "LIMIT 21" in sql:
                deleted.append(bundle.pk)
                NSOLACPBundleState.objects.filter(pk=bundle.pk).delete()
            return execute(sql, params, many, context)

        with without_commit_drain(), connection.execute_wrapper(delete_before_lookup):
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:overlay_field_edit", args=("lacp_member", member.pk)),
                {"port_priority": "200"},
            )
        self.assertEqual(deleted, [bundle.pk])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("tracked LACP bundle", response.json()["message"])
        member.refresh_from_db()
        self.assertIsNone(member.port_priority)
        self.assertEqual(member.status, "imported")
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="lacp").exists())

    def test_member_rename_between_selection_and_lock_refuses_accept(self):
        self.assert_reconcile_ran()

        def rename():
            native = Interface.objects.get(pk=self.member_interfaces[0].pk)
            native.name = "et-0/0/3"
            native.save(update_fields=("name",))

        with without_commit_drain(), self._interleave_plan(rename):
            self.assertIn("et-0/0/1", self.assert_acquisition_refused())

    def test_bundle_type_flip_between_selection_and_lock_refuses_accept(self):
        self.assert_reconcile_ran()

        def retype():
            native = Interface.objects.get(pk=self.bundle_interface.pk)
            native.type = "other"
            native.save(update_fields=("type",))

        with without_commit_drain(), self._interleave_plan(retype):
            self.assertIn("as a LAG", self.assert_acquisition_refused())

    def _gated_lacp(self, payload, *, attempt_id=1):
        from netbox_nso_plugin.lacp_reconciler import lacp_reconcile_plan, reconcile_lag_config
        from netbox_nso_plugin.read_gate import gated_family_run

        from .test_gated_reconcile import _rs

        with without_commit_drain():
            return gated_family_run(
                self.management,
                "lag_config",
                _rs(attempt_id=attempt_id),
                lambda: reconcile_lag_config(self.device, payload),
                epoch=self.management.adapter_device_id,
                pre_body=lambda: lacp_reconcile_plan(self.device, payload),
            )

    def test_gated_last_member_removal_executes_frozen_saves_and_deletes(self):
        from netbox_nso_plugin.read_gate import RAN

        self.assertEqual(self._gated_lacp(self.adapter.documents["lag-config"]).disposition, RAN)
        self.assertEqual(self._gated_lacp({"bundles": []}, attempt_id=2).disposition, RAN)
        self.assertFalse(NSOLACPBundleState.objects.filter(management=self.management).exists())
        self.assertFalse(NSOLACPMemberState.objects.filter(management=self.management).exists())
        for member in self.member_interfaces:
            member.refresh_from_db()
            self.assertIsNone(member.lag_id)

    def _accept_without_plan_wrapper(self):
        bundle = self.bundle_state()
        response = self.client.post(reverse("plugins:netbox_nso_plugin:lacp_accept_bundle", args=(bundle.pk,)))
        self.assertIn(
            (SUCCESS, "Accepted LACP bundle ae1."), [(m.level, str(m)) for m in get_messages(response.wsgi_request)]
        )

    def test_gated_native_removal_cannot_overwrite_accept_during_plan_construction(self):
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        self.assert_reconcile_ran()
        with self._interleave_plan(self._accept_without_plan_wrapper, before_build=True):
            result = self._gated_lacp({"bundles": []})
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assert_native_topology()
        self.assertEqual(
            NSOOwnershipManifest.objects.filter(
                device_id=self.device.pk, scope="lacp", ownership_state="owned"
            ).count(),
            3,
        )
        self.assertTrue(
            all(row.status == "in_sync" for row in NSOLACPMemberState.objects.filter(management=self.management))
        )

    def test_gated_empty_bundle_prune_cannot_delete_accept_during_plan_construction(self):
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        self.adapter.documents["lag-config"]["bundles"][0]["members"] = []
        self.assert_reconcile_ran()
        with self._interleave_plan(self._accept_without_plan_wrapper, before_build=True):
            result = self._gated_lacp({"bundles": []})
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        bundle = self.bundle_state()
        self.assertEqual(bundle.status, "in_sync")
        self.assertEqual(
            NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="lacp").ownership_state, "owned"
        )

    def test_gated_observation_retires_lost_member_episode(self):
        from netbox_nso_plugin.read_gate import RAN

        self.reconcile_and_accept_bundle()
        NSOLACPMemberState.objects.filter(pk=self.lacp_states[1].pk).delete()
        result = self._gated_lacp(self.adapter.documents["lag-config"])
        self.assertEqual(result.disposition, RAN)
        self.assert_retired_bundle()

    def test_observed_members_are_read_only_in_rest(self):
        from netbox_nso_plugin.api.serializers import NSOLACPBundleStateSerializer, NSOLACPMemberStateSerializer

        self.assert_reconcile_ran()
        bundle = self.bundle_state()
        serializer = NSOLACPBundleStateSerializer(
            instance=bundle, data={"observed_members": ["et-0/0/3"]}, partial=True
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn("observed_members", serializer.validated_data)
        self.assertEqual(serializer.data["observed_members"], ["et-0/0/1", "et-0/0/2"])
        relations = {
            field.name for field in NSOLACPMemberStateSerializer.Meta.model._meta.concrete_fields if field.is_relation
        }
        self.assertEqual(relations, {"management", "interface"})

    def test_grid_shows_device_members_and_bundle_absence(self):
        self.reconcile_and_accept_bundle()
        self.adapter.documents["lag-config"] = {"bundles": []}
        self.assert_reconcile_ran()
        response = self.client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_category", args=(self.device.pk, "lacp")) + "?format=json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()["rows"][0]
        self.assertEqual(row["observed_members"], [])
        self.assertFalse(row["device_present"])
        self.assertEqual(sorted(member["interface"]["name"] for member in row["members"]), ["et-0/0/1", "et-0/0/2"])

    def test_reported_member_does_not_overwrite_existing_netbox_only_membership(self):
        from netbox_nso_plugin.lacp_reconciler import reconcile_lag_config

        with without_commit_drain():
            native_bundle = Interface.objects.create(device=self.device, name="ae2", type="lag")
            member = self.member_interfaces[0]
            member.lag = native_bundle
            member.save(update_fields=("lag",))
            reconcile_lag_config(self.device, self.adapter.documents["lag-config"])
            reconcile_lag_config(self.device, self.adapter.documents["lag-config"])
        member.refresh_from_db()
        self.assertEqual(member.lag_id, native_bundle.pk)
        self.assertFalse(NSOLACPMemberState.objects.filter(interface=member).exists())
        self.assertIn(member.name, self.assert_acquisition_refused())

    def test_member_with_non_lag_or_cross_device_parent_is_unqualified(self):
        from netbox_nso_plugin.lacp_topology import bundle_of, member_interfaces
        from netbox_nso_plugin.ownership_grants import OwnershipGrant
        from netbox_nso_plugin.ownership_planner import OwnershipNotQualified
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
            renderer_writes,
        )

        with without_commit_drain():
            other_device, _management = make_managed("foreign-lacp-device", None)
            parents = [self.bundle_interface, Interface.objects.create(device=other_device, name="ae2", type="lag")]
            for parent in parents:
                with self.subTest(parent=parent.name):
                    member = self.member_interfaces[0]
                    member.lag = parent
                    member.save(update_fields=("lag",))
                    self.assertIsNone(bundle_of(member))
                    self.assertFalse(member_interfaces(self.device.pk).filter(pk=member.pk).exists())
                    state = NSOLACPMemberState.objects.create(
                        management=self.management, interface=member, status="imported"
                    )
                    state.status = "accepted"
                    with self.assertRaises(OwnershipNotQualified):
                        plan = RendererMutationPlan.build(
                            grant=OwnershipGrant("accept"), saves=(planned_save(state, update_fields=("status",)),)
                        )
                        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
                        with mutation(plan) as writer:
                            writer.save(state, update_fields=("status",))
                    self.assertFalse(
                        NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="lacp").exists()
                    )
                    state.delete()
