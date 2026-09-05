# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for route_policy_reconciler.reconcile_route_policy.

Exercises the reconcile against the REAL netbox_routing models (PrefixList,
CommunityList, ASPath, RouteMap) so a model rename can't silently disable the
feature via the broad ImportError guard (which is exactly how the
ASPathAccessList->ASPath rename slipped through unnoticed).
"""

from __future__ import annotations

import json
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase
from django.urls import reverse


class TestReconcileRoutePolicy(TestCase):
    def test_object_classification_lookups_are_batched(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from django.utils import timezone
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyObjectClass
        from netbox_nso_plugin.route_policy_reconciler import _RoutePolicyGraphPlanner

        self._make_mgmt(self.device)
        payload = {"prefix_lists": [{"name": f"CLASS-BATCH-{index}", "entries": []} for index in range(4)]}
        PrefixList.objects.bulk_create(PrefixList(name=row["name"]) for row in payload["prefix_lists"])

        with CaptureQueriesContext(connection) as captured:
            _RoutePolicyGraphPlanner(self.device, payload, timezone.now()).build()

        class_table = NSORoutePolicyObjectClass._meta.db_table
        class_queries = [query for query in captured if class_table in query["sql"]]
        self.assertEqual(len(class_queries), 1)

    def test_prefix_unit_owner_lookup_is_batched(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from django.utils import timezone

        from netbox_nso_plugin.route_policy_reconciler import _RoutePolicyGraphPlanner

        self._make_mgmt(self.device)
        payload = {
            "prefix_lists": [
                {"name": f"BATCH-{index}", "entries": [{"prefix": f"198.18.{index}.0/24"}]} for index in range(3)
            ]
        }
        planner = _RoutePolicyGraphPlanner(self.device, payload, timezone.now())

        with CaptureQueriesContext(connection) as captured:
            planner._seed_prefix_units()

        self.assertEqual(len(captured), 1)

    def test_direct_reconcile_builds_the_route_policy_graph_once(self):
        from unittest.mock import patch

        from netbox_nso_plugin import route_policy_reconciler

        self._make_mgmt(self.device)
        payload = self._pl_payload("ONE-PLAN", "198.18.4.0/24")

        with patch.object(
            route_policy_reconciler,
            "_route_policy_reconcile_operations",
            wraps=route_policy_reconciler._route_policy_reconcile_operations,
        ) as build_operations:
            route_policy_reconciler.reconcile_route_policy(self.device, payload)

        self.assertEqual(build_operations.call_count, 1)

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RpMfg", slug="rpmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RpDev", slug="rpdev")
        role = DeviceRole.objects.create(name="RpRole", slug="rprole")
        site = Site.objects.create(name="RpSite", slug="rpsite")
        cls.device = Device.objects.create(name="rp-router", device_type=dt, role=role, site=site)
        cls.device2 = Device.objects.create(name="rp-router-2", device_type=dt, role=role, site=site)

    def _make_mgmt(self, device):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="rp-inst", defaults={"adapter_instance_id": "rp-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=device,
            defaults={"nso_instance": inst, "nso_device_name": f"rp-{device.pk}", "adapter_device_id": device.pk},
        )[0]

    def _payload(self):
        return {
            "prefix_lists": [{"name": "PL-CUSTOMER", "entries": [{"seq": 5, "action": "permit"}]}],
            "community_lists": [{"name": "CL-LOCAL", "entries": [{"community": "65000:1"}]}],
            "as_paths": [{"name": "AP-PRIVATE", "entries": [{"regex": "^65000_"}]}],
            "route_maps": [{"name": "RM-IMPORT", "entries": [{"seq": 10, "action": "permit"}]}],
        }

    def test_no_mgmt_returns_empty(self):
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self.assertEqual(reconcile_route_policy(self.device, self._payload()), [])

    def test_reconcile_plan_marks_empty_shell_fills_as_content_changes(self):
        from netbox_routing.models import ASPath, CommunityList, PrefixList, RouteMap

        from netbox_nso_plugin.route_policy_reconciler import route_policy_reconcile_plan

        self._make_mgmt(self.device)
        cases = (
            ("prefix_lists", PrefixList.objects.create(name="PL-EMPTY"), {"name": "PL-EMPTY", "family": 4}),
            ("community_lists", CommunityList.objects.create(name="CL-EMPTY"), {"name": "CL-EMPTY"}),
            ("as_paths", ASPath.objects.create(name="AP-EMPTY"), {"name": "AP-EMPTY"}),
            ("route_maps", RouteMap.objects.create(name="RM-EMPTY"), {"name": "RM-EMPTY"}),
        )

        for payload_key, _empty_shell, captured in cases:
            with self.subTest(payload_key=payload_key):
                payload = {
                    "prefix_lists": [],
                    "community_lists": [],
                    "as_paths": [],
                    "route_maps": [],
                    payload_key: [{**captured, "entries": []}],
                }
                self.assertTrue(route_policy_reconcile_plan(self.device, payload).changes_content)

    def test_case_insensitive_name_adopts_existing_object(self):
        """A device object whose name differs only in CASE from an existing netbox_routing
        object must ADOPT it, not crash on the Lower(name) unique constraint.

        Regression: get_or_create(name='ACCEPT-ALL') with an existing 'accept-all' did
        get-miss → create → IntegrityError, which aborted the whole route-policy reconcile
        and left EVERY row marked 'error' (self-perpetuating once the status machine also
        couldn't move error→conflict).
        """
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        existing = RouteMap.objects.create(name="accept-all")  # e.g. imported from another device
        self._make_mgmt(self.device)

        payload = {
            "prefix_lists": [],
            "community_lists": [],
            "as_paths": [],
            "route_maps": [{"name": "ACCEPT-ALL", "entries": [{"seq": 10, "action": "permit"}]}],
        }
        reconcile_route_policy(self.device, payload)  # must not raise

        # No duplicate object created; the device's row adopts the existing (other-case) one.
        self.assertEqual(RouteMap.objects.filter(name__iexact="accept-all").count(), 1)
        st = NSORoutePolicyState.objects.get(
            management__device=self.device, family="route_map", object_name="ACCEPT-ALL"
        )
        self.assertNotEqual(st.status, "error")
        self.assertEqual(st.object_id, existing.pk)

    def test_deploying_row_waits_for_correlated_apply_evidence(self):
        """An ordinary device read cannot identify the Apply attempt that it reflects."""
        from uuid import uuid4

        self._make_mgmt(self.device)
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        payload = {"community_lists": [{"name": "CL-EMPTY", "entries": []}]}
        reconcile_route_policy(self.device, payload)
        st = NSORoutePolicyState.objects.get(
            management__device=self.device, family="community_list", object_name="CL-EMPTY"
        )
        attempt_id = uuid4()
        st.status = "deploying"
        st.apply_attempt_id = attempt_id
        st.save(update_fields=["status", "apply_attempt_id"])

        reconcile_route_policy(self.device, payload)
        st.refresh_from_db()
        self.assertEqual(st.status, "deploying")
        self.assertEqual(st.apply_attempt_id, attempt_id)

    def test_local_deploying_row_waits_for_correlated_apply_evidence(self):
        """A LOCAL row is not rendered, so a device read cannot settle its Apply attempt."""
        from uuid import uuid4

        from netbox_nso_plugin.models import NSORoutePolicyObjectClass, NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        NSORoutePolicyObjectClass.objects.create(
            family="community_list",
            object_name="CL-LOCAL",
            mode="local",
        )
        payload = {"community_lists": [{"name": "CL-LOCAL", "entries": []}]}
        reconcile_route_policy(self.device, payload)
        state = NSORoutePolicyState.objects.get(
            management__device=self.device,
            family="community_list",
            object_name="CL-LOCAL",
        )
        attempt_id = uuid4()
        state.status = "deploying"
        state.apply_attempt_id = attempt_id
        state.save(update_fields=["status", "apply_attempt_id"])

        reconcile_route_policy(self.device, payload)

        state.refresh_from_db()
        self.assertEqual(state.status, "deploying")
        self.assertEqual(state.apply_attempt_id, attempt_id)

    def test_apply_promotes_only_rows_in_the_rendered_route_policy_snapshot(self):
        """Apply must not bind an omitted LOCAL row to the MASTER carrier evidence."""
        from types import SimpleNamespace
        from uuid import uuid4

        from netbox_nso_plugin import apply_state, delivery
        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSOIntentRevision, NSORoutePolicyObjectClass, NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        management = self._make_mgmt(self.device)
        NSORoutePolicyObjectClass.objects.create(
            family="community_list",
            object_name="CL-LOCAL",
            mode="local",
        )
        payload = {
            "community_lists": [
                {"name": "CL-MASTER", "entries": []},
                {"name": "cl-local", "entries": []},
                {"name": "CL-ATTACHED", "entries": []},
            ]
        }
        reconcile_route_policy(self.device, payload)
        master = NSORoutePolicyState.objects.get(management=management, object_name="CL-MASTER")
        local = NSORoutePolicyState.objects.get(management=management, object_name="cl-local")
        attached_local = NSORoutePolicyState.objects.get(management=management, object_name="CL-ATTACHED")
        NSORoutePolicyObjectClass.objects.create(
            family="community_list",
            object_name="CL-ATTACHED",
            mode="local",
        )
        for row in (master, local, attached_local):
            with intent_transaction(footprint_for_instance(row)):
                row.status = "accepted"
                row.save(update_fields=["status"])

        registry = delivery.delivery_keys()
        pushed = {}
        for push_seq, entry in enumerate(
            (candidate for candidate in registry.values() if candidate.in_protocol),
            start=1,
        ):
            revision, _created = NSOIntentRevision.objects.get_or_create(device=self.device, scope=entry.key)
            pushed[entry.key] = SimpleNamespace(revision=revision.revision, push_seq=push_seq)
        attempt_id = uuid4()

        apply_state.promote_current_intent(
            management,
            registry,
            pushed,
            apply_attempt_id=attempt_id,
            static_route_stored=False,
        )

        master.refresh_from_db()
        local.refresh_from_db()
        attached_local.refresh_from_db()
        self.assertEqual(master.status, "deploying")
        self.assertEqual(master.apply_attempt_id, attempt_id)
        self.assertEqual(attached_local.status, "deploying")
        self.assertEqual(attached_local.apply_attempt_id, attempt_id)
        self.assertEqual(local.status, "accepted")
        self.assertIsNone(local.apply_attempt_id)

    def _accepted_local_candidates(self, device, names, *, materialized: bool):
        """Accept one LOCAL route-policy row per name and return the promotion inputs."""
        from types import SimpleNamespace

        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSORoutePolicyObjectClass, NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        management = self._make_mgmt(device)

        def classify():
            for name in names:
                NSORoutePolicyObjectClass.objects.get_or_create(
                    family="community_list", object_name=name, defaults={"mode": "local"}
                )

        # Classifying before the reconcile leaves the row unmaterialized; after it keeps the GFK.
        if not materialized:
            classify()
        reconcile_route_policy(device, {"community_lists": [{"name": name, "entries": []} for name in names]})
        if materialized:
            classify()
        rows = list(NSORoutePolicyState.objects.filter(management=management).order_by("pk"))
        self.assertEqual(len(rows), len(names))
        for row in rows:
            self.assertEqual(row.object_id is not None, materialized)
            with intent_transaction(footprint_for_instance(row)):
                row.status = "accepted"
                row.save(update_fields=["status"])
        prepared = SimpleNamespace(management=management, rows=rows)
        self._refresh_pushed_snapshot(prepared, device)
        return prepared

    def _refresh_pushed_snapshot(self, prepared, device):
        """Re-read the current intent revisions so promotion sees an unchanged receipt."""
        from types import SimpleNamespace

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision

        prepared.registry = delivery.delivery_keys()
        prepared.pushed = {}
        for push_seq, entry in enumerate(
            (candidate for candidate in prepared.registry.values() if candidate.in_protocol),
            start=1,
        ):
            revision, _created = NSOIntentRevision.objects.get_or_create(device=device, scope=entry.key)
            prepared.pushed[entry.key] = SimpleNamespace(revision=revision.revision, push_seq=push_seq)

    def _promotion_queries(self, prepared):
        """Run promote_current_intent for one prepared device and return the SQL it issued."""
        from uuid import uuid4

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin import apply_state

        with CaptureQueriesContext(connection) as captured:
            apply_state.promote_current_intent(
                prepared.management,
                prepared.registry,
                prepared.pushed,
                apply_attempt_id=uuid4(),
                static_route_stored=False,
            )
        return [query["sql"] for query in captured.captured_queries]

    def test_promotion_classifies_route_policy_candidates_in_one_query(self):
        """Excluding unmaterialized LOCAL rows must not cost one classification query per row."""
        from netbox_nso_plugin.models import NSORoutePolicyObjectClass

        one = self._accepted_local_candidates(self.device, ["CL-SOLO"], materialized=False)
        many = self._accepted_local_candidates(
            self.device2, [f"CL-BULK-{index}" for index in range(6)], materialized=False
        )

        one_sql = self._promotion_queries(one)
        many_sql = self._promotion_queries(many)

        table = NSORoutePolicyObjectClass._meta.db_table
        classifications = (
            sum(table in sql for sql in one_sql),
            sum(table in sql for sql in many_sql),
        )
        self.assertEqual(
            classifications[0],
            classifications[1],
            f"classification queries grew with the candidate count: {classifications}",
        )
        self.assertEqual(
            len(one_sql),
            len(many_sql),
            f"promotion queries grew with the candidate count: {len(one_sql)} -> {len(many_sql)}",
        )
        for prepared in (one, many):
            for row in prepared.rows:
                row.refresh_from_db()
                self.assertEqual(row.status, "accepted")
                self.assertIsNone(row.apply_attempt_id)

    def test_promotion_resolves_linked_route_policy_targets_without_per_row_queries(self):
        """Linked LOCAL rows are promoted, and their target check stays one query per content type."""
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.models import NSOApplyAttempt, NSORoutePolicyObjectClass

        one = self._accepted_local_candidates(self.device, ["CL-LINKED-SOLO"], materialized=True)
        many = self._accepted_local_candidates(
            self.device2, [f"CL-LINKED-{index}" for index in range(6)], materialized=True
        )

        one_sql = self._promotion_queries(one)
        many_sql = self._promotion_queries(many)

        attempt_table = NSOApplyAttempt._meta.db_table
        class_table = NSORoutePolicyObjectClass._meta.db_table
        target_table = CommunityList._meta.db_table
        counts = []
        for sqls in (one_sql, many_sql):
            insert_at = next(index for index, sql in enumerate(sqls) if attempt_table in sql)
            before_insert = sqls[:insert_at]
            counts.append(
                (
                    sum(class_table in sql for sql in before_insert),
                    sum(target_table in sql for sql in before_insert),
                )
            )
        self.assertEqual(counts[0], counts[1], f"exclusion queries grew with the candidate count: {counts}")
        for prepared in (one, many):
            for row in prepared.rows:
                row.refresh_from_db()
                self.assertEqual(row.status, "deploying")
                self.assertIsNotNone(row.apply_attempt_id)

    def test_promotion_excludes_a_local_row_whose_materialized_target_is_gone(self):
        """A LOCAL row with both GFK ids set but no target row stays excluded, as it does today."""
        from uuid import uuid4

        from netbox_nso_plugin import apply_state

        from ._outbox_case import content_bulk_update

        prepared = self._accepted_local_candidates(self.device, ["CL-DANGLING"], materialized=True)
        row = prepared.rows[0]
        content_bulk_update(row, object_id=row.object_id + 10_000)
        self._refresh_pushed_snapshot(prepared, self.device)

        apply_state.promote_current_intent(
            prepared.management,
            prepared.registry,
            prepared.pushed,
            apply_attempt_id=uuid4(),
            static_route_stored=False,
        )

        row.refresh_from_db()
        self.assertIsNotNone(row.content_type_id)
        self.assertIsNotNone(row.object_id)
        self.assertEqual(row.status, "accepted")
        self.assertIsNone(row.apply_attempt_id)

    def test_classification_mode_agrees_with_apply_on_a_mixed_case_name(self):
        """The NSO tab badge and Apply must read one classification, case-insensitively."""
        from netbox_nso_plugin.models import NSORoutePolicyObjectClass, NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import _group_mode, reconcile_route_policy

        management = self._make_mgmt(self.device)
        NSORoutePolicyObjectClass.objects.create(family="community_list", object_name="CL-LOCAL", mode="local")
        reconcile_route_policy(self.device, {"community_lists": [{"name": "cl-local", "entries": []}]})
        state = NSORoutePolicyState.objects.get(management=management, object_name="cl-local")

        self.assertEqual(_group_mode(state.family, state.object_name), "local")
        self.assertEqual(state.classification_mode, "local")

    def test_promotion_excludes_a_local_row_that_only_postgres_case_folds_onto_its_class(self):
        """UPPER() folds the greek final sigma onto sigma; python str.lower()/upper() does not."""
        from types import SimpleNamespace
        from uuid import uuid4

        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSORoutePolicyObjectClass, NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import _group_mode, reconcile_route_policy

        management = self._make_mgmt(self.device)
        NSORoutePolicyObjectClass.objects.create(family="community_list", object_name="CL-\u03c3", mode="local")
        reconcile_route_policy(self.device, {"community_lists": [{"name": "CL-\u03c2", "entries": []}]})
        row = NSORoutePolicyState.objects.get(management=management, object_name="CL-\u03c2")
        self.assertNotEqual("CL-\u03c2".lower(), "CL-\u03c3".lower())
        self.assertEqual(_group_mode(row.family, row.object_name), "local")
        self.assertEqual(row.classification_mode, "local")
        self.assertIsNone(row.object_id)
        with intent_transaction(footprint_for_instance(row)):
            row.status = "accepted"
            row.save(update_fields=["status"])
        prepared = SimpleNamespace(management=management, rows=[row])
        self._refresh_pushed_snapshot(prepared, self.device)

        apply_state.promote_current_intent(
            prepared.management,
            prepared.registry,
            prepared.pushed,
            apply_attempt_id=uuid4(),
            static_route_stored=False,
        )

        row.refresh_from_db()
        self.assertEqual(row.status, "accepted")
        self.assertIsNone(row.apply_attempt_id)

    def test_reconciles_all_families(self):
        """One object per family → created in netbox_routing + a state row each."""
        self._make_mgmt(self.device)
        from netbox_routing.models import ASPath, CommunityList, PrefixList, RouteMap

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        states = reconcile_route_policy(self.device, self._payload())

        self.assertEqual(len(states), 4)
        self.assertEqual(PrefixList.objects.filter(name="PL-CUSTOMER").count(), 1)
        self.assertEqual(CommunityList.objects.filter(name="CL-LOCAL").count(), 1)
        self.assertEqual(ASPath.objects.filter(name="AP-PRIVATE").count(), 1)
        self.assertEqual(RouteMap.objects.filter(name="RM-IMPORT").count(), 1)

    def test_preflight_plan_freezes_roots_entries_m2m_and_through_rows_without_writes(self):
        """One route-policy plan covers every materialized graph write before locking."""
        from netbox_routing.models import (
            ASPath,
            ASPathEntry,
            Community,
            CommunityList,
            CommunityListEntry,
            CustomPrefix,
            PrefixList,
            PrefixListEntry,
            RouteMap,
            RouteMapEntry,
            RouteMapEntrySetCommunity,
        )

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import route_policy_reconcile_plan

        self._make_mgmt(self.device)
        payload = {
            "prefix_lists": [
                {
                    "name": "PLAN-PREFIXES",
                    "family": 4,
                    "entries": [{"action": "permit", "prefix": "198.18.0.0/24"}],
                }
            ],
            "community_lists": [
                {"name": "PLAN-COMMUNITIES", "entries": [{"action": "permit", "community": "64512:7"}]}
            ],
            "as_paths": [{"name": "PLAN-AS-PATH", "entries": [{"action": "permit", "pattern": "^64512$"}]}],
            "route_maps": [
                {
                    "name": "PLAN-ROUTE-MAP",
                    "entries": [
                        {
                            "action": "permit",
                            "match_prefix_lists": ["PLAN-PREFIXES"],
                            "match_community_lists": ["PLAN-COMMUNITIES"],
                            "match_as_paths": ["PLAN-AS-PATH"],
                            "set": '{"community_add": ["PLAN-COMMUNITIES", "64512:99"]}',
                        }
                    ],
                }
            ],
        }
        models = (
            PrefixList,
            CustomPrefix,
            PrefixListEntry,
            CommunityList,
            Community,
            CommunityListEntry,
            ASPath,
            ASPathEntry,
            RouteMap,
            RouteMapEntry,
            RouteMapEntrySetCommunity,
            NSORoutePolicyState,
        )
        before = {model._meta.label_lower: model.objects.count() for model in models}

        plan = route_policy_reconcile_plan(self.device, payload)

        self.assertEqual({model._meta.label_lower: model.objects.count() for model in models}, before)
        save_labels = {write.model_label for write in plan.write_set if write.operation == "save"}
        self.assertTrue({model._meta.label_lower for model in models} <= save_labels)
        through_models = {
            dict(write.values)["through_model"] for write in plan.write_set if write.operation == "m2m_add"
        }
        self.assertEqual(
            through_models,
            {
                "netbox_routing.routemapentry_match_aspath",
                "netbox_routing.routemapentry_match_community_list",
                "netbox_routing.routemapentry_match_prefix_list",
                "netbox_routing.routemapentrysetcommunity_communities",
            },
        )
        self.assertFalse(plan.content_keys)
        self.assertEqual(
            set(plan.lock_footprint.revision_keys),
            {
                (self.device.pk, "bgp"),
                (self.device.pk, "isis"),
                (self.device.pk, "ospf"),
                (self.device.pk, "route_policy"),
            },
        )

    def test_preflight_filters_shared_dependency_prefetches_to_the_payload(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from netbox_routing.models import ASPath, Community, CommunityList, CustomPrefix, PrefixList, RouteMap

        from netbox_nso_plugin.route_policy_reconciler import route_policy_reconcile_plan

        PrefixList.objects.create(name="PAYLOAD-PL")
        CommunityList.objects.create(name="PAYLOAD-CL")
        ASPath.objects.create(name="PAYLOAD-AP")
        RouteMap.objects.create(name="PAYLOAD-RM")
        CustomPrefix.objects.create(prefix="198.18.32.0/24")
        Community.objects.create(community="64532:100")
        payload = {
            "prefix_lists": [
                {
                    "name": "PAYLOAD-PL",
                    "entries": [{"action": "permit", "prefix": "198.18.32.0/24"}],
                }
            ],
            "community_lists": [
                {
                    "name": "PAYLOAD-CL",
                    "entries": [{"action": "permit", "community": "64532:100"}],
                }
            ],
            "as_paths": [{"name": "PAYLOAD-AP", "entries": [{"action": "permit", "pattern": "^64532$"}]}],
            "route_maps": [{"name": "PAYLOAD-RM", "entries": []}],
        }

        with CaptureQueriesContext(connection) as captured:
            route_policy_reconcile_plan(self.device, payload)

        sql = [query["sql"] for query in captured.captured_queries]
        for model in (PrefixList, CommunityList, ASPath, RouteMap, CustomPrefix, Community):
            table = model._meta.db_table
            prefetches = [query for query in sql if query.startswith("SELECT") and f'FROM "{table}"' in query]
            self.assertEqual(len(prefetches), 1)
            self.assertIn(" WHERE ", prefetches[0])

    def test_display_projection_rejects_a_registered_through_model(self):
        from netbox_routing.models import CommunityList, RouteMapEntry

        from netbox_nso_plugin.intent_state import IntentMutationProtocolError
        from netbox_nso_plugin.route_policy_reconciler import _Operations

        operations = _Operations()
        entry = RouteMapEntry()
        community_list = CommunityList(name="REGISTERED-DISPLAY")

        with self.assertRaisesRegex(IntentMutationProtocolError, "registered renderer input"):
            operations.display_m2m_add(entry, "match_community_list", (community_list,))

    def test_prefix_list_family_propagated_from_capture(self):
        """The materialized PrefixList mirrors the owner capture's address family.

        Without this the netbox_routing.PrefixList kept the model default (4), so every v6
        list (MARTIANS_V6, LGI_PREFIXES_V6, ...) showed as IPv4 even after the reader was
        fixed to report family 6.
        """
        self._make_mgmt(self.device)
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        payload = {
            "prefix_lists": [
                {
                    "name": "PL-V6",
                    "family": 6,
                    "entries": [{"sequence": 10, "action": "permit", "prefix": "2001:db8::/32"}],
                },
                {
                    "name": "PL-V4",
                    "family": 4,
                    "entries": [{"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8"}],
                },
            ],
            "community_lists": [],
            "as_paths": [],
            "route_maps": [],
        }
        reconcile_route_policy(self.device, payload)
        self.assertEqual(PrefixList.objects.get(name="PL-V6").family, 6)
        self.assertEqual(PrefixList.objects.get(name="PL-V4").family, 4)

    def test_prefix_list_family_corrected_on_owner_re_read(self):
        """A pre-existing PrefixList created as IPv4 is corrected to v6 when the owner
        re-imports it with family 6 (the stale-family backfill path)."""
        self._make_mgmt(self.device)
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        PrefixList.objects.create(name="PL-STALE6", family=4)
        payload = {
            "prefix_lists": [
                {"name": "PL-STALE6", "family": 6, "entries": [{"sequence": 10, "action": "permit", "prefix": "::/0"}]}
            ],
            "community_lists": [],
            "as_paths": [],
            "route_maps": [],
        }
        reconcile_route_policy(self.device, payload)
        self.assertEqual(PrefixList.objects.get(name="PL-STALE6").family, 6)

    def _pl_payload(self, name, prefix, family=4):
        return {
            "prefix_lists": [
                {"name": name, "family": family, "entries": [{"sequence": 10, "action": "permit", "prefix": prefix}]}
            ],
            "community_lists": [],
            "as_paths": [],
            "route_maps": [],
        }

    def test_master_default_conflicts_on_cross_device_divergence(self):
        """Control: with no classification (implicit MASTER), a second device whose version
        diverges is real drift — the existing dedup behavior."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        reconcile_route_policy(self.device, self._pl_payload("SHARED-PL", "10.0.0.0/8"))
        reconcile_route_policy(self.device2, self._pl_payload("SHARED-PL", "10.1.0.0/16"))
        s2 = NSORoutePolicyState.objects.get(
            management__device=self.device2, family="prefix_list", object_name="SHARED-PL"
        )
        self.assertEqual(s2.status, "conflict")

    def test_master_grouping_is_case_insensitive_across_devices(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        reconcile_route_policy(self.device, self._pl_payload("SHARED-CASE", "10.0.0.0/8"))
        reconcile_route_policy(self.device2, self._pl_payload("shared-case", "10.1.0.0/16"))

        rows = NSORoutePolicyState.objects.filter(family="prefix_list", object_name__iexact="shared-case")
        second = rows.get(management__device=self.device2)
        self.assertEqual(second.status, "conflict")
        self.assertEqual(rows.filter(is_materialized=True).count(), 1)

    def test_casing_only_rename_is_not_treated_as_stale_on_same_device(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        reconcile_route_policy(self.device, self._pl_payload("PRESENT-CASE", "10.0.0.0/8"))
        reconcile_route_policy(self.device, self._pl_payload("present-case", "10.0.0.0/8"))

        rows = NSORoutePolicyState.objects.filter(
            management__device=self.device,
            family="prefix_list",
            object_name__iexact="present-case",
        )
        self.assertEqual(rows.count(), 1)
        state = rows.get()
        self.assertTrue(state.device_present)
        self.assertNotIn(state.status, ("changed", "conflict"))

    def _rm_payload(self, name, entries):
        return {
            "prefix_lists": [],
            "community_lists": [],
            "as_paths": [],
            "route_maps": [{"name": name, "entries": entries}],
        }

    def test_cosmetic_cross_vendor_route_map_does_not_conflict(self):
        """Two devices carry the SAME logical route-map spelled in different vendor encodings
        (Junos term/terminal markers + scalar protocol vs Nokia action-type + leaf-list
        protocol) — the canonical route-map hash equates them, so the second device imports
        instead of a false cross-vendor ``conflict``. Models the live SET-LP-250 (rc1 vs ra1).
        """
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        junos = [
            {
                "sequence": 10,
                "action": "permit",
                "match": '{"_junos_term": "lp250", "protocol": "bgp"}',
                "set": '{"_junos_terminal": "none", "local_preference": 250}',
            }
        ]
        nokia = [
            {
                "sequence": 10,
                "action": "permit",
                "match": '{"protocol": ["bgp"]}',
                "set": '{"_timos_action_type": "next-policy", "local_preference": 250}',
            }
        ]
        reconcile_route_policy(self.device, self._rm_payload("SET-LP-250", junos))
        reconcile_route_policy(self.device2, self._rm_payload("SET-LP-250", nokia))
        s2 = NSORoutePolicyState.objects.get(
            management__device=self.device2, family="route_map", object_name="SET-LP-250"
        )
        self.assertNotEqual(s2.status, "conflict")

    def test_genuinely_different_route_map_still_conflicts(self):
        """Control: the canonical hash must NOT suppress real drift — a second device whose
        route-map sets a DIFFERENT local-preference is genuine cross-device divergence."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        owner = [{"sequence": 10, "action": "permit", "match": "{}", "set": '{"local_preference": 250}'}]
        diff = [{"sequence": 10, "action": "permit", "match": "{}", "set": '{"local_preference": 300}'}]
        reconcile_route_policy(self.device, self._rm_payload("SET-LP", owner))
        reconcile_route_policy(self.device2, self._rm_payload("SET-LP", diff))
        s2 = NSORoutePolicyState.objects.get(management__device=self.device2, family="route_map", object_name="SET-LP")
        self.assertEqual(s2.status, "conflict")

    # --- BOGONS: Junos inline route-filter vs Nokia named prefix-lists (content expansion) ---
    _MARTIANS = [
        {"sequence": 10, "action": "permit", "prefix": "0.0.0.0/8", "ge": 8, "le": 32},
        {"sequence": 20, "action": "permit", "prefix": "10.0.0.0/8", "ge": 8, "le": 32},
    ]
    _DEFAULT = [
        {"sequence": 10, "action": "permit", "prefix": "0.0.0.0/0"},  # bare → exact /0
        {"sequence": 20, "action": "permit", "prefix": "0.0.0.0/0", "ge": 25, "le": 32},
    ]
    _DEFAULT_2 = [{"sequence": 10, "action": "permit", "prefix": "0.0.0.0/0", "ge": 1, "le": 7}]
    _JUNOS_RM_ENTRY = [
        {
            "sequence": 10,
            "action": "deny",
            "match": json.dumps(
                {
                    "_junos_family": "inet",
                    "_junos_prefix_list_filter": [{"list": "MARTIANS_V4", "match": "orlonger"}],
                    "_junos_route_filter": [
                        {"match": "exact", "prefix": "0.0.0.0/0"},
                        {"match": "prefix-length-range", "arg": "/25-/32", "prefix": "0.0.0.0/0"},
                        {"match": "prefix-length-range", "arg": "/1-/7", "prefix": "0.0.0.0/0"},
                    ],
                    "protocol": "bgp",
                }
            ),
            "set": '{"_junos_terminal": "reject"}',
        }
    ]

    def _nokia_rm_entry(self, names):
        return [
            {
                "sequence": 1,
                "action": "deny",
                "match_prefix_lists": names,
                "match": json.dumps({"family": ["ipv4"], "protocol": ["bgp"]}),
                "set": "{}",
            }
        ]

    def _full_payload(self, prefix_lists, route_maps):
        return {"prefix_lists": prefix_lists, "community_lists": [], "as_paths": [], "route_maps": route_maps}

    def test_bogons_inline_route_filter_converges_with_named_lists(self):
        """rc1(Junos) inlines a route-filter set; ra1(Nokia) references the equivalent NAMED
        prefix-lists (MARTIANS_V4 + DEFAULT_ROUTE_IPv4 + DEFAULT_ROUTE_IPv4_2). The route-map
        hash expands both to prefix CONTENT, so the Nokia version imports — no false conflict.
        Real end-to-end: real prefix-lists materialise, the real DB resolver expands them."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        # device (Junos, owner): only MARTIANS_V4 exists as a list; the defaults are inline.
        reconcile_route_policy(
            self.device,
            self._full_payload(
                [{"name": "MARTIANS_V4", "family": 4, "entries": self._MARTIANS}],
                [{"name": "BOGONS-EXT-V4-out", "entries": self._JUNOS_RM_ENTRY}],
            ),
        )
        # device2 (Nokia): MARTIANS + both DEFAULT lists named; route-map references all three.
        reconcile_route_policy(
            self.device2,
            self._full_payload(
                [
                    {"name": "MARTIANS_V4", "family": 4, "entries": self._MARTIANS},
                    {"name": "DEFAULT_ROUTE_IPv4", "family": 4, "entries": self._DEFAULT},
                    {"name": "DEFAULT_ROUTE_IPv4_2", "family": 4, "entries": self._DEFAULT_2},
                ],
                [
                    {
                        "name": "BOGONS-EXT-V4-out",
                        "entries": self._nokia_rm_entry(["MARTIANS_V4", "DEFAULT_ROUTE_IPv4", "DEFAULT_ROUTE_IPv4_2"]),
                    }
                ],
            ),
        )
        s2 = NSORoutePolicyState.objects.get(
            management__device=self.device2, family="route_map", object_name="BOGONS-EXT-V4-out"
        )
        self.assertNotEqual(s2.status, "conflict")

    def test_bogons_extra_inline_filter_still_conflicts(self):
        """Control: the Nokia INBOUND version references only DEFAULT_ROUTE_IPv4 (no _2), so the
        Junos term's extra /1-7 filter is a GENUINE content difference → conflict preserved."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        reconcile_route_policy(
            self.device,
            self._full_payload(
                [{"name": "MARTIANS_V4", "family": 4, "entries": self._MARTIANS}],
                [{"name": "BOGONS-EXT-V4-in", "entries": self._JUNOS_RM_ENTRY}],
            ),
        )
        reconcile_route_policy(
            self.device2,
            self._full_payload(
                [
                    {"name": "MARTIANS_V4", "family": 4, "entries": self._MARTIANS},
                    {"name": "DEFAULT_ROUTE_IPv4", "family": 4, "entries": self._DEFAULT},
                ],
                [{"name": "BOGONS-EXT-V4-in", "entries": self._nokia_rm_entry(["MARTIANS_V4", "DEFAULT_ROUTE_IPv4"])}],
            ),
        )
        s2 = NSORoutePolicyState.objects.get(
            management__device=self.device2, family="route_map", object_name="BOGONS-EXT-V4-in"
        )
        self.assertEqual(s2.status, "conflict")

    def test_group_mode_is_case_insensitive(self):
        """_group_mode must match the object dedup (name__iexact): a peer device reporting a
        different case (ACCEPT-ALL vs accept-all — the same shared object) must still see the
        operator's LOCAL classification, not silently revert to implicit MASTER."""
        from netbox_nso_plugin.models import NSORoutePolicyObjectClass
        from netbox_nso_plugin.route_policy_reconciler import _group_mode

        NSORoutePolicyObjectClass.objects.create(family="prefix_list", object_name="ACCEPT-ALL", mode="local")
        self.assertEqual(_group_mode("prefix_list", "accept-all"), "local")  # different case still LOCAL
        self.assertEqual(_group_mode("prefix_list", "ACCEPT-ALL"), "local")  # exact case unchanged

    def test_local_classification_suppresses_cross_device_conflict(self):
        """A LOCAL group legitimately differs per device → captured-only, no materialization,
        and a diverging sibling is NOT flagged conflict (each keeps its own version)."""
        from netbox_nso_plugin.models import NSORoutePolicyObjectClass, NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        NSORoutePolicyObjectClass.objects.create(family="prefix_list", object_name="VRRP-PL", mode="local")
        reconcile_route_policy(self.device, self._pl_payload("VRRP-PL", "10.0.0.0/8"))
        reconcile_route_policy(self.device2, self._pl_payload("VRRP-PL", "10.1.0.0/16"))
        s1 = NSORoutePolicyState.objects.get(
            management__device=self.device, family="prefix_list", object_name="VRRP-PL"
        )
        s2 = NSORoutePolicyState.objects.get(
            management__device=self.device2, family="prefix_list", object_name="VRRP-PL"
        )
        self.assertNotEqual(s2.status, "conflict")
        self.assertFalse(s1.is_materialized)
        self.assertFalse(s2.is_materialized)
        # each device keeps its OWN version (no shared canonical)
        self.assertEqual(s1.captured["entries"][0]["prefix"], "10.0.0.0/8")
        self.assertEqual(s2.captured["entries"][0]["prefix"], "10.1.0.0/16")
        # LOCAL is not materialized into netbox-routing
        from netbox_routing.models import PrefixList

        self.assertFalse(PrefixList.objects.filter(name="VRRP-PL").exists())

    def test_classify_view_marks_local_and_redirects(self):
        """End-to-end through the classify view: POST mode=local → reclassifies, clears the
        cross-device conflict, and redirects to the versions page."""
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        from netbox_nso_plugin.models import NSORoutePolicyObjectClass, NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        reconcile_route_policy(self.device, self._pl_payload("LGI", "10.0.0.0/8"))
        reconcile_route_policy(self.device2, self._pl_payload("LGI", "10.1.0.0/16"))
        s2 = NSORoutePolicyState.objects.get(management__device=self.device2, family="prefix_list", object_name="LGI")
        self.assertEqual(s2.status, "conflict")

        su = get_user_model().objects.create_superuser("rp-classify-admin", "a@b.c", "pw")
        self.client.force_login(su)
        url = reverse("plugins:netbox_nso_plugin:routing_classify_route_policy", args=[s2.pk])
        resp = self.client.post(url, {"mode": "local"})

        self.assertEqual(resp.status_code, 302)
        s2.refresh_from_db()
        self.assertNotEqual(s2.status, "conflict")
        self.assertEqual(NSORoutePolicyObjectClass.objects.get(family="prefix_list", object_name="LGI").mode, "local")

    def test_classify_bulk_view_marks_selected_local(self):
        """End-to-end through the bulk classify view: POST selected divergent rows → each group
        is marked LOCAL and its cross-device conflict clears."""
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        from netbox_nso_plugin.models import NSORoutePolicyObjectClass, NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        reconcile_route_policy(self.device, self._pl_payload("LGI", "10.0.0.0/8"))
        reconcile_route_policy(self.device2, self._pl_payload("LGI", "10.1.0.0/16"))
        s2 = NSORoutePolicyState.objects.get(management__device=self.device2, family="prefix_list", object_name="LGI")
        self.assertEqual(s2.status, "conflict")

        su = get_user_model().objects.create_superuser("rp-bulk-admin", "a@b.c", "pw")
        self.client.force_login(su)
        url = reverse("plugins:netbox_nso_plugin:routing_classify_bulk_route_policy", args=[self.device2.pk])
        resp = self.client.post(url, {"state": [str(s2.pk)]})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(NSORoutePolicyObjectClass.objects.get(family="prefix_list", object_name="LGI").mode, "local")
        s2.refresh_from_db()
        self.assertNotEqual(s2.status, "conflict")

    def test_resettle_false_conflicts_clears_stale_conflict(self):
        """A row left 'conflict' after its hash converged with the owner's (the device not
        re-read) settles via the recompute pass, without a device round-trip."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import (
            reconcile_route_policy,
            resettle_false_conflicts,
            route_policy_resettle_plan,
        )

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        reconcile_route_policy(self.device, self._pl_payload("SHARED", "10.0.0.0/8"))
        reconcile_route_policy(self.device2, self._pl_payload("SHARED", "10.1.0.0/16"))
        s2 = NSORoutePolicyState.objects.get(
            management__device=self.device2, family="prefix_list", object_name="SHARED"
        )
        self.assertEqual(s2.status, "conflict")

        owner = NSORoutePolicyState.objects.get(family="prefix_list", object_name="SHARED", is_materialized=True)
        s2.content_hash = owner.content_hash  # hashes converged, but device2 wasn't re-read
        s2.save(update_fields=["content_hash"])

        plan = route_policy_resettle_plan({("prefix_list", "SHARED")})

        s2.refresh_from_db()
        self.assertEqual(s2.status, "conflict")
        self.assertEqual(
            {(write.operation, write.model_label) for write in plan.write_set},
            {("save", "netbox_nso_plugin.nsoroutepolicystate")},
        )

        cleared = resettle_false_conflicts()

        s2.refresh_from_db()
        self.assertNotEqual(s2.status, "conflict")
        self.assertGreaterEqual(cleared, 1)

    def test_set_classification_local_clears_existing_conflict(self):
        """Operator marks a diverging MASTER group LOCAL → the cross-device conflict clears and
        the group de-materializes, re-processing the stored per-device captures (no device read)."""
        from netbox_nso_plugin.models import NSORoutePolicyObjectClass, NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, set_classification

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        reconcile_route_policy(self.device, self._pl_payload("LGI", "10.0.0.0/8"))
        reconcile_route_policy(self.device2, self._pl_payload("LGI", "10.1.0.0/16"))
        s2 = NSORoutePolicyState.objects.get(management__device=self.device2, family="prefix_list", object_name="LGI")
        self.assertEqual(s2.status, "conflict")  # MASTER default → genuine cross-device drift

        set_classification("prefix_list", "LGI", "local")

        s1 = NSORoutePolicyState.objects.get(management__device=self.device, family="prefix_list", object_name="LGI")
        s2.refresh_from_db()
        self.assertNotEqual(s2.status, "conflict")
        self.assertFalse(s1.is_materialized)
        self.assertFalse(s2.is_materialized)
        self.assertEqual(NSORoutePolicyObjectClass.objects.get(family="prefix_list", object_name="LGI").mode, "local")

    def test_set_classification_master_re_establishes_drift(self):
        """Promote a LOCAL group back to MASTER → it re-materializes an owner and a diverging
        sibling drifts again."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, set_classification

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        set_classification("prefix_list", "LGI", "local")  # classify before import
        reconcile_route_policy(self.device, self._pl_payload("LGI", "10.0.0.0/8"))
        reconcile_route_policy(self.device2, self._pl_payload("LGI", "10.1.0.0/16"))
        s2 = NSORoutePolicyState.objects.get(management__device=self.device2, family="prefix_list", object_name="LGI")
        self.assertNotEqual(s2.status, "conflict")  # LOCAL → no cross-device drift

        set_classification("prefix_list", "LGI", "master")

        s2.refresh_from_db()
        self.assertEqual(s2.status, "conflict")  # divergent sibling drifts against the new master
        self.assertTrue(
            NSORoutePolicyState.objects.filter(family="prefix_list", object_name="LGI", is_materialized=True).exists()
        )

    def test_master_reclassification_reuses_the_existing_root(self):
        from netbox_routing.models import PrefixList, PrefixListEntry

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, set_classification

        self._make_mgmt(self.device)
        reconcile_route_policy(self.device, self._pl_payload("REUSE-ROOT", "198.18.2.0/24"))
        root = PrefixList.objects.get(name="REUSE-ROOT")
        set_classification("prefix_list", "REUSE-ROOT", "local")
        reconcile_route_policy(self.device, self._pl_payload("REUSE-ROOT", "198.18.3.0/24"))

        set_classification("prefix_list", "REUSE-ROOT", "master")

        self.assertEqual(PrefixList.objects.filter(name="REUSE-ROOT").count(), 1)
        self.assertEqual(PrefixList.objects.get(name="REUSE-ROOT").pk, root.pk)
        self.assertEqual(PrefixListEntry.objects.filter(prefix_list=root).count(), 1)
        self.assertEqual(str(PrefixListEntry.objects.get(prefix_list=root).assigned_prefix), "198.18.3.0/24")

    def test_classification_preflight_freezes_master_graph_without_writes(self):
        """LOCAL-to-MASTER classification plans the class, graph, and every overlay first."""
        from netbox_routing.models import CustomPrefix, PrefixList, PrefixListEntry

        from netbox_nso_plugin.models import NSORoutePolicyObjectClass, NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import (
            reconcile_route_policy,
            route_policy_classification_plan,
            set_classification,
        )

        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        set_classification("prefix_list", "PLAN-CLASSIFICATION", "local")
        reconcile_route_policy(self.device, self._pl_payload("PLAN-CLASSIFICATION", "198.18.0.0/24"))
        reconcile_route_policy(self.device2, self._pl_payload("PLAN-CLASSIFICATION", "198.18.1.0/24"))
        models = (NSORoutePolicyObjectClass, NSORoutePolicyState, PrefixList, CustomPrefix, PrefixListEntry)
        before = {model._meta.label_lower: model.objects.count() for model in models}

        plan = route_policy_classification_plan("prefix_list", "PLAN-CLASSIFICATION", "master")

        self.assertEqual({model._meta.label_lower: model.objects.count() for model in models}, before)
        save_labels = {write.model_label for write in plan.write_set if write.operation == "save"}
        self.assertTrue({model._meta.label_lower for model in models} <= save_labels)

    def test_community_invert_match_reconciled(self):
        """A community-list reported with invert_match=True sets the field; a plain
        list stays False; an idempotent re-read keeps it stable."""
        self._make_mgmt(self.device)
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        reconcile_route_policy(
            self.device,
            {
                "community_lists": [
                    {"name": "CL-INV", "invert_match": True, "entries": [{"community": "no-export"}]},
                    {"name": "CL-PLAIN", "invert_match": False, "entries": [{"community": "65000:1"}]},
                ]
            },
        )
        self.assertTrue(CommunityList.objects.get(name="CL-INV").invert_match)
        self.assertFalse(CommunityList.objects.get(name="CL-PLAIN").invert_match)

        # Idempotent re-read (same invert_match) keeps it stable.
        reconcile_route_policy(
            self.device,
            {"community_lists": [{"name": "CL-INV", "invert_match": True, "entries": [{"community": "no-export"}]}]},
        )
        self.assertTrue(CommunityList.objects.get(name="CL-INV").invert_match)

    def test_community_invert_match_flip_tracked_for_sole_owner(self):
        """A device-side invert_match flip diverges the content hash. For a sole-device
        owner (the only authority for the name) NetBox tracks the flip and the row stays
        ``imported``; a cross-device flip still conflicts (test_rematerialize_community_
        invert_match covers that)."""
        self._make_mgmt(self.device)
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        reconcile_route_policy(
            self.device,
            {"community_lists": [{"name": "CL-FLIP", "invert_match": True, "entries": [{"community": "no-export"}]}]},
        )
        self.assertTrue(CommunityList.objects.get(name="CL-FLIP").invert_match)

        reconcile_route_policy(
            self.device,
            {"community_lists": [{"name": "CL-FLIP", "invert_match": False, "entries": [{"community": "no-export"}]}]},
        )
        st = NSORoutePolicyState.objects.get(family="community_list", object_name="CL-FLIP")
        self.assertEqual(st.status, "imported")
        # Sole owner: NetBox mirror tracks the device flip.
        self.assertFalse(CommunityList.objects.get(name="CL-FLIP").invert_match)

    def test_as_path_uses_aspath_model(self):
        """Regression guard: as_paths reconcile into netbox_routing.ASPath.

        If the import in _get_routing_models breaks (e.g. a model rename), the
        reconcile silently returns [] via the ImportError guard — this asserts it
        actually created the ASPath object.
        """
        self._make_mgmt(self.device)
        from netbox_routing.models import ASPath

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        reconcile_route_policy(self.device, {"as_paths": [{"name": "AP-X", "entries": []}]})
        self.assertTrue(ASPath.objects.filter(name="AP-X").exists())

    def test_global_dedup_by_name(self):
        """Same-named object reported by two devices → one netbox_routing object."""
        self._make_mgmt(self.device)
        self._make_mgmt(self.device2)
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        pl = {"prefix_lists": [{"name": "PL-SHARED", "entries": [{"seq": 5}]}]}
        reconcile_route_policy(self.device, pl)
        reconcile_route_policy(self.device2, pl)
        self.assertEqual(PrefixList.objects.filter(name="PL-SHARED").count(), 1)

    def test_fills_entries_all_families(self):
        """Entries (not just parent shells) are written into netbox_routing."""
        self._make_mgmt(self.device)
        from netbox_routing.models import (
            ASPathEntry,
            CommunityListEntry,
            PrefixList,
            PrefixListEntry,
            RouteMapEntry,
        )

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        payload = {
            "prefix_lists": [
                {
                    "name": "PL-A",
                    "entries": [
                        {"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8", "le": 24},
                        {"sequence": 20, "action": "deny", "prefix": "10.0.0.0/8"},
                    ],
                },
            ],
            "community_lists": [{"name": "CL-A", "entries": [{"action": "permit", "community": "65000:1"}]}],
            "as_paths": [{"name": "AP-A", "entries": [{"action": "permit", "pattern": ".* 65000 .*"}]}],
            "route_maps": [
                {
                    "name": "RM-A",
                    "entries": [
                        {
                            "sequence": 10,
                            "action": "permit",
                            "match_prefix_lists": ["PL-A"],
                            "match_as_paths": ["AP-A"],
                            "match": '{"x": 1}',
                            "set": '{"local_preference": 200}',
                        },
                    ],
                }
            ],
        }
        reconcile_route_policy(self.device, payload)

        pl = PrefixList.objects.get(name="PL-A")
        ples = list(PrefixListEntry.objects.filter(prefix_list=pl).order_by("sequence"))
        self.assertEqual(len(ples), 2)
        self.assertEqual(ples[0].action, "permit")
        self.assertEqual(ples[0].le, 24)
        self.assertEqual(str(ples[0].assigned_prefix.prefix), "10.0.0.0/8")
        self.assertEqual([e.sequence for e in ples], [1, 2])  # positional, smallint-safe

        self.assertEqual(CommunityListEntry.objects.filter(community_list__name="CL-A").count(), 1)
        self.assertEqual(ASPathEntry.objects.filter(aspath__name="AP-A").count(), 1)

        rme = RouteMapEntry.objects.get(route_map__name="RM-A")
        self.assertEqual(rme.set, {"local_preference": 200})
        self.assertEqual([p.name for p in rme.match_prefix_list.all()], ["PL-A"])
        self.assertEqual([a.name for a in rme.match_aspath.all()], ["AP-A"])

    def test_route_map_keeps_existing_policy_references_absent_from_its_payload(self):
        from netbox_routing.models import CommunityList, PrefixList, RouteMapEntry

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        PrefixList.objects.create(name="PL-EXISTING")
        CommunityList.objects.create(name="CL-EXISTING")

        reconcile_route_policy(
            self.device,
            {
                "route_maps": [
                    {
                        "name": "RM-EXISTING-REFERENCE",
                        "entries": [
                            {
                                "sequence": 10,
                                "action": "permit",
                                "match_prefix_lists": ["PL-EXISTING"],
                                "set": '{"community_add": ["CL-EXISTING"]}',
                            }
                        ],
                    }
                ]
            },
        )

        entry = RouteMapEntry.objects.get(route_map__name="RM-EXISTING-REFERENCE")
        self.assertEqual(list(entry.match_prefix_list.values_list("name", flat=True)), ["PL-EXISTING"])
        self.assertEqual(
            list(entry.set_communities.values_list("community_list__name", flat=True)),
            ["CL-EXISTING"],
        )

    def test_new_route_map_reference_keeps_an_unreported_materialized_object(self):
        from netbox_routing.models import PrefixList, RouteMapEntry

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        reconcile_route_policy(
            self.device,
            {
                "prefix_lists": [
                    {
                        "name": "PL-NEW-REFERENCE",
                        "entries": [{"sequence": 10, "action": "permit", "prefix": "198.18.0.0/24"}],
                    }
                ]
            },
        )

        reconcile_route_policy(
            self.device,
            {
                "route_maps": [
                    {
                        "name": "RM-NEW-REFERENCE",
                        "entries": [
                            {
                                "sequence": 10,
                                "action": "permit",
                                "match_prefix_lists": ["PL-NEW-REFERENCE"],
                            }
                        ],
                    }
                ]
            },
        )

        state = NSORoutePolicyState.objects.get(family="prefix_list", object_name="PL-NEW-REFERENCE")
        self.assertFalse(state.device_present)
        self.assertTrue(PrefixList.objects.filter(name="PL-NEW-REFERENCE").exists())
        entry = RouteMapEntry.objects.get(route_map__name="RM-NEW-REFERENCE")
        self.assertEqual(list(entry.match_prefix_list.values_list("name", flat=True)), ["PL-NEW-REFERENCE"])

    def test_flow_control_lifted_from_set_json(self):
        """flow_control (IOS continue) rides in set-json → lifted into the field and
        removed from the stored set blob."""
        self._make_mgmt(self.device)
        from netbox_routing.models import RouteMapEntry

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        reconcile_route_policy(
            self.device,
            {
                "route_maps": [
                    {
                        "name": "RM-FC",
                        "entries": [
                            {
                                "sequence": 10,
                                "action": "permit",
                                "set": '{"flow_control": 30, "local_preference": 100}',
                            },
                        ],
                    }
                ]
            },
        )
        rme = RouteMapEntry.objects.get(route_map__name="RM-FC")
        self.assertEqual(rme.flow_control, 30)
        self.assertEqual(rme.set, {"local_preference": 100})  # flow_control popped out

    def test_all_member_kinds_land_verbatim_in_one_list(self):
        """The universal Community model stores EVERY member verbatim in the single
        CommunityList — standard, well-known keyword, typed extended (exact + regex),
        RFC 8092 large (exact + regex), and inline regex/wildcard. No parallel typed lists;
        the kind is derived by parsing. Well-known keywords are stored as text (no numeric
        normalization), so they round-trip."""
        self._make_mgmt(self.device)
        from netbox_routing.models import Community, CommunityListEntry

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        members = [
            ("1111:100", "standard"),
            ("no-export", "standard"),  # well-known keyword, stored verbatim (not numeric)
            ("target:1111:100", "extended"),  # extended exact
            ("target:*:*", "extended"),  # extended regex
            ("large:65000:1:2", "large"),  # large exact
            ("large:1111:.*:[0-4]", "large"),  # large regex
            ("1111:*", "standard"),  # inline regex
            ("1111:1113.", "standard"),  # wildcard
        ]
        payload = {
            "community_lists": [
                {"name": "CL-MIX", "entries": [{"action": "permit", "community": v} for v, _ in members]}
            ],
        }
        reconcile_route_policy(self.device, payload)

        # ALL members land in the one CommunityList, stored verbatim.
        self.assertEqual(CommunityListEntry.objects.filter(community_list__name="CL-MIX").count(), len(members))
        for value, expected_kind in members:
            comm = Community.objects.filter(community=value).first()
            self.assertIsNotNone(comm, f"{value!r} not stored verbatim")
            self.assertEqual(comm.kind, expected_kind, f"{value!r} kind")

    def test_route_map_links_single_community_list(self):
        """A route-map matching a community-list of ANY kind links the one CommunityList
        via match_community_list (there is no parallel extended/large list anymore)."""
        self._make_mgmt(self.device)
        from netbox_routing.models import RouteMapEntry

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        payload = {
            "community_lists": [{"name": "CL-RT", "entries": [{"action": "permit", "community": "target:1111:100"}]}],
            "route_maps": [
                {
                    "name": "RM-RT",
                    "entries": [
                        {"sequence": 10, "action": "permit", "match_community_lists": ["CL-RT"]},
                    ],
                }
            ],
        }
        reconcile_route_policy(self.device, payload)

        rme = RouteMapEntry.objects.get(route_map__name="RM-RT")
        self.assertEqual([e.name for e in rme.match_community_list.all()], ["CL-RT"])

    def test_sole_device_owner_auto_refreshes_on_divergence(self):
        """A sole-device materialized owner that re-reports different content is the only
        authority for that name, so NetBox tracks the change (full-replace) and the row
        stays ``imported`` instead of freezing in a (non-existent) cross-device conflict.

        The brownfield no-clobber rule still holds where it matters — when ANOTHER device
        shares the name — proven by test_divergent_second_device_conflicts_without_clobber.
        """
        self._make_mgmt(self.device)
        from netbox_routing.models import PrefixList, PrefixListEntry

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        reconcile_route_policy(
            self.device,
            {
                "prefix_lists": [
                    {"name": "PL-C", "entries": [{"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8"}]}
                ]
            },
        )
        pl = PrefixList.objects.get(name="PL-C")
        self.assertEqual(PrefixListEntry.objects.filter(prefix_list=pl).count(), 1)

        # Same (sole) device re-reports richer content → NetBox tracks it, no conflict.
        reconcile_route_policy(
            self.device,
            {
                "prefix_lists": [
                    {
                        "name": "PL-C",
                        "entries": [
                            {"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8"},
                            {"sequence": 20, "action": "permit", "prefix": "192.168.0.0/16"},
                        ],
                    }
                ]
            },
        )
        st = NSORoutePolicyState.objects.get(management__device=self.device, family="prefix_list", object_name="PL-C")
        self.assertEqual(st.status, "imported")
        self.assertTrue(st.is_materialized)
        self.assertEqual(PrefixListEntry.objects.filter(prefix_list=pl).count(), 2)  # refreshed to match device

    def test_owned_sole_owner_is_not_auto_refreshed(self):
        """The sole-owner auto-refresh never touches an operator-owned row: an ``accepted``
        owner that the device diverges from keeps its content (intent is not clobbered);
        the divergence is preserved for the operator to resolve via Apply/Accept."""
        from django.utils import timezone

        self._make_mgmt(self.device)
        from netbox_routing.models import PrefixList, PrefixListEntry

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        reconcile_route_policy(
            self.device,
            {
                "prefix_lists": [
                    {"name": "PL-OWN", "entries": [{"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8"}]}
                ]
            },
        )
        st = NSORoutePolicyState.objects.get(family="prefix_list", object_name="PL-OWN")
        st.status = "accepted"
        st.accepted_at = timezone.now()
        st.save(update_fields=["status", "accepted_at"])

        reconcile_route_policy(
            self.device,
            {
                "prefix_lists": [
                    {
                        "name": "PL-OWN",
                        "entries": [
                            {"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8"},
                            {"sequence": 20, "action": "permit", "prefix": "192.168.0.0/16"},
                        ],
                    }
                ]
            },
        )
        st.refresh_from_db()
        self.assertIn(st.status, ("accepted", "deploying", "in_sync", "apply_failed"))
        pl = PrefixList.objects.get(name="PL-OWN")
        self.assertEqual(PrefixListEntry.objects.filter(prefix_list=pl).count(), 1)  # intent not clobbered

    def test_unowned_removed_object_is_deleted(self):
        """A sole-device UNOWNED object the device stops reporting (referenced by nothing) is
        tracked away — the shared object and the overlay row are removed, not left as drift."""
        self._make_mgmt(self.device)
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        reconcile_route_policy(
            self.device,
            {"route_maps": [{"name": "RM-GONE", "entries": [{"sequence": 10, "action": "permit"}]}]},
        )
        self.assertTrue(RouteMap.objects.filter(name="RM-GONE").exists())

        reconcile_route_policy(self.device, {"route_maps": []})  # device removed it
        self.assertFalse(NSORoutePolicyState.objects.filter(family="route_map", object_name="RM-GONE").exists())
        self.assertFalse(RouteMap.objects.filter(name="RM-GONE").exists())  # object gone too

    def test_owned_removed_object_kept_as_drift(self):
        """An ACCEPTED object the device removes is KEPT and flagged: status=changed,
        device_present=False — operator intent is never auto-deleted."""
        from django.utils import timezone

        self._make_mgmt(self.device)
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        reconcile_route_policy(
            self.device,
            {"route_maps": [{"name": "RM-KEEP", "entries": [{"sequence": 10, "action": "permit"}]}]},
        )
        st = NSORoutePolicyState.objects.get(family="route_map", object_name="RM-KEEP")
        st.status = "accepted"
        st.accepted_at = timezone.now()
        st.save(update_fields=["status", "accepted_at"])

        reconcile_route_policy(self.device, {"route_maps": []})  # device removed it
        st.refresh_from_db()
        self.assertFalse(st.device_present)
        self.assertIn(st.status, ("accepted", "deploying", "in_sync", "apply_failed"))
        self.assertTrue(RouteMap.objects.filter(name="RM-KEEP").exists())  # kept (operator owns)

    def _rp_revision(self, device=None):
        from netbox_nso_plugin.models import NSOIntentRevision

        row = NSOIntentRevision.objects.filter(device=device or self.device, scope="route_policy").first()
        return row.revision if row else 0

    def test_omitted_unowned_group_removal_runs_under_the_gate(self):
        """The plan must predict the stale-row cascade: the gate refuses it under a mirror permit."""
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        self._make_mgmt(self.device)
        seeded = {"route_maps": [{"name": "RM-OMIT", "entries": [{"sequence": 10, "action": "permit"}]}]}
        reconcile_route_policy(self.device, seeded)
        before = self._rp_revision()

        empty = {"route_maps": []}
        plan = route_policy_reconcile_plan(self.device, empty)
        self.assertTrue(plan.changes_content)
        reconcile_route_policy(self.device, empty)

        self.assertFalse(NSORoutePolicyState.objects.filter(family="route_map", object_name="RM-OMIT").exists())
        self.assertFalse(RouteMap.objects.filter(name="RM-OMIT").exists())
        self.assertGreater(self._rp_revision(), before)

    def test_omitted_in_sync_owner_flag_runs_under_the_gate(self):
        """in_sync -> changed drops the owned group from the wire, so the plan must bump."""
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        self._make_mgmt(self.device)
        seeded = {"route_maps": [{"name": "RM-SYNCED", "entries": [{"sequence": 10, "action": "permit"}]}]}
        reconcile_route_policy(self.device, seeded)
        state = NSORoutePolicyState.objects.get(family="route_map", object_name="RM-SYNCED")
        with intent_transaction(footprint_for_instance(state)):
            state.status = "in_sync"
            state.save(update_fields=["status"])
        before = self._rp_revision()

        empty = {"route_maps": []}
        plan = route_policy_reconcile_plan(self.device, empty)
        self.assertTrue(plan.changes_content)
        reconcile_route_policy(self.device, empty)

        state.refresh_from_db()
        self.assertEqual(state.status, "changed")
        self.assertFalse(state.device_present)
        self.assertTrue(RouteMap.objects.filter(name="RM-SYNCED").exists())
        self.assertGreater(self._rp_revision(), before)

    def test_omitted_accepted_owner_is_a_lifecycle_only_read(self):
        """An accepted row keeps its status on absence: flags only, no bump."""
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        self._make_mgmt(self.device)
        seeded = {"route_maps": [{"name": "RM-ACCEPTED", "entries": [{"sequence": 10, "action": "permit"}]}]}
        reconcile_route_policy(self.device, seeded)
        state = NSORoutePolicyState.objects.get(family="route_map", object_name="RM-ACCEPTED")
        with intent_transaction(footprint_for_instance(state)):
            state.status = "accepted"
            state.save(update_fields=["status"])
        before = self._rp_revision()

        empty = {"route_maps": []}
        plan = route_policy_reconcile_plan(self.device, empty)
        self.assertFalse(plan.changes_content)
        reconcile_route_policy(self.device, empty)

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertFalse(state.device_present)
        self.assertTrue(RouteMap.objects.filter(name="RM-ACCEPTED").exists())
        self.assertEqual(self._rp_revision(), before)

    def test_omitted_referenced_unowned_group_is_a_lifecycle_only_read(self):
        """A still-referenced object is kept and flagged, so absence writes no content."""
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        self._make_mgmt(self.device)
        rmaps = [
            {"name": "RM-REF", "entries": [{"sequence": 10, "action": "permit", "match_prefix_lists": ["PL-KEPT"]}]}
        ]
        seeded = {
            "prefix_lists": [{"name": "PL-KEPT", "entries": [{"sequence": 10, "prefix": "10.0.0.0/8"}]}],
            "route_maps": rmaps,
        }
        reconcile_route_policy(self.device, seeded)
        before = self._rp_revision()

        dropped = {"route_maps": rmaps}
        plan = route_policy_reconcile_plan(self.device, dropped)
        self.assertFalse(plan.changes_content)
        reconcile_route_policy(self.device, dropped)

        state = NSORoutePolicyState.objects.get(family="prefix_list", object_name="PL-KEPT")
        self.assertFalse(state.device_present)
        self.assertTrue(PrefixList.objects.filter(name="PL-KEPT").exists())
        self.assertEqual(self._rp_revision(), before)

    def test_route_map_expands_matched_community_list_into_match_community(self):
        """A route-map matching a community-list also links that list's member
        Communities into match_community (devices never match communities directly)."""
        self._make_mgmt(self.device)
        from netbox_routing.models import Community, RouteMapEntry

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        payload = {
            "community_lists": [
                {
                    "name": "CL-M",
                    "entries": [
                        {"action": "permit", "community": "65000:1"},
                        {"action": "permit", "community": "65000:2"},
                    ],
                }
            ],
            "route_maps": [
                {
                    "name": "RM-M",
                    "entries": [
                        {"sequence": 10, "action": "permit", "match_community_lists": ["CL-M"]},
                    ],
                }
            ],
        }
        reconcile_route_policy(self.device, payload)

        rme = RouteMapEntry.objects.get(route_map__name="RM-M")
        self.assertEqual([c.name for c in rme.match_community_list.all()], ["CL-M"])
        matched = {str(c.community) for c in rme.match_community.all()}
        self.assertEqual(matched, {"65000:1", "65000:2"})
        self.assertEqual(Community.objects.filter(community__in=["65000:1", "65000:2"]).count(), 2)

    def test_same_operation_literal_communities_share_one_set_row(self):
        from netbox_routing.models import RouteMapEntrySetCommunity

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        reconcile_route_policy(
            self.device,
            {
                "route_maps": [
                    {
                        "name": "RM-LITERAL-GROUP",
                        "entries": [
                            {
                                "action": "permit",
                                "set": '{"community_add": ["64540:10", "64540:20"]}',
                            }
                        ],
                    }
                ]
            },
        )

        rows = RouteMapEntrySetCommunity.objects.filter(
            route_map_entry__route_map__name="RM-LITERAL-GROUP",
            operation="add",
            community_list__isnull=True,
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(
            set(rows.get().communities.values_list("community", flat=True)),
            {"64540:10", "64540:20"},
        )


class TestDeviceCaughtUpSettle(TestCase):
    """#93: device-caught-up settle for OWNED shared-object rows.

    Route-policy rows never left ``accepted`` on reconcile (settles_owned=False — its
    'matches' means materialized-content-unchanged, not device confirmation), so intent
    staged on 2026-06-14 sat for a month with the device long since matching (example-comm).
    When the device capture equals the CURRENT NetBox object content, that IS genuine
    device confirmation → settle accepted→in_sync. Sequence numbers are artifacts on
    both sides (readers synthesize 10/20/…, the push renumbers 1..n) — position is truth.
    """

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RpSettleMfg", slug="rpsettlemfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RpSettleDev", slug="rpsettledev")
        role = DeviceRole.objects.create(name="RpSettleRole", slug="rpsettlerole")
        site = Site.objects.create(name="RpSettleSite", slug="rpsettlesite")
        cls.device = Device.objects.create(name="rp-settle-router", device_type=dt, role=role, site=site)

    def _make_mgmt(self, device):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="rp-inst", defaults={"adapter_instance_id": "rp-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=device,
            defaults={"nso_instance": inst, "nso_device_name": f"rp-{device.pk}", "adapter_device_id": device.pk},
        )[0]

    def _accept(self, family, name):
        from django.utils import timezone

        from netbox_nso_plugin.models import NSORoutePolicyState

        st = NSORoutePolicyState.objects.get(family=family, object_name=name)
        st.status = "accepted"
        st.accepted_at = timezone.now()
        st.save(update_fields=["status", "accepted_at"])
        return st

    def test_accepted_settles_when_device_matches_current_object(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        payload = {
            "prefix_lists": [
                {"name": "PL-SETTLE", "entries": [{"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8"}]}
            ]
        }
        reconcile_route_policy(self.device, payload)
        self._accept("prefix_list", "PL-SETTLE")
        reconcile_route_policy(self.device, payload)  # device still reports the same content
        st = NSORoutePolicyState.objects.get(family="prefix_list", object_name="PL-SETTLE")
        self.assertEqual(st.status, "in_sync")

    def test_accepted_stays_when_device_diverges(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        reconcile_route_policy(
            self.device,
            {
                "prefix_lists": [
                    {"name": "PL-DIVERGE", "entries": [{"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8"}]}
                ]
            },
        )
        self._accept("prefix_list", "PL-DIVERGE")
        reconcile_route_policy(
            self.device,
            {
                "prefix_lists": [
                    {"name": "PL-DIVERGE", "entries": [{"sequence": 10, "action": "deny", "prefix": "10.0.0.0/8"}]}
                ]
            },
        )
        st = NSORoutePolicyState.objects.get(family="prefix_list", object_name="PL-DIVERGE")
        self.assertEqual(st.status, "accepted")  # intent differs from device → still pending

    def test_settle_is_sequence_insensitive(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        reconcile_route_policy(
            self.device,
            {
                "community_lists": [
                    {
                        "name": "CL-SEQ",
                        "invert_match": False,
                        "entries": [
                            {"sequence": 10, "action": "permit", "community": "65000:1"},
                            {"sequence": 20, "action": "permit", "community": "no-export"},
                        ],
                    }
                ]
            },
        )
        self._accept("community_list", "CL-SEQ")
        # Same members in the same ORDER, different sequence artifacts → device caught up.
        reconcile_route_policy(
            self.device,
            {
                "community_lists": [
                    {
                        "name": "CL-SEQ",
                        "invert_match": False,
                        "entries": [
                            {"sequence": 5, "action": "permit", "community": "65000:1"},
                            {"sequence": 15, "action": "permit", "community": "no-export"},
                        ],
                    }
                ]
            },
        )
        st = NSORoutePolicyState.objects.get(family="community_list", object_name="CL-SEQ")
        self.assertEqual(st.status, "in_sync")

    def test_roundtrip_extract_matches_capture(self):
        """fill() then extract() must land in the same canonical hash for every family
        with an extractor — the normalization guarantee the settle depends on."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy
        from netbox_nso_plugin.shared_object_ownership import device_caught_up

        self._make_mgmt(self.device)
        captures = {
            "prefix_list": {
                "name": "PL-RT",
                "entries": [
                    {"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8", "ge": 9, "le": 24},
                    {"sequence": 20, "action": "deny", "prefix": "0.0.0.0/0"},
                ],
            },
            "community_list": {
                "name": "CL-RT",
                "invert_match": True,
                "entries": [
                    {"sequence": 10, "action": "permit", "community": "65000:*"},
                    {"sequence": 20, "action": "permit", "community": "large:65000:1:2"},
                ],
            },
            "as_path": {
                "name": "AP-RT",
                "entries": [{"sequence": 10, "action": "permit", "pattern": "^65000_"}],
            },
        }
        reconcile_route_policy(
            self.device,
            {
                "prefix_lists": [captures["prefix_list"]],
                "community_lists": [captures["community_list"]],
                "as_paths": [captures["as_path"]],
            },
        )
        for family, cap in captures.items():
            st = NSORoutePolicyState.objects.get(family=family, object_name=cap["name"])
            self.assertIs(
                device_caught_up(family, cap, st.assigned_object),
                True,
                f"{family} roundtrip broke the settle normalization",
            )

    def test_route_map_settles_when_device_matches(self):
        """#100: route_map extract — a rich entry (match/set blobs, flow_control lifted
        into the model field, prefix-list name ref) must roundtrip through
        canonical_route_map and settle when the device re-reports the same content."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        payload = {
            "prefix_lists": [
                {"name": "PL-RM", "entries": [{"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8"}]}
            ],
            "route_maps": [
                {
                    "name": "RM-SETTLE",
                    "entries": [
                        {
                            "seq": 10,
                            "action": "permit",
                            "match": {"protocol": ["bgp"], "family": "ipv4"},
                            "set": {"local_preference": 200, "flow_control": 20},
                            "match_prefix_lists": ["PL-RM"],
                        },
                        {"seq": 20, "action": "deny", "match": {}, "set": {}},
                    ],
                }
            ],
        }
        reconcile_route_policy(self.device, payload)
        self._accept("route_map", "RM-SETTLE")
        reconcile_route_policy(self.device, payload)
        st = NSORoutePolicyState.objects.get(family="route_map", object_name="RM-SETTLE")
        self.assertEqual(st.status, "in_sync")

    def test_route_map_stays_when_device_diverges(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        payload = {"route_maps": [{"name": "RM-DIV", "entries": [{"seq": 10, "action": "permit"}]}]}
        reconcile_route_policy(self.device, payload)
        self._accept("route_map", "RM-DIV")
        reconcile_route_policy(
            self.device,
            {"route_maps": [{"name": "RM-DIV", "entries": [{"seq": 10, "action": "deny"}]}]},
        )
        st = NSORoutePolicyState.objects.get(family="route_map", object_name="RM-DIV")
        self.assertEqual(st.status, "accepted")

    def test_community_settle_ignores_recorded_unsupported_members(self):
        """#101: the REPRESENTABLE intent — a member the NED cannot hold (recorded in
        unsupported_members by the push transparency) must not block the settle: the
        device capture lacks it BY DESIGN, not by drift (the example-comm-2 wildcard case)."""
        from netbox_routing.models import Community, CommunityListEntry

        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        payload = {
            "community_lists": [
                {
                    "name": "CL-UNSUP",
                    "invert_match": False,
                    "entries": [{"sequence": 10, "action": "permit", "community": "65000:1"}],
                }
            ]
        }
        reconcile_route_policy(self.device, payload)
        st = self._accept("community_list", "CL-UNSUP")
        # operator adds a wildcard the NED can't hold; the push recorded it as unsupported
        wc, _ = Community.objects.get_or_create(community="color:0:12.")
        CommunityListEntry.objects.create(community_list=st.assigned_object, community=wc, action="PERMIT")
        st.unsupported_members = ["color:0:12."]
        st.save(update_fields=["unsupported_members"])
        reconcile_route_policy(self.device, payload)  # device still reports only 65000:1
        st.refresh_from_db()
        self.assertEqual(st.status, "in_sync")

    def test_community_settle_not_fooled_by_stale_unsupported_list(self):
        """The exclusion applies ONLY to the object side: if the device actually HOLDS a
        member that is (stale-)listed as unsupported, the sides differ → no settle."""
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._make_mgmt(self.device)
        payload = {
            "community_lists": [
                {
                    "name": "CL-STALE",
                    "invert_match": False,
                    "entries": [
                        {"sequence": 10, "action": "permit", "community": "65000:1"},
                        {"sequence": 20, "action": "permit", "community": "color:0:12."},
                    ],
                }
            ]
        }
        reconcile_route_policy(self.device, payload)
        st = self._accept("community_list", "CL-STALE")
        st.unsupported_members = ["color:0:12."]  # stale — the device DOES hold it
        st.save(update_fields=["unsupported_members"])
        reconcile_route_policy(self.device, payload)
        st.refresh_from_db()
        self.assertEqual(st.status, "accepted")  # object-filtered (1) != capture (2)


class TestSharedObjectOwnership(TestCase):
    """Per-device capture + materialized-owner + operator re-point (universal core).

    These exercise the shared_object_ownership machinery THROUGH the route-policy
    reconciler against the real netbox_routing models, so the cross-device behaviour the
    operator actually sees (show every version; pick which to own) is covered end to end.
    """

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="OwnMfg", slug="ownmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="OwnDev", slug="owndev")
        role = DeviceRole.objects.create(name="OwnRole", slug="ownrole")
        site = Site.objects.create(name="OwnSite", slug="ownsite")
        cls.d1 = Device.objects.create(name="own-r1", device_type=dt, role=role, site=site)
        cls.d2 = Device.objects.create(name="own-r2", device_type=dt, role=role, site=site)

    def _mgmt(self, device):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="own-inst", defaults={"adapter_instance_id": "own-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=device,
            defaults={"nso_instance": inst, "nso_device_name": f"own-{device.pk}", "adapter_device_id": device.pk},
        )[0]

    def _pl(self, name, prefixes):
        entries = [{"sequence": 10 * (i + 1), "action": "permit", "prefix": p} for i, p in enumerate(prefixes)]
        return {"prefix_lists": [{"name": name, "entries": entries}]}

    def test_registry_has_all_route_policy_families(self):
        from netbox_nso_plugin import route_policy_reconciler  # noqa: F401 — registers specs on import
        from netbox_nso_plugin import shared_object_ownership as ownership

        for family in ("prefix_list", "community_list", "as_path", "route_map"):
            self.assertIsNotNone(ownership.get_spec(family), f"missing spec for {family}")

    def test_capture_stored_per_device(self):
        """Each device's own reported content is persisted on its row (so we can show it)."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        reconcile_route_policy(self.d1, self._pl("PL-CAP", ["10.0.0.0/8"]))
        st = NSORoutePolicyState.objects.get(management__device=self.d1, object_name="PL-CAP")
        self.assertEqual(st.captured.get("name"), "PL-CAP")
        self.assertEqual(st.captured["entries"][0]["prefix"], "10.0.0.0/8")

    def test_first_writer_is_materialized_owner(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        reconcile_route_policy(self.d1, self._pl("PL-OWN", ["10.0.0.0/8"]))
        st = NSORoutePolicyState.objects.get(management__device=self.d1, object_name="PL-OWN")
        self.assertTrue(st.is_materialized)
        self.assertEqual(st.status, "imported")

    def test_reconcile_plan_rechecks_owner_after_acquisition(self):
        from netbox_routing.models import PrefixListEntry

        from netbox_nso_plugin.intent_state import IntentMutationProtocolError
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        from ._outbox_case import content_update

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        payload = self._pl("PL-OWNER-PREDICTION", ["198.18.0.0/24"])
        reconcile_route_policy(self.d1, payload)
        owner = NSORoutePolicyState.objects.get(management__device=self.d1, object_name="PL-OWNER-PREDICTION")
        plan = route_policy_reconcile_plan(self.d2, payload)
        self.assertFalse(plan.changes_content)
        content_update(owner, is_materialized=False)

        with self.assertRaisesRegex(IntentMutationProtocolError, "outside the frozen write set"):
            with renderer_mirror_writes(plan):
                reconcile_route_policy(self.d2, payload)

        entries = PrefixListEntry.objects.filter(prefix_list__name="PL-OWNER-PREDICTION")
        self.assertEqual({str(entry.assigned_prefix) for entry in entries}, {"198.18.0.0/24"})

    def test_reconcile_plan_rechecks_route_map_prefix_list_dependencies_after_acquisition(self):
        from netbox_nso_plugin import shared_object_ownership as ownership
        from netbox_nso_plugin.intent_state import IntentMutationProtocolError
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        from ._outbox_case import content_update

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        prefix_name = "PL-ROUTE-MAP-PREDICTION"
        route_map_name = "RM-PREFIX-PREDICTION"
        prefix_capture = {
            "name": prefix_name,
            "entries": [{"sequence": 10, "action": "permit", "prefix": "198.18.0.0/24"}],
        }
        route_map_capture = {
            "name": route_map_name,
            "entries": [
                {"sequence": 10, "action": "permit", "match_prefix_lists": [prefix_name]},
            ],
        }
        payload = {"prefix_lists": [prefix_capture], "route_maps": [route_map_capture]}
        reconcile_route_policy(self.d1, payload)
        d1_prefix = NSORoutePolicyState.objects.get(
            management__device=self.d1,
            family="prefix_list",
            object_name=prefix_name,
        )
        d1_route_map = NSORoutePolicyState.objects.get(
            management__device=self.d1,
            family="route_map",
            object_name=route_map_name,
        )
        content_update(d1_prefix, status="accepted")
        content_update(d1_route_map, status="accepted")
        reconcile_route_policy(self.d2, payload)
        d2_prefix = NSORoutePolicyState.objects.get(
            management__device=self.d2,
            family="prefix_list",
            object_name=prefix_name,
        )
        d2_route_map = NSORoutePolicyState.objects.get(
            management__device=self.d2,
            family="route_map",
            object_name=route_map_name,
        )
        ownership.rematerialize(d2_route_map)
        d1_prefix.refresh_from_db()
        d1_route_map.refresh_from_db()
        d2_prefix.refresh_from_db()
        d2_route_map.refresh_from_db()
        self.assertEqual((d1_prefix.status, d1_prefix.is_materialized), ("accepted", True))
        self.assertEqual((d1_route_map.status, d1_route_map.is_materialized), ("accepted", False))
        self.assertEqual((d2_prefix.status, d2_prefix.is_materialized), ("imported", False))
        self.assertEqual((d2_route_map.status, d2_route_map.is_materialized), ("imported", True))

        plan = route_policy_reconcile_plan(self.d2, payload)
        self.assertFalse(plan.changes_content)
        changed_prefix_capture = {
            "name": prefix_name,
            "entries": [{"sequence": 10, "action": "permit", "prefix": "198.18.1.0/24"}],
        }
        content_update(
            d1_prefix,
            captured=changed_prefix_capture,
            content_hash=ownership.hash_captured("prefix_list", changed_prefix_capture),
        )

        with self.assertRaisesRegex(IntentMutationProtocolError, "outside the frozen write set"):
            with renderer_mirror_writes(plan):
                reconcile_route_policy(self.d2, payload)

        d2_prefix.refresh_from_db()
        d2_route_map.refresh_from_db()
        self.assertEqual((d2_prefix.status, d2_prefix.is_materialized), ("imported", False))
        self.assertEqual((d2_route_map.status, d2_route_map.is_materialized), ("imported", True))

    def test_first_prefix_list_capture_replaces_a_populated_unowned_root(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import CustomPrefix, PrefixList, PrefixListEntry

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.reconcile import _LeaseOutcome, reconcile_category
        from netbox_nso_plugin.route_policy_reconciler import route_policy_reconcile_plan

        root = PrefixList.objects.create(name="PL-FIRST-CAPTURE")
        old_prefix = CustomPrefix.objects.create(prefix="198.18.40.0/24")
        old_entry = PrefixListEntry.objects.create(
            prefix_list=root,
            assigned_prefix_type=ContentType.objects.get_for_model(CustomPrefix),
            assigned_prefix_id=old_prefix.pk,
            sequence=10,
            action="permit",
        )
        management = self._mgmt(self.d1)
        payload = self._pl(root.name, [])

        plan = route_policy_reconcile_plan(self.d1, payload)
        planned_deletes = [
            write
            for write in plan.write_set
            if write.operation == "delete"
            and write.model_label == "netbox_routing.prefixlistentry"
            and write.pk == old_entry.pk
        ]
        self.assertEqual(len(planned_deletes), 1)

        with (
            patch("netbox_nso_plugin.reconcile._acquire_reconcile_lease", return_value=_LeaseOutcome()),
            patch("netbox_nso_plugin.adapter_client.get_route_policy", return_value=payload),
        ):
            result = reconcile_category(self.d1, management, "route_policy")

        state = NSORoutePolicyState.objects.get(management__device=self.d1, object_name=root.name)
        self.assertEqual(result["_gate"]["route_policy"], "legacy")
        self.assertTrue(state.is_materialized)
        self.assertFalse(PrefixListEntry.objects.filter(prefix_list=root).exists())

    def test_first_community_list_capture_replaces_a_populated_unowned_root(self):
        from netbox_routing.models import Community, CommunityList, CommunityListEntry

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        root = CommunityList.objects.create(name="CL-FIRST-CAPTURE")
        old_community = Community.objects.create(community="64512:1")
        CommunityListEntry.objects.create(community_list=root, community=old_community, action="permit")
        self._mgmt(self.d1)
        payload = {"community_lists": [{"name": root.name, "entries": []}]}

        reconcile_route_policy(self.d1, payload)

        state = NSORoutePolicyState.objects.get(management__device=self.d1, object_name=root.name)
        self.assertTrue(state.is_materialized)
        self.assertFalse(CommunityListEntry.objects.filter(community_list=root).exists())

    def test_owned_prefix_list_capture_replaces_content_when_the_group_has_no_owner(self):
        from netbox_routing.models import PrefixListEntry

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.reconcile import _LeaseOutcome, reconcile_category
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        from ._outbox_case import content_update

        management = self._mgmt(self.d1)
        initial = self._pl("PL-OWNED-FIRST-CAPTURE", ["198.18.64.0/24"])
        reconcile_route_policy(self.d1, initial)
        state = NSORoutePolicyState.objects.get(management=management, object_name="PL-OWNED-FIRST-CAPTURE")
        content_update(state, status="accepted", is_materialized=False)
        payload = self._pl(state.object_name, [])

        self.assertTrue(route_policy_reconcile_plan(self.d1, payload).changes_content)

        with (
            patch("netbox_nso_plugin.reconcile._acquire_reconcile_lease", return_value=_LeaseOutcome()),
            patch("netbox_nso_plugin.adapter_client.get_route_policy", return_value=payload),
        ):
            result = reconcile_category(self.d1, management, "route_policy")

        state.refresh_from_db()
        self.assertEqual(result["_gate"]["route_policy"], "legacy")
        self.assertEqual(state.status, "accepted")
        self.assertTrue(state.is_materialized)
        self.assertFalse(PrefixListEntry.objects.filter(prefix_list_id=state.object_id).exists())

    def test_reconcile_plans_duplicate_materialized_owner_retirement(self):
        from netbox_nso_plugin.intent_state import SourceRow
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        from ._outbox_case import content_update

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        payload = self._pl("PL-SOLE", ["198.18.0.0/24"])
        reconcile_route_policy(self.d1, payload)
        reconcile_route_policy(self.d2, payload)
        owner = NSORoutePolicyState.objects.get(management__device=self.d1, object_name="PL-SOLE")
        sibling = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="PL-SOLE")
        content_update(sibling, is_materialized=True)

        plan = route_policy_reconcile_plan(self.d1, payload)

        sibling_writes = [
            write
            for write in plan.write_set
            if write.operation == "save"
            and write.model_label == "netbox_nso_plugin.nsoroutepolicystate"
            and write.pk == sibling.pk
        ]
        self.assertEqual(len(sibling_writes), 1)
        self.assertEqual(sibling_writes[0].update_fields, ("is_materialized",))
        self.assertFalse(dict(sibling_writes[0].values)["is_materialized"])
        self.assertIn(
            SourceRow("netbox_nso_plugin.nsoroutepolicystate", sibling.pk),
            plan.lock_footprint.overlay_rows,
        )

        reconcile_route_policy(self.d1, payload)

        owner.refresh_from_db()
        sibling.refresh_from_db()
        self.assertTrue(owner.is_materialized)
        self.assertFalse(sibling.is_materialized)
        self.assertEqual(
            NSORoutePolicyState.objects.filter(
                family="prefix_list",
                object_name="PL-SOLE",
                is_materialized=True,
            ).count(),
            1,
        )

    def test_rematerialize_bumps_former_owner_last_sync_at(self):
        """Re-pointing a shared object resettles the old owner's sibling row and recomputes its
        status — its last_sync_at must be bumped too, else the UI shows a stale 'last seen' next
        to the freshly-recomputed status."""
        from django.utils import timezone

        from netbox_nso_plugin import shared_object_ownership as ownership
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(self.d1, self._pl("PL-RS", ["10.0.0.0/8"]))  # d1 owns
        reconcile_route_policy(self.d2, self._pl("PL-RS", ["10.1.0.0/16"]))  # d2 diverges
        sib = NSORoutePolicyState.objects.get(management__device=self.d1, object_name="PL-RS")
        other = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="PL-RS")
        old = timezone.now() - timezone.timedelta(days=1)
        sib.last_sync_at = old
        sib.save(update_fields=["last_sync_at"])  # backdate to prove the bump

        ownership.rematerialize(other)  # re-point to d2 → resettles the d1 sibling

        sib.refresh_from_db()
        self.assertGreater(sib.last_sync_at, old)

    def test_divergent_second_device_conflicts_without_clobber(self):
        """Two devices, same name, different content → ONE object holding the first
        device's content; the second device is a NON-owner flagged conflict, its own
        version captured, the shared object NOT clobbered."""
        from netbox_routing.models import PrefixList, PrefixListEntry

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(self.d1, self._pl("PL-DIV", ["10.0.0.0/8"]))
        reconcile_route_policy(self.d2, self._pl("PL-DIV", ["10.0.0.0/8", "192.168.0.0/16"]))

        self.assertEqual(PrefixList.objects.filter(name="PL-DIV").count(), 1)
        pl = PrefixList.objects.get(name="PL-DIV")
        self.assertEqual(PrefixListEntry.objects.filter(prefix_list=pl).count(), 1)  # d1 content, not clobbered

        s1 = NSORoutePolicyState.objects.get(management__device=self.d1, object_name="PL-DIV")
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="PL-DIV")
        self.assertTrue(s1.is_materialized)
        self.assertEqual(s1.status, "imported")
        self.assertFalse(s2.is_materialized)
        self.assertEqual(s2.status, "conflict")
        # d2's own divergent version is captured so the UI can show it.
        self.assertEqual(len(s2.captured["entries"]), 2)

    def test_matching_second_device_imports_not_conflict(self):
        """Same name AND same content on the second device is NOT a conflict."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(self.d1, self._pl("PL-SAME", ["10.0.0.0/8"]))
        reconcile_route_policy(self.d2, self._pl("PL-SAME", ["10.0.0.0/8"]))
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="PL-SAME")
        self.assertEqual(s2.status, "imported")
        self.assertFalse(s2.is_materialized)

    def test_owner_auto_refreshes_even_with_other_devices(self):
        """NetBox mirrors ONE version per name (the materialized owner's). When that owner's
        own device changes and the row is unowned, NetBox tracks it — even if other devices
        share the name. Only the owner ever writes, so there is no last-writer churn; the
        non-owners still surface as ``conflict`` when they diverge from the tracked version."""
        from netbox_routing.models import PrefixList, PrefixListEntry

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(self.d1, self._pl("PL-MULTI", ["10.0.0.0/8"]))  # d1 owns
        reconcile_route_policy(self.d2, self._pl("PL-MULTI", ["10.0.0.0/8"]))  # d2 matches

        # The owner's device changes → NetBox follows it (not a frozen conflict).
        reconcile_route_policy(self.d1, self._pl("PL-MULTI", ["10.0.0.0/8", "192.168.0.0/16"]))
        s1 = NSORoutePolicyState.objects.get(management__device=self.d1, object_name="PL-MULTI")
        self.assertEqual(s1.status, "imported")
        self.assertTrue(s1.is_materialized)
        pl = PrefixList.objects.get(name="PL-MULTI")
        self.assertEqual(PrefixListEntry.objects.filter(prefix_list=pl).count(), 2)  # NetBox tracked the owner

        # A non-owner that now diverges from the tracked (owner) version still conflicts.
        reconcile_route_policy(self.d2, self._pl("PL-MULTI", ["10.0.0.0/8"]))
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="PL-MULTI")
        self.assertEqual(s2.status, "conflict")

    def test_owner_removal_repoints_to_sibling(self):
        """The owner's device removes a shared object another device still reports → re-point
        ownership to that device (object lives on) and drop the removing device's row."""
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(self.d1, self._pl("PL-RP", ["10.0.0.0/8"]))  # d1 owns
        reconcile_route_policy(self.d2, self._pl("PL-RP", ["10.0.0.0/8"]))  # d2 also reports it

        reconcile_route_policy(self.d1, {"prefix_lists": []})  # d1 removes it
        self.assertFalse(NSORoutePolicyState.objects.filter(management__device=self.d1, object_name="PL-RP").exists())
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="PL-RP")
        self.assertTrue(s2.is_materialized)  # d2 is the new owner
        self.assertTrue(PrefixList.objects.filter(name="PL-RP").exists())  # object kept

    def test_referenced_object_kept_on_removal(self):
        """A removed unowned object still referenced by a route-map is NOT deleted (that would
        break the reference) — it is kept and flagged drift instead."""
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        rmaps = [
            {
                "name": "RM-REF",
                "entries": [{"sequence": 10, "action": "permit", "match_prefix_lists": ["PL-REF"]}],
            }
        ]
        reconcile_route_policy(
            self.d1,
            {
                "prefix_lists": [{"name": "PL-REF", "entries": [{"sequence": 10, "prefix": "10.0.0.0/8"}]}],
                "route_maps": rmaps,
            },
        )
        # Device drops PL-REF as a top-level prefix-list, but the route-map still references it.
        reconcile_route_policy(self.d1, {"route_maps": rmaps})
        pl_state = NSORoutePolicyState.objects.get(family="prefix_list", object_name="PL-REF")
        self.assertFalse(pl_state.device_present)  # flagged removed
        self.assertTrue(PrefixList.objects.filter(name="PL-REF").exists())  # kept — still referenced

    def _rp_revision(self, device):
        from netbox_nso_plugin.models import NSOIntentRevision

        row = NSOIntentRevision.objects.filter(device=device, scope="route_policy").first()
        return row.revision if row else 0

    def test_non_owner_drop_of_a_shared_object_is_a_lifecycle_only_read(self):
        """Dropping a non-materialized row leaves the shared object untouched: no bump."""
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        for device in (self.d1, self.d2):
            payload = self._pl("PL-DROP", ["10.0.0.0/8"])
            reconcile_route_policy(device, payload)
        before = (self._rp_revision(self.d1), self._rp_revision(self.d2))

        empty = {"prefix_lists": []}
        plan = route_policy_reconcile_plan(self.d2, empty)
        self.assertFalse(plan.changes_content)
        reconcile_route_policy(self.d2, empty)

        self.assertFalse(NSORoutePolicyState.objects.filter(management__device=self.d2, object_name="PL-DROP").exists())
        self.assertTrue(PrefixList.objects.filter(name="PL-DROP").exists())
        self.assertEqual((self._rp_revision(self.d1), self._rp_revision(self.d2)), before)

    def test_owner_drop_of_a_shared_object_rematerializes_under_the_gate(self):
        """Re-pointing refills the shared object from the sibling, so the plan must bump."""
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        for device in (self.d1, self.d2):
            payload = self._pl("PL-HANDOVER", ["10.0.0.0/8"])
            reconcile_route_policy(device, payload)
        before = self._rp_revision(self.d1)

        empty = {"prefix_lists": []}
        plan = route_policy_reconcile_plan(self.d1, empty)
        self.assertTrue(plan.changes_content)
        reconcile_route_policy(self.d1, empty)

        self.assertFalse(
            NSORoutePolicyState.objects.filter(management__device=self.d1, object_name="PL-HANDOVER").exists()
        )
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="PL-HANDOVER")
        self.assertTrue(s2.is_materialized)
        self.assertTrue(PrefixList.objects.filter(name="PL-HANDOVER").exists())
        self.assertGreater(self._rp_revision(self.d1), before)

    def _race_standalone(self, device, payload, mutate):
        """Run the standalone reconcile with *mutate* applied between plan and lock."""
        from netbox_nso_plugin import renderer_writer
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        real_mirror = renderer_writer.renderer_mirror_writes
        fired = []

        def racing_mirror(plan):
            if not fired:
                fired.append(True)
                mutate()
            return real_mirror(plan)

        with patch.object(renderer_writer, "renderer_mirror_writes", racing_mirror):
            reconcile_route_policy(device, payload)
        self.assertTrue(fired, "the raced mirror acquisition never ran")

    def test_standalone_replay_refuses_a_sibling_removed_after_planning(self):
        """The omitted-group branch reads the live sibling, so its removal must refuse the replay."""
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSODeviceManagement, NSORoutePolicyState
        from netbox_nso_plugin.renderer_writer import IntentPlanStaleError
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        payload = self._pl("PL-RACE-GONE", ["10.0.0.0/8"])
        reconcile_route_policy(self.d1, payload)
        reconcile_route_policy(self.d2, payload)
        self.assertTrue(
            NSORoutePolicyState.objects.get(management__device=self.d1, object_name="PL-RACE-GONE").is_materialized
        )
        empty = {"prefix_lists": []}
        self.assertFalse(route_policy_reconcile_plan(self.d2, empty).changes_content)

        with self.assertRaises(IntentPlanStaleError):
            self._race_standalone(
                self.d2,
                empty,
                lambda: NSODeviceManagement.objects.get(device=self.d1).delete(),
            )

        # Nothing was written: the unreferenced root is still there with D2's row.
        self.assertTrue(PrefixList.objects.filter(name="PL-RACE-GONE").exists())
        self.assertTrue(
            NSORoutePolicyState.objects.filter(management__device=self.d2, object_name="PL-RACE-GONE").exists()
        )

        before = self._rp_revision(self.d2)
        plan = route_policy_reconcile_plan(self.d2, empty)
        self.assertTrue(plan.changes_content)
        reconcile_route_policy(self.d2, empty)
        self.assertFalse(NSORoutePolicyState.objects.filter(object_name="PL-RACE-GONE").exists())
        self.assertFalse(PrefixList.objects.filter(name="PL-RACE-GONE").exists())
        self.assertGreater(self._rp_revision(self.d2), before)

    def test_standalone_replay_refuses_a_sibling_whose_liveness_flipped_after_planning(self):
        """A sibling that stops reporting between plan and lock changes the stale-group branch."""
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.renderer_writer import IntentPlanStaleError
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        from ._outbox_case import content_update

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        payload = self._pl("PL-RACE-FLIP", ["10.0.0.0/8"])
        reconcile_route_policy(self.d1, payload)
        reconcile_route_policy(self.d2, payload)
        owner = NSORoutePolicyState.objects.get(management__device=self.d1, object_name="PL-RACE-FLIP")
        empty = {"prefix_lists": []}
        self.assertFalse(route_policy_reconcile_plan(self.d2, empty).changes_content)

        with self.assertRaises(IntentPlanStaleError):
            self._race_standalone(self.d2, empty, lambda: content_update(owner, device_present=False))

        self.assertTrue(PrefixList.objects.filter(name="PL-RACE-FLIP").exists())
        self.assertTrue(
            NSORoutePolicyState.objects.filter(management__device=self.d2, object_name="PL-RACE-FLIP").exists()
        )

    def _drop_match_edge(self, prefix_list_name):
        """Another writer drops the match edge, exactly as a re-read of that route-map would."""
        from dataclasses import replace

        from netbox_routing.models import RouteMapEntry

        from netbox_nso_plugin.intent_state import deletion_footprint_for_instance, intent_transaction
        from netbox_nso_plugin.signals import suppress_intent_push

        edge = RouteMapEntry.match_prefix_list.through.objects.get(prefixlist__name=prefix_list_name)
        footprint = deletion_footprint_for_instance(edge)
        footprint = replace(footprint, device_ids=tuple(sorted({self.d1.pk, self.d2.pk})))
        with suppress_intent_push(), intent_transaction(footprint):
            edge.delete()

    def _seed_referenced_prefix_list(self, prefix_list_name, route_map_name):
        """D1 owns the prefix list; D2's route-map entry is its only reference."""
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(self.d1, self._pl(prefix_list_name, ["10.0.0.0/8"]))
        reconcile_route_policy(
            self.d2,
            {
                "route_maps": [
                    {
                        "name": route_map_name,
                        "entries": [{"sequence": 10, "action": "permit", "match_prefix_lists": [prefix_list_name]}],
                    }
                ]
            },
        )

    def test_standalone_replay_refuses_a_reference_deleted_at_the_capture_seam(self):
        """Retention and its read dependencies share one capture, so nothing escapes between them."""
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin import route_policy_reconciler
        from netbox_nso_plugin.intent_state import IntentMutationProtocolError
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        self._seed_referenced_prefix_list("PL-SEAM-REF", "RM-SEAM-REF")
        real_references = route_policy_reconciler._object_references
        fired = []

        def racing_references(obj, family):
            captured = real_references(obj, family)
            if not fired and family == "prefix_list":
                fired.append(True)
                self._drop_match_edge("PL-SEAM-REF")
            return captured

        empty = {"prefix_lists": []}
        with patch.object(route_policy_reconciler, "_object_references", racing_references):
            with self.assertRaisesRegex(IntentMutationProtocolError, "routemapentry_match_prefix_list"):
                reconcile_route_policy(self.d1, empty)
        self.assertTrue(fired, "the reference capture never ran")

        # The retention branch never ran: D1 still reports the group as present.
        self.assertTrue(
            NSORoutePolicyState.objects.get(management__device=self.d1, object_name="PL-SEAM-REF").device_present
        )
        self.assertTrue(PrefixList.objects.filter(name="PL-SEAM-REF").exists())

        before = self._rp_revision(self.d1)
        self.assertTrue(route_policy_reconcile_plan(self.d1, empty).changes_content)
        reconcile_route_policy(self.d1, empty)
        self.assertFalse(NSORoutePolicyState.objects.filter(object_name="PL-SEAM-REF").exists())
        self.assertFalse(PrefixList.objects.filter(name="PL-SEAM-REF").exists())
        self.assertGreater(self._rp_revision(self.d1), before)

    def test_retention_enumerates_the_reference_rows_once(self):
        """One enumeration decides retention and freezes it, so the two-query interval cannot return."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from netbox_routing.models import RouteMapEntry

        from netbox_nso_plugin.route_policy_reconciler import route_policy_reconcile_plan

        self._seed_referenced_prefix_list("PL-ONCE-REF", "RM-ONCE-REF")
        through = RouteMapEntry.match_prefix_list.through._meta
        enumeration = f'"{through.db_table}"."{through.get_field("prefixlist").attname}" ='

        with CaptureQueriesContext(connection) as captured:
            plan = route_policy_reconcile_plan(self.d1, {"prefix_lists": []})

        self.assertFalse(plan.changes_content)
        # Only the by-pk re-fetch that freezes the declared row may follow it.
        enumerations = [query["sql"] for query in captured if enumeration in query["sql"]]
        self.assertEqual(len(enumerations), 1, enumerations)

    def test_standalone_replay_refuses_a_reference_removed_after_planning(self):
        """The retention branch reads the rows referencing the root, so their loss must refuse."""
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.intent_state import IntentMutationProtocolError
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan

        self._seed_referenced_prefix_list("PL-RACE-REF", "RM-RACE-REF")
        empty = {"prefix_lists": []}
        self.assertFalse(route_policy_reconcile_plan(self.d1, empty).changes_content)

        # The frozen match edge refuses the cached retention branch.
        with self.assertRaisesRegex(IntentMutationProtocolError, "routemapentry_match_prefix_list"):
            self._race_standalone(self.d1, empty, lambda: self._drop_match_edge("PL-RACE-REF"))

        # The retention branch never ran: D1 still reports the group as present.
        d1_row = NSORoutePolicyState.objects.get(management__device=self.d1, object_name="PL-RACE-REF")
        self.assertTrue(d1_row.device_present)
        self.assertTrue(PrefixList.objects.filter(name="PL-RACE-REF").exists())

        before = self._rp_revision(self.d1)
        plan = route_policy_reconcile_plan(self.d1, empty)
        self.assertTrue(plan.changes_content)
        reconcile_route_policy(self.d1, empty)
        self.assertFalse(NSORoutePolicyState.objects.filter(object_name="PL-RACE-REF").exists())
        self.assertFalse(PrefixList.objects.filter(name="PL-RACE-REF").exists())
        self.assertGreater(self._rp_revision(self.d1), before)

    def test_rematerialize_repoints_ownership(self):
        """Operator picks the second device's version → the shared object is refilled from
        it, ownership flips, and the former owner becomes the conflict."""
        from netbox_routing.models import PrefixList, PrefixListEntry

        from netbox_nso_plugin import shared_object_ownership as ownership
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, route_policy_rematerialize_plan

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(self.d1, self._pl("PL-RE", ["10.0.0.0/8"]))
        reconcile_route_policy(self.d2, self._pl("PL-RE", ["10.0.0.0/8", "192.168.0.0/16"]))

        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="PL-RE")
        plan = route_policy_rematerialize_plan(s2)
        s2.refresh_from_db()
        self.assertFalse(s2.is_materialized)
        self.assertTrue(
            {
                "netbox_routing.prefixlistentry",
                "netbox_routing.customprefix",
                "netbox_nso_plugin.nsoroutepolicystate",
            }
            <= {write.model_label for write in plan.write_set}
        )
        ownership.rematerialize(s2)

        pl = PrefixList.objects.get(name="PL-RE")
        self.assertEqual(PrefixListEntry.objects.filter(prefix_list=pl).count(), 2)  # now d2's content
        s1 = NSORoutePolicyState.objects.get(management__device=self.d1, object_name="PL-RE")
        s2.refresh_from_db()
        s1.refresh_from_db()
        self.assertTrue(s2.is_materialized)
        self.assertEqual(s2.status, "imported")
        self.assertFalse(s1.is_materialized)
        self.assertEqual(s1.status, "conflict")

    def test_rematerialize_community_invert_match(self):
        """Re-point works for community-lists incl. the invert_match flag (universal path)."""
        from netbox_routing.models import CommunityList, CommunityListEntry

        from netbox_nso_plugin import shared_object_ownership as ownership
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(
            self.d1,
            {"community_lists": [{"name": "CL-RE", "invert_match": False, "entries": [{"community": "65000:1"}]}]},
        )
        reconcile_route_policy(
            self.d2,
            {
                "community_lists": [
                    {
                        "name": "CL-RE",
                        "invert_match": True,
                        "entries": [{"community": "65000:9"}, {"community": "65000:8"}],
                    }
                ]
            },
        )
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="CL-RE")
        self.assertEqual(s2.status, "conflict")
        ownership.rematerialize(s2)

        cl = CommunityList.objects.get(name="CL-RE")
        self.assertTrue(cl.invert_match)  # d2's flag now materialized
        members = {str(e.community.community) for e in CommunityListEntry.objects.filter(community_list=cl)}
        self.assertEqual(members, {"65000:9", "65000:8"})


class TestRouteMapStructuredMaterialisation(TestCase):
    """route-map entries materialise the structured fields (match_afi,
    set_communities, call_policy, vendor_ext, RouteMap.default_action) from the opaque
    match/set blobs the reader packs — end-to-end through reconcile_route_policy against
    the REAL netbox_routing models + DB. The structured fields are an ADDITIVE projection:
    the full match/set blobs are kept verbatim (authoritative for the write-side round-trip
    until P3), so a push reproduces them byte-for-byte. Anything not yet first-class lands in
    vendor_ext, never dropped.
    """

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="StMfg", slug="stmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="StDev", slug="stdev")
        role = DeviceRole.objects.create(name="StRole", slug="strole")
        site = Site.objects.create(name="StSite", slug="stsite")
        cls.device = Device.objects.create(name="st-router", device_type=dt, role=role, site=site)

    def _mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="st-inst", defaults={"adapter_instance_id": "st-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "st-dev", "adapter_device_id": self.device.pk},
        )[0]

    def _reconcile(self, payload):
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt()
        reconcile_route_policy(self.device, payload)

    def _entry(self):
        from netbox_routing.models import RouteMapEntry

        return RouteMapEntry.objects.get(route_map__name="RM")

    def test_set_community_by_ref_junos_ops(self):
        """Junos `then community add|set|delete <list>` → one by-ref set_communities row each."""
        self._reconcile(
            {
                "community_lists": [
                    {"name": "CL-ADD", "entries": [{"community": "65000:1"}]},
                    {"name": "CL-DEL", "entries": [{"community": "65000:2"}]},
                ],
                "route_maps": [
                    {
                        "name": "RM",
                        "entries": [
                            {
                                "sequence": 10,
                                "action": "permit",
                                "set": '{"community": ["CL-ADD", "CL-DEL"], "_junos_community_op": ["add", "delete"]}',
                            }
                        ],
                    }
                ],
            }
        )
        rme = self._entry()
        rows = {(sc.operation, sc.community_list.name) for sc in rme.set_communities.all()}
        self.assertEqual(rows, {("add", "CL-ADD"), ("delete", "CL-DEL")})
        # The full set blob is kept verbatim so the write-side push round-trips byte-for-byte.
        self.assertEqual(rme.set, {"community": ["CL-ADD", "CL-DEL"], "_junos_community_op": ["add", "delete"]})

    def test_set_community_timos_by_ref_ops(self):
        """Nokia action community add|remove|replace <list> → add | delete | set by-ref."""
        self._reconcile(
            {
                "community_lists": [{"name": f"CL-{x}", "entries": [{"community": "65000:1"}]} for x in "ABC"],
                "route_maps": [
                    {
                        "name": "RM",
                        "entries": [
                            {
                                "sequence": 10,
                                "action": "permit",
                                "set": '{"community_add": ["CL-A"], "community_remove": ["CL-B"], '
                                '"community_replace": ["CL-C"]}',
                            }
                        ],
                    }
                ],
            }
        )
        rows = {(sc.operation, sc.community_list.name) for sc in self._entry().set_communities.all()}
        self.assertEqual(rows, {("add", "CL-A"), ("delete", "CL-B"), ("set", "CL-C")})

    def test_set_community_iosxr_additive(self):
        """IOS-XR `set community <set> additive` → op add by-ref; no additive → set."""
        self._reconcile(
            {
                "community_lists": [{"name": "CS-X", "entries": [{"community": "65000:1"}]}],
                "route_maps": [
                    {
                        "name": "RM",
                        "entries": [
                            {
                                "sequence": 10,
                                "action": "permit",
                                "set": '{"community": "CS-X", "community_additive": true}',
                            }
                        ],
                    }
                ],
            }
        )
        sc = self._entry().set_communities.get()
        self.assertEqual((sc.operation, sc.community_list.name), ("add", "CS-X"))

    def test_set_community_inline_literal(self):
        """A set-community target that is a community LITERAL (not a defined list) becomes an
        inline community, not a dangling by-ref."""
        from netbox_routing.models import Community

        self._reconcile(
            {
                "route_maps": [
                    {
                        "name": "RM",
                        "entries": [
                            {"sequence": 10, "action": "permit", "set": '{"community_add": ["65000:7"]}'},
                        ],
                    }
                ],
            }
        )
        sc = self._entry().set_communities.get()
        self.assertEqual(sc.operation, "add")
        self.assertIsNone(sc.community_list)
        self.assertEqual([c.community for c in sc.communities.all()], ["65000:7"])
        self.assertTrue(Community.objects.filter(community="65000:7").exists())

    def test_unresolved_set_community_preserved_in_vendor_ext(self):
        """A by-ref set-community to a list the device never defined (and not a literal) is
        preserved in vendor_ext.unmapped — never silently dropped, and makes no junk row."""
        self._reconcile(
            {
                "route_maps": [
                    {
                        "name": "RM",
                        "entries": [
                            {"sequence": 10, "action": "permit", "set": '{"community_remove": ["GHOST-LIST"]}'},
                        ],
                    }
                ],
            }
        )
        rme = self._entry()
        self.assertEqual(rme.set_communities.count(), 0)
        self.assertEqual(rme.vendor_ext["unmapped"]["set_community"], [{"operation": "delete", "name": "GHOST-LIST"}])

    def test_match_afi_lifted_and_normalised(self):
        """match-json family (Nokia tokens) + _junos_family (inet6) → normalised match_afi;
        unknown tokens go to vendor_ext.unmapped, not the column."""
        self._reconcile(
            {
                "route_maps": [
                    {
                        "name": "RM",
                        "entries": [
                            {
                                "sequence": 10,
                                "action": "permit",
                                "match": '{"family": ["ipv4", "vpn-ipv4", "mvpn-ipv4"]}',
                            },
                            {"sequence": 20, "action": "permit", "match": '{"_junos_family": "inet6"}'},
                        ],
                    }
                ],
            }
        )
        from netbox_routing.models import RouteMapEntry

        e1 = RouteMapEntry.objects.get(route_map__name="RM", sequence=1)
        e2 = RouteMapEntry.objects.get(route_map__name="RM", sequence=2)
        self.assertEqual(e1.match_afi, ["ipv4", "vpn-ipv4"])
        self.assertEqual(e1.vendor_ext["unmapped"]["family"], ["mvpn-ipv4"])
        self.assertEqual(e2.match_afi, ["ipv6"])
        self.assertIn("family", e1.match)  # full blob kept for the write-side round-trip

    def test_vendor_ext_projection_with_full_blob_kept(self):
        """All _rpl_/_junos_/_timos_ keys are projected (namespaced) into vendor_ext, while
        the full match/set blobs are kept verbatim so the write-side push round-trips."""
        self._reconcile(
            {
                "route_maps": [
                    {
                        "name": "RM",
                        "entries": [
                            {
                                "sequence": 10,
                                "action": "permit",
                                "match": '{"_rpl_done": true, "protocol": ["bgp"]}',
                                "set": '{"_junos_priority": "high", "local_preference": 200}',
                            }
                        ],
                    }
                ],
            }
        )
        rme = self._entry()
        self.assertEqual(rme.vendor_ext, {"xr": {"done": True}, "junos": {"priority": "high"}})
        # Blobs unchanged (full) — the structured fields are additive, not a replacement.
        self.assertEqual(rme.match, {"_rpl_done": True, "protocol": ["bgp"]})
        self.assertEqual(rme.set, {"_junos_priority": "high", "local_preference": 200})

    def test_call_policy_resolved_from_junos_from_policy(self):
        """Junos `from policy <name>` (a match subroutine) → call_policy FK to the RouteMap."""
        from netbox_routing.models import RouteMap

        RouteMap.objects.create(name="SUB-POLICY")
        self._reconcile(
            {
                "route_maps": [
                    {
                        "name": "RM",
                        "entries": [
                            {
                                "sequence": 10,
                                "action": "permit",
                                "match": '{"_junos_from_policy": ["SUB-POLICY"]}',
                            }
                        ],
                    }
                ],
            }
        )
        self.assertEqual(self._entry().call_policy.name, "SUB-POLICY")

    def test_call_policy_can_reference_a_later_greenfield_route_map(self):
        self._reconcile(
            {
                "route_maps": [
                    {
                        "name": "RM",
                        "entries": [
                            {
                                "sequence": 10,
                                "action": "permit",
                                "match": '{"_junos_from_policy": ["SUB-POLICY"]}',
                            }
                        ],
                    },
                    {"name": "SUB-POLICY", "entries": []},
                ]
            }
        )

        self.assertEqual(self._entry().call_policy.name, "SUB-POLICY")

    def test_timos_default_action_projected_to_route_map(self):
        """A Nokia default-action arrives as a synthetic trailing entry flagged
        _timos_default_action → RouteMap.default_action mirrors it. The synthetic entry is
        KEPT (not dropped) so the write-side blob round-trips byte-symmetric; P2 hides it."""
        from netbox_routing.models import RouteMap, RouteMapEntry

        self._reconcile(
            {
                "route_maps": [
                    {
                        "name": "RM",
                        "entries": [
                            {"sequence": 10, "action": "permit", "match": '{"protocol": ["bgp"]}'},
                            {
                                "sequence": 999999,
                                "action": "deny",
                                "match": '{"_timos_default_action": true}',
                                "set": "{}",
                            },
                        ],
                    }
                ],
            }
        )
        self.assertEqual(RouteMap.objects.get(name="RM").default_action, "deny")
        # Both device entries are materialised (positional) — nothing dropped.
        seqs = list(RouteMapEntry.objects.filter(route_map__name="RM").values_list("sequence", flat=True))
        self.assertEqual(seqs, [1, 2])
        shell = RouteMapEntry.objects.get(route_map__name="RM", sequence=2)
        self.assertEqual(shell.vendor_ext, {"timos": {"default_action": True}})

    def test_timos_default_action_with_set_keeps_full_blob(self):
        """A default-action that also carries set knobs: default_action mirrors the action AND
        the entry keeps its full set blob — no set knob is lost on the projection."""
        from netbox_routing.models import RouteMap, RouteMapEntry

        self._reconcile(
            {
                "route_maps": [
                    {
                        "name": "RM",
                        "entries": [
                            {
                                "sequence": 999999,
                                "action": "permit",
                                "match": '{"_timos_default_action": true}',
                                "set": '{"local_preference": 50}',
                            },
                        ],
                    }
                ],
            }
        )
        self.assertEqual(RouteMap.objects.get(name="RM").default_action, "permit")
        rme = RouteMapEntry.objects.get(route_map__name="RM")
        self.assertEqual(rme.set, {"local_preference": 50})
        self.assertEqual(rme.vendor_ext, {"timos": {"default_action": True}})


class TestRoutePolicyVersionsStructuredUI(TestCase):
    """the 'Versions' surface renders each device's STRUCTURED route-map (via
    summarize_route_map) so operators compare versions without reading raw JSON — end-to-end
    through the real view + template against real models."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        mfg = Manufacturer.objects.create(name="UiMfg", slug="uimfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="UiDev", slug="uidev")
        role = DeviceRole.objects.create(name="UiRole", slug="uirole")
        site = Site.objects.create(name="UiSite", slug="uisite")
        cls.device = Device.objects.create(name="ui-router", device_type=dt, role=role, site=site)
        cls.user = get_user_model().objects.create_superuser(
            username="rpuiadmin", password="pw", email="rpui@test.example"
        )

    def _mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="ui-inst", defaults={"adapter_instance_id": "ui-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "ui-dev", "adapter_device_id": self.device.pk},
        )[0]

    def test_versions_page_renders_structured_route_map(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt()
        reconcile_route_policy(
            self.device,
            {
                "community_lists": [{"name": "CL-X", "entries": [{"community": "65000:1"}]}],
                "route_maps": [
                    {
                        "name": "RM-UI",
                        "entries": [
                            {
                                "sequence": 10,
                                "action": "permit",
                                "match_prefix_lists": [],
                                "match_community_lists": [],
                                "match_as_paths": [],
                                "match": '{"family": ["ipv4"]}',
                                "set": '{"community_add": ["CL-X"], "local_preference": 200}',
                            },
                            {
                                "sequence": 999999,
                                "action": "deny",
                                "match": '{"_timos_default_action": true}',
                                "set": "{}",
                            },
                        ],
                    }
                ],
            },
        )
        state = NSORoutePolicyState.objects.get(family="route_map", object_name="RM-UI")
        self.client.force_login(self.user)
        url = reverse("plugins:netbox_nso_plugin:routing_route_policy_versions", kwargs={"pk": state.pk})
        resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("community add CL-X", html)  # set-community op rendered structurally
        self.assertIn("ipv4", html)  # match AFI chip
        self.assertIn("Default action", html)  # folded default-action surfaced, not an entry
        self.assertIn("local_preference=200", html)  # residual set knob

    def test_versions_page_non_route_map_has_no_detail(self):
        # A community-list version page must not error and carries no route-map detail toggle.
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt()
        reconcile_route_policy(
            self.device,
            {"community_lists": [{"name": "CL-Y", "entries": [{"community": "65000:2"}]}]},
        )
        state = NSORoutePolicyState.objects.get(family="community_list", object_name="CL-Y")
        self.client.force_login(self.user)
        url = reverse("plugins:netbox_nso_plugin:routing_route_policy_versions", kwargs={"pk": state.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("rpv", resp.content.decode())  # no route-map collapse target
