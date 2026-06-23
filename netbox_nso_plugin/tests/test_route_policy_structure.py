# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Unit tests for the pure route_policy_structure transform (no Django / no DB).

The end-to-end materialisation is covered in test_route_policy_reconciler.py; these cover
the transform's edge branches directly: AFI dedup/order, the Junos per-name op fallback,
vendor_ext namespacing, and the residual-blob trimming.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from netbox_nso_plugin.route_policy_structure import normalize_afi, structure_entry


class TestNormalizeAfi(SimpleTestCase):
    def test_maps_dedups_and_preserves_order(self):
        mapped, unmapped = normalize_afi(["inet", "ipv4", "inet6", "vpn-ipv4"])
        self.assertEqual(mapped, ["ipv4", "ipv6", "vpn-ipv4"])  # inet+ipv4 collapse, order kept
        self.assertEqual(unmapped, [])

    def test_unknown_tokens_returned_not_dropped(self):
        mapped, unmapped = normalize_afi(["ipv4", "mvpn-ipv4", "rtfilter"])
        self.assertEqual(mapped, ["ipv4"])
        self.assertEqual(unmapped, ["mvpn-ipv4", "rtfilter"])


class TestStructureEntry(SimpleTestCase):
    def test_empty_blobs_are_inert(self):
        s = structure_entry({}, {})
        self.assertEqual(s.match_afi, [])
        self.assertEqual(s.set_communities, [])
        self.assertEqual(s.vendor_ext, {})
        self.assertFalse(s.is_default_action)
        self.assertIsNone(s.call_policy)

    def test_junos_op_fallback_when_ops_shorter_than_names(self):
        # Defensive: fewer ops than community names → the unmatched name defaults to 'set'.
        s = structure_entry({}, {"community": ["A", "B"], "_junos_community_op": ["add"]})
        self.assertEqual(
            [(c.operation, c.name) for c in s.set_communities],
            [("add", "A"), ("set", "B")],
        )
        # The Junos op list is still preserved verbatim in vendor_ext (lossless).
        self.assertEqual(s.vendor_ext["junos"]["community_op"], ["add"])

    def test_iosxr_additive_flag_maps_to_add(self):
        s = structure_entry({}, {"community": "CS", "community_additive": True})
        self.assertEqual([(c.operation, c.name) for c in s.set_communities], [("add", "CS")])

    def test_vendor_keys_namespaced_and_residual_trimmed(self):
        s = structure_entry(
            {"family": "ipv4", "protocol": ["bgp"], "_rpl_done": True},
            {"local_preference": 100, "_junos_priority": "high", "community_add": ["CL"]},
        )
        self.assertEqual(s.match_afi, ["ipv4"])
        self.assertEqual(s.vendor_ext, {"xr": {"done": True}, "junos": {"priority": "high"}})
        self.assertEqual(s.residual_match, {"protocol": ["bgp"]})  # family consumed, _rpl_ moved
        self.assertEqual(s.residual_set, {"local_preference": 100})  # community_add consumed
        self.assertEqual([(c.operation, c.name) for c in s.set_communities], [("add", "CL")])

    def test_default_action_flag_detected(self):
        s = structure_entry({"_timos_default_action": True}, {})
        self.assertTrue(s.is_default_action)
        self.assertEqual(s.vendor_ext, {"timos": {"default_action": True}})


class TestSummarizeRouteMap(SimpleTestCase):
    def test_summary_projects_entries_and_folds_default_action(self):
        from netbox_nso_plugin.route_policy_structure import summarize_route_map

        captured = {
            "name": "RM",
            "entries": [
                {
                    "sequence": 10,
                    "action": "permit",
                    "match_prefix_lists": ["PL-A"],
                    "match_community_lists": [],
                    "match_as_paths": [],
                    "match": '{"family": ["ipv4"], "_rpl_done": true}',
                    "set": '{"local_preference": 200, "community_add": ["CL-X"], "_junos_priority": "high"}',
                },
                # Synthetic Nokia default-action — folded into default_action, not an entry.
                {"sequence": 999999, "action": "deny", "match": '{"_timos_default_action": true}', "set": "{}"},
            ],
        }
        summary = summarize_route_map(captured)

        self.assertEqual(summary["default_action"], "deny")
        self.assertEqual(len(summary["entries"]), 1)  # default-action shell folded away
        e = summary["entries"][0]
        self.assertEqual(e["action"], "permit")
        self.assertEqual(e["match_afi"], ["ipv4"])
        self.assertEqual(e["match_prefix_lists"], ["PL-A"])
        self.assertEqual(e["set_communities"], [{"operation": "add", "name": "CL-X"}])
        self.assertEqual(e["set_knobs"], {"local_preference": 200})  # vendor-neutral knob kept
        self.assertEqual(e["vendor_ext"], {"xr": {"done": True}, "junos": {"priority": "high"}})

    def test_summary_empty_capture_is_safe(self):
        from netbox_nso_plugin.route_policy_structure import summarize_route_map

        self.assertEqual(summarize_route_map({}), {"default_action": None, "entries": []})
        self.assertEqual(summarize_route_map(None), {"default_action": None, "entries": []})


class TestCanonicalRouteMap(SimpleTestCase):
    """The vendor-neutral semantic digest that converges cosmetic cross-vendor route-maps.

    Fixtures mirror live rc1(Junos) vs ra1(Nokia) captures — proven cosmetic-equal by
    diffing the real export oper-data.
    """

    def _canon(self, captured):
        from netbox_nso_plugin.route_policy_structure import canonical_route_map

        return canonical_route_map(captured)

    def _rm(self, entries):
        return {"name": "RM", "entries": entries}

    def test_set_lp_250_junos_and_nokia_converge(self):
        # SET-LP-250: permit, match protocol bgp, set local-preference 250. Junos spells the
        # fall-through as `_junos_terminal: none` + scalar protocol + a term label; Nokia as
        # `_timos_action_type: next-policy` + leaf-list protocol. Same policy.
        junos = self._rm(
            [
                {
                    "sequence": 10,
                    "action": "permit",
                    "match": '{"_junos_term": "lp250", "protocol": "bgp"}',
                    "set": '{"_junos_terminal": "none", "local_preference": 250}',
                }
            ]
        )
        nokia = self._rm(
            [
                {
                    "sequence": 10,
                    "action": "permit",
                    "match": '{"protocol": ["bgp"]}',
                    "set": '{"_timos_action_type": "next-policy", "local_preference": 250}',
                }
            ]
        )
        self.assertEqual(self._canon(junos), self._canon(nokia))

    def test_crpd_export_community_add_converges(self):
        # CRPD-export: permit protocol direct, add community CRPD-VPN. Junos: community + the
        # additive flag + per-name op; Nokia: by-ref community_add. Same add action.
        junos = self._rm(
            [
                {
                    "sequence": 10,
                    "action": "permit",
                    "match": '{"_junos_term": "1", "protocol": "direct"}',
                    "set": '{"_junos_community_op": ["add"], "_junos_terminal": "accept",'
                    ' "community": "CRPD-VPN", "community_additive": true}',
                }
            ]
        )
        nokia = self._rm(
            [
                {
                    "sequence": 10,
                    "action": "permit",
                    "match": '{"protocol": ["direct"]}',
                    "set": '{"community_add": ["CRPD-VPN"]}',
                }
            ]
        )
        self.assertEqual(self._canon(junos), self._canon(nokia))

    def test_as_path_match_and_flow_converge_across_vendors(self):
        # LEAKG-NON-TIER1: deny on as-path ALL_TIER1_ASNS. Junos: seq 10 + term label +
        # terminal reject; Nokia: seq 1 + description label, flow derived from deny. Same.
        junos = self._rm(
            [
                {
                    "sequence": 10,
                    "action": "deny",
                    "match_as_paths": ["ALL_TIER1_ASNS"],
                    "match": '{"_junos_term": "block-asns"}',
                    "set": '{"_junos_terminal": "reject"}',
                }
            ]
        )
        nokia = self._rm(
            [
                {
                    "sequence": 1,
                    "action": "deny",
                    "match_as_paths": ["ALL_TIER1_ASNS"],
                    "match": '{"_timos_description": "block-asns"}',
                    "set": "{}",
                }
            ]
        )
        self.assertEqual(self._canon(junos), self._canon(nokia))

    def test_junos_policy_default_folds_like_nokia_default_action(self):
        # The policy-level default entry folds the same whether marked _junos_default or
        # _timos_default_action; both reject-everything REJECT-ALLs share a default.
        junos_default = self._rm(
            [
                {
                    "sequence": 20,
                    "action": "deny",
                    "match": '{"_junos_default": true}',
                    "set": '{"_junos_terminal": "reject"}',
                }
            ]
        )
        nokia_default = self._rm(
            [{"sequence": 999999, "action": "deny", "match": '{"_timos_default_action": true}', "set": "{}"}]
        )
        cj, cn = self._canon(junos_default), self._canon(nokia_default)
        self.assertEqual(cj["entries"], [])  # the default shell is not counted as an entry
        self.assertEqual(cj["default"], {"action": "deny", "flow": "reject", "set_communities": [], "set_knobs": {}})
        self.assertEqual(cj, cn)

    def test_genuine_value_difference_stays_distinct(self):
        owner = self._rm([{"sequence": 10, "action": "permit", "match": "{}", "set": '{"local_preference": 250}'}])
        other = self._rm([{"sequence": 10, "action": "permit", "match": "{}", "set": '{"local_preference": 300}'}])
        self.assertNotEqual(self._canon(owner), self._canon(other))

    def test_junos_inline_route_filter_is_preserved_as_drift(self):
        # Vendor-specific content with no neutral home (Junos inline route-filter) is KEPT in
        # the residual vendor blob → genuinely different from a plain named-list match.
        with_rf = self._rm(
            [
                {
                    "sequence": 10,
                    "action": "deny",
                    "match": '{"_junos_family": "inet", "_junos_route_filter":'
                    ' [{"match": "exact", "prefix": "0.0.0.0/0"}]}',
                    "set": '{"_junos_terminal": "reject"}',
                }
            ]
        )
        without = self._rm([{"sequence": 1, "action": "deny", "match": '{"family": ["ipv4"]}', "set": "{}"}])
        cw = self._canon(with_rf)
        self.assertEqual(
            cw["entries"][0]["vendor"], {"junos": {"route_filter": [{"match": "exact", "prefix": "0.0.0.0/0"}]}}
        )
        self.assertNotEqual(cw, self._canon(without))

    def test_terminal_accept_distinct_from_fall_through(self):
        # Flow IS semantic: an entry that terminally accepts must not equate to one that just
        # sets an attribute and continues (don't over-normalise the verb away).
        accept = self._rm([{"sequence": 10, "action": "permit", "match": "{}", "set": '{"_junos_terminal": "accept"}'}])
        cont = self._rm([{"sequence": 10, "action": "permit", "match": "{}", "set": '{"_junos_terminal": "none"}'}])
        self.assertNotEqual(self._canon(accept), self._canon(cont))
