# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Switching snapshots prepare selectable revisions outside the receipt protocol.

LACP and switchport preparations carry a content revision and explicit root deletions.
They do not take a push sequence or lease. Failed preparations retain outbox entries
and deletion rows for retry. Apply later authorizes the selected revisions.
"""

from __future__ import annotations

import copy
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


class TestASwitchingPreparationIsRetried(_DirectApplyCase):
    """A lost preparation response leaves its captured work for a later send."""

    tag = "replay"
    adapter_device_id = 7801

    def test_a_lost_response_reprepares_the_same_snapshot(self):
        from netbox_nso_plugin import drain

        enqueue(self.device, "lacp")
        self.adapter.drop_response_once = True
        assert self.drain() == drain.FAILED
        assert len(self.adapter.applied) == 1
        assert len(entries(self.device, "lacp")) >= 1

        assert expire_claim(self.device, "lacp") is False
        assert self.drain() == drain.SUCCEEDED
        config, session = self.adapter.patches()
        with config, session:
            drain.drain_intent_outbox()

        assert len(self.adapter.applied) == 2
        assert self.adapter.applied[0][1] == self.adapter.applied[1][1]
        assert entries(self.device, "lacp") == []

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
    """Only a validated prepared envelope acknowledges a switching snapshot."""

    tag = "envelope"
    adapter_device_id = 7802

    def test_an_error_envelope_is_journaled_and_never_retired_as_a_success(self):
        from netbox_nso_plugin import drain

        enqueue(self.device, "switchport")
        self.adapter._respond = lambda body: {"status": "error", "message": "the device refused the apply"}

        assert self.drain("switchport") == drain.FAILED

        recorded = self.errors().get("switchport") or {}
        assert "not acknowledged" in recorded.get("message", ""), self.errors()
        assert len(entries(self.device, "switchport")) >= 1
        assert state_of(self.device, "switchport").push_seq is None

    def test_a_deployed_envelope_does_not_prove_preparation(self):
        from netbox_nso_plugin import drain

        enqueue(self.device, "lacp")
        self.adapter._respond = lambda body: {"status": "deployed", "device": "nso-cl-envelope"}

        assert self.drain() == drain.FAILED
        assert len(entries(self.device, "lacp")) >= 1


class TestTheBurstStillCoalesces(_DirectApplyCase):
    """The split takes the claim away, not the outbox: one send per burst, from the trigger."""

    tag = "burst"
    adapter_device_id = 7803

    def test_switching_preparation_carries_revision_and_explicit_empty_deletions(self):
        from netbox_nso_plugin import drain

        enqueue(self.device, "lacp")
        assert self.drain("lacp") == drain.SUCCEEDED
        request = self.adapter.requests[-1]
        assert request["body"]["deleted_roots"] == []
        assert isinstance(request["body"]["source_revision"], int)
        assert request["params"].get("delete_origin") is None

    def _owned_bundle(self):
        from netbox_nso_plugin.models import NSOLACPBundleState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        with without_commit_drain(), transaction.atomic():
            # Auto mode prepares the LAG snapshot after the writer commits.
            self.mgmt.auto_apply = True
            self.mgmt.save(update_fields=["auto_apply"])
            lag = Interface.objects.create(device=self.device, name="Port-channel1", type="lag")
            bundle = NSOLACPBundleState(
                management=self.mgmt, interface=lag, lag_id=1, min_links=2, timer="fast", status="accepted"
            )
            plan = RendererMutationPlan.build(
                saves=(planned_save(bundle, force_insert=True, natural_key=("management", "interface")),)
            )
            with renderer_writes(plan) as writer:
                writer.save(bundle, force_insert=True)
            return bundle

    def test_n_saves_in_one_transaction_reach_the_device_once(self):
        bundle = self._owned_bundle()
        config, session = self.adapter.patches()

        with config, session, transaction.atomic():
            from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

            for min_links in (3, 4, 5):
                candidate = copy.copy(bundle)
                candidate.min_links = min_links
                plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("min_links",)),))
                with renderer_writes(plan) as writer:
                    writer.save(candidate, update_fields=("min_links",))
                bundle.min_links = min_links

        assert len(self.adapter.requests) == 1, self.adapter.requests
        assert self.adapter.requests[0]["push_seq"] is None
        assert entries(self.device, "lacp") == [], "the entries retire after a validated preparation"


class TestRepairContributionsAreNeutralForDirectApply(_DirectApplyCase):
    tag = "directrepair"
    adapter_device_id = 7804

    def test_a_repair_cannot_turn_a_legacy_boolean_into_root_identity(self):
        from netbox_nso_plugin import drain, outbox

        enqueue(self.device, "lacp", delete_origin=True)
        enqueue(self.device, "lacp", kind=outbox.CONTRIBUTION_KIND_REPAIR)

        assert self.drain("lacp") == drain.SUCCEEDED
        assert self.adapter.requests[-1]["body"]["deleted_roots"] == []
        assert self.adapter.requests[-1]["params"].get("delete_origin") is None

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
