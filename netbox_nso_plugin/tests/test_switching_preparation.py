# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Switching deletion identity from exact writer events to prepared snapshots."""

from __future__ import annotations

import copy
from unittest.mock import patch
from uuid import UUID

import requests
from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.urls import reverse

from ._outbox_case import ReceiptAdapter, make_managed, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestSwitchingRootPreparation(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        self.device, self.management = make_managed("switching-roots", 4218)

    def _bundle(self, name):
        from netbox_nso_plugin.models import NSOLACPBundleState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        with without_commit_drain():
            interface = Interface.objects.create(device=self.device, name=name, type="lag")
            state = NSOLACPBundleState(
                management=self.management,
                interface=interface,
                lag_id=int("".join(character for character in name if character.isdigit())),
                status="accepted",
            )
            plan = RendererMutationPlan.build(
                saves=(planned_save(state, force_insert=True, natural_key=("management", "interface")),)
            )
            with renderer_writes(plan) as writer:
                writer.save(state, force_insert=True)
        return state

    def _switchport(self, name):
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        with without_commit_drain():
            interface = Interface.objects.create(device=self.device, name=name, type="1000base-t", mode="tagged-all")
            state = NSOSwitchportState(
                management=self.management, interface=interface, mode="tagged-all", status="accepted"
            )
            plan = RendererMutationPlan.build(
                saves=(planned_save(state, force_insert=True, natural_key=("management", "interface")),)
            )
            with renderer_writes(plan) as writer:
                writer.save(state, force_insert=True)
        return state

    def _apply(self):
        from .test_apply_selector import _ApplyContractAdapter, _promoted

        user = get_user_model().objects.filter(username="switching-apply-admin").first()
        if user is None:
            user = get_user_model().objects.create_superuser(
                username="switching-apply-admin", password="test-password-1685", email="switching@test.example"
            )
        self.client.force_login(user)
        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[self.management.pk, "apply"])
        config, session = adapter.patches()
        with config, session:
            response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        assert response.status_code == 200, response.content
        assert response.json()["status"] == "ok", response.content
        return adapter

    def _delete_native_root(self, create, name):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        row = create(name)
        with renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(row.interface),))) as writer:
            writer.delete(row.interface)
        return row

    def test_failed_http_preparations_keep_rows_and_entries_for_retry(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion

        from ._outbox_case import enqueue, entries

        failures = (
            ("exception", requests.exceptions.ConnectionError("preparation lost")),
            ("envelope", {"status": "error", "message": "preparation refused"}),
            ("client", (400, {"error": {"code": "bad_request", "message": "refused"}})),
            ("server", (503, {"error": {"code": "unavailable", "message": "retry"}})),
        )
        for scope, create, prefix in (
            ("lacp", self._bundle, "Port-channel"),
            ("switchport", self._switchport, "Ethernet"),
        ):
            for index, (failure, outcome) in enumerate(failures, start=70):
                with self.subTest(scope=scope, failure=failure):
                    name = f"{prefix}{index}"
                    self._delete_native_root(create, name)
                    enqueue(self.device, scope)
                    adapter = ReceiptAdapter()
                    if isinstance(outcome, Exception):
                        adapter.fail_with = outcome
                    else:
                        adapter._respond = lambda _body, result=outcome: result
                    config, session = adapter.patches()
                    with config, session:
                        assert drain.drain_key(self.device.pk, scope) == drain.FAILED
                    assert entries(self.device, scope, unconsumed=True)
                    assert NSOSwitchingRootDeletion.objects.filter(
                        management=self.management, scope=scope, root_name=name
                    ).exists()
                    adapter.fail_with = None
                    adapter._respond = ReceiptAdapter._default_response
                    config, session = adapter.patches()
                    with config, session:
                        assert drain.drain_key(self.device.pk, scope) == drain.SUCCEEDED
                    assert name in adapter.requests[-1]["body"]["deleted_roots"]
                    assert entries(self.device, scope, unconsumed=True) == []

    def test_invalid_http_acknowledgements_cannot_discharge_deletion(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion

        from ._outbox_case import enqueue, entries

        cases = (
            ("stored", lambda body, stream: {"status": "stored"}),
            (
                "device",
                lambda body, stream: {
                    "status": "prepared",
                    "device_id": 9999,
                    "stream": stream,
                    "selection_revision": 1,
                    "unauthorized_deleted_roots": body["deleted_roots"],
                },
            ),
            (
                "stream",
                lambda body, stream: {
                    "status": "prepared",
                    "stream": "wrong",
                    "selection_revision": 1,
                    "unauthorized_deleted_roots": body["deleted_roots"],
                },
            ),
            (
                "revision",
                lambda body, stream: {
                    "status": "prepared",
                    "stream": stream,
                    "selection_revision": 0,
                    "unauthorized_deleted_roots": body["deleted_roots"],
                },
            ),
            (
                "unknown_root",
                lambda body, stream: {
                    "status": "prepared",
                    "stream": stream,
                    "selection_revision": 1,
                    "unauthorized_deleted_roots": ["not-in-request"],
                },
            ),
        )
        for scope, create, prefix, stream in (
            ("lacp", self._bundle, "Port-channel", "lag"),
            ("switchport", self._switchport, "Ethernet", "switchport"),
        ):
            for index, (reason, response) in enumerate(cases, start=80):
                with self.subTest(scope=scope, reason=reason):
                    name = f"{prefix}{index}"
                    self._delete_native_root(create, name)
                    enqueue(self.device, scope)
                    adapter = ReceiptAdapter(respond=lambda body, callback=response: callback(body, stream))
                    config, session = adapter.patches()
                    with config, session:
                        assert drain.drain_key(self.device.pk, scope) == drain.FAILED
                    assert entries(self.device, scope, unconsumed=True)
                    assert NSOSwitchingRootDeletion.objects.filter(
                        management=self.management, scope=scope, root_name=name
                    ).exists()

    def test_revision_conflict_recaptures_and_then_acknowledges(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion

        from ._outbox_case import enqueue, entries

        row = self._delete_native_root(self._bundle, "Port-channel91")
        enqueue(self.device, "lacp")
        calls = 0

        def respond(body):
            nonlocal calls
            calls += 1
            if calls == 1:
                return 409, {
                    "error": {"code": "conflict", "message": "recheck", "detail": {"reason": "revision_conflict"}}
                }
            return ReceiptAdapter._default_response(body)

        adapter = ReceiptAdapter(respond=respond)
        config, session = adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "lacp") == drain.SUCCEEDED
        assert calls == 2
        assert all(row.interface.name in request["body"]["deleted_roots"] for request in adapter.requests)
        assert entries(self.device, "lacp", unconsumed=True) == []
        assert NSOSwitchingRootDeletion.objects.filter(
            management=self.management, root_name=row.interface.name
        ).exists()

    def test_second_capture_mismatch_reports_concurrent_change_after_repair_exits(self):
        from django.db import connection

        from netbox_nso_plugin import drain, renderer_audit
        from netbox_nso_plugin.models import NSOIntentRevision, NSOSwitchingRootDeletion

        from ._outbox_case import enqueue, entries

        self._delete_native_root(self._bundle, "Port-channel92")
        enqueue(self.device, "lacp")
        real_audit = renderer_audit.audit_renderer_scopes
        real_repair = renderer_audit.repair_scope
        repair_calls = []

        def audit_then_change(*args, **kwargs):
            result = real_audit(*args, **kwargs)
            if kwargs.get("trigger") == "drain._drain_once":
                NSOIntentRevision.objects.filter(device=self.device, scope="lacp").update(verified_fingerprint="stale")
            return result

        def repair_after_transaction(*args, **kwargs):
            assert not connection.in_atomic_block
            repair_calls.append(True)
            result = real_repair(*args, **kwargs)
            NSOIntentRevision.objects.filter(device=self.device, scope="lacp").update(verified_fingerprint="stale")
            return result

        adapter = ReceiptAdapter()
        config, session = adapter.patches()
        with (
            config,
            session,
            patch.object(renderer_audit, "audit_renderer_scopes", side_effect=audit_then_change),
            patch.object(renderer_audit, "repair_scope", side_effect=repair_after_transaction),
        ):
            assert drain.drain_key(self.device.pk, "lacp") == drain.FAILED
        assert repair_calls == [True]
        assert adapter.requests == []
        assert entries(self.device, "lacp", unconsumed=True)
        assert NSOSwitchingRootDeletion.objects.filter(management=self.management, root_name="Port-channel92").exists()
        self.management.refresh_from_db()
        assert self.management.intent_push_errors["lacp"]["code"] == "concurrent_change"

    def test_capture_locks_management_before_revision(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSODeviceManagement, NSOIntentRevision

        from ._outbox_case import enqueue

        self._delete_native_root(self._bundle, "Port-channel93")
        enqueue(self.device, "lacp")
        config, session = self.adapter.patches()
        with config, session, CaptureQueriesContext(connection) as queries:
            assert drain.drain_key(self.device.pk, "lacp") == drain.SUCCEEDED
        statements = [query["sql"].lower() for query in queries]
        management_table = NSODeviceManagement._meta.db_table.lower()
        revision_table = NSOIntentRevision._meta.db_table.lower()
        management_locks = [
            index for index, sql in enumerate(statements) if management_table in sql and "for update" in sql
        ]
        revision_locks = [
            index for index, sql in enumerate(statements) if revision_table in sql and "for update" in sql
        ]
        assert management_locks and revision_locks
        assert min(management_locks) < min(revision_locks)

    def test_every_public_sender_carries_outstanding_deletion_identity(self):
        from netbox_nso_plugin import drain, renderer_audit
        from netbox_nso_plugin.models import NSOIntentRevision, NSOSwitchingRootDeletion
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        from ._outbox_case import enqueue

        scenarios = ("apply", "audit", "retract", "rename", "tick")
        for scope, create, prefix, suffix in (
            ("lacp", self._bundle, "Port-channel", "/lag-config/apply"),
            ("switchport", self._switchport, "Ethernet", "/switchport/apply"),
        ):
            for index, trigger in enumerate(scenarios, start=101):
                with self.subTest(scope=scope, trigger=trigger):
                    name = f"{prefix}{index}"
                    self._delete_native_root(create, name)
                    enqueue(self.device, scope)
                    assert NSOSwitchingRootDeletion.objects.filter(
                        management=self.management, scope=scope, root_name=name
                    ).exists()
                    if trigger == "apply":
                        adapter = self._apply()
                    else:
                        adapter = ReceiptAdapter()
                        config, session = adapter.patches()
                        with config, session:
                            if trigger == "audit":
                                NSOIntentRevision.objects.filter(device=self.device, scope=scope).update(
                                    verified_fingerprint="stale"
                                )
                                renderer_audit.audit_renderer_scopes(self.device.pk, (scope,), trigger="test")
                            elif trigger == "retract":
                                retained = create(f"{prefix}{index + 100}")
                                if scope == "lacp":
                                    with without_commit_drain():
                                        member = Interface.objects.create(
                                            device=self.device,
                                            name=f"Ethernet{index + 300}",
                                            type="1000base-t",
                                            lag=retained.interface,
                                        )
                                    from netbox_nso_plugin.models import NSOLACPMemberState

                                    member_state = NSOLACPMemberState(
                                        management=self.management,
                                        interface=member,
                                        lag_bundle=retained.interface,
                                        status="accepted",
                                    )
                                    with renderer_writes(
                                        RendererMutationPlan.build(
                                            saves=(
                                                planned_save(
                                                    member_state,
                                                    force_insert=True,
                                                    natural_key=("management", "interface"),
                                                ),
                                            )
                                        )
                                    ) as writer:
                                        writer.save(member_state, force_insert=True)
                                reconcile_scope_ownership(self.device.pk, (scope,))
                                if scope == "lacp":
                                    Interface.objects.filter(pk=retained.interface_id).update(type="1000base-t")
                                else:
                                    Interface.objects.filter(pk=retained.interface_id).update(mode="")
                                reconcile_scope_ownership(self.device.pk, (scope,))
                            elif trigger == "rename":
                                retained = create(f"{prefix}{index + 100}")
                                self.management.auto_apply = True
                                self.management.save(update_fields=["auto_apply"])
                                changed = copy.copy(retained.interface)
                                changed.name = f"{prefix}{index + 200}"
                                with renderer_writes(
                                    RendererMutationPlan.build(saves=(planned_save(changed, update_fields=("name",)),))
                                ) as writer:
                                    writer.save(changed, update_fields=("name",))
                            elif trigger == "tick":
                                drain.drain_intent_outbox()
                            if trigger in {"audit", "retract"} and not adapter.requests:
                                drain.drain_key(self.device.pk, scope)
                    bodies = [request["body"] for request in adapter.requests if request["url"].endswith(suffix)]
                    assert bodies and all(name in body["deleted_roots"] for body in bodies)

    def test_switching_apply_promotes_bundle_member_and_switchport_and_public_evidence_settles_them(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts
        from netbox_nso_plugin.models import NSOLACPMemberState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        from .test_apply_settlement import _attempt, _payload

        bundle = self._bundle("Port-channel31")
        switchport = self._switchport("Ethernet31")
        with without_commit_drain():
            member_interface = Interface.objects.create(
                device=self.device, name="Ethernet32", type="1000base-t", lag=bundle.interface
            )
        member = NSOLACPMemberState(
            management=self.management,
            interface=member_interface,
            lag_bundle=bundle.interface,
            status="accepted",
            mode="",
        )
        with (
            without_commit_drain(),
            renderer_writes(
                RendererMutationPlan.build(
                    saves=(planned_save(member, force_insert=True, natural_key=("management", "interface")),)
                )
            ) as writer,
        ):
            writer.save(member, force_insert=True)

        adapter = self._apply()
        attempt_id = UUID(adapter.apply_requests[0]["apply_attempt_id"])
        for row in (bundle, member, switchport):
            row.refresh_from_db()
            assert row.status == "deploying"
            assert row.apply_attempt_id == attempt_id

        selected = adapter.apply_requests[0]["selected"]
        generation = _attempt(
            attempt_id,
            self.management.adapter_device_id,
            81,
            selected,
            "settled",
            result={
                "lag_count_by_outcome": {"in_sync": 2, "apply_failed": 0},
                "switchport_count_by_outcome": {"in_sync": 1, "apply_failed": 0},
            },
        )
        # The stored Apply response has two generations. Evidence must name both.
        second = copy.deepcopy(generation["generations"][0])
        second["generation_id"] = 82
        second["seq"] = 82
        generation["generations"].append(second)
        generation["response"] = adapter.apply_response(selected)[1]
        settle_apply_attempts(
            self.management, _payload(self.management.adapter_device_id, [generation]), static_route_feed_drained=True
        )
        for row in (bundle, member, switchport):
            row.refresh_from_db()
            assert row.status == "in_sync"

    def test_failed_switching_generation_settles_both_streams_as_apply_failed(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts

        from .test_apply_settlement import _attempt, _payload

        bundle = self._bundle("Port-channel33")
        switchport = self._switchport("Ethernet33")
        adapter = self._apply()
        attempt_id = UUID(adapter.apply_requests[0]["apply_attempt_id"])
        selected = adapter.apply_requests[0]["selected"]
        evidence = _attempt(attempt_id, self.management.adapter_device_id, 81, selected, "failed")
        second = copy.deepcopy(evidence["generations"][0])
        second["generation_id"] = 82
        second["seq"] = 82
        evidence["generations"].append(second)
        evidence["response"] = adapter.apply_response(selected)[1]
        settle_apply_attempts(
            self.management, _payload(self.management.adapter_device_id, [evidence]), static_route_feed_drained=True
        )
        for row in (bundle, switchport):
            row.refresh_from_db()
            assert row.status == "apply_failed"
            assert row.apply_attempt_id == attempt_id

    def test_read_reconcile_keeps_switching_attempt_pending_until_failed_evidence(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts
        from netbox_nso_plugin.lacp_reconciler import lacp_reconcile_plan, reconcile_lag_config
        from netbox_nso_plugin.models import NSOLACPMemberState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, switchport_reconcile_plan

        from .test_apply_settlement import _attempt, _payload

        bundle = self._bundle("Port-channel34")
        switchport = self._switchport("Ethernet34")
        with without_commit_drain():
            interface = Interface.objects.create(
                device=self.device, name="Ethernet35", type="1000base-t", lag=bundle.interface
            )
        member = NSOLACPMemberState(
            management=self.management, interface=interface, lag_bundle=bundle.interface, status="accepted"
        )
        with renderer_writes(
            RendererMutationPlan.build(
                saves=(planned_save(member, force_insert=True, natural_key=("management", "interface")),)
            )
        ) as writer:
            writer.save(member, force_insert=True)

        adapter = self._apply()
        attempt_id = UUID(adapter.apply_requests[0]["apply_attempt_id"])
        selected = adapter.apply_requests[0]["selected"]
        lag_payload = {
            "bundles": [
                {
                    "name": bundle.interface.name,
                    "lag_id": bundle.lag_id,
                    "members": [{"interface_name": interface.name}],
                }
            ]
        }
        switchport_payload = {
            "interfaces": [
                {
                    "interface_name": switchport.interface.name,
                    "mode": "trunk",
                    "untagged_vlan": None,
                    "tagged_vlans": [],
                }
            ]
        }
        assert lacp_reconcile_plan(self.device, lag_payload).settles_deploying is False
        assert switchport_reconcile_plan(self.device, switchport_payload).settles_deploying is False

        def read_and_assert_pending():
            reconcile_lag_config(self.device, lag_payload)
            reconcile_switchport(self.device, switchport_payload)
            for row in (bundle, member, switchport):
                row.refresh_from_db()
                assert row.status == "deploying"
                assert row.apply_attempt_id == attempt_id

        def evidence(status):
            item = _attempt(attempt_id, self.management.adapter_device_id, 81, selected, status)
            second = copy.deepcopy(item["generations"][0])
            second["generation_id"] = 82
            second["seq"] = 82
            item["generations"].append(second)
            item["response"] = adapter.apply_response(selected)[1]
            return item

        read_and_assert_pending()
        pending = evidence("pending")
        settle_apply_attempts(
            self.management,
            _payload(self.management.adapter_device_id, [pending], head=pending["generations"][0]),
            static_route_feed_drained=True,
        )
        read_and_assert_pending()
        failed = evidence("failed")
        settle_apply_attempts(
            self.management,
            _payload(self.management.adapter_device_id, [failed], head=failed["generations"][0]),
            static_route_feed_drained=True,
        )
        for row in (bundle, member, switchport):
            row.refresh_from_db()
            assert row.status == "apply_failed"
            assert row.apply_attempt_id == attempt_id

    def test_apply_does_not_promote_member_under_unowned_bundle(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts
        from netbox_nso_plugin.models import NSOLACPMemberState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        from .test_apply_settlement import _attempt, _payload

        selected_bundle = self._bundle("Port-channel36")
        excluded_bundle = self._bundle("Port-channel37")
        with without_commit_drain():
            interface = Interface.objects.create(
                device=self.device, name="Ethernet37", type="1000base-t", lag=excluded_bundle.interface
            )
        member = NSOLACPMemberState(
            management=self.management, interface=interface, lag_bundle=excluded_bundle.interface, status="accepted"
        )
        with renderer_writes(
            RendererMutationPlan.build(
                saves=(planned_save(member, force_insert=True, natural_key=("management", "interface")),)
            )
        ) as writer:
            writer.save(member, force_insert=True)
        unowned = copy.copy(excluded_bundle)
        unowned.status = "changed"
        with renderer_writes(
            RendererMutationPlan.build(saves=(planned_save(unowned, update_fields=("status",)),))
        ) as writer:
            writer.save(unowned, update_fields=("status",))

        adapter = self._apply()
        selected = adapter.apply_requests[0]["selected"]
        attempt_id = UUID(adapter.apply_requests[0]["apply_attempt_id"])
        lag_body = next(request["body"] for request in adapter.requests if request["url"].endswith("/lag-config/apply"))
        assert [bundle["name"] for bundle in lag_body["bundles"]] == [selected_bundle.interface.name]
        selected_bundle.refresh_from_db()
        member.refresh_from_db()
        assert selected_bundle.status == "deploying"
        assert member.status == "accepted"
        assert member.apply_attempt_id is None

        settled = _attempt(
            attempt_id,
            self.management.adapter_device_id,
            81,
            selected,
            "settled",
            result={"lag_count_by_outcome": {"in_sync": 1, "apply_failed": 0}},
        )
        settled["response"] = adapter.apply_response(selected)[1]
        second = copy.deepcopy(settled["generations"][0])
        second["generation_id"] = 82
        second["seq"] = 82
        settled["generations"].append(second)
        settle_apply_attempts(
            self.management, _payload(self.management.adapter_device_id, [settled]), static_route_feed_drained=True
        )
        selected_bundle.refresh_from_db()
        member.refresh_from_db()
        assert selected_bundle.status == "in_sync"
        assert member.status == "accepted"
        assert member.apply_attempt_id is None

    def test_unowned_switching_deletions_do_not_gain_authority_from_an_owned_sibling(self):
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_writes,
        )

        for scope, create, first, second in (
            ("lacp", self._bundle, "Port-channel41", "Port-channel42"),
            ("switchport", self._switchport, "Ethernet41", "Ethernet42"),
        ):
            unowned = create(first)
            owned = create(second)
            detached = copy.copy(unowned)
            detached.status = "changed"
            with renderer_writes(
                RendererMutationPlan.build(saves=(planned_save(detached, update_fields=("status",)),))
            ) as writer:
                writer.save(detached, update_fields=("status",))
            edited = copy.copy(owned)
            if scope == "lacp":
                edited.min_links = 2
                fields = ("min_links",)
            else:
                edited.mode = "access"
                fields = ("mode",)
            with renderer_writes(
                RendererMutationPlan.build(
                    deletes=(planned_delete(unowned),), saves=(planned_save(edited, update_fields=fields),)
                )
            ) as writer:
                writer.delete(unowned)
                writer.save(edited, update_fields=fields)
            assert not NSOSwitchingRootDeletion.objects.filter(
                management=self.management, scope=scope, root_name=first
            ).exists()

    def test_unowned_native_cascade_does_not_record_switching_root(self):
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_mirror_writes,
            renderer_writes,
        )

        for scope, create, name in (
            ("lacp", self._bundle, "Port-channel43"),
            ("switchport", self._switchport, "Ethernet43"),
        ):
            row = create(name)
            detached = copy.copy(row)
            detached.status = "changed"
            with renderer_writes(
                RendererMutationPlan.build(saves=(planned_save(detached, update_fields=("status",)),))
            ) as writer:
                writer.save(detached, update_fields=("status",))
            with renderer_mirror_writes(RendererMutationPlan.build(deletes=(planned_delete(row.interface),))) as writer:
                writer.delete(row.interface)
            assert not NSOSwitchingRootDeletion.objects.filter(
                management=self.management, scope=scope, root_name=name
            ).exists()

    def test_rename_restoration_cancels_deleted_identity_at_capture(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_writes,
        )

        for scope, create, old_name, new_name in (
            ("lacp", self._bundle, "Port-channel51", "Port-channel52"),
            ("switchport", self._switchport, "Ethernet51", "Ethernet52"),
        ):
            deleted = create(old_name)
            replacement = create(new_name)
            with renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(deleted.interface),))) as writer:
                writer.delete(deleted.interface)
            for name in (old_name, f"{new_name}-final"):
                current = Interface.objects.get(pk=replacement.interface_id)
                changed = copy.copy(current)
                changed.name = name
                with renderer_writes(
                    RendererMutationPlan.build(saves=(planned_save(changed, update_fields=("name",)),))
                ) as writer:
                    writer.save(changed, update_fields=("name",))
            config, session = self.adapter.patches()
            with config, session:
                assert drain.push_now(self.device.pk, scope, force=True) is not None
            assert old_name not in self.adapter.requests[-1]["body"]["deleted_roots"]
            assert not NSOSwitchingRootDeletion.objects.filter(
                management=self.management, scope=scope, root_name=old_name
            ).exists()

    def test_switchport_recreation_and_reown_cancel_old_deletion_identity(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion, NSOSwitchportState
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_mirror_writes,
            renderer_writes,
        )

        deleted = self._switchport("Ethernet61")
        with renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(deleted),))) as writer:
            writer.delete(deleted)
        assert NSOSwitchingRootDeletion.objects.filter(management=self.management, root_name="Ethernet61").exists()
        unowned = NSOSwitchportState(
            management=self.management, interface=deleted.interface, mode="tagged-all", status="changed"
        )
        with renderer_mirror_writes(
            RendererMutationPlan.build(
                saves=(planned_save(unowned, force_insert=True, natural_key=("management", "interface")),)
            )
        ) as writer:
            writer.save(unowned, force_insert=True)
        restored = copy.copy(unowned)
        restored.status = "accepted"
        with renderer_writes(
            RendererMutationPlan.build(saves=(planned_save(restored, update_fields=("status",)),))
        ) as writer:
            writer.save(restored, update_fields=("status",))
        config, session = self.adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "switchport", force=True) is not None
        assert self.adapter.requests[-1]["body"]["deleted_roots"] == []
        assert not NSOSwitchingRootDeletion.objects.filter(management=self.management, root_name="Ethernet61").exists()

    def test_member_deletion_is_absent_from_the_http_bundle_without_deleting_its_root(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOLACPMemberState, NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_writes,
        )

        bundle = self._bundle("Port-channel62")
        with without_commit_drain():
            interface = Interface.objects.create(
                device=self.device, name="Ethernet62", type="1000base-t", lag=bundle.interface
            )
        member = NSOLACPMemberState(
            management=self.management,
            interface=interface,
            lag_bundle=bundle.interface,
            mode="active",
            status="accepted",
        )
        with renderer_writes(
            RendererMutationPlan.build(
                saves=(planned_save(member, force_insert=True, natural_key=("management", "interface")),)
            )
        ) as writer:
            writer.save(member, force_insert=True)
        with renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(member),))) as writer:
            writer.delete(member)
        config, session = self.adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "lacp", force=True) is not None
        body = self.adapter.requests[-1]["body"]
        assert [item["name"] for item in body["bundles"]] == ["Port-channel62"]
        assert body["bundles"][0]["members"] == []
        assert body["deleted_roots"] == []
        assert not NSOSwitchingRootDeletion.objects.filter(management=self.management, scope="lacp").exists()

    def test_apply_authorization_then_later_preparation_discharges_obsolete_deletion(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion

        for scope, create, name in (
            ("lacp", self._bundle, "Port-channel63"),
            ("switchport", self._switchport, "Ethernet63"),
        ):
            self._delete_native_root(create, name)
        adapter = self._apply()
        for scope, path, name in (
            ("lacp", "/lag-config/apply", "Port-channel63"),
            ("switchport", "/switchport/apply", "Ethernet63"),
        ):
            body = next(request["body"] for request in adapter.requests if request["url"].endswith(path))
            assert name in body["deleted_roots"]
            response_adapter = ReceiptAdapter(
                respond=lambda body: {
                    **ReceiptAdapter._default_response(body),
                    "unauthorized_deleted_roots": list(body["deleted_roots"]),
                }
            )
            config, session = response_adapter.patches()
            with config, session:
                assert drain.push_now(self.device.pk, scope, force=True) is not None
            assert name in response_adapter.requests[-1]["body"]["deleted_roots"]
            assert not NSOSwitchingRootDeletion.objects.filter(
                management=self.management, scope=scope, root_name=name
            ).exists()

    def test_never_authorized_switchport_deletion_is_discarded_after_http_acknowledgement(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion

        self._delete_native_root(self._switchport, "Ethernet64")
        adapter = ReceiptAdapter(
            respond=lambda body: {
                **ReceiptAdapter._default_response(body),
                "unauthorized_deleted_roots": list(body["deleted_roots"]),
            }
        )
        config, session = adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "switchport", force=True) is not None
        assert adapter.requests[-1]["body"]["deleted_roots"] == ["Ethernet64"]
        assert not NSOSwitchingRootDeletion.objects.filter(
            management=self.management, scope="switchport", root_name="Ethernet64"
        ).exists()

    def test_manual_bundle_delete_and_unown_keep_only_deleted_root(self):
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_writes,
        )

        deleted = self._bundle("Port-channel1")
        detached = self._bundle("Port-channel2")
        unowned = copy.copy(detached)
        unowned.status = "changed"
        plan = RendererMutationPlan.build(
            deletes=(planned_delete(deleted),),
            saves=(planned_save(unowned, update_fields=("status",)),),
        )
        with renderer_writes(plan) as writer:
            writer.delete(deleted)
            writer.save(unowned, update_fields=("status",))

        assert list(
            NSOSwitchingRootDeletion.objects.filter(management=self.management, scope="lacp").values_list(
                "root_name", flat=True
            )
        ) == ["Port-channel1"]
        adapter = self._apply()
        body = next(request["body"] for request in adapter.requests if request["url"].endswith("/lag-config/apply"))
        assert body["bundles"] == []
        assert body["deleted_roots"] == ["Port-channel1"]
        assert NSOSwitchingRootDeletion.objects.filter(management=self.management, root_name="Port-channel1").exists()

    def test_recreating_a_root_withdraws_its_deletion(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentRevision, NSOLACPBundleState, NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_writes,
        )

        deleted = self._bundle("Port-channel3")
        before = NSOIntentRevision.objects.get(device=self.device, scope="lacp").revision
        with renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(deleted),))) as writer:
            writer.delete(deleted)
        deleted_revision = NSOIntentRevision.objects.get(device=self.device, scope="lacp").revision
        assert deleted_revision > before
        assert NSOSwitchingRootDeletion.objects.filter(management=self.management, root_name="Port-channel3").exists()

        restored = NSOLACPBundleState(
            management=self.management,
            interface=deleted.interface,
            lag_id=deleted.lag_id,
            status="accepted",
        )
        plan = RendererMutationPlan.build(
            saves=(planned_save(restored, force_insert=True, natural_key=("management", "interface")),)
        )
        with renderer_writes(plan) as writer:
            writer.save(restored, force_insert=True)
        assert NSOIntentRevision.objects.get(device=self.device, scope="lacp").revision > deleted_revision
        assert not NSOSwitchingRootDeletion.objects.filter(
            management=self.management, root_name="Port-channel3"
        ).exists()
        config, session = self.adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "lacp", force=True) is not None
        assert self.adapter.requests[-1]["body"]["deleted_roots"] == []

    def test_manual_switchport_delete_and_unown_keep_only_deleted_root(self):
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_writes,
        )

        deleted = self._switchport("Ethernet1")
        detached = self._switchport("Ethernet2")
        unowned = copy.copy(detached)
        unowned.status = "changed"
        plan = RendererMutationPlan.build(
            deletes=(planned_delete(deleted),),
            saves=(planned_save(unowned, update_fields=("status",)),),
        )
        with renderer_writes(plan) as writer:
            writer.delete(deleted)
            writer.save(unowned, update_fields=("status",))
        adapter = self._apply()
        body = next(request["body"] for request in adapter.requests if request["url"].endswith("/switchport/apply"))
        assert body["interfaces"] == []
        assert body["deleted_roots"] == ["Ethernet1"]
        assert NSOSwitchingRootDeletion.objects.filter(management=self.management, root_name="Ethernet1").exists()

    def test_planned_native_cascade_records_root_from_locked_preimage(self):
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        bundle = self._bundle("Port-channel12")
        native = bundle.interface
        plan = RendererMutationPlan.build(deletes=(planned_delete(native),))
        with renderer_writes(plan) as writer:
            writer.delete(native)

        assert NSOSwitchingRootDeletion.objects.filter(
            management=self.management, scope="lacp", root_name="Port-channel12"
        ).exists()

    def test_planned_switchport_native_cascade_records_root(self):
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        state = self._switchport("Ethernet12")
        native = state.interface
        with renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(native),))) as writer:
            writer.delete(native)
        assert NSOSwitchingRootDeletion.objects.filter(
            management=self.management, scope="switchport", root_name="Ethernet12"
        ).exists()

    def test_auto_preparations_repeat_deletion_until_apply_authorizes_it(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_writes,
        )

        deleted = self._bundle("Port-channel4")
        detached = self._bundle("Port-channel5")
        retained = self._bundle("Port-channel6")
        self.management.auto_apply = True
        self.management.save(update_fields=["auto_apply"])
        unowned = copy.copy(detached)
        unowned.status = "changed"
        plan = RendererMutationPlan.build(
            deletes=(planned_delete(deleted),),
            saves=(planned_save(unowned, update_fields=("status",)),),
        )
        config, session = self.adapter.patches()
        with config, session, renderer_writes(plan) as writer:
            writer.delete(deleted)
            writer.save(unowned, update_fields=("status",))
        assert self.adapter.requests[-1]["body"]["deleted_roots"] == ["Port-channel4"]

        changed = copy.copy(retained)
        changed.min_links = 2
        plan = RendererMutationPlan.build(saves=(planned_save(changed, update_fields=("min_links",)),))
        config, session = self.adapter.patches()
        with config, session, renderer_writes(plan) as writer:
            writer.save(changed, update_fields=("min_links",))
        assert self.adapter.requests[-1]["body"]["deleted_roots"] == ["Port-channel4"]
        config, session = self.adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "lacp", force=True) is not None
        assert self.adapter.requests[-1]["body"]["deleted_roots"] == ["Port-channel4"]
        adapter = self._apply()
        body = next(request["body"] for request in adapter.requests if request["url"].endswith("/lag-config/apply"))
        assert body["deleted_roots"] == ["Port-channel4"]

    def test_switchport_auto_preparations_carry_deletion_through_apply(self):
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_writes,
        )

        deleted = self._switchport("Ethernet65")
        retained = self._switchport("Ethernet66")
        detached = self._switchport("Ethernet67")
        self.management.auto_apply = True
        self.management.save(update_fields=["auto_apply"])
        config, session = self.adapter.patches()
        with config, session, renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(deleted),))) as writer:
            writer.delete(deleted)
        assert self.adapter.requests[-1]["body"]["deleted_roots"] == ["Ethernet65"]
        changed = copy.copy(retained)
        changed.mode = "access"
        config, session = self.adapter.patches()
        with (
            config,
            session,
            renderer_writes(
                RendererMutationPlan.build(saves=(planned_save(changed, update_fields=("mode",)),))
            ) as writer,
        ):
            writer.save(changed, update_fields=("mode",))
        assert self.adapter.requests[-1]["body"]["deleted_roots"] == ["Ethernet65"]
        unowned = copy.copy(detached)
        unowned.status = "changed"
        config, session = self.adapter.patches()
        with (
            config,
            session,
            renderer_writes(
                RendererMutationPlan.build(saves=(planned_save(unowned, update_fields=("status",)),))
            ) as writer,
        ):
            writer.save(unowned, update_fields=("status",))
        assert all(
            "Ethernet65" in request["body"]["deleted_roots"]
            for request in self.adapter.requests
            if request["url"].endswith("/switchport/apply")
        )
        adapter = self._apply()
        body = next(request["body"] for request in adapter.requests if request["url"].endswith("/switchport/apply"))
        assert body["deleted_roots"] == ["Ethernet65"]
        assert all(item["interface_name"] != "Ethernet67" for item in body["interfaces"])

    def test_switchport_auto_delete_and_unown_in_one_plan_keep_deletion_through_apply(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_writes,
        )

        deleted = self._switchport("Ethernet68")
        detached = self._switchport("Ethernet69")
        retained = self._switchport("Ethernet70")
        self.management.auto_apply = True
        self.management.save(update_fields=["auto_apply"])

        config, session = self.adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "switchport", force=True) is not None
        baseline = self.adapter.requests[-1]["body"]
        assert {item["interface_name"] for item in baseline["interfaces"]} == {
            "Ethernet68",
            "Ethernet69",
            "Ethernet70",
        }
        self.adapter.requests.clear()

        unowned = copy.copy(detached)
        unowned.status = "changed"
        plan = RendererMutationPlan.build(
            deletes=(planned_delete(deleted),),
            saves=(planned_save(unowned, update_fields=("status",)),),
        )
        config, session = self.adapter.patches()
        with config, session, renderer_writes(plan) as writer:
            writer.delete(deleted)
            writer.save(unowned, update_fields=("status",))

        def assert_preparations(requests):
            bodies = [request["body"] for request in requests if request["url"].endswith("/switchport/apply")]
            assert bodies
            for body in bodies:
                assert body["deleted_roots"] == ["Ethernet68"]
                assert not {"Ethernet68", "Ethernet69"} & {item["interface_name"] for item in body["interfaces"]}

        assert_preparations(self.adapter.requests)
        before_edit = len(self.adapter.requests)
        edited = copy.copy(retained)
        edited.mode = "access"
        config, session = self.adapter.patches()
        with (
            config,
            session,
            renderer_writes(
                RendererMutationPlan.build(saves=(planned_save(edited, update_fields=("mode",)),))
            ) as writer,
        ):
            writer.save(edited, update_fields=("mode",))
        assert_preparations(self.adapter.requests[before_edit:])

        adapter = self._apply()
        assert_preparations(adapter.requests)

    def test_never_authorized_root_is_reported_and_discarded(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        deleted = self._bundle("Port-channel7")
        with renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(deleted),))) as writer:
            writer.delete(deleted)
        self.adapter._respond = lambda body: {
            "status": "prepared",
            "stream": "lag",
            "selection_revision": 1,
            "unauthorized_deleted_roots": list(body["deleted_roots"]),
        }
        config, session = self.adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "lacp", force=True) is not None
        assert self.adapter.requests[-1]["body"]["deleted_roots"] == ["Port-channel7"]
        assert not NSOSwitchingRootDeletion.objects.filter(management=self.management, scope="lacp").exists()
        config, session = self.adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "lacp", force=True) is not None
        assert self.adapter.requests[-1]["body"]["deleted_roots"] == []

    def test_public_delivery_uses_the_same_pending_deletion_identity(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        deleted = self._bundle("Port-channel13")
        with renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(deleted),))) as writer:
            writer.delete(deleted)
        config, session = self.adapter.patches()
        with config, session:
            answer = deliver("lacp", self.device.pk, self.management.adapter_device_id)
        assert answer["status"] == "prepared"
        assert self.adapter.requests[-1]["body"]["deleted_roots"] == ["Port-channel13"]

    def test_stale_preparation_retires_entries_but_keeps_deletion(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        from ._outbox_case import enqueue, entries

        deleted = self._bundle("Port-channel8")
        with renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(deleted),))) as writer:
            writer.delete(deleted)
        enqueue(self.device, "lacp")
        self.adapter._respond = lambda body: (
            409,
            {"error": {"code": "conflict", "message": "stale", "detail": {"reason": "stale_preparation"}}},
        )
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "lacp") == drain.SUPERSEDED
        assert entries(self.device, "lacp") == []
        assert NSOSwitchingRootDeletion.objects.filter(management=self.management, root_name="Port-channel8").exists()

    def test_revision_conflict_retains_entries_and_deletion(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        from ._outbox_case import enqueue, entries

        deleted = self._bundle("Port-channel9")
        with renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(deleted),))) as writer:
            writer.delete(deleted)
        enqueue(self.device, "lacp")
        self.adapter._respond = lambda body: (
            409,
            {"error": {"code": "conflict", "message": "digest differs", "detail": {"reason": "revision_conflict"}}},
        )
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "lacp") == drain.FAILED
        assert len(self.adapter.requests) == 2
        assert len(entries(self.device, "lacp")) >= 1
        assert NSOSwitchingRootDeletion.objects.filter(management=self.management, root_name="Port-channel9").exists()
        self.management.refresh_from_db()
        assert self.management.intent_push_errors["lacp"]["code"] == "revision_invariant"

    def test_discharge_cannot_clear_a_newer_deletion_event(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOLACPBundleState, NSOSwitchingRootDeletion
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_writes,
        )

        deleted = self._bundle("Port-channel10")
        with renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(deleted),))) as writer:
            writer.delete(deleted)
        first_version = NSOSwitchingRootDeletion.objects.get(
            management=self.management, root_name="Port-channel10"
        ).event_version

        def interleave(body):
            restored = NSOLACPBundleState(
                management=self.management,
                interface=deleted.interface,
                lag_id=deleted.lag_id,
                status="accepted",
            )
            plan = RendererMutationPlan.build(
                saves=(planned_save(restored, force_insert=True, natural_key=("management", "interface")),)
            )
            with renderer_writes(plan) as writer:
                writer.save(restored, force_insert=True)
            with renderer_writes(RendererMutationPlan.build(deletes=(planned_delete(restored),))) as writer:
                writer.delete(restored)
            return {
                "status": "prepared",
                "stream": "lag",
                "selection_revision": 1,
                "unauthorized_deleted_roots": ["Port-channel10"],
            }

        self.adapter._respond = interleave
        config, session = self.adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "lacp", force=True) is not None
        current = NSOSwitchingRootDeletion.objects.get(management=self.management, root_name="Port-channel10")
        assert current.event_version > first_version

    def test_foreign_save_after_audit_repairs_before_capture_and_retains_retry(self):
        from netbox_nso_plugin import drain, renderer_audit
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision

        from ._outbox_case import mirror_update

        bundle = self._bundle("Port-channel11")
        mirror_update(bundle, status="in_sync")
        before = NSOIntentRevision.objects.get(device=self.device, scope="lacp").revision
        real_audit = renderer_audit.audit_renderer_scopes

        def audit_then_foreign_save(*args, **kwargs):
            result = real_audit(*args, **kwargs)
            bundle.min_links = 3
            bundle.save(update_fields=["min_links"])
            return result

        self.adapter.fail_with = requests.exceptions.ConnectionError("lost")
        config, session = self.adapter.patches()
        with (
            config,
            session,
            patch.object(renderer_audit, "audit_renderer_scopes", side_effect=audit_then_foreign_save),
        ):
            assert drain.drain_key(self.device.pk, "lacp", force=True) == drain.FAILED
        bundle.refresh_from_db()
        assert bundle.status == "accepted"
        assert NSOIntentRevision.objects.get(device=self.device, scope="lacp").revision > before
        assert NSOIntentOutboxEntry.objects.filter(device=self.device, scope="lacp", kind="repair").exists()
