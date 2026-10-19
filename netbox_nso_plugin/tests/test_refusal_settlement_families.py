# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Settle independent contributors through real renderer capture and HTTP admission."""

import copy
from uuid import UUID

from dcim.models import Interface
from django.db import transaction
from django.urls import reverse
from ipam.models import VLAN, VLANGroup
from netbox_routing.models import ISISInstance, ISISLevel

from netbox_nso_plugin.models import (
    NSOApplyAttempt,
    NSOISISInstanceState,
    NSOLACPBundleState,
    NSOLACPMemberState,
    NSOSwitchportState,
)
from netbox_nso_plugin.refusal_registry import OVERLAYS

from ._outbox_case import without_commit_drain
from ._ownership_case import acquire_overlay, save_overlay_fixture
from .test_apply_selector import _promoted
from .test_durable_bgp_capture import BGPCaptureCase


class TestRefusalSettlementFamilies(BGPCaptureCase):
    def apply_owners(self, owners):
        self.owners = owners
        self.prior = {}
        for row in owners:
            self.bookkeeping(row)
            row.refresh_from_db()
            self.prior[(row._meta.label_lower, row.pk)] = (
                row.status,
                row.last_apply_at,
                copy.deepcopy(row.last_apply_error),
                row.apply_attempt_id,
            )
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

    def evidence(self, refused=None, dispositions=None):
        import json
        from pathlib import Path

        catalog = json.loads((Path(__file__).parent / "fixtures/refusal_settlement.json").read_text(encoding="utf-8"))[
            "bgp"
        ]["refusal"]
        outcomes, attributed, revisions, sources = [], [], {}, {}
        for snapshot in self.attempt.overlay_snapshots.all():
            for contributor in snapshot.contributors["rows"]:
                stream = contributor["stream"]
                selector = contributor["source_selector"]
                source = selector.get("source_push_seq")
                revision = selector.get("selection_revision", 7)
                revisions[stream], sources[stream] = revision, source
                ref, parent = contributor["logical_ref"], contributor["carrier_ref"]
                carrier = {
                    "stream": stream,
                    "row": {key: parent[key] for key in ("subject", "key")},
                    "source_push_seq": source,
                    "frozen_revision": revision,
                    "accepted_at": self.stamp.isoformat(),
                    "generation_id": 81,
                    "settlement_cohort": 9,
                }
                identity = {key: value for key, value in carrier.items() if key not in {"row", "accepted_at"}}
                identity.update(
                    row={key: ref[key] for key in ("subject", "key")},
                    acceptance_identity=self.stamp.isoformat(),
                    carrier=carrier,
                )
                failed = refused is not None and (snapshot.model_label, snapshot.overlay_pk) == (
                    refused._meta.label_lower,
                    refused.pk,
                )
                disposition = (dispositions or {}).get(ref["subject"], "refused" if failed else "not_applied")
                causes = []
                if disposition == "refused":
                    cause = {**catalog, "family": ref["family"], "row": identity["row"]}
                    causes.append(cause)
                    attributed.append(
                        {
                            **cause,
                            "logical_rows": [copy.deepcopy(identity)],
                            "provenance": "current",
                            "sender_phase": "commit",
                        }
                    )
                outcomes.append({**identity, "disposition": disposition, "refusals": causes})
        priority = {"in_sync": 0, "not_applied": 1, "unavailable": 2, "refused": 3}
        carriers = {}
        for outcome in outcomes:
            carrier = outcome["carrier"]
            key = (carrier["stream"], carrier["row"]["subject"], carrier["row"]["key"])
            existing = carriers.get(key)
            if existing is None or priority[outcome["disposition"]] > priority[existing["disposition"]]:
                carriers[key] = {**carrier, "disposition": outcome["disposition"]}
        counts = {}
        for carrier in carriers.values():
            spec = next(spec for spec in OVERLAYS.values() if carrier["row"]["subject"] in spec.subjects)
            key = spec.counter_keys[0] if len(spec.counter_keys) == 1 else f"{carrier['stream']}_count_by_outcome"
            count = counts.setdefault(key, dict.fromkeys(("in_sync", "apply_failed", "not_applied", "unavailable"), 0))
            count["apply_failed" if carrier["disposition"] == "refused" else carrier["disposition"]] += 1
        result = {
            **counts,
            "row_outcomes": outcomes,
            "refusals": attributed,
            "settlement_owners": list(carriers.values()),
            "commit_acknowledged": not attributed,
        }
        return {
            "generation_id": 81,
            "seq": 4,
            "status": "failed" if attributed else "settled",
            "sections": sorted(revisions),
            "stream_revisions": revisions,
            "source_push_seq": sources,
            "carrier_job_id": 501,
            "carrier_job_status": "succeeded",
            "carrier_job_result": result,
            "carrier_job_error": None,
            "row_outcomes": outcomes,
            "refusals": attributed,
            "updated_at": self.stamp.isoformat(),
        }

    def settle(self, generation):
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        return settle_terminal_generation(
            self.management, generation, "placeholder-store", attempt=self.attempt, settle_seq=1
        )

    def assert_restored(self, row):
        row.refresh_from_db()
        self.assertEqual(
            (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
            self.prior[(row._meta.label_lower, row.pk)],
        )

    def test_lag_member_refusal_preserves_other_member(self):
        with without_commit_drain(), transaction.atomic():
            interface = Interface.objects.create(device=self.device, name="Port-channel71", type="lag")
            bundle = save_overlay_fixture(
                NSOLACPBundleState(
                    management=self.management,
                    interface=interface,
                    lag_id=71,
                    status="accepted",
                    accepted_at=self.stamp,
                )
            )
            members = []
            for name in ("Ethernet1", "Ethernet2"):
                member = Interface.objects.create(device=self.device, name=name, type="1000base-t", lag=interface)
                members.append(
                    save_overlay_fixture(
                        NSOLACPMemberState(
                            management=self.management, interface=member, status="accepted", accepted_at=self.stamp
                        )
                    )
                )
        self.apply_owners([bundle, *members])
        execution = self.settle(self.evidence(refused=members[0]))
        members[0].refresh_from_db()
        self.assertEqual(members[0].status, "apply_failed")
        self.assert_restored(bundle)
        self.assert_restored(members[1])
        self.assertEqual(execution.terminal_projection["carrier_job_result"]["lag_count_by_outcome"]["apply_failed"], 1)

    def test_isis_levels_reduce_to_instance_without_false_success(self):
        with without_commit_drain(), transaction.atomic():
            native = ISISInstance.objects.create(
                device=self.device, process_tag="placeholder-process", net="49.0001.0000.0000.0001.00"
            )
            ISISLevel.objects.create(instance=native, level=1, disabled=True)
            ISISLevel.objects.create(instance=native, level=2, wide_metrics_only=True)
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
        self.assertEqual(len(generation["row_outcomes"]), 3)
        execution = self.settle(generation)
        self.assert_restored(row)
        self.assertEqual(execution.overlay_snapshots.get().settlement_disposition, "unavailable")

    def test_switchport_tags_share_one_owner(self):
        with without_commit_drain(), transaction.atomic():
            group = VLANGroup.objects.create(name="placeholder-tags", slug="placeholder-tags")
            first = VLAN.objects.create(group=group, vid=71, name="placeholder-first")
            second = VLAN.objects.create(group=group, vid=72, name="placeholder-second")
            interface = Interface.objects.create(
                device=self.device, name="Ethernet71", type="1000base-t", mode="tagged"
            )
            interface.tagged_vlans.add(first, second)
            row = acquire_overlay(
                NSOSwitchportState, management=self.management, interface=interface, mode="trunk", status="accepted"
            )
        self.apply_owners([row])
        generation = self.evidence(refused=row)
        self.assertEqual(len(generation["row_outcomes"]), 1)
        execution = self.settle(generation)
        self.assertEqual(execution.overlay_snapshots.count(), 1)
        self.assertEqual(set(interface.tagged_vlans.values_list("vid", flat=True)), {71, 72})
        row.refresh_from_db()
        self.assertEqual(row.status, "apply_failed")
