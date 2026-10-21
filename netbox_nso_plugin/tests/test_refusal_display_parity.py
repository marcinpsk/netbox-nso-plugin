# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Registry-wide agreement across authenticated REST, device views and Apply preview."""

import json
from collections import Counter
from html.parser import HTMLParser

from dcim.models import Interface
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.urls import reverse
from ipam.models import VLAN
from netbox_routing.models import PrefixList

from netbox_nso_plugin import models
from netbox_nso_plugin.delivery import delivery_keys
from netbox_nso_plugin.durable_capture import mapping_epoch
from netbox_nso_plugin.refusal_display import UNAVAILABLE
from netbox_nso_plugin.refusal_registry import OVERLAYS, SECTION_COUNTER_KEYS, document_section_for_stream
from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

from ._adapter_http import make_apply_preview, make_response
from ._bgp_settlement import BGPSettlementHelpers
from ._outbox_case import mirror_update, without_commit_drain
from ._ownership_case import acquire_overlay
from ._refusal_display import RefusalDisplayHelpers
from ._settlement_case import _route
from .test_durable_bgp_capture import BGPCaptureCase

CANARY = "placeholder-parity-secret-canary"
_CATEGORIES = {
    "nsointerfacestate": "interface",
    "nsointerfaceipstate": "interface",
    "nsosnmpcommunitystate": "snmp",
    "nsosnmpv3userstate": "snmp",
    "nsosnmphoststate": "snmp",
    "nsosnmpsysteminfostate": "snmp",
    "nsologginghoststate": "logging",
    "nsologginglevelstate": "logging",
    "nsosvistate": "svi",
    "nsosubinterfacestate": "subinterface",
    "nsointerfacemtustate": "interface",
    "nsovlanstate": "vlan",
    "nsobfdinterfacestate": "bfd",
    "nsostaticroutestate": "static",
    "nsol2sapstate": "l2_services",
    "nsoisisinstancestate": "isis",
    "nsoisisinterfacestate": "isis",
    "nsoisisflexalgostate": "isis",
    "nsobgprouterstate": "bgp",
    "nsobgppeerstate": "bgp",
    "nsoredistributionstate": "redistribution",
    "nsoospfinstancestate": "ospf",
    "nsoospfinterfacestate": "ospf",
    "nsoroutepolicystate": "route_policy",
    "nsolacpbundlestate": "lacp",
    "nsolacpmemberstate": "lacp",
    "nsoswitchportstate": "interface",
}


def owner_projections(value):
    if isinstance(value, dict):
        if "model" in value and "pk" in value:
            yield (value["model"], value["pk"]), value
        for child in value.values():
            yield from owner_projections(child)
    elif isinstance(value, list):
        for child in value:
            yield from owner_projections(child)


class OwnerHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.owners = {}
        self.script = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "nso-owner-state" in attrs.get("class", "").split():
            self.owners[(attrs["data-model"], int(attrs["data-pk"]))] = {
                "state": attrs["data-state"],
                "pending": attrs["data-pending"] == "true",
                "advisory": attrs["data-advisory"],
            }
        if tag == "script" and attrs.get("type") == "application/json":
            self.script = ""

    def handle_data(self, data):
        if self.script is not None:
            self.script += data

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.owners.update(owner_projections(json.loads(self.script)))
            self.script = None


class TestOwnerDisplayParity(RefusalDisplayHelpers, BGPSettlementHelpers, BGPCaptureCase):
    def owners(self):
        """Create one real owner of every registered model through the acquisition writer."""
        with without_commit_drain(), transaction.atomic():
            flags = tuple(field.name for field in self.management._meta.fields if field.name.startswith("manage_"))
            for flag in flags:
                setattr(self.management, flag, True)
            self.management.save(update_fields=flags)
            scope = self.scope()
            existing = [self.router_owner(), self.peer(scope, "198.18.0.2"), self.redistribution(scope)]
            by_model = {row._meta.model_name: row for row in existing}
            lag = Interface.objects.create(device=self.device, name="Port-channel71", type="lag")
            parent = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")
            vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=71, name="placeholder-vlan")
            route = _route("198.18.71.0/24", "198.18.0.1", devices=[self.device])
            policy = PrefixList.objects.create(name="placeholder-policy")
            values = {
                "nsointerfacestate": {"attribute": "description", "nso_value": "placeholder desired"},
                "nsointerfaceipstate": {"address": "198.18.0.1/24"},
                "nsointerfacemtustate": {"l2_mtu": 9000},
                "nsoswitchportstate": {"mode": "access", "untagged_vlan": vlan},
                "nsosnmpcommunitystate": {"community_hash": "a" * 64},
                "nsosnmpv3userstate": {"username": "placeholder-user"},
                "nsosnmphoststate": {"address": "198.18.0.3"},
                "nsologginghoststate": {"address": "198.18.0.4"},
                "nsovlanstate": {"vlan": vlan},
                "nsostaticroutestate": {"static_route": route},
                "nsol2sapstate": {"service_name": "placeholder-service", "sap_id": "placeholder-sap"},
                "nsoisisinstancestate": {"process_tag": "placeholder-process"},
                "nsoisisinterfacestate": {"process_tag": "placeholder-process", "af": "ipv4"},
                "nsoisisflexalgostate": {"process_tag": "placeholder-process", "algo_id": 130},
                "nsoospfinstancestate": {"process_id": "71"},
                "nsoospfinterfacestate": {"process_id": "71"},
                "nsoroutepolicystate": {
                    "family": "prefix_list",
                    "object_name": policy.name,
                    "content_type": ContentType.objects.get_for_model(policy),
                    "object_id": policy.pk,
                },
                "nsolacpbundlestate": {"interface": lag, "lag_id": 71},
                "nsosubinterfacestate": {"parent_interface": parent},
            }
            for spec in OVERLAYS.values():
                model = spec.model
                name = model._meta.model_name
                self.assertIn(name, _CATEGORIES, "Add display parity coverage for the new owner.")
                if name in by_model:
                    continue
                data = {"status": "imported", **values.get(name, {})}
                fields = {field.name for field in model._meta.fields}
                if "management" in fields:
                    data["management"] = self.management
                if "interface" in fields and "interface" not in data:
                    data["interface"] = Interface.objects.create(
                        device=self.device,
                        name=f"Ethernet{len(by_model) + 1}",
                        type="1000base-t",
                        description="placeholder desired",
                        parent=parent if name == "nsosubinterfacestate" else None,
                        mtu=9000 if name == "nsointerfacemtustate" else None,
                        mode="access" if name == "nsoswitchportstate" else "",
                        untagged_vlan=vlan if name == "nsoswitchportstate" else None,
                        lag=lag if name == "nsolacpmemberstate" else None,
                    )
                by_model[name] = acquire_overlay(model, **data)
        return [by_model[spec.model._meta.model_name] for spec in OVERLAYS.values()]

    def scenario(self, owners, scenario):
        """Persist exact current execution links and catalog evidence for each owner."""
        if scenario == "ordinary_matching_no_fence":
            for row in owners:
                mirror_update(row, status="accepted")
                row.refresh_from_db()
            for row in owners:
                error = {"code": "placeholder_failure", "detail": "placeholder detail"}
                mirror_update(
                    row,
                    status="apply_failed",
                    apply_attempt_id=None,
                    last_apply_error=error
                    if OVERLAYS[row._meta.label_lower].error_type == "JSONField"
                    else "placeholder previous failure",
                )
                row.refresh_from_db()
                self.assertEqual(row.status, "apply_failed")
                if isinstance(row, models.NSOInterfaceState):
                    self.assertEqual(row.nso_value, row.interface.description)
            self.assertFalse(self.management.settlement_fences.filter(current_attempt__isnull=False).exists())
            return
        self.attempt = models.NSOApplyAttempt.objects.create(management=self.management)
        unavailable = scenario.startswith("unavailable")
        disposition = "unavailable" if unavailable else "refused"
        status = "in_sync" if scenario in {"unavailable_in_sync", "invalid_evidence"} else "apply_failed"
        epoch = mapping_epoch(self.management)
        for row in owners:
            mirror_update(row, status="accepted")
            row.refresh_from_db()
        for row in owners:
            spec = OVERLAYS[row._meta.label_lower]
            subject = spec.subjects[0]
            family = subject.split(".")[0]
            ref = {"family": family, "subject": subject, "key": "a" * 64}
            stream = delivery_keys()[spec.delivery_keys[0]].section
            mirror_update(row, status=status, apply_attempt_id=self.attempt.pk)
            row.refresh_from_db()
            models.NSOApplyOverlaySnapshot.objects.create(
                attempt=self.attempt,
                model_label=spec.model_label,
                overlay_pk=row.pk,
                mapping_epoch=epoch,
                prior_status=status,
                expected_status="deploying",
                accepted_at=row.accepted_at,
                content_digest="d" * 64,
                expected_owner_revision=0,
                settlement_disposition=disposition,
                contributors={
                    "rows": [
                        {
                            "stream": stream,
                            "logical_ref": ref,
                            "carrier_ref": ref,
                            "source_selector": {"source_push_seq": 1},
                        }
                    ]
                },
            )
        subjects = [spec.subjects[0] for spec in OVERLAYS.values()]
        generation = self.evidence(dispositions=dict.fromkeys(subjects, disposition))
        generation["sections"] = sorted({document_section_for_stream(stream) for stream in generation["sections"]})
        for section in generation["sections"]:
            for counter in SECTION_COUNTER_KEYS[section]:
                generation["carrier_job_result"].setdefault(
                    counter, dict.fromkeys(("in_sync", "apply_failed", "not_applied", "unavailable"), 0)
                )
        if scenario == "invalid_evidence":
            generation["row_outcomes"][0]["refusals"][0]["message"] = CANARY
        execution = models.NSOApplyExecution.objects.create(
            attempt=self.attempt,
            management=self.management,
            mapping_epoch=epoch,
            adapter_device_id=1558,
            store_incarnation="placeholder-store",
            generation_id=81,
            generation_seq=4,
            carrier_job_id=501,
            terminal_projection=generation,
            terminal_at=self.stamp,
            consumed_at=self.stamp,
            consumption_state="consumed",
        )
        for row in owners:
            snapshot = self.attempt.overlay_snapshots.get(model_label=row._meta.label_lower, overlay_pk=row.pk)
            snapshot.executions.add(execution)
            models.NSOOverlaySettlementFence.objects.update_or_create(
                management=self.management,
                mapping_epoch=epoch,
                model_label=row._meta.label_lower,
                overlay_pk=row.pk,
                defaults={
                    "accepted_at": row.accepted_at,
                    "owner_revision": 0,
                    "current_attempt": self.attempt,
                    "last_execution": execution,
                },
            )
            if scenario == "ordinary_json":
                error = {"code": "placeholder_failure", "detail": "placeholder detail"}
                snapshot.settlement_disposition = "not_applied"
                snapshot.save(update_fields=("settlement_disposition",))
            elif scenario == "invalid_evidence":
                error = {"refusals": [{"message": CANARY}]}
            elif scenario == "secret_canary":
                error = {"message": CANARY}
            elif scenario == "unavailable_in_sync":
                error = {}
            else:
                error = {"message": "placeholder previous failure"}
            mirror_update(
                row,
                last_apply_error=error
                if OVERLAYS[row._meta.label_lower].error_type == "JSONField"
                else error.get("message", str(error))
                if error
                else "",
            )

    def assert_management_summary(self, expected_state, advisory):
        """Project owner counts even when live or cached adapter counts claim success."""
        compliance = {"managed_interfaces": 1, "by_status": {"in_sync": 1}}
        mirror_update(self.management, state_snapshot={"interfaces": [], "compliance": compliance})
        api = self.rest_client()
        for action in ("detail", "list"):
            response = api.get(
                reverse(
                    f"plugins-api:netbox_nso_plugin-api:nsodevicemanagement-{action}",
                    args=(self.management.pk,) if action == "detail" else (),
                )
            )
            self.assertEqual(response.status_code, 200, response.content)
            data = response.json()
            if action == "list":
                data = next(row for row in data["results"] if row["id"] == self.management.pk)
            self.assertEqual(data["state_snapshot"]["source"], "adapter-reported")
            self.assertEqual(data["state_snapshot"]["compliance"], compliance)
        original = self.adapter._handle
        for cached in (False, True):

            def handle(method, url, **kwargs):
                if method == "GET" and url.endswith(("/interfaces-doc", "/state")):
                    if cached:
                        return make_response(503, {"error": {"code": "nso_unreachable", "message": "down"}})
                    return make_response(200, compliance if url.endswith("/state") else {"interfaces": []})
                return original(method, url, **kwargs)

            self.adapter._handle = handle
            try:
                with self.subTest(surface="managed-device-detail", cached=cached):
                    response = self.client.get(self.management.get_absolute_url())
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertNotIn(CANARY, response.content.decode())
                    self.assertEqual(bool(response.context["adapter_error"]), cached)
                    self.assertEqual(response.context["compliance"]["by_status"], {expected_state: 1})
                    self.assertNotContains(response, "<th>In Sync</th>", html=True)
                    if advisory:
                        self.assertContains(response, advisory)
            finally:
                self.adapter._handle = original

    def assert_legacy_interface_parity(self, expected_state, advisory):
        for state_filter in ("all", "pending", "in_sync"):
            response = self.category("interfaces", state=state_filter)
            rows = response.context["rows"]
            self.assertEqual(response.context["counts"]["pending"], 1)
            self.assertEqual(len(rows), 0 if state_filter == "in_sync" else 1)
            for row in rows:
                self.assertEqual(row["kind"], expected_state)
                self.assertEqual(row["advisory"], advisory)
                self.assertContains(response, row["label"])
                if advisory:
                    self.assertContains(response, advisory)

    def assert_category_parity(self, owners, expected_state, advisory, scenario):
        from netbox_nso_plugin.views import NSOCategoryView

        expected_counts = Counter(_CATEGORIES[row._meta.model_name] for row in owners)
        extra_categories = {
            "interface_ips": "nsointerfaceipstate",
            "interface_mtu": "nsointerfacemtustate",
            "switchport": "nsoswitchportstate",
        }
        for category in [*expected_counts, *extra_categories]:
            with self.subTest(category=category, surface="category", scenario=scenario):
                response = self.category(category)
                self.assertNotIn(CANARY, response.content.decode())
                parsed = OwnerHTML()
                parsed.feed(response.content.decode())
                for row in owners:
                    if _CATEGORIES[row._meta.model_name] == category or row._meta.model_name == extra_categories.get(
                        category
                    ):
                        key = (row._meta.model_name, row.pk)
                        self.assertIn(key, parsed.owners)
                        projected = parsed.owners[key]
                        self.assertEqual(projected["state"], expected_state)
                        self.assertTrue(projected["pending"])
                        self.assertEqual(projected["advisory"], advisory)
                if category == "interface" or category in NSOCategoryView._GRID_CATEGORIES:
                    response = self.category(category, format="json")
                    projected_rows = dict(owner_projections(response.json()))
                    for key, projected in parsed.owners.items():
                        self.assertEqual(projected_rows[key]["state"], projected["state"])
                        self.assertEqual(projected_rows[key]["pending"], projected["pending"])
                        self.assertEqual(projected_rows[key]["advisory"], projected["advisory"])

    def assert_parity(self, scenario):
        owners = self.owners()
        with without_commit_drain(), transaction.atomic():
            self.scenario(owners, scenario)
        for row in owners:
            self.assertNotIn(f"[{row.status}]", str(row))
        expected_state = "unavailable" if scenario.startswith("unavailable") else "apply_failed"
        advisory = UNAVAILABLE if expected_state == "unavailable" else ""
        self.assert_management_summary(expected_state, advisory)
        api = self.rest_client()
        interface_owner = next(row for row in owners if isinstance(row, models.NSOInterfaceState))
        self.assert_legacy_interface_parity(expected_state, advisory)
        for route, args, params in (
            ("plugins:netbox_nso_plugin:nsointerfacestate_list", (), {}),
            ("plugins:netbox_nso_plugin:nsointerfacestate_list", (), {"state": expected_state}),
            ("plugins:netbox_nso_plugin:nsointerfacestate", (interface_owner.pk,), {}),
            ("dcim:interface", (interface_owner.interface_id,), {}),
        ):
            with self.subTest(surface=route, params=params, scenario=scenario):
                response = self.client.get(reverse(route, args=args), params)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertNotIn(CANARY, response.content.decode())
                self.assertEqual(response.content.decode().count(f"[{interface_owner.status}]"), 0)
                parsed = OwnerHTML()
                parsed.feed(response.content.decode())
                projected = parsed.owners[(interface_owner._meta.model_name, interface_owner.pk)]
                self.assertEqual(projected["state"], expected_state)
                self.assertTrue(projected["pending"])
                self.assertEqual(projected["advisory"], advisory)
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsointerfacestate_bulk_delete"), {"pk": [interface_owner.pk]}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn(CANARY, response.content.decode())
        self.assertTrue(models.NSOInterfaceState.objects.filter(pk=interface_owner.pk).exists())
        parsed = OwnerHTML()
        parsed.feed(response.content.decode())
        projected = parsed.owners[(interface_owner._meta.model_name, interface_owner.pk)]
        self.assertEqual(projected["state"], expected_state)
        self.assertTrue(projected["pending"])
        self.assertEqual(projected["advisory"], advisory)
        policy_owner = next(row for row in owners if isinstance(row, models.NSORoutePolicyState))
        for url in (
            policy_owner.assigned_object.get_absolute_url(),
            reverse("plugins:netbox_nso_plugin:routing_route_policy_versions", args=(policy_owner.pk,)),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertNotIn(CANARY, response.content.decode())
            parsed = OwnerHTML()
            parsed.feed(response.content.decode())
            projected = parsed.owners[(policy_owner._meta.model_name, policy_owner.pk)]
            self.assertEqual(projected["state"], expected_state)
            self.assertTrue(projected["pending"])
            self.assertEqual(projected["advisory"], advisory)
        response = self.client.get(
            reverse("plugins:netbox_nso_plugin:routing_classify_bulk_route_policy", args=(self.device.pk,))
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn(policy_owner.pk, [row.pk for row in response.context["rows"]])
        counts_response = self.client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_category_counts", args=(self.device.pk,))
        )
        tab = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        preview_response = self.preview(make_apply_preview(device_id=1558, diffs={}))
        expected_counts = Counter(_CATEGORIES[row._meta.model_name] for row in owners)
        for response in (counts_response, tab, preview_response):
            self.assertEqual(response.status_code, 200, response.content)
            self.assertNotIn(CANARY, response.content.decode())
        self.assertEqual(UNAVAILABLE in tab.content.decode(), bool(advisory))
        with self.subTest(surface="preview", scenario=scenario):
            preview = preview_response.json()
            self.assertFalse(preview["nothing_pending"])
            self.assertEqual(preview["total"], len(owners))
            self.assertEqual(
                {(change["model"], change["pk"]) for change in preview["changes"] + preview["routing_changes"]},
                {(row._meta.model_name, row.pk) for row in owners},
            )
            for change in preview["changes"] + preview["routing_changes"]:
                self.assertEqual(change.get("state"), expected_state)
                self.assertEqual(change.get("advisory"), advisory)
        for category, count in expected_counts.items():
            with self.subTest(category=category, surface="counts", scenario=scenario):
                self.assertEqual(counts_response.json()["categories"][category]["pending"], count)
                summary = next(cat for cat in tab.context["nso_categories"] if cat["key"] == category)
                self.assertEqual(summary["counts"]["pending"], count)
        self.assert_category_parity(owners, expected_state, advisory, scenario)
        from netbox_nso_plugin.api.urls import router

        for row in owners:
            endpoints = [("plugins-api:netbox_nso_plugin-api:owner-state-detail", (row._meta.model_name, row.pk))]
            endpoints.extend(
                (f"plugins-api:netbox_nso_plugin-api:{basename}-{action}", (row.pk,) if action == "detail" else ())
                for _prefix, view, basename in router.registry
                if view.queryset.model is type(row)
                for action in ("detail", "list")
            )
            for name, args in endpoints:
                with self.subTest(owner=row._meta.model_name, surface=name, scenario=scenario):
                    response = api.get(reverse(name, args=args))
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertNotIn(CANARY, response.content.decode())
                    data = response.json()
                    if "results" in data:
                        data = next(result for result in data["results"] if result["id"] == row.pk)
                    if "display" in data:
                        self.assertNotIn(f"[{row.status}]", data["display"])
                    self.assertEqual(data.get("state"), expected_state)
                    self.assertTrue(data.get("pending"))
                    self.assertEqual(data.get("advisory"), advisory)
                    self.assertEqual(
                        data.get("execution_disposition"),
                        None
                        if scenario == "ordinary_matching_no_fence"
                        else "not_applied"
                        if scenario == "ordinary_json"
                        else "unavailable"
                        if scenario.startswith("unavailable")
                        else "refused",
                    )
                    if row is interface_owner and name.endswith("-list"):
                        filtered = api.get(reverse(name), {"state": expected_state})
                        self.assertEqual(filtered.status_code, 200, filtered.content)
                        self.assertIn(row.pk, [item["id"] for item in filtered.json()["results"]])
                        excluded = api.get(reverse(name), {"state": "in_sync"})
                        self.assertEqual(excluded.status_code, 200, excluded.content)
                        self.assertNotIn(row.pk, [item["id"] for item in excluded.json()["results"]])

    def test_catalog_refusal(self):
        self.assert_parity("catalog_refusal")

    def test_peer_template_mirror_uses_common_projection(self):
        from netbox_nso_plugin.api.serializers import NSOBGPPeerTemplateStateSerializer

        with without_commit_drain(), transaction.atomic():
            self.management.manage_routing = True
            self.management.save(update_fields=("manage_routing",))
            row = acquire_overlay(
                models.NSOBGPPeerTemplateState,
                management=self.management,
                template_name="placeholder-template",
                status="changed",
            )
        for state, expected in (("changed", "drift"), ("imported", "in_sync")):
            mirror_update(row, status=state)
            row.refresh_from_db()
            response = self.category("bgp")
            parsed = OwnerHTML()
            parsed.feed(response.content.decode())
            projected = parsed.owners[(row._meta.model_name, row.pk)]
            self.assertEqual(projected["state"], expected)
            self.assertFalse(projected["pending"])
            serialized = NSOBGPPeerTemplateStateSerializer(row).data
            self.assertEqual(serialized["state"], expected)
            self.assertFalse(serialized["pending"])
            response = self.client.get(
                reverse("plugins:netbox_nso_plugin:device_nso_category_counts", args=(self.device.pk,))
            )
            self.assertEqual(response.status_code, 200, response.content)
            counts = response.json()["categories"]["bgp"]
            self.assertEqual(counts["total"], 1)
            self.assertEqual(counts["pending"], 0)
            self.assertEqual(counts["drift"], int(state == "changed"))

    def test_job_reads_keep_unavailable_execution_advisory(self):
        owners = self.owners()
        with without_commit_drain(), transaction.atomic():
            self.scenario(owners, "unavailable_in_sync")
        generation = self.attempt.executions.get().terminal_projection
        job = {
            "id": 501,
            "type": "apply",
            "status": "succeeded",
            "result": generation["carrier_job_result"],
            "row_outcomes": generation["row_outcomes"],
            "refusals": generation["refusals"],
            "error": {"message": CANARY},
        }
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs/501"):
                return make_response(200, job)
            if method == "GET" and url.endswith("/jobs"):
                return make_response(200, [job])
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        for route, args in (("nsojob_status", (501,)), ("device_nso_jobs", (self.device.pk,))):
            response = self.client.get(reverse(f"plugins:netbox_nso_plugin:{route}", args=args))
            self.assertEqual(response.status_code, 200, response.content)
            self.assertNotIn(CANARY, response.content.decode())
            data = response.json()
            projected = data if route == "nsojob_status" else data["last"]
            self.assertEqual(projected["refusal_summary"], UNAVAILABLE)
            self.assertIsNone(projected["error"])

    def test_patch_cannot_replace_settlement_evidence(self):
        owners = self.owners()
        self.scenario(owners, "catalog_refusal")
        row = next(row for row in owners if isinstance(row, models.NSOInterfaceState))
        row.refresh_from_db()
        before = {field.attname: getattr(row, field.attname) for field in row._meta.concrete_fields}
        cause = self.attempt.executions.get().terminal_projection["row_outcomes"][0]["refusals"][0]
        response = self.rest_client().patch(
            reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-detail", args=(row.pk,)),
            {"last_apply_error": {"refusals": [{**cause, "message": CANARY}]}},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertNotIn(CANARY, response.content.decode())
        row.refresh_from_db()
        self.assertEqual({field: getattr(row, field) for field in before}, before)

    def test_every_overlay_serializer_protects_settlement_fields(self):
        from netbox_nso_plugin.api import serializers

        protected = {
            "status",
            "accepted_at",
            "last_sync_at",
            "last_apply_at",
            "last_apply_error",
            "apply_attempt_id",
            "generation_started_at",
            "last_result_advisory",
            "device_present",
            "content_hash",
            "device_base_hash",
            "captured",
            "is_materialized",
            "intent_generation",
            "expected_generation",
            "expected_fingerprint",
            "last_acked_triple",
            "observed_members",
        }
        for model in [spec.model for spec in OVERLAYS.values()] + [models.NSOBGPPeerTemplateState]:
            serializer = getattr(serializers, model.__name__ + "Serializer")()
            for name in serializer.fields.keys() & protected:
                with self.subTest(model=model._meta.label_lower, field=name):
                    self.assertTrue(serializer.fields[name].read_only)

    def test_unavailable_in_sync(self):
        self.assert_parity("unavailable_in_sync")

    def test_unavailable_failure(self):
        self.assert_parity("unavailable_failure")

    def test_invalid_evidence(self):
        self.assert_parity("invalid_evidence")

    def test_ordinary_json(self):
        self.assert_parity("ordinary_json")

    def test_ordinary_matching_no_fence(self):
        self.assert_parity("ordinary_matching_no_fence")

    def test_secret_canary(self):
        self.assert_parity("secret_canary")
