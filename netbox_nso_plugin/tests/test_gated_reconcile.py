# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""READSEM S4 Slice B3 — the gate plumbed into BOTH reconcile paths (D9).

Red-first behavior list from the ratified plan: unavailable KEEPS rows (fails
pre-gate), authoritative-empty CLEARS, ``result=error`` fails closed, missing
``read_state`` key = legacy (body runs), present-stale replaces, strictly-older
attempt skips, the IS-IS document gets ONE gate decision driving BOTH bodies,
a behavioral no-bypass sweep (all families skip ⇒ ZERO reconciler bodies run;
all admit ⇒ every body exactly once), web-busy fail-fast + RQ marker-deferral +
redis-down fail-closed dispositions, the interfaces fetch moving to the S4
``interfaces-doc``, and the category view rendering persisted rows on a skip.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOL2SapState

from ._outbox_case import content_update, mirror_update

User = get_user_model()

_INC = ("55555555-eeee-4eee-8eee-555555555555", "2026-07-02T00:00:00Z")


def _rs(
    outcome="present",
    reason=None,
    freshness="fresh",
    result="replaced",
    succeeded=True,
    attempt_id=1,
):
    return {
        "outcome": outcome,
        "reason": reason,
        "freshness": freshness,
        "result": result,
        "succeeded": succeeded,
        "read_at": "2026-07-21T10:00:00Z",
        "attempt_id": attempt_id,
        "incarnation": _INC[0],
        "incarnation_born": _INC[1],
    }


def _l2_payload(names=("TL",), read_state=None):
    doc = {
        "services": [
            {
                "service_name": n,
                "service_type": "epipe",
                "service_id": 4000 + i,
                "saps": [{"sap_id": f"lag-60:{390 + i}", "port": "lag-60", "outer_tag": 390 + i, "inner_tag": None}],
            }
            for i, n in enumerate(names)
        ]
    }
    if read_state is not None:
        doc["read_state"] = read_state
    return doc


def _make(tag, **mgmt_flags):
    mfg = Manufacturer.objects.create(name=f"{tag}Mfg", slug=f"{tag}mfg")
    dt = DeviceType.objects.create(manufacturer=mfg, model=f"{tag}Dev", slug=f"{tag}dev")
    role = DeviceRole.objects.create(name=f"{tag}Role", slug=f"{tag}role")
    site = Site.objects.create(name=f"{tag}Site", slug=f"{tag}site")
    device = Device.objects.create(name=f"{tag}-rtr", device_type=dt, role=role, site=site)
    inst = NSOInstance.objects.create(name=f"{tag}-inst", adapter_instance_id=f"{tag}-inst")
    mgmt = NSODeviceManagement.objects.create(
        device=device,
        nso_instance=inst,
        nso_device_name=f"{tag}-rtr",
        adapter_device_id=device.pk,
        **mgmt_flags,
    )
    Interface.objects.create(device=device, name="lag-60", type="lag")
    return device, mgmt


def _sap_names(mgmt):
    return sorted(NSOL2SapState.objects.filter(management=mgmt).values_list("service_name", flat=True))


class _L2Base(TestCase):
    """Drive the l2_service family through reconcile_category('l2_services')."""

    def setUp(self):
        self.device, self.mgmt = _make(f"gr{uuid.uuid4().hex[:6]}", manage_l2=True)

    def _reconcile(self, doc):
        from netbox_nso_plugin.reconcile import reconcile_category

        with patch("netbox_nso_plugin.adapter_client.get_l2_services", return_value=doc):
            return reconcile_category(self.device, self.mgmt, "l2_services")

    def _prime(self, attempt_id=1):
        ctx = self._reconcile(_l2_payload(("TL",), read_state=_rs(attempt_id=attempt_id)))
        assert _sap_names(self.mgmt) == ["TL"], "priming reconcile must materialize the row"
        return ctx


class TestGatedReconcileBehavior(_L2Base):
    def test_unavailable_keeps_rows(self):
        self._prime()
        ctx = self._reconcile(
            _l2_payload(
                (),
                read_state=_rs(
                    outcome="unavailable", reason="export_down", result="kept", succeeded=False, attempt_id=2
                ),
            )
        )
        self.assertEqual(_sap_names(self.mgmt), ["TL"])  # rows survive the outage
        self.assertEqual(ctx["_gate"]["l2_service"], "skipped_unavailable")
        row = NSOL2SapState.objects.get(management=self.mgmt, service_name="TL")
        self.assertNotEqual(row.status, "error")  # a skip is NOT a scope fault

    def test_result_error_fails_closed(self):
        self._prime()
        ctx = self._reconcile(_l2_payload((), read_state=_rs(result="error", succeeded=False, attempt_id=2)))
        self.assertEqual(_sap_names(self.mgmt), ["TL"])
        self.assertEqual(ctx["_gate"]["l2_service"], "skipped_unavailable")

    def test_authoritative_empty_runs_body_and_drifts_rows(self):
        """D3: absence-of-rows in an AUTHORITATIVE payload legitimately drifts — the
        body runs and applies today's per-family lifecycle (l2: absent → 'changed'),
        unlike unavailable (body skipped, row stays 'imported' untouched)."""
        self._prime()
        ctx = self._reconcile(
            _l2_payload(
                (), read_state=_rs(outcome="absent_authoritative", freshness=None, result="cleared", attempt_id=2)
            )
        )
        self.assertEqual(ctx["_gate"]["l2_service"], "ran")
        row = NSOL2SapState.objects.get(management=self.mgmt, service_name="TL")
        self.assertEqual(row.status, "changed")  # drifted by the ran body, not kept

    def test_present_stale_replaces(self):
        self._prime()
        ctx = self._reconcile(_l2_payload(("NEW",), read_state=_rs(freshness="stale", attempt_id=2)))
        self.assertIn("NEW", _sap_names(self.mgmt))  # degraded-success still replaces
        self.assertEqual(ctx["_gate"]["l2_service"], "ran")

    def test_matching_read_does_not_settle_a_deploying_sap(self):
        from netbox_nso_plugin.models import NSOApplyAttempt

        self._prime()
        row = NSOL2SapState.objects.get(management=self.mgmt, service_name="TL")
        attempt = NSOApplyAttempt.objects.create(management=self.mgmt)
        content_update(row, status="deploying", apply_attempt_id=attempt.pk)

        self._reconcile(_l2_payload(("TL",), read_state=_rs(attempt_id=2)))

        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_a_vanished_confirmed_sap_repends_a_deploying_sibling(self):
        """A vanished confirmed SAP bears content, so every deploying row in the scope is stale."""
        from netbox_nso_plugin.models import NSOApplyAttempt, NSOIntentRevision

        self._reconcile(_l2_payload(("TL", "TL2"), read_state=_rs(attempt_id=1)))
        deploying = NSOL2SapState.objects.get(management=self.mgmt, service_name="TL")
        confirmed = NSOL2SapState.objects.get(management=self.mgmt, service_name="TL2")
        content_update(deploying, status="accepted")
        content_update(confirmed, status="in_sync")
        attempt = NSOApplyAttempt.objects.create(management=self.mgmt)
        # Marked LAST, and lifecycle-only: a sibling content write re-pends a deploying row.
        mirror_update(deploying, status="deploying", apply_attempt_id=attempt.pk)
        revision = NSOIntentRevision.objects.get(device=self.device, scope="l2_sap")
        before = revision.revision
        deploying.refresh_from_db()
        self.assertEqual((deploying.status, deploying.apply_attempt_id), ("deploying", attempt.pk))

        self._reconcile(_l2_payload(("TL",), read_state=_rs(attempt_id=2)))  # TL2 vanishes

        deploying.refresh_from_db()
        confirmed.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(deploying.status, "accepted")
        self.assertIsNone(deploying.apply_attempt_id)
        self.assertEqual(confirmed.status, "changed")
        self.assertEqual(revision.revision, before + 1)

    def test_missing_read_state_key_is_legacy_and_runs(self):
        self._prime()
        ctx = self._reconcile(_l2_payload(("NEW",)))  # pre-S4 adapter: no read_state
        self.assertIn("NEW", _sap_names(self.mgmt))
        self.assertEqual(ctx["_gate"]["l2_service"], "legacy")

    def test_explicit_null_read_state_fails_closed_not_legacy(self):
        """codex B5-F4: `"read_state": null` in an S4 response is malformed — it must
        fail CLOSED (rows kept, body skipped), never fall back to legacy semantics
        that would happily drift/replace rows from a defective response."""
        self._prime()
        doc = _l2_payload(("NEW",))
        doc["read_state"] = None  # explicit null — distinct from an absent key
        ctx = self._reconcile(doc)
        self.assertEqual(_sap_names(self.mgmt), ["TL"])  # rows untouched
        self.assertEqual(ctx["_gate"]["l2_service"], "skipped_unavailable")

    def test_strictly_older_attempt_skips(self):
        self._prime(attempt_id=5)
        ctx = self._reconcile(_l2_payload(("OTHER",), read_state=_rs(attempt_id=4)))
        self.assertEqual(_sap_names(self.mgmt), ["TL"])  # the older payload lost
        self.assertEqual(ctx["_gate"]["l2_service"], "skipped_stale_attempt")


class TestIsisCompoundGate(TestCase):
    """R3-6: ONE isis document → ONE gate decision → both bodies (or neither)."""

    def setUp(self):
        self.device, self.mgmt = _make(f"gi{uuid.uuid4().hex[:6]}", manage_routing=True, manage_isis=True)

    def _reconcile(self, doc):
        from netbox_nso_plugin.reconcile import reconcile_category

        with (
            patch("netbox_nso_plugin.adapter_client.get_isis_interfaces", return_value=doc),
            patch(
                "netbox_nso_plugin.isis_reconciler.reconcile_isis",
                return_value={"interfaces": [], "processes": []},
            ) as reconcile,
        ):
            ctx = reconcile_category(self.device, self.mgmt, "isis")
        return ctx, reconcile

    def test_admit_runs_both_bodies_exactly_once(self):
        doc = {"interfaces": [], "processes": [], "read_state": _rs()}
        ctx, reconcile = self._reconcile(doc)
        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(ctx["_gate"]["isis"], "ran")

    def test_skip_runs_zero_bodies(self):
        doc = {
            "interfaces": [],
            "processes": [],
            "read_state": _rs(outcome="unavailable", reason="not_ready", result=None, succeeded=None),
        }
        ctx, reconcile = self._reconcile(doc)
        self.assertEqual(reconcile.call_count, 0)
        self.assertEqual(ctx["_gate"]["isis"], "skipped_unavailable")


class TestRealReconcilerGateFootprints(TestCase):
    """Run registered overlay writers through their production read gates."""

    def setUp(self):
        self.device, self.mgmt = _make(
            f"gf{uuid.uuid4().hex[:6]}",
            manage_interfaces=True,
            manage_routing=True,
            manage_bgp=True,
        )

    def test_lacp_gate_covers_bundle_and_member_rows(self):
        from netbox_nso_plugin.models import NSOLACPBundleState, NSOLACPMemberState
        from netbox_nso_plugin.reconcile import reconcile_category

        lag = Interface.objects.create(device=self.device, name="Port-channel1", type="lag")
        member_iface = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")
        payload = {
            "bundles": [
                {
                    "name": "Port-channel1",
                    "lag_id": 1,
                    "members": [{"interface_name": "Ethernet1", "mode": "active"}],
                }
            ],
            "read_state": _rs(),
        }

        with patch("netbox_nso_plugin.adapter_client.get_lag_config", return_value=payload):
            ctx = reconcile_category(self.device, self.mgmt, "lacp")

        self.assertEqual(ctx["_gate"]["lag_config"], "ran")
        bundle = NSOLACPBundleState.objects.get(management=self.mgmt, interface=lag)
        self.assertEqual(bundle.lag_id, 1)
        member = NSOLACPMemberState.objects.get(management=self.mgmt, interface=member_iface)
        self.assertEqual(member.lag_bundle_id, lag.pk)  # the LAG Interface, not the bundle overlay
        self.assertEqual(member.mode, "active")

    def test_bgp_gate_covers_the_materialized_graph_and_overlay(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import BGPPeer, BGPRouter, BGPScope

        from netbox_nso_plugin.models import NSOBGPPeerState
        from netbox_nso_plugin.reconcile import reconcile_category

        payload = self._bgp_payload()

        with patch("netbox_nso_plugin.adapter_client.get_bgp_config", return_value=payload):
            ctx = reconcile_category(self.device, self.mgmt, "bgp")

        self.assertEqual(ctx["_gate"]["bgp"], "ran")
        router = BGPRouter.objects.get(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=self.device.pk,
        )
        self.assertEqual(router.assigned_object, self.device)  # both halves of the generic FK
        self.assertEqual(router.asn.asn, 64512)
        scope = BGPScope.objects.get(router=router)
        self.assertIsNone(scope.vrf_id)  # the default VRF
        peer = BGPPeer.objects.get(scope=scope)
        self.assertEqual(str(peer.peer.address.ip), "198.18.0.1")
        self.assertEqual(peer.remote_as.asn, 64513)
        state = NSOBGPPeerState.objects.get(management=self.mgmt)
        self.assertEqual(state.bgp_peer_id, peer.pk)

    def test_bfd_gate_covers_native_and_overlay_creations(self):
        from netbox_routing.models import BFDInterface

        from netbox_nso_plugin.models import NSOBFDInterfaceState
        from netbox_nso_plugin.reconcile import reconcile_category

        interface = Interface.objects.create(device=self.device, name="Port-channel1", type="lag")
        payload = {
            "interfaces": [
                {
                    "interface_name": interface.name,
                    "micro_bfd": True,
                    "enabled": True,
                    "min_tx": 300,
                    "min_rx": 300,
                    "multiplier": 3,
                }
            ],
            "read_state": _rs(),
        }

        with patch("netbox_nso_plugin.adapter_client.get_bfd", return_value=payload):
            ctx = reconcile_category(self.device, self.mgmt, "bfd")

        self.assertEqual(ctx["_gate"]["bfd"], "ran")
        native = BFDInterface.objects.get(interface=interface)
        self.assertTrue(native.micro_bfd)
        self.assertTrue(native.enabled)
        profile = native.bfd_profile
        self.assertIsNotNone(profile)
        self.assertEqual(profile.name, "bfd-300-300-x3")  # shared, deduped by its timer-set
        self.assertEqual((profile.min_tx_int, profile.min_rx_int, profile.multiplier), (300, 300, 3))
        state = NSOBFDInterfaceState.objects.get(management=self.mgmt, interface=interface)
        self.assertEqual(state.status, "imported")

    def test_bgp_gate_predicts_an_owned_native_peer_change(self):
        from netbox_nso_plugin.models import NSOBGPPeerState, NSOIntentRevision
        from netbox_nso_plugin.reconcile import reconcile_category

        from ._outbox_case import content_update

        with patch("netbox_nso_plugin.adapter_client.get_bgp_config", return_value=self._bgp_payload()):
            reconcile_category(self.device, self.mgmt, "bgp")
        state = NSOBGPPeerState.objects.get(management=self.mgmt)
        content_update(state, status="accepted")
        revision = NSOIntentRevision.objects.get(device=self.device, scope="bgp")
        before = revision.revision

        with patch(
            "netbox_nso_plugin.adapter_client.get_bgp_config",
            return_value=self._bgp_payload(ttl=2, attempt_id=2),
        ):
            reconcile_category(self.device, self.mgmt, "bgp")

        state.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(state.bgp_peer.ttl, 2)
        self.assertEqual(revision.revision, before + 1)

    @staticmethod
    def _bgp_payload(*, ttl=None, attempt_id=1):
        peer = {
            "peer_address": "198.18.0.1",
            "remote_as": "64513",
            "enabled": True,
            "address_families": [{"af": "ipv4-unicast", "enabled": True}],
        }
        if ttl is not None:
            peer["ttl"] = ttl
        return {
            "routers": [
                {
                    "asn": "64512",
                    "scopes": [
                        {
                            "vrf": "",
                            "address_families": ["ipv4-unicast"],
                            "peers": [peer],
                        }
                    ],
                }
            ],
            "read_state": _rs(attempt_id=attempt_id),
        }


class TestOptionalRoutingDependencyPlans(TestCase):
    """Preflight keeps the reconcilers' optional dependency boundary."""

    def test_missing_netbox_routing_returns_empty_plans(self):
        from django.apps import apps

        from netbox_nso_plugin.bfd_reconciler import bfd_reconcile_plan
        from netbox_nso_plugin.bgp_reconciler import bgp_reconcile_plan
        from netbox_nso_plugin.intent_state import MutationFootprint
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.route_policy_reconciler import route_policy_reconcile_plan
        from netbox_nso_plugin.template_content import static_route_reconcile_plan

        device, _management = _make("missing-routing")
        Interface.objects.create(device=device, name="Ethernet1", type="1000base-t")
        bgp_payload = {
            "routers": [
                {
                    "asn": "64512",
                    "scopes": [
                        {
                            "vrf": "",
                            "peers": [{"peer_address": "198.18.0.1", "remote_as": "64513"}],
                        }
                    ],
                }
            ]
        }
        planners = (
            (
                bfd_reconcile_plan,
                [
                    {
                        "interface_name": "Ethernet1",
                        "min_tx": 300,
                        "min_rx": 300,
                        "multiplier": 3,
                    }
                ],
            ),
            (bgp_reconcile_plan, bgp_payload),
            (route_policy_reconcile_plan, {"prefix_lists": [{"name": "PL", "entries": []}]}),
            (
                static_route_reconcile_plan,
                {"routes": [{"prefix": "198.18.0.0/15", "next_hop": "198.18.0.1", "metric": 1}]},
            ),
        )

        config = apps.get_app_config("netbox_nso_plugin")
        with patch.object(config, "_static_route_auto_create", True):
            for planner, payload in planners:
                with self.subTest(control=planner.__name__):
                    self.assertTrue(planner(device, payload).write_set)

        with patch.dict(sys.modules, {"netbox_routing.models": None}):
            for planner, payload in planners:
                with self.subTest(planner=planner.__name__):
                    plan = planner(device, payload)
                    self.assertIsInstance(plan, RendererMutationPlan)
                    self.assertEqual(plan.write_set, ())
                    self.assertEqual(plan.lock_footprint, MutationFootprint())
                    self.assertFalse(plan.changes_content)


#: every family fetcher reconcile_device consumes, with a minimal doc shape.
_DEVICE_FETCHERS = {
    "get_interfaces_doc": {"device_id": 0, "interfaces": []},
    "get_state": {},
    "get_svi": {"svis": []},
    "get_subinterface": {"subinterfaces": []},
    "get_interface_mtu": {"interfaces": []},
    "get_interface_ips": {"interfaces": []},
    "get_lag_config": {"bundles": []},
    "get_vlan_database": {"vlans": []},
    "get_switchport": {"interfaces": []},
    "get_snmp_config": {"communities": []},
    "get_logging_config": {"hosts": []},
    "get_l2_services": {"services": []},
    "get_static_routes": {"routes": []},
    "get_isis_interfaces": {"interfaces": [], "processes": []},
    "get_route_policy": {"route_maps": []},
    "get_ospf": {"instances": [], "interfaces": []},
    "get_bgp_config": {"routers": []},
    "get_bfd": {"interfaces": []},
    "get_redistribution": {"entries": []},
}

_ALL_SCOPES = {
    "manage_interfaces": True,
    "manage_snmp": True,
    "manage_logging": True,
    "manage_l2": True,
    "manage_routing": True,
    "manage_static": True,
    "manage_isis": True,
    "manage_ospf": True,
    "manage_bgp": True,
    "manage_route_policy": True,
    "manage_redistribution": True,
}


class TestRealBodyGateSweep(TestCase):
    """Exercise real reconciler bodies through each gate plan shape."""

    def setUp(self):
        self.device, self.mgmt = _make(f"gn{uuid.uuid4().hex[:6]}", **_ALL_SCOPES)
        Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")

    def _run(self, read_state):
        from contextlib import ExitStack

        from netbox_nso_plugin.reconcile import reconcile_device

        with ExitStack() as stack:
            for fetcher, shape in _DEVICE_FETCHERS.items():
                doc = dict(shape)
                if fetcher == "get_interface_mtu":
                    doc["interfaces"] = [
                        {
                            "interface_name": "Ethernet1",
                            "mtu": 9100,
                            "ip_mtu": 9000,
                            "mpls_mtu": 9088,
                        }
                    ]
                elif fetcher == "get_l2_services":
                    doc = _l2_payload(("gate-sweep-l2",))
                elif fetcher == "get_route_policy":
                    doc["prefix_lists"] = [{"name": "GATE-SWEEP-PREFIX", "entries": []}]
                if fetcher != "get_state" and read_state is not None:
                    doc["read_state"] = read_state
                stack.enter_context(patch(f"netbox_nso_plugin.adapter_client.{fetcher}", return_value=doc))
            return reconcile_device(self.device, self.mgmt)

    def test_all_unavailable_skips_every_real_body(self):
        from netbox_nso_plugin.models import NSOInterfaceMtuState, NSORoutePolicyState

        ctx = self._run(_rs(outcome="unavailable", reason="export_down", result="kept", succeeded=False))
        from netbox_nso_plugin.reconcile import _enabled_device_families

        for family in _enabled_device_families(self.mgmt):
            self.assertEqual(ctx["_gate"].get(family), "skipped_unavailable", family)
        self.assertFalse(NSOInterfaceMtuState.objects.filter(management=self.mgmt).exists())
        self.assertFalse(NSOL2SapState.objects.filter(management=self.mgmt).exists())
        self.assertFalse(NSORoutePolicyState.objects.filter(management=self.mgmt).exists())

    def test_all_present_runs_real_bodies_for_every_plan_shape(self):
        from netbox_nso_plugin.models import NSOInterfaceMtuState, NSORoutePolicyState

        ctx = self._run(_rs())
        from netbox_nso_plugin.reconcile import _enabled_device_families

        for family in _enabled_device_families(self.mgmt):
            self.assertEqual(ctx["_gate"].get(family), "ran", family)
        mtu = NSOInterfaceMtuState.objects.get(management=self.mgmt, interface__name="Ethernet1")
        self.assertEqual((mtu.l2_mtu, mtu.ip_mtu, mtu.mpls_mtu), (9100, 9000, 9088))
        self.assertTrue(NSOL2SapState.objects.filter(management=self.mgmt, service_name="gate-sweep-l2").exists())
        self.assertTrue(
            NSORoutePolicyState.objects.filter(management=self.mgmt, object_name="GATE-SWEEP-PREFIX").exists()
        )

    def test_interfaces_fetch_uses_the_s4_doc(self):
        """The bare-list get_interfaces must no longer be reconcile's source."""
        with patch("netbox_nso_plugin.adapter_client.get_interfaces") as legacy:
            self._run(_rs())
        legacy.assert_not_called()


class TestDefaultPlanContentMutation(TestCase):
    """Exercise an owned-fragment change through a real default-plan body."""

    def test_late_stale_bfd_transition_repends_a_row_settled_earlier_in_the_body(self):
        from netbox_nso_plugin.models import NSOBFDInterfaceState
        from netbox_nso_plugin.reconcile import reconcile_category

        device, mgmt = _make(f"gl{uuid.uuid4().hex[:6]}", manage_routing=True, manage_bgp=True)
        deploying_interface = Interface.objects.create(device=device, name="Ethernet2", type="1000base-t")
        confirmed_interface = Interface.objects.create(device=device, name="Ethernet3", type="1000base-t")
        deploying = NSOBFDInterfaceState.objects.create(
            management=mgmt,
            interface=deploying_interface,
            min_tx=300,
            min_rx=300,
            multiplier=3,
            status="accepted",
        )
        confirmed = NSOBFDInterfaceState.objects.create(
            management=mgmt,
            interface=confirmed_interface,
            min_tx=300,
            min_rx=300,
            multiplier=3,
            status="in_sync",
        )
        mirror_update(deploying, status="deploying", apply_attempt_id=uuid.uuid4())
        document = {
            "interfaces": [
                {
                    "interface_name": deploying_interface.name,
                    "min_tx": 300,
                    "min_rx": 300,
                    "multiplier": 3,
                }
            ],
            "read_state": _rs(),
        }

        with patch("netbox_nso_plugin.adapter_client.get_bfd", return_value=document):
            context = reconcile_category(device, mgmt, "bfd")

        deploying.refresh_from_db()
        confirmed.refresh_from_db()
        self.assertEqual(context["_gate"]["bfd"], "ran")
        self.assertEqual(confirmed.status, "changed")
        self.assertEqual(deploying.status, "accepted")
        self.assertIsNone(deploying.apply_attempt_id)

    def test_stale_bfd_drift_repends_the_complete_scope(self):
        from netbox_nso_plugin.models import NSOBFDInterfaceState
        from netbox_nso_plugin.reconcile import reconcile_category

        device, mgmt = _make(f"gd{uuid.uuid4().hex[:6]}", manage_routing=True, manage_bgp=True)
        confirmed_interface = Interface.objects.create(device=device, name="Ethernet2", type="1000base-t")
        deploying_interface = Interface.objects.create(device=device, name="Ethernet3", type="1000base-t")
        confirmed = NSOBFDInterfaceState.objects.create(
            management=mgmt,
            interface=confirmed_interface,
            min_tx=300,
            min_rx=300,
            multiplier=3,
            status="in_sync",
        )
        deploying = NSOBFDInterfaceState.objects.create(
            management=mgmt,
            interface=deploying_interface,
            min_tx=300,
            min_rx=300,
            multiplier=3,
            status="deploying",
            apply_attempt_id=uuid.uuid4(),
        )
        document = {"interfaces": [], "read_state": _rs()}

        with patch("netbox_nso_plugin.adapter_client.get_bfd", return_value=document):
            context = reconcile_category(device, mgmt, "bfd")

        confirmed.refresh_from_db()
        deploying.refresh_from_db()
        self.assertEqual(context["_gate"]["bfd"], "ran")
        self.assertEqual(confirmed.status, "changed")
        self.assertEqual(deploying.status, "accepted")
        self.assertIsNone(deploying.apply_attempt_id)


class TestContentionDispositions(TestCase):
    def setUp(self):
        self.device, self.mgmt = _make(f"gc{uuid.uuid4().hex[:6]}", manage_l2=True)

    def _hold_lease(self):
        import django_rq

        from netbox_nso_plugin.read_gate import DeviceReadLease, lease_key

        conn = django_rq.get_queue("default").connection
        holder = DeviceReadLease(conn, lease_key(self.mgmt.pk), ttl_s=30)
        assert holder.acquire()
        self.addCleanup(holder.release)
        return holder

    def test_web_busy_fails_fast_and_fetches_nothing(self):
        from netbox_nso_plugin.reconcile import reconcile_category

        self._hold_lease()
        with patch("netbox_nso_plugin.adapter_client.get_l2_services") as fetch:
            ctx = reconcile_category(self.device, self.mgmt, "l2_services")
        fetch.assert_not_called()
        self.assertEqual(ctx["_gate"]["l2_service"], "skipped_busy")

    def test_rq_contention_defers_with_marker(self):
        from netbox_nso_plugin import reconcile as reconcile_mod
        from netbox_nso_plugin.read_gate import marker_key

        self._hold_lease()
        import django_rq

        conn = django_rq.get_queue("default").connection
        self.addCleanup(conn.delete, marker_key(self.device.pk))
        with (
            patch.object(reconcile_mod, "_RQ_RETRY_BUDGET_S", 0.05),
            patch("netbox_nso_plugin.adapter_client.get_l2_services") as fetch,
        ):
            summary = reconcile_mod.run_device_reconcile(self.device.pk)
        fetch.assert_not_called()
        self.assertTrue(summary.get("deferred"))
        self.assertGreaterEqual(summary.get("attempts", 0), 1)
        self.assertIsNotNone(conn.get(marker_key(self.device.pk)))

    def test_redis_down_fails_closed_no_error_rows(self):
        from netbox_nso_plugin.read_gate import LockUnavailable
        from netbox_nso_plugin.reconcile import reconcile_category

        # seed a row through the ungated legacy path so we can prove no error-marking
        from netbox_nso_plugin.reconcile import reconcile_category as rc

        with patch("netbox_nso_plugin.adapter_client.get_l2_services", return_value=_l2_payload(("TL",))):
            rc(self.device, self.mgmt, "l2_services")
        with (
            patch(
                "netbox_nso_plugin.reconcile._acquire_reconcile_lease",
                side_effect=LockUnavailable("redis down"),
            ),
            patch("netbox_nso_plugin.adapter_client.get_l2_services") as fetch,
        ):
            ctx = reconcile_category(self.device, self.mgmt, "l2_services")
        fetch.assert_not_called()
        self.assertEqual(ctx["_gate"]["l2_service"], "skipped_lock_unavailable")
        row = NSOL2SapState.objects.get(management=self.mgmt, service_name="TL")
        self.assertNotEqual(row.status, "error")


class _ContendedStaleRow:
    """A persisted-shaped row whose status changes before each attempted row lock."""

    class DoesNotExist(Exception):
        pass

    pk = 37

    def __init__(self):
        self.status = "imported"
        self.last_sync_at = None
        self.deleted = False
        self.saved_fields = None
        self._statuses = iter(("accepted", "deploying", "in_sync", "apply_failed"))

    def refresh_from_db(self):
        self.status = next(self._statuses)

    def delete(self):
        self.deleted = True

    def save(self, update_fields=None):
        self.saved_fields = list(update_fields) if update_fields else None


_ContendedStaleRow._meta = SimpleNamespace(
    label_lower="netbox_nso_plugin.teststate",
    model=_ContendedStaleRow,
)


class TestStaleOverlayContention(SimpleTestCase):
    def test_repeated_contention_is_left_for_the_next_reconcile(self):
        from netbox_nso_plugin import status_machine as sm

        row = _ContendedStaleRow()
        with (
            patch("netbox_nso_plugin.intent_state.footprint_for_instance", return_value=object()),
            patch("netbox_nso_plugin.intent_state.reconcile_transaction", side_effect=lambda _plan: nullcontext()),
            self.assertLogs("netbox_nso_plugin.status_machine", level="WARNING") as logs,
        ):
            sm.finalise_stale_overlay(row, vestigial=False)

        self.assertEqual(row.status, sm.APPLY_FAILED)
        self.assertFalse(row.deleted)
        self.assertIsNone(row.saved_fields)
        self.assertIn("next reconcile will retry", logs.output[0])


class TestCategoryViewSkipFallback(TestCase):
    """D9: on a skip disposition the small-category view must render the PERSISTED
    rows (never an empty panel, never rows marked error)."""

    def setUp(self):
        self.device, self.mgmt = _make(f"gv{uuid.uuid4().hex[:6]}", manage_l2=True)
        self.user = User.objects.create_superuser(username=f"gv-{uuid.uuid4().hex[:6]}")
        self.client.force_login(self.user)

    def test_skip_renders_last_known_rows(self):
        from netbox_nso_plugin.reconcile import reconcile_category

        with patch(
            "netbox_nso_plugin.adapter_client.get_l2_services",
            return_value=_l2_payload(("TL",), read_state=_rs(attempt_id=1)),
        ):
            reconcile_category(self.device, self.mgmt, "l2_services")
        self.assertEqual(_sap_names(self.mgmt), ["TL"])

        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "l2_services"},
        )
        unavailable = _l2_payload(
            (),
            read_state=_rs(outcome="unavailable", reason="export_down", result="kept", succeeded=False, attempt_id=2),
        )
        with patch("netbox_nso_plugin.adapter_client.get_l2_services", return_value=unavailable):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("lag-60:390", html)  # last-known row STAYS VISIBLE
        self.assertEqual(_sap_names(self.mgmt), ["TL"])  # and was not cleared

    def test_a_quiesced_gate_renders_last_known_rows_not_a_500(self):
        """codex O3b review P2: a deployment window degrades a GET, it never breaks it."""
        from netbox_nso_plugin.deployment import quiesce, resume
        from netbox_nso_plugin.reconcile import reconcile_category

        with patch(
            "netbox_nso_plugin.adapter_client.get_l2_services",
            return_value=_l2_payload(("TL",), read_state=_rs(attempt_id=1)),
        ):
            reconcile_category(self.device, self.mgmt, "l2_services")

        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "l2_services"},
        )
        activated = quiesce()
        try:
            with patch(
                "netbox_nso_plugin.adapter_client.get_l2_services",
                return_value=_l2_payload(("TL",), read_state=_rs(attempt_id=2)),
            ) as live_read:
                resp = self.client.get(url, {"refresh": "1"})
        finally:
            if activated:
                resume()
        self.assertEqual(resp.status_code, 200)
        live_read.assert_not_called()  # the window blocks the live read, it never 500s
        html = resp.content.decode()
        self.assertIn("lag-60:390", html)  # persisted state renders under the banner
        self.assertIn("Intent deployment is temporarily unavailable. See the server log.", html)
