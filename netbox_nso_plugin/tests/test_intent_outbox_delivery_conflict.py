# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1915: an adapter delivery conflict dissolves the claim and is not resent until intent changes."""

from __future__ import annotations

import datetime

from django.db import transaction
from django.db.models import F

from ._outbox_case import (
    as_per_object,
    delete_vlan_state,
    entries,
    own_route,
    partition,
    rename_vlan,
    state_of,
    without_commit_drain,
)
from ._static_route_case import _edit_owned_route, _touch_owned_route
from .test_intent_outbox_marking import _MarkCase, vlan_member
from .test_intent_outbox_outcome import _OutcomeCase

PROVENANCE = {"streams": {"vlan": "outstanding_deletion_provenance"}}
PROOF = {"streams": ["interface_config", "ip"], "field": "parent_binding"}


def conflict(code, detail):
    """The adapter's documented delivery-conflict envelope."""

    def respond(_body):
        return 409, {"error": {"code": code, "message": "the push cannot be promoted", "detail": detail}}

    return respond


class TestADeliveryConflictDissolvesTheClaim(_MarkCase):
    tag = "dconf"
    adapter_device_id = 7718

    def rename(self, state, name):
        with without_commit_drain(), transaction.atomic():
            rename_vlan(state, name)

    def push_error(self):
        from netbox_nso_plugin.models import NSODeviceManagement

        return (NSODeviceManagement.objects.get(pk=self.mgmt.pk).intent_push_errors or {}).get("vlan")

    def assert_refused_without_resend(self, code):
        from netbox_nso_plugin import drain

        assert self.drain() == drain.REJECTED
        state = state_of(self.device, "vlan")
        assert state.push_seq is None, "the adapter rolled the refused delivery back, so the claim dissolves"
        assert state.last_error_code == code
        assert self.push_error()["code"] == code, "the operator sees the adapter's reason"
        sent = len(self.adapter.requests)
        assert self.drain() == drain.NOTHING
        assert len(self.adapter.requests) == sent, "the refused request is not resent as if it were transient"
        return self.adapter.sequences[-1]

    def test_an_unmarked_conflict_retires_its_trigger_and_the_correction_sends_fresh(self):
        from netbox_nso_plugin import drain

        states = self.own(921, 922)
        self.rename(states[921], "cl-dconf-renamed")
        accepted = self.adapter._respond
        self.adapter._respond = conflict("apply_unexecutable", PROVENANCE)

        burned = self.assert_refused_without_resend("apply_unexecutable")
        assert entries(self.device, "vlan") == []

        self.adapter._respond = accepted
        self.rename(states[921], "cl-dconf-corrected")
        assert self.drain() == drain.SUCCEEDED
        assert self.adapter.sequences[-1] > burned, "the refused sequence is burned, never reissued"
        assert self.push_error() is None

    def test_an_interface_proof_conflict_dissolves_the_same_way(self):
        states = self.own(923)
        self.rename(states[923], "cl-dconf-proof")
        self.adapter._respond = conflict("interface_proof_conflict", PROOF)

        self.assert_refused_without_resend("interface_proof_conflict")
        assert entries(self.device, "vlan") == []

    def test_a_marked_conflict_keeps_its_authority_until_new_intent_arrives(self):
        from netbox_nso_plugin import drain

        states = self.own(931, 932, 933)
        with without_commit_drain(), transaction.atomic():
            delete_vlan_state(states[931])
        accepted = self.adapter._respond
        self.adapter._respond = conflict("apply_unexecutable", PROVENANCE)

        self.assert_refused_without_resend("apply_unexecutable")
        assert [row.mark_any for row in entries(self.device, "vlan", unconsumed=True)] == [True], (
            "the deletion authority survives the refusal"
        )

        self.adapter._respond = accepted
        with without_commit_drain(), transaction.atomic():
            delete_vlan_state(states[932])
        assert self.drain() == drain.SUCCEEDED
        assert self.marks()[-1] == "true", "the new claim still carries the earlier deletion"
        assert self.adapter.on_device[self.adapter_device_id] == {vlan_member(933)}
        assert entries(self.device, "vlan") == []

    def test_a_forced_push_resends_a_refused_request(self):
        from netbox_nso_plugin import drain

        states = self.own(941, 942)
        with without_commit_drain(), transaction.atomic():
            delete_vlan_state(states[941])
        accepted = self.adapter._respond
        self.adapter._respond = conflict("interface_proof_conflict", PROOF)
        self.assert_refused_without_resend("interface_proof_conflict")

        self.adapter._respond = accepted
        assert self.drain(force=True) == drain.SUCCEEDED
        assert self.adapter.on_device[self.adapter_device_id] == {vlan_member(942)}
        assert state_of(self.device, "vlan").refused_identity == "", "a success lifts the refusal"

    def test_a_marked_refusal_is_resent_after_the_backoff(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxState

        states = self.own(961, 962)
        with without_commit_drain(), transaction.atomic():
            delete_vlan_state(states[961])
        accepted = self.adapter._respond
        self.adapter._respond = conflict("apply_unexecutable", PROVENANCE)
        self.assert_refused_without_resend("apply_unexecutable")

        # Apply cannot carry queued deletion authority store-only, so the tick is the recovery path.
        NSOIntentOutboxState.objects.filter(device=self.device, scope="vlan").update(
            last_error_at=F("last_error_at") - drain.REFUSED_RETRY - datetime.timedelta(seconds=1)
        )
        self.adapter._respond = accepted
        assert self.drain() == drain.SUCCEEDED
        assert self.marks()[-1] == "true"
        assert self.adapter.on_device[self.adapter_device_id] == {vlan_member(962)}

    def test_a_device_busy_conflict_is_replayed_at_the_same_sequence(self):
        from netbox_nso_plugin import drain

        states = self.own(951)
        self.rename(states[951], "cl-dconf-busy")
        accepted = self.adapter._respond
        self.adapter._respond = conflict("conflict", {})

        assert self.drain() == drain.FAILED
        held = state_of(self.device, "vlan").push_seq
        assert held is not None, "a generic conflict is transient: the operation is kept for replay"

        self.adapter._respond = accepted
        assert self.drain() == drain.SUCCEEDED
        assert self.adapter.sequences[-2:] == [held, held]


class TestAChangedDeletionLineageIsANewRequest(_OutcomeCase):
    tag = "dclin"
    adapter_device_id = 7719

    def test_a_redeletion_with_a_new_lineage_is_sent_after_a_refusal(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.208/28", "198.51.100.40")
        with as_per_object("static_route"):
            assert self.drain() == drain.SUCCEEDED
            # An unmarked contributor in both folds keeps the request mark equal.
            with without_commit_drain(), transaction.atomic():
                _touch_owned_route(route)
            self.unown(route)
            self.adapter._respond = conflict(
                "apply_unexecutable", {"streams": {"static_route": "no_executable_interface"}}
            )
            assert self.drain() == drain.REJECTED
            refused = self.adapter.requests[-1]

            # Same route id and body, but the deletion now carries the edited triple too.
            self.reown(route)
            with without_commit_drain(), transaction.atomic():
                _edit_owned_route(route, next_hop="198.51.100.41")
            self.unown(route)
            self.adapter._respond = lambda body: partition(executed=[route.pk])
            sent = len(self.adapter.requests)
            assert self.drain() == drain.SUCCEEDED

        assert len(self.adapter.requests) == sent + 1, "a changed deletion record is a different request"
        resent = self.adapter.requests[-1]
        assert (resent["params"], resent["body"]["routes"]) == (refused["params"], refused["body"]["routes"])
        [record] = resent["body"]["deleted_routes"]
        assert len(record["triples"]) == 2


def test_the_interface_proof_conflict_has_fixed_public_text():
    from netbox_nso_plugin.adapter_client import AdapterError, public_error_message

    message = public_error_message(AdapterError("", code="interface_proof_conflict"))
    assert "interface" in message and "Apply" in message
