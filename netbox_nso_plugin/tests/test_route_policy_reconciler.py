# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for route_policy_reconciler.reconcile_route_policy.

Exercises the reconcile against the REAL netbox_routing models (PrefixList,
CommunityList, ASPath, RouteMap) so a model rename can't silently disable the
feature via the broad ImportError guard (which is exactly how the
ASPathAccessList->ASPath rename slipped through unnoticed).
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase


class TestReconcileRoutePolicy(TestCase):
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

    def test_community_invert_match_flip_is_drift_not_clobber(self):
        """A device-side invert_match flip diverges the content hash → the overlay
        goes to conflict and the stored value is NOT silently overwritten (brownfield
        no-clobber), exactly like an entries divergence."""
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
        self.assertIn(st.status, ("conflict", "changed"))
        # No silent clobber: the imported value is preserved until the operator resolves.
        self.assertTrue(CommunityList.objects.get(name="CL-FLIP").invert_match)

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

    def test_conflict_does_not_clobber_entries(self):
        """Once filled, a divergent re-report flags conflict and leaves entries intact."""
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

        # Same device re-reports different content → conflict, entries untouched.
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
        self.assertEqual(st.status, "conflict")
        self.assertEqual(PrefixListEntry.objects.filter(prefix_list=pl).count(), 1)  # not clobbered

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
