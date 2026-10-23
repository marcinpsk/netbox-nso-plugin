# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Settle independent contributors through real renderer capture and HTTP admission."""

from dcim.models import Interface
from django.db import transaction
from ipam.models import VLAN, VLANGroup
from netbox_routing.models import ISISInstance, ISISLevel

from netbox_nso_plugin.models import (
    NSOISISInstanceState,
    NSOLACPBundleState,
    NSOLACPMemberState,
    NSOSwitchportState,
)

from ._bgp_settlement import BGPSettlementHelpers
from ._outbox_case import without_commit_drain
from ._ownership_case import acquire_overlay, save_overlay_fixture
from .test_durable_bgp_capture import BGPCaptureCase


class TestRefusalSettlementFamilies(BGPSettlementHelpers, BGPCaptureCase):
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
