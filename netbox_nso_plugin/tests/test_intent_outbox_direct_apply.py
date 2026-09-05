# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1), the Rev 15 split: the two direct-apply keys leave the protocol.

``lacp`` and ``switchport`` write to NSO synchronously inside the request and answer a failed
apply with HTTP 200 and an error envelope (O-P12c, §7.1), so no receipt can be atomic with
their effect and the generic admission path cannot tell their failure from their success.
Riding the claim path costs them twice: a lost outcome lets the scavenger replay the body
into a SECOND device write, and the outcome path retires an error envelope as a success.

So they take no sequence, no lease and no takeover. What survives is the coalescing the
fleet-wide outbox gives every key: the burst folds into one send, the entries are consumed on
the attempt, and a failure lands in the push journal alone, exactly as today's direct call
leaves it. Joining them to the protocol properly is §7.1's own card.
"""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Interface
from django.db import transaction
from django.test import TransactionTestCase

from ._outbox_case import (
    ReceiptAdapter,
    enqueue,
    entries,
    expire_claim,
    make_managed,
    state_of,
    without_commit_drain,
)
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class _LostOutcome(Exception):
    """The worker died between the send and the outcome, which is O1.10's crash."""


class _DirectApplyCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """One managed device and a recorded far side, for a key that is not in protocol."""

    tag = "direct"
    adapter_device_id = 7800

    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        self.device, self.mgmt = make_managed(self.tag, self.adapter_device_id)

    def drain(self, scope="lacp", **kwargs):
        from netbox_nso_plugin import drain

        config, session = self.adapter.patches()
        with config, session:
            return drain.drain_key(self.device.pk, scope, **kwargs)

    def errors(self) -> dict:
        from netbox_nso_plugin.models import NSODeviceManagement

        return NSODeviceManagement.objects.get(pk=self.mgmt.pk).intent_push_errors or {}


class TestADirectApplyIsNeverReplayed(_DirectApplyCase):
    """R14-B1, the split's motivating defect: there is no receipt to make a replay safe."""

    tag = "replay"
    adapter_device_id = 7801

    def test_a_lost_outcome_never_applies_the_key_a_second_time(self):
        from netbox_nso_plugin import delivery, drain

        enqueue(self.device, "lacp")
        real_send = delivery.send

        def lose_the_outcome(*args, **kwargs):
            """The body reaches the device, and nothing records that it did."""
            real_send(*args, **kwargs)
            raise _LostOutcome

        # Not `settle`: this path returns its own outcome and never calls it (drain.py
        # `_deliver_direct`), so patching `settle` here would fire nothing at all.
        with patch.object(delivery, "send", side_effect=lose_the_outcome):
            assert self.drain() == drain.FAILED
        assert len(self.adapter.applied) == 1, "the body reached the device once"

        assert expire_claim(self.device, "lacp") is False, "a direct-apply key took a lease"
        self.drain()  # the scavenger's turn
        config, session = self.adapter.patches()
        with config, session:
            drain.drain_intent_outbox()  # and the tick's

        assert len(self.adapter.applied) == 1, "the receipt-less endpoint applied the same body a second time"

    def test_the_key_takes_no_sequence_at_all(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.outbox import allocate_push_seq

        enqueue(self.device, "switchport")
        with patch.object(drain, "allocate_push_seq", wraps=allocate_push_seq) as allocate:
            assert self.drain("switchport") == drain.SUCCEEDED

        assert allocate.call_count == 0, "an out-of-protocol key took a sequence no receipt can answer"
        assert self.adapter.requests[-1]["push_seq"] is None
        state = state_of(self.device, "switchport")
        assert (state.push_seq, state.claimed_at, state.claim_payload) == (None, None, None)


class TestAnErrorEnvelopeIsAFailure(_DirectApplyCase):
    """§7.1(2): a failed device write answered with HTTP 200 may never read as a success."""

    tag = "envelope"
    adapter_device_id = 7802

    def test_an_error_envelope_is_journaled_and_never_retired_as_a_success(self):
        from netbox_nso_plugin import drain

        enqueue(self.device, "switchport")
        self.adapter._respond = lambda body: {"status": "error", "message": "the device refused the apply"}

        assert self.drain("switchport") == drain.FAILED

        recorded = self.errors().get("switchport") or {}
        assert "the device refused the apply" in recorded.get("message", ""), self.errors()
        assert entries(self.device, "switchport") == [], "the entry is consumed on the attempt, as today"
        assert state_of(self.device, "switchport").push_seq is None, "nothing is left to replay"

    def test_a_deployed_envelope_still_succeeds(self):
        from netbox_nso_plugin import drain

        enqueue(self.device, "lacp")
        self.adapter._respond = lambda body: {"status": "deployed", "device": "nso-cl-envelope"}

        assert self.drain() == drain.SUCCEEDED
        assert self.errors() == {}


class TestTheBurstStillCoalesces(_DirectApplyCase):
    """The split takes the claim away, not the outbox: one send per burst, from the trigger."""

    tag = "burst"
    adapter_device_id = 7803

    def _owned_bundle(self):
        from netbox_nso_plugin.models import NSOLACPBundleState

        with without_commit_drain(), transaction.atomic():
            # Committing LACP is a device write, so only auto-apply pushes it on save.
            self.mgmt.auto_apply = True
            self.mgmt.save(update_fields=["auto_apply"])
            lag = Interface.objects.create(device=self.device, name="Port-channel1", type="lag")
            return NSOLACPBundleState.objects.create(
                management=self.mgmt, interface=lag, lag_id=1, min_links=2, timer="fast", status="accepted"
            )

    def test_n_saves_in_one_transaction_reach_the_device_once(self):
        bundle = self._owned_bundle()
        config, session = self.adapter.patches()

        with config, session, transaction.atomic():
            for _ in range(3):
                bundle.save()

        assert len(self.adapter.requests) == 1, self.adapter.requests
        assert self.adapter.requests[0]["push_seq"] is None
        assert entries(self.device, "lacp") == [], "the entries are consumed on the attempt"


class TestRepairContributionsAreNeutralForDirectApply(_DirectApplyCase):
    tag = "directrepair"
    adapter_device_id = 7804

    def test_a_repair_cannot_strip_a_lacp_deletion_mark(self):
        from netbox_nso_plugin import drain, outbox

        enqueue(self.device, "lacp", delete_origin=True)
        enqueue(self.device, "lacp", kind=outbox.CONTRIBUTION_KIND_REPAIR)

        assert self.drain("lacp") == drain.SUCCEEDED
        assert self.adapter.requests[-1]["params"].get("delete_origin") == "true"

    def test_a_repair_only_switchport_burst_cannot_grant_a_deletion_mark(self):
        from netbox_nso_plugin import drain, outbox

        enqueue(
            self.device,
            "switchport",
            delete_origin=True,
            kind=outbox.CONTRIBUTION_KIND_REPAIR,
        )

        assert self.drain("switchport") == drain.SUCCEEDED
        assert self.adapter.requests[-1]["params"].get("delete_origin") is None
