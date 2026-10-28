# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Catalog refusal presentation through real settlement, HTTP views and templates."""

import copy

from dcim.models import Interface
from django.db import transaction
from django.test import SimpleTestCase
from django.urls import reverse

from netbox_nso_plugin.models import NSOInterfaceState

from ._adapter_http import make_apply_preview, make_response
from ._bgp_settlement import BGPSettlementHelpers
from ._outbox_case import without_commit_drain
from ._ownership_case import acquire_overlay
from ._refusal_display import RefusalDisplayHelpers
from .test_apply_selector import _promoted
from .test_durable_bgp_capture import BGPCaptureCase


class TestRefusalDisplay(RefusalDisplayHelpers, BGPSettlementHelpers, BGPCaptureCase):
    def refused_bgp(self, refused=1):
        from datetime import datetime

        from ._outbox_case import mirror_update
        from .test_read_gate import _INC_A

        mirror_update(
            self.management,
            adapter_incarnation=_INC_A[0],
            adapter_incarnation_born=datetime.fromisoformat(_INC_A[1]),
            adapter_source_epoch=1,
            source_epoch_aware=True,
        )
        with without_commit_drain():
            self.management.manage_routing = True
            self.management.save(update_fields=("manage_routing",))
        self.prepare()
        generation = self.terminal(refused=refused)
        self.settle(generation)
        self._serve_bgp_observation(generation)
        self.category("bgp", refresh=1)
        return generation["refusals"][0]

    def interface_execution(
        self, *, unavailable=False, matching=False, prior_status="accepted", disposition=None, prior_error=None
    ):
        with without_commit_drain(), transaction.atomic():
            self.management.manage_interfaces = True
            self.management.manage_description = True
            self.management.save(update_fields=("manage_interfaces", "manage_description"))
            iface = Interface.objects.create(
                device=self.device, name="Ethernet1", type="1000base-t", description="placeholder desired"
            )
            row = acquire_overlay(
                NSOInterfaceState,
                interface=iface,
                attribute="description",
                status="accepted",
                nso_value="placeholder desired" if matching else "placeholder observed",
            )
        self.apply_owners([row], prior_status=prior_status, prior_error=prior_error)
        disposition = disposition or ("unavailable" if unavailable else "refused")
        generation = self.evidence(
            refused=row if disposition == "refused" else None,
            dispositions={"interface.description": disposition},
        )
        from netbox_nso_plugin.refusal_registry import SECTION_COUNTER_KEYS

        for section in generation["sections"]:
            for counter in SECTION_COUNTER_KEYS[section]:
                generation["carrier_job_result"].setdefault(
                    counter, dict.fromkeys(("in_sync", "apply_failed", "not_applied", "unavailable"), 0)
                )
        self.settle(generation)
        from netbox_nso_plugin.lifecycle_fence import read_refresh
        from netbox_nso_plugin.template_content import _upsert_interface_states

        with read_refresh():
            _upsert_interface_states(
                self.device,
                [
                    {
                        "name": iface.name,
                        "attrs": {
                            "description": {
                                "status": "in_sync",
                                "nso_value": "placeholder desired" if matching else "placeholder observed",
                                "last_apply_error": {},
                            }
                        },
                    }
                ],
            )
        row.refresh_from_db()
        return iface, row, generation

    def test_device_tab_shows_refusal_reason_and_message(self):
        cause = self.refused_bgp()
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"{cause['reason']}: {cause['message']}")

    def test_interface_badge_shows_catalog_reason(self):
        iface, row, generation = self.interface_execution()
        cause = generation["refusals"][0]
        response = self.client.get(iface.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"{cause['reason']}: {cause['message']}")

    def test_interface_views_keep_ordinary_json_error_without_message(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.template_content import _upsert_interface_states

        error = {"code": "placeholder_failure", "detail": "<placeholder-error> & detail"}
        with without_commit_drain():
            iface = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")
        interfaces = [
            {
                "name": iface.name,
                "attrs": {
                    "description": {
                        "status": "apply_failed",
                        "nso_value": "placeholder observed",
                        "last_apply_error": error,
                    }
                },
            }
        ]
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/interfaces"):
                return make_response(200, interfaces)
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        with without_commit_drain():
            _upsert_interface_states(self.device, adapter_client.get_interfaces(1558))
        row = NSOInterfaceState.objects.get(interface=iface, attribute="description")
        self.assertEqual(row.last_apply_error, error)
        for url in (row.get_absolute_url(), iface.get_absolute_url()):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertContains(response, "placeholder_failure")
                self.assertContains(response, "&lt;placeholder-error&gt; &amp; detail")
                self.assertNotContains(response, "<placeholder-error>")
        cell = self.category("interface", format="json").json()["rows"][0]["description"]
        self.assertIsNone(cell["refusal"])
        self.assertIn("placeholder_failure", cell["error"])
        response = self.rest_client().get(
            reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-detail", args=(row.pk,))
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["last_apply_error"], error)

    def test_peer_refusal_restores_router_and_sibling_history(self):
        cause = self.refused_bgp()
        payload = self.category("bgp", format="json").json()
        peer = next(row for row in payload["peers"]["rows"] if row["pk"] == self.owners[1].pk)
        self.assertEqual(peer["state"], "apply_failed")
        self.assertEqual(peer.get("refusal_text"), f"{cause['reason']}: {cause['message']}")
        self.assertEqual(peer["refusal"]["causes"][0]["message_id"], cause["message_id"])
        for section, owner in (("routers", self.owners[0]), ("peers", self.owners[2])):
            restored = next(row for row in payload[section]["rows"] if row["pk"] == owner.pk)
            self.assertEqual(restored["status"], self.prior[(owner._meta.label_lower, owner.pk)][0])
            self.assertIsNone(restored["refusal"])
            self.assertEqual(restored["error"], self.prior[(owner._meta.label_lower, owner.pk)][2])
            self.assertEqual(restored["error_history"], restored["state"] == "in_sync")
        self.assertContains(self.category("bgp"), cause["reason"])

    def test_unavailable_execution_keeps_prior_state_with_advisory(self):
        self.prepare()
        generation = self.terminal(refused=None, disposition="unavailable")
        self.settle(generation)
        payload = self.category("bgp", format="json").json()
        for section in ("routers", "peers"):
            for row in payload[section]["rows"]:
                self.assertIn("unavailable", row.get("advisory") or "")
                self.assertIsNone(row["refusal"])
                self.assertTrue(row["label"].startswith("previous "))
        self.assertContains(self.category("bgp"), "unavailable")

    def test_rest_exposes_unavailable_settlement_on_prior_in_sync_owner(self):
        _iface, row, _generation = self.interface_execution(unavailable=True, prior_status="in_sync", prior_error={})
        response = self.rest_client().get(
            reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-detail", args=(row.pk,))
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["status"], "in_sync")
        self.assertEqual(data["last_apply_error"], {})
        self.assertEqual(data.get("state"), "unavailable")
        self.assertEqual(data.get("execution_disposition"), "unavailable")
        self.assertIn("unavailable", data.get("advisory", ""))

    def test_invalid_retained_evidence_keeps_current_unavailable_advisory(self):
        from ._outbox_case import mirror_update

        _iface, row, _generation = self.interface_execution(unavailable=True, prior_status="in_sync", prior_error={})
        mirror_update(row, last_apply_error={"refusals": [{"message": "placeholder-secret-canary"}]})
        response = self.rest_client().get(
            reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-detail", args=(row.pk,))
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("placeholder-secret-canary", response.content.decode())
        data = response.json()
        self.assertEqual(data["state"], "apply_failed")
        self.assertEqual(data["refusal_text"], "invalid refusal evidence")
        self.assertEqual(data["execution_disposition"], "unavailable")
        self.assertIn("unavailable", data["advisory"])

    def test_preview_includes_invalid_evidence_on_in_sync_ip_owner(self):
        from netbox_nso_plugin.models import NSOInterfaceIPState

        from ._outbox_case import mirror_update

        with without_commit_drain():
            iface = Interface.objects.create(device=self.device, name="Ethernet71", type="1000base-t")
            row = acquire_overlay(NSOInterfaceIPState, interface=iface, address="198.18.0.1/24", status="accepted")
        mirror_update(row, status="in_sync", last_apply_error={"refusals": [{"message": "placeholder-canary"}]})
        grid = self.category("interface", format="json").json()
        self.assertEqual(grid["rows"][0]["ips"][0]["kind"], "apply_failed")
        result = self.preview(make_apply_preview(device_id=1558, diffs={})).json()
        self.assertFalse(result["nothing_pending"])
        self.assertEqual(result["total"], 1)

    def test_mixed_interface_grid_filters_the_same_bucket_it_counts(self):
        from netbox_nso_plugin.models import NSOInterfaceIPState

        with without_commit_drain():
            self.management.manage_interfaces = True
            self.management.manage_description = True
            self.management.save(update_fields=("manage_interfaces", "manage_description"))
            iface = Interface.objects.create(
                device=self.device, name="Ethernet71", type="1000base-t", description="placeholder desired"
            )
            acquire_overlay(
                NSOInterfaceState,
                interface=iface,
                attribute="description",
                status="accepted",
                nso_value="placeholder observed",
            )
            NSOInterfaceIPState.objects.create(interface=iface, address="198.18.0.1/24", status="changed")
        payload = self.category("interface", format="json").json()
        self.assertEqual(payload["counts"]["drift"], 1)
        self.assertEqual(payload["rows"][0]["state"], "drift")
        self.assertEqual(payload["rows"][0]["bucket"], "drift")
        self.assertFalse(payload["rows"][0]["pending"])

    def test_registry_owner_rest_requires_view_permission_and_refuses_writes(self):
        from django.test import override_settings
        from rest_framework.test import APIClient
        from users.constants import TOKEN_PREFIX
        from users.models import Token, User

        with without_commit_drain():
            iface = Interface.objects.create(device=self.device, name="Ethernet71", type="1000base-t")
            row = NSOInterfaceState.objects.create(interface=iface, attribute="description", status="imported")
        url = reverse("plugins-api:netbox_nso_plugin-api:owner-state-detail", args=(row._meta.model_name, row.pk))
        api = self.rest_client()
        self.assertEqual(api.get(url).status_code, 200)
        self.assertEqual(api.post(url, {}).status_code, 405)
        self.assertEqual(APIClient().get(url).status_code, 403)
        user = User.objects.create_user(username="placeholder-unprivileged-operator")
        token = Token.objects.create(user=user)
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}")
        with override_settings(EXEMPT_VIEW_PERMISSIONS=[]):
            # A missing model permission is 403; an excluded row is 404 (#1709).
            self.assertEqual(api.get(url).status_code, 403)

    def test_preview_refusal_keeps_diffs_and_identity_fields(self):
        cause = self.refused_bgp()
        preview = make_apply_preview(
            device_id=1558, status="refused", refusals=[cause], diffs={"device_intent": "placeholder diff"}
        )
        result = self.preview(preview).json()
        self.assertEqual(result["device_diff"], preview["diffs"])
        self.assertEqual(result["generation_id"], preview["generation_id"])
        self.assertEqual(result["document_digest"], preview["document_digest"])
        self.assertEqual(result["refusal_text"], f"{cause['reason']}: {cause['message']}")
        self.assertFalse(result["diff_available"])
        self.assertFalse(result["nothing_pending"])
        for field in ("changes", "routing_changes", "total", "refusals"):
            self.assertIn(field, result)

    def test_preview_unavailable_lacp_member_prevents_nothing_pending(self):
        from netbox_nso_plugin.models import NSOLACPBundleState, NSOLACPMemberState

        with without_commit_drain(), transaction.atomic():
            interface = Interface.objects.create(device=self.device, name="Port-channel71", type="lag")
            bundle = acquire_overlay(
                NSOLACPBundleState, management=self.management, interface=interface, lag_id=71, status="accepted"
            )
            member_iface = Interface.objects.create(
                device=self.device, name="Ethernet71", type="1000base-t", lag=interface
            )
            member = acquire_overlay(
                NSOLACPMemberState, management=self.management, interface=member_iface, status="accepted"
            )
        self.apply_owners([bundle, member], prior_status="in_sync")
        self.settle(self.evidence(dispositions={"lag.bundle.member": "unavailable"}))
        rows = self.category("lacp", format="json").json()["rows"]
        self.assertIn("unavailable", rows[0]["members"][0]["advisory"])
        result = self.preview(make_apply_preview(device_id=1558, diffs={})).json()
        self.assertTrue(result["diff_available"])
        self.assertFalse(result["nothing_pending"])
        self.assertEqual(result["total"], 1)
        change = result["routing_changes"][0]
        self.assertEqual(change["item"], member_iface.name)
        self.assertEqual(change["status"], "in_sync")
        self.assertIn("unavailable", change["advisory"])
        self.assertIsNone(change["refusal"])
        self.assertEqual(change["error"], self.prior[(member._meta.label_lower, member.pk)][2])

    def test_preview_includes_independent_ip_mtu_member_and_flex_algo_refusals(self):
        from ipam.models import IPAddress
        from netbox_routing.models import ISISFlexAlgo, ISISInstance

        from netbox_nso_plugin import models
        from netbox_nso_plugin.refusal_registry import SECTION_COUNTER_KEYS, document_section_for_stream

        with without_commit_drain(), transaction.atomic():
            self.management.manage_routing = True
            self.management.manage_isis = True
            self.management.manage_interfaces = True
            self.management.save(update_fields=("manage_routing", "manage_isis", "manage_interfaces"))
            iface = Interface.objects.create(device=self.device, name="Ethernet71", type="1000base-t", mtu=9000)
            IPAddress.objects.create(address="198.18.0.1/24", assigned_object=iface)
            ip = acquire_overlay(
                models.NSOInterfaceIPState, interface=iface, address="198.18.0.1/24", status="accepted"
            )
            mtu = acquire_overlay(
                models.NSOInterfaceMtuState,
                management=self.management,
                interface=iface,
                l2_mtu=9000,
                status="accepted",
            )
            bundle_iface = Interface.objects.create(device=self.device, name="Port-channel71", type="lag")
            bundle = acquire_overlay(
                models.NSOLACPBundleState,
                management=self.management,
                interface=bundle_iface,
                lag_id=71,
                status="accepted",
            )
            iface.lag = bundle_iface
            iface.save(update_fields=("lag",))
            member = acquire_overlay(
                models.NSOLACPMemberState, management=self.management, interface=iface, status="accepted"
            )
            instance = ISISInstance.objects.create(
                device=self.device, process_tag="placeholder-process", net="49.0001.0000.0000.0001.00"
            )
            native = ISISFlexAlgo.objects.create(instance=instance, algo_id=130)
            flex_algo = acquire_overlay(
                models.NSOISISFlexAlgoState,
                management=self.management,
                isis_flex_algo=native,
                process_tag=instance.process_tag,
                algo_id=130,
                status="accepted",
            )
        self.apply_owners([ip, mtu, bundle, member, flex_algo], prior_status="in_sync")
        generation = self.evidence(
            dispositions=dict.fromkeys(
                ("interface.ipv4-address", "mtu.interface", "lag.bundle.member", "isis.process-config.flex-algo"),
                "refused",
            )
        )
        generation["sections"] = sorted({document_section_for_stream(stream) for stream in generation["sections"]})
        for section in generation["sections"]:
            for counter in SECTION_COUNTER_KEYS[section]:
                generation["carrier_job_result"].setdefault(
                    counter, dict.fromkeys(("in_sync", "apply_failed", "not_applied", "unavailable"), 0)
                )
        self.settle(generation)
        result = self.preview(make_apply_preview(device_id=1558, diffs={})).json()
        expected = {"Interface IP", "Interface MTU", "LACP member", "IS-IS Flex-Algo"}
        self.assertEqual({change["category"] for change in result["routing_changes"]}, expected)
        self.assertEqual(result["total"], 4)
        self.assertFalse(result["nothing_pending"])
        for change in result["routing_changes"]:
            with self.subTest(category=change["category"]):
                self.assertTrue(change["refusal"]["valid"])
                self.assertTrue(change["refusal_text"].startswith("inexpressible-on-ned: "))
                self.assertEqual(change["error"], change["refusal_text"])

    def flex_algo_execution(self, disposition):
        from netbox_routing.models import ISISFlexAlgo, ISISInstance

        from netbox_nso_plugin import models

        with without_commit_drain(), transaction.atomic():
            self.management.manage_routing = True
            self.management.manage_isis = True
            self.management.save(update_fields=("manage_routing", "manage_isis"))
            native = ISISInstance.objects.create(
                device=self.device, process_tag="placeholder-process", net="49.0001.0000.0000.0001.00"
            )
            instance = acquire_overlay(
                models.NSOISISInstanceState,
                management=self.management,
                isis_instance=native,
                process_tag=native.process_tag,
                net=native.net,
                status="accepted",
            )
            iface = Interface.objects.create(device=self.device, name="Ethernet71", type="1000base-t")
            interface = acquire_overlay(
                models.NSOISISInterfaceState,
                management=self.management,
                interface=iface,
                process_tag=native.process_tag,
                af="ipv4",
                status="accepted",
            )
            algo = ISISFlexAlgo.objects.create(instance=native, algo_id=130)
            flex_algo = acquire_overlay(
                models.NSOISISFlexAlgoState,
                management=self.management,
                isis_flex_algo=algo,
                process_tag=native.process_tag,
                algo_id=130,
                status="accepted",
            )
        self.apply_owners([instance, interface, flex_algo], prior_status="in_sync")
        generation = self.evidence(
            dispositions={
                "isis.process-config": "in_sync",
                "isis.interface-config": "in_sync",
                "isis.process-config.flex-algo": disposition,
            }
        )
        from netbox_nso_plugin.refusal_registry import document_section_for_stream

        generation["sections"] = sorted({document_section_for_stream(stream) for stream in generation["sections"]})
        self.settle(generation)
        return flex_algo

    def assert_flex_algo_pending_display(self, disposition):
        from netbox_nso_plugin.refusal_display import UNAVAILABLE

        owner = self.flex_algo_execution(disposition)
        with self.subTest(surface="category counts"):
            counts = self.client.get(
                reverse("plugins:netbox_nso_plugin:device_nso_category_counts", args=(self.device.pk,))
            ).json()["categories"]["isis"]
            self.assertEqual(counts["pending"], 1)
            self.assertEqual(counts["total"], 3)
            self.assertEqual(counts["drift"], 0)
        with self.subTest(surface="device tab"):
            response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
            self.assertEqual(response.status_code, 200)
            category = next(cat for cat in response.context["nso_categories"] if cat["key"] == "isis")
            self.assertEqual(category["counts"]["pending"], 1)
        with self.subTest(surface="category grid"):
            payload = self.category("isis", format="json").json()
            self.assertIn("flex_algos", payload)
            rows = payload["flex_algos"]["rows"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["pk"], owner.pk)
            self.assertEqual(rows[0]["algo_id"], 130)
            self.assertEqual(payload["flex_algos"]["counts"], {"all": 1, "drift": 0, "pending": 1})
            for sibling in ("instances", "interfaces"):
                self.assertEqual(payload[sibling]["rows"][0]["state"], "in_sync")
                self.assertEqual(payload[sibling]["counts"]["pending"], 0)
            if disposition == "refused":
                self.assertEqual(rows[0]["state"], "apply_failed")
                self.assertTrue(rows[0]["refusal"]["valid"])
                self.assertTrue(rows[0]["refusal_text"].startswith("inexpressible-on-ned: "))
            else:
                self.assertEqual(rows[0]["state"], "unavailable")
                self.assertIsNone(rows[0]["refusal"])
                self.assertEqual(rows[0]["advisory"], UNAVAILABLE)
                self.assertEqual(rows[0]["status"], "in_sync")
        with self.subTest(surface="category HTML"):
            self.assertContains(self.category("isis"), 'class="nso-grid nso-isis-flex"')
        with self.subTest(surface="preview"):
            preview = self.preview(make_apply_preview(device_id=1558, diffs={})).json()
            self.assertEqual(preview["total"], 1)
            self.assertEqual(preview["routing_changes"][0]["category"], "IS-IS Flex-Algo")
            self.assertFalse(preview["nothing_pending"])

    def test_refused_flex_algo_is_pending_beside_in_sync_isis_siblings(self):
        self.assert_flex_algo_pending_display("refused")

    def test_unavailable_flex_algo_is_pending_beside_in_sync_isis_siblings(self):
        self.assert_flex_algo_pending_display("unavailable")

    def test_document_refusal_summary_requires_no_commit_proof(self):
        from netbox_nso_plugin.refusal_display import document_projection

        self.prepare()
        generation = self.terminal()
        projection = document_projection(generation["carrier_job_result"])
        self.assertIn("commit not attempted", projection["summary"])
        for phase, acknowledged in (
            ("pre_commit_dry_run", True),
            ("post_commit_verification", False),
            ("commit", None),
        ):
            result = copy.deepcopy(generation["carrier_job_result"])
            result["commit_acknowledged"] = acknowledged
            result["refusals"][0]["sender_phase"] = phase
            self.assertNotIn("nothing committed", document_projection(result)["summary"])

    def test_refusal_secret_canary_is_absent_from_ui_logs_and_errors(self):
        iface, row, generation = self.interface_execution()
        canary = "placeholder-forged-secret-canary"
        forged = copy.deepcopy(row.last_apply_error)
        forged["refusals"][0]["message"] = canary
        from ._outbox_case import mirror_update

        mirror_update(row, last_apply_error=forged)
        import logging

        with self.assertLogs("netbox_nso_plugin", level="DEBUG") as captured:
            logging.getLogger("netbox_nso_plugin").debug("Inspect refusal projection")
            responses = [
                self.client.get(iface.get_absolute_url()),
                self.category("interface"),
                self.category("interface", format="json"),
                self.category("interfaces"),
            ]
        self.assertNotIn(canary, "\n".join(captured.output))
        for response in responses:
            self.assertNotIn(canary, response.content.decode())
        self.assertContains(responses[0], "invalid refusal evidence")

    def test_forged_catalog_message_is_rejected(self):
        iface, row, generation = self.interface_execution()
        canary = "placeholder-forged-secret-canary"
        from netbox_nso_plugin import adapter_client

        from ._outbox_case import mirror_update

        mirror_update(row, last_apply_error={})
        forged = copy.deepcopy(generation["row_outcomes"][0]["refusals"][0])
        forged["message"] = canary
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/interfaces"):
                return make_response(
                    200,
                    [
                        {
                            "name": iface.name,
                            "attrs": {
                                "description": {
                                    "nso_value": "placeholder",
                                    "status": "apply_failed",
                                    "last_apply_error": {"refusals": [forged]},
                                }
                            },
                        }
                    ],
                )
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        with self.assertRaises(adapter_client.AdapterError) as raised:
            adapter_client.get_interfaces(1558)
        self.assertNotIn(canary, str(raised.exception))
        self.assertEqual(raised.exception.code, "invalid_response")

    def test_interface_rest_rejects_forged_stored_refusal(self):
        from ._outbox_case import mirror_update

        iface, row, generation = self.interface_execution()
        api = self.rest_client()
        detail = reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-detail", args=(row.pk,))
        listing = reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-list")
        cause = generation["row_outcomes"][0]["refusals"][0]
        valid = api.get(detail)
        self.assertEqual(valid.status_code, 200, valid.content)
        self.assertEqual(valid.json()["last_apply_error"]["refusals"][0]["message"], cause["message"])
        stored = copy.deepcopy(row.last_apply_error)
        for field in ("message", "message_id", "reason", "field"):
            with self.subTest(field=field):
                forged = copy.deepcopy(stored)
                forged["refusals"][0][field] = "placeholder-forged-secret-canary"
                forged["message"] = "placeholder-forged-secret-canary"
                mirror_update(row, last_apply_error=forged)
                for url in (detail, listing):
                    response = api.get(url)
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertNotIn("placeholder-forged-secret-canary", response.content.decode())
                    self.assertContains(response, "invalid refusal evidence")

    def test_interface_rest_projects_correlated_refusal_over_replaced_json_error(self):
        from ._outbox_case import mirror_update

        iface, row, generation = self.interface_execution()
        api = self.rest_client()
        detail = reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-detail", args=(row.pk,))
        listing = reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-list")
        cause = generation["row_outcomes"][0]["refusals"][0]
        for error in ({"message": "placeholder-secret-canary"}, {}):
            with self.subTest(error=error):
                mirror_update(row, last_apply_error=error)
                for url in (detail, listing):
                    with self.subTest(url=url):
                        response = api.get(url)
                        self.assertEqual(response.status_code, 200, response.content)
                        self.assertNotIn("placeholder-secret-canary", response.content.decode())
                        data = response.json()
                        projected = data["results"][0] if url == listing else data
                        self.assertEqual(projected["last_apply_error"]["refusals"], [cause])
                        self.assertEqual(
                            projected["last_apply_error"]["message"], f"{cause['reason']}: {cause['message']}"
                        )

    def test_management_rest_rejects_forged_cached_refusal(self):
        from ._outbox_case import mirror_update

        iface, row, generation = self.interface_execution()
        forged = copy.deepcopy(row.last_apply_error)
        forged["refusals"][0]["message"] = "placeholder-forged-secret-canary"
        mirror_update(
            self.management,
            state_snapshot={
                "interfaces": [
                    {
                        "name": iface.name,
                        "attrs": {"description": {"status": "apply_failed", "last_apply_error": forged}},
                    }
                ]
            },
        )
        response = self.rest_client().get(
            reverse("plugins-api:netbox_nso_plugin-api:nsodevicemanagement-detail", args=(self.management.pk,))
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("placeholder-forged-secret-canary", response.content.decode())
        self.assertContains(response, "invalid refusal evidence")

    def test_management_rest_drops_malformed_cached_snapshot_shapes(self):
        from ._outbox_case import mirror_update

        iface, row, generation = self.interface_execution()
        canary = "placeholder-forged-secret-canary"
        forged = copy.deepcopy(row.last_apply_error)
        forged["refusals"][0]["message"] = canary
        error = {"status": "apply_failed", "last_apply_error": forged}
        valid = {"name": iface.name}
        api = self.rest_client()
        detail = reverse("plugins-api:netbox_nso_plugin-api:nsodevicemanagement-detail", args=(self.management.pk,))
        listing = reverse("plugins-api:netbox_nso_plugin-api:nsodevicemanagement-list")
        for interfaces, expected in (
            ({"placeholder-0": {"attrs": {"description": error}}}, []),
            ([canary, valid], [valid]),
            ([{"name": "placeholder-0", "attrs": [error]}, valid], [valid]),
            ([{"name": "placeholder-0", "attrs": {"description": error, "enabled": canary}}, valid], [valid]),
        ):
            mirror_update(self.management, state_snapshot={"interfaces": interfaces})
            for url in (detail, listing):
                with self.subTest(interfaces=interfaces, url=url):
                    response = api.get(url)
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertNotIn(canary, response.content.decode())
                    data = response.json()
                    projected = data["results"][0] if url == listing else data
                    self.assertEqual(
                        projected["state_snapshot"], {"interfaces": expected, "source": "adapter-reported"}
                    )

    def test_interface_read_rejects_non_mapping_attrs(self):
        from netbox_nso_plugin import adapter_client

        original = self.adapter._handle

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/interfaces"):
                return make_response(200, [{"name": "placeholder-0", "attrs": ["description"]}])
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        with self.assertRaises(adapter_client.AdapterError) as raised:
            adapter_client.get_interfaces(1558)
        self.assertEqual(raised.exception.code, "invalid_response")

    def test_router_rest_projects_catalog_from_ledger(self):
        from ._outbox_case import mirror_update

        cause = self.refused_bgp(refused=0)
        router = self.owners[0]
        mirror_update(router, last_apply_error="placeholder-forged-secret-canary")
        api = self.rest_client()
        for name, args in (("nsobgprouterstate-detail", (router.pk,)), ("nsobgprouterstate-list", ())):
            response = api.get(reverse(f"plugins-api:netbox_nso_plugin-api:{name}", args=args))
            self.assertEqual(response.status_code, 200, response.content)
            self.assertNotIn("placeholder-forged-secret-canary", response.content.decode())
            self.assertContains(response, f"{cause['reason']}: {cause['message']}")

    def test_interface_object_views_qualify_restored_in_sync(self):
        iface, row, generation = self.interface_execution(unavailable=True, prior_status="in_sync")
        self.assertEqual(row.status, "in_sync")
        for url in (row.get_absolute_url(), iface.get_absolute_url()):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertContains(response, "previous in sync")
                self.assertContains(response, "Execution unavailable; showing previous state.")
                self.assertContains(response, f"placeholder prior nsointerfacestate {row.pk}")

    def test_route_policy_object_view_qualifies_restored_in_sync(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        with without_commit_drain(), transaction.atomic():
            self.management.manage_routing = True
            self.management.manage_route_policy = True
            self.management.save(update_fields=("manage_routing", "manage_route_policy"))
            reconcile_route_policy(
                self.device,
                {
                    "prefix_lists": [
                        {
                            "name": "placeholder-policy",
                            "entries": [{"seq": 5, "action": "permit", "prefix": "198.18.0.0/15"}],
                        }
                    ],
                    "community_lists": [],
                    "as_paths": [],
                    "route_maps": [],
                },
            )
            row = NSORoutePolicyState.objects.get(management=self.management)
        self.apply_owners([row], prior_status="in_sync")
        generation = self.evidence(dispositions={"route-policy.prefix-list": "unavailable"})
        self.settle(generation)
        row.refresh_from_db()
        self.assertEqual(row.status, "in_sync")
        response = self.client.get(row.assigned_object.get_absolute_url())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertContains(response, "previous in sync")
        self.assertContains(response, "Execution unavailable; showing previous state.")
        self.assertContains(response, f"placeholder prior nsoroutepolicystate {row.pk}")

    def test_matching_interface_value_does_not_hide_correlated_refusal(self):
        iface, row, generation = self.interface_execution(matching=True)
        payload = self.category("interface", format="json").json()
        cell = payload["rows"][0]["description"]
        self.assertEqual(cell["kind"], "apply_failed")
        self.assertTrue(cell["refusal_text"])
        self.assertContains(self.category("interfaces"), generation["refusals"][0]["reason"])

    def test_matching_value_applies_again_after_the_refusal_correlation_is_invalidated(self):
        from ._outbox_case import mirror_update

        iface, row, generation = self.interface_execution(matching=True)
        self.assertEqual(row.status, "apply_failed")
        stored = row.last_apply_error
        self.assertTrue(stored["refusals"])
        cause = generation["refusals"][0]
        # A store rekey invalidates the old epoch binding, so the stored refusal is history only.
        mirror_update(self.management, adapter_source_epoch=(self.management.adapter_source_epoch or 0) + 1)
        payload = self.category("interface", format="json").json()
        cell = payload["rows"][0]["description"]
        self.assertEqual((cell["kind"], cell["refusal_text"], cell["error"]), ("in_sync", "", None))
        self.assertFalse(cell["error_history"])
        self.assertEqual(payload["counts"]["pending"], 0)
        self.assertNotContains(self.category("interfaces", state="pending"), iface.name)
        api = self.rest_client()
        detail = api.get(reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-detail", args=(row.pk,)))
        self.assertEqual(detail.json()["state"], "in_sync")
        self.assertEqual(detail.json()["last_apply_error"]["refusals"], stored["refusals"])
        listing = api.get(
            reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-list"), {"state": "apply_failed"}
        )
        self.assertEqual(listing.json()["results"], [])
        badge = self.client.get(iface.get_absolute_url())
        self.assertContains(badge, ">in sync<")
        self.assertNotContains(badge, cause["message"])

    def test_restored_in_sync_owner_shows_its_prior_error_as_history(self):
        prior_error = "placeholder prior restored error"
        iface, row, generation = self.interface_execution(
            matching=True, prior_status="in_sync", prior_error=prior_error, disposition="not_applied"
        )
        self.assertEqual(row.status, "in_sync")
        cell = self.category("interface", format="json").json()["rows"][0]["description"]
        self.assertEqual((cell["kind"], cell["error"], cell["error_history"]), ("in_sync", prior_error, True))
        self.assertIsNone(cell["refusal"])
        page = self.client.get(iface.get_absolute_url()).content.decode()
        self.assertIn(f"Previous error: {prior_error}", page)
        self.assertNotIn(f'<small class="text-danger ms-1">{prior_error}', page)

    def test_in_sync_rows_render_no_refusal_or_error_text(self):
        from datetime import timedelta

        from ._outbox_case import content_update

        iface, row, generation = self.interface_execution(matching=True)
        cause = generation["refusals"][0]
        with without_commit_drain():
            content_update(row, status="accepted", accepted_at=self.stamp + timedelta(seconds=1))
        row.refresh_from_db()
        self.assertTrue(row.last_apply_error["refusals"])
        cell = self.category("interface", format="json").json()["rows"][0]["description"]
        self.assertEqual(cell["kind"], "in_sync")
        self.assertEqual((cell["refusal"], cell["refusal_text"], cell["error"]), (None, "", None))
        for response in (self.client.get(iface.get_absolute_url()), self.client.get(row.get_absolute_url())):
            with self.subTest(url=response.request["PATH_INFO"]):
                self.assertNotContains(response, cause["message"])
        self.assertNotContains(self.category("interfaces"), cause["message"])

    def test_interface_state_detail_shows_refusal_text_once(self):
        iface, row, generation = self.interface_execution()
        cause = generation["refusals"][0]
        page = self.client.get(row.get_absolute_url()).content.decode()
        self.assertEqual(page.count(cause["message"]), 1)

    def test_invalid_evidence_badge_does_not_repeat_its_evidence_text(self):
        from ._outbox_case import mirror_update

        iface, row, generation = self.interface_execution()
        mirror_update(row, last_apply_error={"refusals": [{"message": "placeholder-forged-secret-canary"}]})
        payload = self.category("interface", format="json").json()
        cell = payload["rows"][0]["description"]
        self.assertEqual(
            (cell["kind"], cell["label"], cell["refusal_text"]),
            ("apply_failed", "apply failed", "invalid refusal evidence"),
        )
        for url in (reverse("plugins:netbox_nso_plugin:nsointerfacestate_list"), row.get_absolute_url()):
            with self.subTest(url=url):
                page = self.client.get(url).content.decode()
                self.assertEqual(page.count("invalid refusal evidence"), 1)
                self.assertNotIn("placeholder-forged-secret-canary", page)

    def test_matching_interface_value_preserves_restored_ordinary_failure(self):
        iface, row, generation = self.interface_execution(
            matching=True, prior_status="apply_failed", disposition="not_applied"
        )
        prior_error = f"placeholder prior nsointerfacestate {row.pk}"
        self.assertEqual(row.status, "apply_failed")
        payload = self.category("interface", state="pending", format="json").json()
        self.assertEqual(payload["counts"]["pending"], 1)
        self.assertEqual(len(payload["rows"]), 1)
        cell = payload["rows"][0]["description"]
        self.assertEqual(cell["kind"], "apply_failed")
        self.assertIsNone(cell["refusal"])
        self.assertEqual(cell["error"], prior_error)
        response = self.category("interfaces", state="pending")
        self.assertContains(response, iface.name)
        self.assertContains(response, prior_error)
        change = self.preview().json()["changes"][0]
        self.assertEqual(change["error"], prior_error)
        self.assertContains(self.client.get(row.get_absolute_url()), prior_error)

    def test_correlated_refusal_remains_in_pending_filter_and_preview(self):
        iface, row, generation = self.interface_execution(matching=True)
        self.assertContains(self.category("interfaces", state="pending"), iface.name)
        payload = self.category("interface", state="pending", format="json").json()
        self.assertEqual(payload["counts"]["pending"], 1)
        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(
            self.preview().json()["changes"][0]["refusal_text"],
            f"{generation['refusals'][0]['reason']}: {generation['refusals'][0]['message']}",
        )

    def test_matching_interface_value_keeps_unavailable_advisory_visible(self):
        iface, row, generation = self.interface_execution(unavailable=True, matching=True)
        payload = self.category("interface", state="pending", format="json").json()
        self.assertEqual(payload["counts"]["pending"], 1)
        cell = payload["rows"][0]["description"]
        self.assertEqual(cell["status"], "accepted")
        self.assertTrue(cell["label"].startswith("previous "))
        self.assertIn("unavailable", cell["advisory"])
        self.assertIsNone(cell["refusal"])
        self.assertContains(self.category("interfaces", state="pending"), "unavailable")
        self.assertIn("unavailable", self.preview().json()["changes"][0]["advisory"])

    def test_structured_job_summary_ignores_free_form_error(self):
        self.prepare()
        generation = self.terminal()
        canary = "placeholder-forged-secret-canary"
        job = {
            "id": 501,
            "type": "apply",
            "status": "failed",
            "result": generation["carrier_job_result"],
            "row_outcomes": generation["row_outcomes"],
            "refusals": generation["refusals"],
            "error": {"message": canary, "detail": {"items": [{"type": "bgp", "error": canary}]}},
        }
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                return make_response(200, [job])
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        response = self.client.get(reverse("plugins:netbox_nso_plugin:device_nso_jobs", args=(self.device.pk,)))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(canary, response.content.decode())
        self.assertIn("commit not attempted", response.json()["jobs"][0]["refusal_summary"])

    def test_all_category_grids_project_safe_refusal(self):
        from netbox_routing.models import ISISInstance, OSPFInstance

        from netbox_nso_plugin import models
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        from ._settlement_case import _route

        with without_commit_drain(), transaction.atomic():
            flags = (
                "manage_routing",
                "manage_interfaces",
                "manage_bgp",
                "manage_isis",
                "manage_ospf",
                "manage_static",
                "manage_route_policy",
                "manage_redistribution",
            )
            for flag in flags:
                setattr(self.management, flag, True)
            self.management.save(update_fields=flags)
            scope = self.scope()
            router = self.router_owner()
            peer = self.peer(scope, "198.18.0.2")
            redistribution = self.redistribution(scope)
            bundle_iface = Interface.objects.create(device=self.device, name="Port-channel71", type="lag")
            bundle = acquire_overlay(
                models.NSOLACPBundleState,
                management=self.management,
                interface=bundle_iface,
                lag_id=71,
                status="accepted",
            )
            iface = Interface.objects.create(device=self.device, name="Ethernet71", type="1000base-t")
            bfd = acquire_overlay(
                models.NSOBFDInterfaceState,
                management=self.management,
                interface=iface,
                min_tx=300,
                min_rx=300,
                multiplier=3,
                status="accepted",
            )
            isis_native = ISISInstance.objects.create(
                device=self.device, process_tag="placeholder-process", net="49.0001.0000.0000.0001.00"
            )
            isis = acquire_overlay(
                models.NSOISISInstanceState,
                management=self.management,
                isis_instance=isis_native,
                process_tag=isis_native.process_tag,
                net=isis_native.net,
                status="accepted",
            )
            ospf_native = OSPFInstance.objects.create(
                device=self.device, name="placeholder-ospf", process_id="71", router_id="198.18.0.1"
            )
            ospf = acquire_overlay(
                models.NSOOSPFInstanceState,
                management=self.management,
                ospf_instance=ospf_native,
                process_id="71",
                router_id="198.18.0.1",
                status="accepted",
            )
            route = _route("198.18.71.0/24", "198.18.0.1", devices=[self.device])
            static = acquire_overlay(
                models.NSOStaticRouteState,
                management=self.management,
                static_route=route,
                nso_prefix=str(route.prefix),
                nso_next_hop=str(route.next_hop),
                status="accepted",
            )
            reconcile_route_policy(
                self.device,
                {
                    "prefix_lists": [
                        {
                            "name": "placeholder-policy",
                            "entries": [{"seq": 5, "action": "permit", "prefix": "198.18.0.0/15"}],
                        }
                    ],
                    "community_lists": [],
                    "as_paths": [],
                    "route_maps": [],
                },
            )
            policy = models.NSORoutePolicyState.objects.get(management=self.management)
            from ._ownership_case import save_overlay_fixture

            policy.status = "accepted"
            save_overlay_fixture(policy, update_fields=("status",))
        owners = [router, peer, redistribution, bundle, bfd, isis, ospf, static, policy]
        self.apply_owners(owners)
        subjects = {
            item["logical_ref"]["subject"]
            for snapshot in self.attempt.overlay_snapshots.all()
            for item in snapshot.contributors["rows"]
        }
        generation = self.evidence(dispositions=dict.fromkeys(subjects, "refused"))
        self.settle(generation)
        expected = {
            "bgp": ("peers", peer),
            "redistribution": (None, redistribution),
            "lacp": (None, bundle),
            "bfd": (None, bfd),
            "isis": ("instances", isis),
            "ospf": ("instances", ospf),
            "static": (None, static),
            "route_policy": (None, policy),
        }
        from netbox_nso_plugin.views import NSOCategoryView

        self.assertEqual(set(expected), set(NSOCategoryView._GRID_CATEGORIES))
        for category, (section, owner) in expected.items():
            with self.subTest(category=category):
                payload = self.category(category, format="json").json()
                rows = payload[section]["rows"] if section else payload["rows"]
                projected = next(row for row in rows if row["pk"] == owner.pk)
                self.assertEqual(projected["state"], "apply_failed")
                self.assertTrue(projected["refusal"]["valid"])
                self.assertTrue(projected["refusal_text"].startswith("inexpressible-on-ned: "))
                self.assertContains(self.category(category), "inexpressible-on-ned")
        counts = self.client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_category_counts", args=(self.device.pk,))
        ).json()["categories"]
        for category in expected:
            with self.subTest(counts=category):
                self.assertGreater(counts[category]["pending"], 0)

    def test_router_refusal_preserves_peer_history(self):
        cause = self.refused_bgp(refused=0)
        payload = self.category("bgp", format="json").json()
        router = payload["routers"]["rows"][0]
        self.assertEqual(router["state"], "apply_failed")
        self.assertEqual(router["refusal"]["causes"][0]["row"]["subject"], "bgp.router")
        self.assertEqual(router["refusal_text"], f"{cause['reason']}: {cause['message']}")
        for peer in payload["peers"]["rows"]:
            owner = next(row for row in self.owners[1:] if row.pk == peer["pk"])
            prior = self.prior[(owner._meta.label_lower, owner.pk)]
            self.assertEqual(peer["status"], prior[0])
            self.assertEqual(peer["error"], prior[2])
            self.assertIsNone(peer["refusal"])

    def test_new_acceptance_replaces_correlated_interface_refusal(self):
        from datetime import timedelta

        from ._outbox_case import content_update

        iface, row, generation = self.interface_execution(matching=True)
        with without_commit_drain():
            content_update(row, status="accepted", accepted_at=self.stamp + timedelta(seconds=1))
        payload = self.category("interface", format="json").json()
        self.assertEqual(payload["rows"][0]["description"]["kind"], "in_sync")
        self.assertEqual(payload["counts"]["pending"], 0)

    def restored_structured_error(self, disposition):
        from uuid import UUID

        from netbox_nso_plugin.models import NSOApplyAttempt
        from netbox_nso_plugin.refusal_registry import SECTION_COUNTER_KEYS

        iface, row, previous = self.interface_execution(matching=True)
        prior = copy.deepcopy(row.last_apply_error)

        def admission(selected):
            result = _promoted(selected)
            result["job_id"] = 502
            result["generations"] = [{**result["generations"][0], "generation_id": 82, "job_id": 502, "seq": 5}]
            return 202, result

        self.adapter.apply_response = admission
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.attempt = NSOApplyAttempt.objects.get(pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"]))
        generation = self.evidence(dispositions={"interface.description": disposition})
        generation["generation_id"] = 82
        generation["carrier_job_id"] = 502
        generation["seq"] = 5
        for outcome in generation["row_outcomes"]:
            outcome["generation_id"] = 82
            outcome["carrier"]["generation_id"] = 82
        for carrier in generation["carrier_job_result"]["settlement_owners"]:
            carrier["generation_id"] = 82
        for section in generation["sections"]:
            for counter in SECTION_COUNTER_KEYS[section]:
                generation["carrier_job_result"].setdefault(
                    counter, dict.fromkeys(("in_sync", "apply_failed", "not_applied", "unavailable"), 0)
                )
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        execution = settle_terminal_generation(
            self.management, generation, "placeholder-store", attempt=self.attempt, settle_seq=2
        )
        self.assertEqual(execution.consumption_state, "consumed")
        row.refresh_from_db()
        self.assertEqual(row.last_apply_error, prior)
        cell = self.category("interface", format="json").json()["rows"][0]["description"]
        self.assertIn(previous["refusals"][0]["reason"], cell["error"] or "")
        response = self.rest_client().get(
            reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-detail", args=(row.pk,))
        )
        self.assertEqual(response.status_code, 200, response.content)
        cause = prior["refusals"][0]
        self.assertEqual(
            response.json()["last_apply_error"],
            {"refusals": prior["refusals"], "message": f"{cause['reason']}: {cause['message']}"},
        )
        return cell

    def test_unavailable_execution_retains_prior_structured_error(self):
        cell = self.restored_structured_error("unavailable")
        self.assertIn("unavailable", cell["advisory"])
        self.assertIsNone(cell["refusal"])

    def test_not_applied_interface_preserves_prior_correlated_refusal(self):
        cell = self.restored_structured_error("not_applied")
        self.assertEqual(cell["kind"], "apply_failed")
        self.assertTrue(cell["refusal"]["valid"])

    def test_restored_text_error_is_projected_from_prior_execution(self):
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        from ._outbox_case import mirror_update

        self.prepare()
        generation = self.terminal()
        self.settle(generation)
        retry = self.terminal(refused=2)
        retry["carrier_job_id"] = 502
        settle_terminal_generation(self.management, retry, "placeholder-store", attempt=self.attempt, settle_seq=2)
        row = self.owners[1]
        row.refresh_from_db()
        mirror_update(row, last_apply_error="placeholder-forged-secret-canary")
        response = self.category("bgp", format="json")
        projected = next(item for item in response.json()["peers"]["rows"] if item["pk"] == row.pk)
        cause = generation["refusals"][0]
        self.assertEqual(projected["error"], f"{cause['reason']}: {cause['message']}")
        self.assertNotIn("placeholder-forged-secret-canary", response.content.decode())

    def test_project_rows_batches_the_previous_refusal_lookup(self):
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation
        from netbox_nso_plugin.refusal_display import project_rows, row_projection

        from ._outbox_case import mirror_update

        self.prepare()
        generation = self.terminal()
        self.settle(generation)
        retry = self.terminal(refused=2)
        retry["carrier_job_id"] = 502
        settle_terminal_generation(self.management, retry, "placeholder-store", attempt=self.attempt, settle_seq=2)
        row = self.owners[1]
        mirror_update(row, last_apply_error="placeholder ordinary failure")
        (row,) = project_rows([type(row).objects.get(pk=row.pk)])
        with self.assertNumQueries(0):
            projected = row_projection(row)
        cause = generation["refusals"][0]
        self.assertEqual(projected["error"], f"{cause['reason']}: {cause['message']}")

    def test_row_projection_finds_the_current_epoch_fence_beside_a_stale_one(self):
        from netbox_nso_plugin.models import NSOOverlaySettlementFence
        from netbox_nso_plugin.refusal_display import project_rows, row_projection

        _iface, row, _generation = self.interface_execution()
        current = NSOOverlaySettlementFence.objects.get(model_label=row._meta.label_lower, overlay_pk=row.pk)
        # The stale fence keeps the lower pk, so an unordered first() reaches it first.
        NSOOverlaySettlementFence.objects.filter(pk=current.pk).update(mapping_epoch="0" * 64)
        current.pk = None
        current.save()
        (batched,) = project_rows([NSOInterfaceState.objects.get(pk=row.pk)])
        expected = row_projection(batched)
        self.assertEqual(expected["execution_disposition"], "refused")
        self.assertEqual(row_projection(NSOInterfaceState.objects.get(pk=row.pk)), expected)

    def test_project_rows_shares_previous_refusal_executions(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.models import NSOApplyOverlaySnapshot
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation
        from netbox_nso_plugin.refusal_display import project_rows

        from ._outbox_case import mirror_update

        self.prepare()
        self.settle(self.terminal())
        retry = self.terminal(refused=2)
        retry["carrier_job_id"] = 502
        settle_terminal_generation(self.management, retry, "placeholder-store", attempt=self.attempt, settle_seq=2)
        refused, other = self.owners[1], self.owners[2]
        snapshot = NSOApplyOverlaySnapshot.objects.get(
            attempt=self.attempt, overlay_pk=refused.pk, settlement_disposition="refused"
        )
        executions = list(snapshot.executions.all())
        other.refresh_from_db()
        NSOApplyOverlaySnapshot.objects.filter(attempt_id=other.apply_attempt_id, overlay_pk=other.pk).delete()
        snapshot.pk = None
        snapshot.attempt_id = other.apply_attempt_id
        snapshot.overlay_pk = other.pk
        snapshot.model_label = other._meta.label_lower
        snapshot.save()
        snapshot.executions.set(executions)
        for row in (refused, other):
            mirror_update(row, last_apply_error="placeholder ordinary failure")
        rows = project_rows([type(row).objects.get(pk=row.pk) for row in (refused, other)])
        first, second = ({execution.pk: execution for execution in row._refusal_previous[1]} for row in rows)
        self.assertTrue(first)
        self.assertEqual(first.keys(), second.keys())
        for pk, execution in first.items():
            self.assertIs(second[pk], execution)
        for row in (refused, other):
            mirror_update(row, last_apply_error="")
        table = NSOApplyOverlaySnapshot._meta.db_table
        with CaptureQueriesContext(connection) as queries:
            project_rows([type(row).objects.get(pk=row.pk) for row in (refused, other)])
        refused_lookup = f'"{table}"."settlement_disposition" = \'refused\''
        self.assertFalse(any(refused_lookup in query["sql"] for query in queries.captured_queries))

    def test_execution_summaries_load_the_interface_with_its_row(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.refusal_display import device_execution_summaries

        # A refused row returns before the value comparison; a not-applied row reads its interface.
        self.interface_execution(disposition="not_applied")
        self.management.refresh_from_db()
        with CaptureQueriesContext(connection) as queries:
            device_execution_summaries(self.management)
        self.assertTrue(any("nsointerfacestate" in query["sql"] for query in queries.captured_queries))
        lazy = [
            query["sql"]
            for query in queries.captured_queries
            if 'FROM "dcim_interface"' in query["sql"] and "nsointerfacestate" not in query["sql"]
        ]
        self.assertEqual(lazy, [])

    def test_refusal_display_is_computed_once_per_row(self):
        _iface, row, generation = self.interface_execution()
        row = NSOInterfaceState.objects.get(pk=row.pk)
        first = row.refusal_display
        with self.assertNumQueries(0):
            self.assertIs(row.refusal_display, first)
        cause = generation["refusals"][0]
        self.assertEqual(first["refusal_text"], f"{cause['reason']}: {cause['message']}")

    def test_interface_rest_list_loads_current_evidence_once_per_page(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.lifecycle_fence import read_refresh
        from netbox_nso_plugin.models import NSOOverlaySettlementFence
        from netbox_nso_plugin.template_content import _upsert_interface_states

        with without_commit_drain():
            interfaces = [
                Interface.objects.create(device=self.device, name=f"Ethernet{index}", type="1000base-t")
                for index in range(1, 4)
            ]
        with read_refresh():
            _upsert_interface_states(
                self.device,
                [
                    {
                        "name": iface.name,
                        "attrs": {
                            "description": {"status": "in_sync", "nso_value": "placeholder", "last_apply_error": {}}
                        },
                    }
                    for iface in interfaces
                ],
            )
        api = self.rest_client()
        table = NSOOverlaySettlementFence._meta.db_table
        with CaptureQueriesContext(connection) as queries:
            response = api.get(reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-list"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["count"], 3)
        self.assertEqual(sum(f'"{table}"' in query["sql"] for query in queries.captured_queries), 1)

    def assert_state_lists_project_only_candidates(self, disposition, fenced_state):
        from unittest import mock

        from netbox_nso_plugin import refusal_display

        from ._outbox_case import mirror_update

        _iface, fenced, _generation = self.interface_execution(matching=True, disposition=disposition)
        # Only the current fence separates this owner from a settled row.
        mirror_update(fenced, status="in_sync", last_apply_error=None)
        desc = "placeholder settled"
        forged = {"refusals": [{"message": "placeholder-forged"}]}
        settled_specs = [
            ("description", "in_sync", desc, None, True),
            ("description", "imported", desc, {}, True),
            ("enabled", "in_sync", "True", None, True),
            ("enabled", "imported", "False", {}, False),
        ]
        candidate_specs = [
            ("description", "in_sync", f" {desc} ", None, True),
            ("enabled", "in_sync", "true", None, True),
            ("enabled", "imported", "", None, False),
            ("description", "imported", "placeholder observed", None, True),
            ("description", "accepted", "placeholder observed", None, True),
            ("description", "deploying", desc, None, True),
            ("description", "apply_failed", "placeholder observed", None, True),
            ("description", "in_sync", desc, forged, True),
            ("placeholder-attribute", "unknown", desc, None, True),
        ]
        created = []
        with without_commit_drain(), transaction.atomic():
            self.management.manage_enabled = True
            self.management.save(update_fields=("manage_enabled",))
            for index, (attribute, status, nso_value, error, enabled) in enumerate(settled_specs + candidate_specs):
                iface = Interface.objects.create(
                    device=self.device,
                    name=f"Ethernet{index + 2}",
                    type="1000base-t",
                    description=desc,
                    enabled=enabled,
                )
                created.append(
                    acquire_overlay(
                        NSOInterfaceState,
                        interface=iface,
                        attribute=attribute,
                        status=status,
                        nso_value=nso_value,
                        last_apply_error=error,
                    ).pk
                )
        settled = set(created[: len(settled_specs)])
        everything = NSOInterfaceState.objects.select_related("interface")
        expected = {
            row.pk: refusal_display.row_projection(row)["state"] for row in refusal_display.project_rows(everything)
        }
        self.assertEqual(expected[fenced.pk], fenced_state)
        self.assertEqual({expected[pk] for pk in settled}, {"in_sync"})
        states = {"unknown", "drift", "pending", "deploying", "in_sync", "apply_failed", fenced_state}
        self.assertEqual(set(expected.values()), states)

        projected = []
        real = refusal_display.project_rows

        def recording(rows):
            rows = list(rows)
            projected.extend(row.pk for row in rows)
            return real(rows)

        api = self.rest_client()
        listing = reverse("plugins:netbox_nso_plugin:nsointerfacestate_list")
        rest = reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-list")
        attention = states - {"in_sync"}
        with mock.patch.object(refusal_display, "project_rows", recording):
            for params in ({}, *({"state": state} for state in attention)):
                with self.subTest(surface="ui", params=params):
                    response = self.client.get(listing, {**params, "per_page": 100})
                    self.assertEqual(response.status_code, 200, response.content)
                    wanted = {params["state"]} if params else attention
                    shown = {row.pk for row in response.context["table"].data}
                    self.assertEqual(shown, {pk for pk, state in expected.items() if state in wanted})
            for state in attention:
                with self.subTest(surface="rest", state=state):
                    response = api.get(rest, {"state": state, "limit": 100})
                    self.assertEqual(response.status_code, 200, response.content)
                    shown = {row["id"] for row in response.json()["results"]}
                    self.assertEqual(shown, {pk for pk, value in expected.items() if value == state})
        self.assertFalse(settled & set(projected))
        for values in (["in_sync"], ["in_sync", "drift"]):
            with self.subTest(surface="rest", state=values):
                response = api.get(rest, {"state": values, "limit": 100})
                self.assertEqual(response.status_code, 200, response.content)
                shown = {row["id"] for row in response.json()["results"]}
                self.assertEqual(shown, {pk for pk, value in expected.items() if value in values})

    def test_refused_state_lists_project_only_candidates(self):
        self.assert_state_lists_project_only_candidates("refused", "apply_failed")

    def test_unavailable_state_lists_project_only_candidates(self):
        self.assert_state_lists_project_only_candidates("unavailable", "unavailable")

    def test_unavailable_category_keeps_pending_filter_and_prior_error(self):
        from uuid import UUID

        from netbox_nso_plugin.models import NSOApplyAttempt

        from ._outbox_case import own_vlan

        with without_commit_drain():
            self.management.manage_interfaces = True
            self.management.save(update_fields=("manage_interfaces",))
            row = own_vlan(self.management, 71, "placeholder-vlan")
        self.bookkeeping(row, status="in_sync")
        self.adapter.apply_response = lambda selected: (
            202,
            {**_promoted(selected), "generations": _promoted(selected)["generations"][:1]},
        )
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.attempt = NSOApplyAttempt.objects.get(pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"]))
        generation = self.evidence(dispositions={"vlan.vlan": "unavailable"})
        self.settle(generation)
        response = self.category("vlan", state="pending")
        self.assertContains(response, "Execution unavailable")
        self.assertContains(response, "previous in sync")
        self.assertContains(response, f"placeholder prior {row._meta.model_name} {row.pk}")

    def test_mixed_contributors_keep_device_summary_unavailable(self):
        from netbox_routing.models import ISISInstance, ISISLevel

        from netbox_nso_plugin.models import NSOISISInstanceState

        with without_commit_drain(), transaction.atomic():
            self.management.manage_routing = True
            self.management.manage_isis = True
            self.management.save(update_fields=("manage_routing", "manage_isis"))
            native = ISISInstance.objects.create(
                device=self.device, process_tag="placeholder-process", net="49.0001.0000.0000.0001.00"
            )
            ISISLevel.objects.create(instance=native, level=1, disabled=True)
            row = acquire_overlay(
                NSOISISInstanceState,
                management=self.management,
                isis_instance=native,
                process_tag=native.process_tag,
                net=native.net,
                status="accepted",
            )
        self.apply_owners([row])
        generation = self.evidence(
            dispositions={"isis.process-config": "in_sync", "isis.process-config.level": "not_applied"}
        )
        self.settle(generation)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, "Execution unavailable; showing previous state.")

    def test_document_summary_qualifies_unapplied_contributions(self):
        from netbox_nso_plugin.refusal_display import document_projection

        self.prepare()
        result = self.terminal(refused=None, disposition="not_applied")["carrier_job_result"]
        projection = document_projection(result)
        self.assertIn("unapplied", projection["summary"])
        self.assertIsNone(projection["refusal"])
        self.assertNotIn("nothing committed", projection["summary"])


class TestRefusalCauseFormatting(SimpleTestCase):
    def test_distinct_causes_are_separated_visibly(self):
        import json
        from pathlib import Path

        from netbox_nso_plugin import refusal_protocol
        from netbox_nso_plugin.refusal_display import format_causes

        base = json.loads((Path(__file__).parent / "fixtures/refusal_settlement.json").read_text(encoding="utf-8"))[
            "bgp"
        ]["refusal"]
        causes = []
        for message_id in ("write-site-conflict", "parent-absent"):
            entry = refusal_protocol.MESSAGES[message_id]
            causes.append({**base, "message_id": message_id, "reason": entry.reason, "message": entry.text})
        phrases = sorted(f"{cause['reason']}: {cause['message']}" for cause in causes)
        self.assertEqual(format_causes(causes), " | ".join(phrases))
