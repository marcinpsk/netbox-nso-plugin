# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O3a: activate per-object static-route deletion authority.

These pins use the real ORM, signal, claim, client, and outcome paths. The adapter double
stands only at the HTTP boundary and implements the landed O2 response and execution shape.
"""

from __future__ import annotations

import threading

from django.core.management import CommandError, call_command
from django.db import connection, transaction
from django.test import TransactionTestCase

from ._outbox_case import (
    ReceiptAdapter,
    entries,
    expire_claim,
    make_managed,
    make_mgmt,
    own_route,
    partition,
    state_of,
    triple,
    without_commit_drain,
)
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


def route_member(route):
    return ("route_id", route.pk)


class _ActivationCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """One managed device whose static-route requests cross the real client boundary."""

    tag = "act"
    adapter_device_id = 7960

    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        self.device, self.mgmt = make_managed(self.tag, self.adapter_device_id)

    def drain(self, **kwargs):
        from netbox_nso_plugin import drain

        config, session = self.adapter.patches()
        with config, session:
            return drain.drain_key(self.device.pk, "static_route", **kwargs)

    def push_now(self, **kwargs):
        from netbox_nso_plugin import drain

        config, session = self.adapter.patches()
        with config, session:
            return drain.push_now(self.device.pk, "static_route", **kwargs)

    def clear_entries(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        NSOIntentOutboxEntry.objects.all().delete()

    def land(self, *routes):
        """Land the owned snapshot on the double before testing a shrink."""
        from netbox_nso_plugin import drain

        assert self.drain() in (drain.SUCCEEDED, drain.NOTHING)
        assert self.adapter.on_device[self.adapter_device_id] == {route_member(route) for route in routes}
        self.clear_entries()
        self.adapter.requests.clear()
        self.adapter.applied.clear()
        self.adapter.jobs.clear()

    def unown(self, route):
        with without_commit_drain():
            route.devices.remove(self.device)

    def reown(self, route, *, callbacks=False):
        from netbox_nso_plugin.signals import _accept_static_route_for_device, suppress_intent_push

        guard = transaction.atomic() if callbacks else without_commit_drain()
        with guard:
            with transaction.atomic():
                with suppress_intent_push():
                    route.devices.add(self.device)
                _accept_static_route_for_device(route, self.device)

    def bypass_unown(self, route):
        from netbox_nso_plugin.models import NSOStaticRouteState

        NSOStaticRouteState.objects.filter(management=self.mgmt, static_route=route).update(status="imported")

    def sent(self):
        return [
            request
            for request in self.adapter.requests
            if f"/devices/{self.adapter_device_id}/static-route-intent" in request["url"]
        ]


class TestPerObjectMarkingDrivesExecution(_ActivationCase):
    """O3.1 and O3.7: one body can retract one route and detach another."""

    tag = "mixed"
    adapter_device_id = 7961

    def test_only_the_route_with_folded_authority_is_retracted(self):
        from netbox_nso_plugin import drain

        retract = own_route(self.mgmt, "198.18.0.0/28", "198.18.0.1")
        detach = own_route(self.mgmt, "198.18.0.16/28", "198.18.0.17")
        self.land(retract, detach)

        with without_commit_drain(), transaction.atomic():
            self.bypass_unown(detach)
            retract.devices.remove(self.device)

        assert self.drain(chain=0) == drain.SUCCEEDED

        [request] = self.sent()
        assert request["body"]["deleted_routes"] == [
            {
                "route_id": retract.pk,
                "triples": [triple("198.18.0.0/28", "198.18.0.1")],
                "unverified": False,
            }
        ]
        assert request["params"].get("delete_origin") is None
        assert self.adapter.on_device[self.adapter_device_id] == {route_member(detach)}
        assert self.adapter.detached[self.adapter_device_id] == {route_member(detach)}
        assert self.adapter.jobs == [
            {"device_id": self.adapter_device_id, "member": route_member(retract), "marking": "delete_origin"},
            {"device_id": self.adapter_device_id, "member": route_member(detach), "marking": "detach"},
        ]


class TestTheListOverridesTheLegacyFlag(_ActivationCase):
    """O3.3: if both marking forms reach the adapter, the object list decides."""

    tag = "precedence"
    adapter_device_id = 7962

    def test_the_query_flag_cannot_retract_an_unlisted_route(self):
        from netbox_nso_plugin import adapter_client, drain

        retract = own_route(self.mgmt, "198.18.0.32/28", "198.18.0.33")
        detach = own_route(self.mgmt, "198.18.0.48/28", "198.18.0.49")
        self.land(retract, detach)
        with without_commit_drain(), transaction.atomic():
            self.bypass_unown(detach)
            retract.devices.remove(self.device)

        config, session = self.adapter.patches()
        with config, session, adapter_client.delete_origin_pushes():
            assert drain.drain_key(self.device.pk, "static_route", chain=0) == drain.SUCCEEDED

        [request] = self.sent()
        assert request["params"]["delete_origin"] == "true", "the precedence arm must carry both forms"
        assert [record["route_id"] for record in request["body"]["deleted_routes"]] == [retract.pk]
        assert self.adapter.on_device[self.adapter_device_id] == {route_member(detach)}
        assert self.adapter.detached[self.adapter_device_id] == {route_member(detach)}


class TestCanonicalLineageIsOnTheWire(_ActivationCase):
    """O3.5(a)(b): same-transaction and later-transaction removals carry acknowledged history."""

    tag = "wireline"
    adapter_device_id = 7963

    def _edit(self, route, next_hop):
        route.next_hop = next_hop
        route.save()
        route.refresh_from_db()

    def test_one_form_edit_and_remove_leads_with_the_acknowledged_triple(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.18.0.64/28", "198.18.0.65")
        self.land(route)
        acknowledged = triple("198.18.0.64/28", "198.18.0.65")

        with without_commit_drain(), transaction.atomic():
            self._edit(route, "198.18.0.66")
            route.devices.remove(self.device)

        assert self.drain(chain=0) == drain.SUCCEEDED
        [record] = self.sent()[-1]["body"]["deleted_routes"]
        assert record == {
            "route_id": route.pk,
            "triples": [acknowledged, triple("198.18.0.64/28", "198.18.0.66")],
            "unverified": False,
        }

    def test_a_later_transaction_does_not_depend_on_the_transient_edit_stash(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.18.0.80/28", "198.18.0.81")
        self.land(route)
        acknowledged = triple("198.18.0.80/28", "198.18.0.81")
        with without_commit_drain(), transaction.atomic():
            self._edit(route, "198.18.0.82")
        with without_commit_drain(), transaction.atomic():
            route.devices.remove(self.device)

        assert self.drain(chain=0) == drain.SUCCEEDED
        [record] = self.sent()[-1]["body"]["deleted_routes"]
        assert record["triples"] == [acknowledged, triple("198.18.0.80/28", "198.18.0.82")]
        assert record["unverified"] is False


class TestMembershipAndContentHaveDifferentAuthority(_ActivationCase):
    """O3.6: membership removal is listed; a field edit is not."""

    tag = "kind"
    adapter_device_id = 7964

    def test_only_membership_removal_emits_a_deleted_route(self):
        from netbox_nso_plugin import drain

        edited = own_route(self.mgmt, "198.18.0.96/28", "198.18.0.97")
        removed = own_route(self.mgmt, "198.18.0.112/28", "198.18.0.113")
        self.land(edited, removed)

        with without_commit_drain(), transaction.atomic():
            edited.metric = 7
            edited.save()
        assert self.drain(chain=0) == drain.SUCCEEDED
        assert self.sent()[-1]["body"]["deleted_routes"] == []

        self.adapter.requests.clear()
        self.unown(removed)
        assert self.drain(chain=0) == drain.SUCCEEDED
        assert [record["route_id"] for record in self.sent()[-1]["body"]["deleted_routes"]] == [removed.pk]


class TestNetZeroMembershipChangeWritesNoTombstone(_ActivationCase):
    """O3.9: remove and re-add in one transaction folds to ownership, not deletion."""

    tag = "netzero"
    adapter_device_id = 7965

    def test_remove_then_readd_is_present_and_unlisted(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.signals import _accept_static_route_for_device

        route = own_route(self.mgmt, "198.18.0.128/28", "198.18.0.129")
        self.land(route)
        with without_commit_drain(), transaction.atomic():
            route.devices.remove(self.device)
            route.devices.add(self.device)
            _accept_static_route_for_device(route, self.device)

        assert self.drain(chain=0) == drain.SUCCEEDED
        [request] = self.sent()
        assert [row["route_id"] for row in request["body"]["routes"]] == [route.pk]
        assert request["body"]["deleted_routes"] == []
        assert self.adapter.jobs == []


class TestOutrightDeletionFansOutAuthority(_ActivationCase):
    """O3.10: pre_delete lists the route for every managed device that owned it."""

    tag = "fanout"
    adapter_device_id = 7966

    def test_route_delete_lists_the_id_for_each_device(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.signals import _accept_static_route_for_device, suppress_intent_push

        other_device, _unused = make_managed("fanout-other", 7967, index=2)
        other_mgmt = _unused
        route = own_route(self.mgmt, "198.18.0.144/28", "198.18.0.145")
        with without_commit_drain(), transaction.atomic():
            with suppress_intent_push():
                route.devices.add(other_device)
            _accept_static_route_for_device(route, other_device)
        assert self.drain() == drain.SUCCEEDED
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(other_device.pk, "static_route") == drain.SUCCEEDED
        self.clear_entries()
        self.adapter.requests.clear()

        route_id = route.pk
        with without_commit_drain(), transaction.atomic():
            route.delete()

        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "static_route", chain=0) == drain.SUCCEEDED
            assert drain.drain_key(other_device.pk, "static_route", chain=0) == drain.SUCCEEDED

        bodies = [
            request["body"] for request in self.adapter.requests if request["url"].endswith("static-route-intent")
        ]
        assert len(bodies) == 2
        assert [[record["route_id"] for record in body["deleted_routes"]] for body in bodies] == [
            [route_id],
            [route_id],
        ]
        assert other_mgmt.device_id == other_device.pk


class TestSignalBypassDetachesOnTheNextPush(_ActivationCase):
    """O3.11: QuerySet.update emits no authority, but a later real push executes the detach."""

    tag = "bypass"
    adapter_device_id = 7968

    def test_a_bypassed_unown_is_omitted_but_not_listed(self):
        from netbox_nso_plugin import drain

        bypassed = own_route(self.mgmt, "198.18.0.160/28", "198.18.0.161")
        keeper = own_route(self.mgmt, "198.18.0.176/28", "198.18.0.177")
        self.land(bypassed, keeper)
        self.bypass_unown(bypassed)
        with without_commit_drain(), transaction.atomic():
            self.mgmt.static_route_states.get(static_route=keeper).save()

        assert self.drain(chain=0) == drain.SUCCEEDED
        [request] = self.sent()
        assert request["body"]["deleted_routes"] == []
        assert self.adapter.detached[self.adapter_device_id] == {route_member(bypassed)}
        assert self.adapter.jobs == [
            {"device_id": self.adapter_device_id, "member": route_member(bypassed), "marking": "detach"}
        ]


class TestFailedDeletionKeepsItsExactAuthority(_ActivationCase):
    """O3.12: a transport failure retains the whole record for an exact later delivery."""

    tag = "retrydel"
    adapter_device_id = 7969

    def test_the_later_success_sends_the_same_lineage(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.18.0.192/28", "198.18.0.193")
        self.land(route)
        self.unown(route)
        self.adapter.fail_with = ConnectionError("adapter unavailable")

        assert self.drain(chain=0) == drain.FAILED
        state = state_of(self.device, "static_route")
        held = list(state.claim_deletions)
        assert [record["route_id"] for record in held] == [route.pk]
        assert state.queued_deletions == []

        self.adapter.fail_with = None
        assert self.drain(chain=0) == drain.SUCCEEDED
        assert self.sent()[-1]["body"]["deleted_routes"] == [
            {name: value for name, value in held[0].items() if name != "op"}
        ]
        assert (
            state_of(self.device, "static_route").queued_deletions,
            state_of(self.device, "static_route").claim_deletions,
        ) == ([], [])


class TestAdapterResponsesSettleTheActivatedScope(_ActivationCase):
    """O3.13: refuse unsafe resyncs and consume landed responses on production paths."""

    tag = "responses"
    adapter_device_id = 7970

    def test_pending_authority_makes_the_fleet_resync_exit_nonzero(self):
        route = own_route(self.mgmt, "198.18.0.208/28", "198.18.0.209")
        self.land(route)
        self.unown(route)
        pending = [row.pk for row in entries(self.device, "static_route", unconsumed=True)]

        config, session = self.adapter.patches()
        with config, session, self.assertRaises(CommandError):
            call_command("nso_resync_static_route_intent", device_ids=[self.device.pk])

        assert self.sent() == []
        assert [row.pk for row in entries(self.device, "static_route", unconsumed=True)] == pending

    def test_store_only_sends_an_explicit_empty_list_and_clears_no_work(self):
        from netbox_nso_plugin import delivery

        route = own_route(self.mgmt, "198.18.0.208/28", "198.18.0.209")
        self.land(route)
        with without_commit_drain(), transaction.atomic():
            self.mgmt.static_route_states.get(static_route=route).save()
        pending = [row.pk for row in entries(self.device, "static_route", unconsumed=True)]

        assert self.push_now(mode=delivery.MODE_STORE_ONLY, force=True) is not None
        assert self.sent()[-1]["body"]["deleted_routes"] == []
        assert [row.pk for row in entries(self.device, "static_route", unconsumed=True)] == pending

    def test_a_lost_deletion_response_replays_the_stored_partition(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.18.0.224/28", "198.18.0.225")
        self.land(route)
        self.unown(route)
        claimed = drain.claim(self.device.pk, "static_route")
        config, session = self.adapter.patches()
        with config, session:
            response = drain.send_claim(claimed)
        assert response["deleted_executed_ids"] == [route.pk]
        first = self.sent()[-1]

        expire_claim(self.device, "static_route")
        assert self.drain(chain=0) == drain.SUCCEEDED
        replay = self.sent()[-1]
        assert replay["push_seq"] == first["push_seq"]
        assert replay["body"] == first["body"]
        assert self.adapter.replays == 1
        assert state_of(self.device, "static_route").claim_deletions == []

    def test_degraded_and_uncorrelated_fields_write_a_durable_record(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.18.0.240/28", "198.18.0.241")
        self.land(route)
        self.unown(route)
        residue = triple("198.18.1.0/28", "198.18.1.1")
        self.adapter._respond = lambda body: partition(degraded=[route.pk], removed=[residue])

        assert self.drain(chain=0) == drain.SUCCEEDED
        [wire] = self.sent()[-1]["body"]["deleted_routes"]
        assert wire["unverified"] is False
        [record] = state_of(self.device, "static_route").degraded_deletions
        assert record["route_ids"] == [route.pk]
        assert record["triples"] == [residue]
        assert record["reason"] == drain.PRE_FENCE_DETACH

    def test_fence_shut_abandons_backfills_then_executes_at_a_new_sequence(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.adapter_client import AdapterError

        route = own_route(self.mgmt, "198.18.1.16/28", "198.18.1.17")
        self.land(route)
        self.unown(route)
        self.adapter.fail_with = AdapterError("fence shut", code="conflict", detail={"reason": "fence_shut"})

        assert self.drain(chain=0) == drain.WITHHELD
        state = state_of(self.device, "static_route")
        assert state.push_seq is None and state.fence_withheld_since is not None
        assert [record["route_id"] for record in state.queued_deletions] == [route.pk]

        self.adapter.fail_with = None
        self.adapter._respond = lambda body: partition(removed=[triple("198.18.1.32/28", "198.18.1.33")])
        assert self.drain(chain=0) == drain.SUCCEEDED
        assert self.sent()[-1]["params"].get("backfill_only") == "true"
        assert self.sent()[-1]["body"]["deleted_routes"] == []

        self.adapter._respond = lambda body: partition(executed=[route.pk])
        assert self.drain(chain=0) == drain.SUCCEEDED
        sent = self.sent()
        assert [record["route_id"] for record in sent[-1]["body"]["deleted_routes"]] == [route.pk]
        assert sent[-1]["push_seq"] > sent[-2]["push_seq"]
        assert self.adapter.on_device[self.adapter_device_id] == set()


class TestAnExecutedPerObjectDeletionIsNoDowngrade(_ActivationCase):
    """O3a review P2: the AND-fold has no wire effect on a per-object scope.

    A deletion folded with an unmarked edit rides ``deleted_routes`` and executes, so the
    query-flag downgrade record would be a false durable warning for this scope.
    """

    tag = "nodowngrade"
    adapter_device_id = 7969

    def test_a_deletion_folded_with_an_unmarked_edit_records_no_downgrade(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOStaticRouteState

        going = own_route(self.mgmt, "198.18.2.0/28", "198.18.2.1")
        staying = own_route(self.mgmt, "198.18.2.16/28", "198.18.2.17")
        self.land(going, staying)

        self.unown(going)
        with without_commit_drain(), transaction.atomic():
            NSOStaticRouteState.objects.get(management=self.mgmt, static_route=staying).save()

        assert self.drain(chain=0) == drain.SUCCEEDED

        [request] = self.sent()
        assert [record["route_id"] for record in request["body"]["deleted_routes"]] == [going.pk]
        assert request["params"].get("delete_origin") is None
        assert self.adapter.on_device[self.adapter_device_id] == {route_member(staying)}

        state = state_of(self.device, "static_route")
        assert state.queued_deletions == []
        assert state.degraded_deletions == [], "an executed per-object deletion was recorded as a downgrade"


class TestRevocationRacesDoNotInventAuthority(_ActivationCase):
    """O3.14(a)(b)(c): failures and re-ownership preserve the single-home algebra."""

    tag = "revokerace"
    adapter_device_id = 7971

    def test_failed_delete_failed_readd_then_bypassed_unown_forces_an_empty_list(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.18.1.48/28", "198.18.1.49")
        self.land(route)
        self.unown(route)
        self.adapter.fail_with = ConnectionError("adapter unavailable")
        assert self.drain(chain=0) == drain.FAILED

        self.reown(route)
        assert self.drain(chain=0) == drain.FAILED
        assert state_of(self.device, "static_route").claim_deletions == []
        self.bypass_unown(route)

        self.adapter.fail_with = None
        assert self.push_now(force=True) is not None
        assert self.sent()[-1]["body"]["deleted_routes"] == []
        assert self.adapter.detached[self.adapter_device_id] == {route_member(route)}

    def test_a_revoke_during_a_failed_attempt_does_not_duplicate_the_claim(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.18.1.64/28", "198.18.1.65")
        self.land(route)
        self.unown(route)

        def revoke_then_fail(body):
            self.reown(type(route)._default_manager.get(pk=route.pk))
            raise ConnectionError("response lost")

        self.adapter._respond = revoke_then_fail
        assert self.drain(chain=0) == drain.FAILED
        state = state_of(self.device, "static_route")
        assert [record["route_id"] for record in state.claim_deletions] == [route.pk]
        assert state.queued_deletions == []
        assert [record["route_id"] for record in self.sent()[-1]["body"]["deleted_routes"]] == [route.pk]

    def test_revoke_then_redelete_survives_while_the_first_claim_owns_the_home(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.18.1.80/28", "198.18.1.81")
        self.land(route)
        self.unown(route)
        claimed = drain.claim(self.device.pk, "static_route")

        config, session = self.adapter.patches()
        with config, session:
            response = drain.send_claim(claimed)
        self.reown(route)
        self.unown(route)
        state = state_of(self.device, "static_route")
        assert [record["route_id"] for record in state.claim_deletions] == [route.pk]
        assert state.queued_deletions == []

        assert drain.settle(claimed, response) == drain.SUCCEEDED
        later = drain.claim(self.device.pk, "static_route")
        assert [record["route_id"] for record in later.deletions] == [route.pk]


class TestBothRevocationArrivalOrdersStayRecoverable(_ActivationCase):
    """O3.15: stale admission and the accepted send-window residual are both visible."""

    tag = "arrival"
    adapter_device_id = 7972

    def test_a_lower_sequence_is_rejected_as_stale_with_the_list_intact(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.adapter_client import AdapterError

        route = own_route(self.mgmt, "198.18.1.96/28", "198.18.1.97")
        self.land(route)
        rendered = delivery.render("static_route", self.device.pk, self.adapter_device_id)
        newer = max(receipt["push_seq"] for receipt in self.adapter.receipts.values()) + 2
        config, session = self.adapter.patches()
        with config, session:
            delivery.send(rendered, rendered.payload, push_seq=newer, deletions=[])
            try:
                delivery.send(
                    rendered,
                    [],
                    push_seq=newer - 1,
                    deletions=[
                        {
                            "op": "delete",
                            "route_id": route.pk,
                            "triples": [triple("198.18.1.96/28", "198.18.1.97")],
                            "unverified": False,
                        }
                    ],
                )
            except AdapterError as exc:
                assert exc.code == "stale"
            else:
                raise AssertionError("the adapter accepted the stale request")

        assert [record["route_id"] for record in self.sent()[-1]["body"]["deleted_routes"]] == [route.pk]

    def test_readd_in_the_send_window_emits_the_corrective_request_before_release(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOStaticRouteState

        route = own_route(self.mgmt, "198.18.1.112/28", "198.18.1.113")
        self.land(route)
        self.unown(route)
        corrective = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def response(body):
            deleted = body.get("deleted_routes") or []
            if deleted:
                live = type(route)._default_manager.get(pk=route.pk)
                self.reown(live, callbacks=True)
                return partition(executed=[route.pk])
            corrective.set()
            assert release.wait(timeout=30)
            return partition()

        self.adapter._respond = response

        def run():
            try:
                assert self.drain() == drain.SUCCEEDED
            except BaseException as exc:  # noqa: BLE001 (re-raised on the test thread)
                errors.append(exc)
            finally:
                connection.close()

        worker = threading.Thread(target=run)
        worker.start()
        assert corrective.wait(timeout=30), "the production outcome path emitted no corrective request"
        correction = self.sent()[-1]
        assert correction["body"]["deleted_routes"] == []
        assert [record["route_id"] for record in correction["body"]["routes"]] == [route.pk]
        release.set()
        worker.join(timeout=60)

        assert not worker.is_alive()
        assert errors == []
        overlay = NSOStaticRouteState.objects.get(management=self.mgmt, static_route=route)
        assert overlay.status == "accepted"


class TestOffboardedAuthorityReResolvesTheAdapterDevice(_ActivationCase):
    """O3.17: authority is keyed on Device and resolves the new adapter id at claim time."""

    tag = "offboard"
    adapter_device_id = 7973

    def test_offboard_and_reonboard_preserve_and_deliver_the_deletion(self):
        from unittest.mock import patch

        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.18.1.128/28", "198.18.1.129")
        self.land(route)
        self.unown(route)
        pending = [row.pk for row in entries(self.device, "static_route", unconsumed=True)]

        with patch("netbox_nso_plugin.adapter_client.delete_device"):
            self.mgmt.delete()
        assert [row.pk for row in entries(self.device, "static_route", unconsumed=True)] == pending

        replacement = make_mgmt(self.device, "offboard-new", 7974)
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "static_route", chain=0) == drain.SUCCEEDED

        [request] = [r for r in self.adapter.requests if r["url"].endswith("static-route-intent")]
        assert "/devices/7974/" in request["url"]
        assert [record["route_id"] for record in request["body"]["deleted_routes"]] == [route.pk]
        assert replacement.device_id == self.device.pk
