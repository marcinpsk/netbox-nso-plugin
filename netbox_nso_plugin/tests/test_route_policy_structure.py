# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Unit tests for the pure route_policy_structure transform (no Django / no DB).

The end-to-end materialisation is covered in test_route_policy_reconciler.py; these cover
the transform's edge branches directly: AFI dedup/order, the Junos per-name op fallback,
vendor_ext namespacing, and the residual-blob trimming.
"""

from __future__ import annotations

import json

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


class TestRouteFilterUnits(SimpleTestCase):
    """Junos route-filter / prefix-list-entry → normalized (action, prefix, ge, le) units."""

    def test_route_filter_match_types(self):
        from netbox_nso_plugin.route_policy_structure import _route_filter_unit

        self.assertEqual(_route_filter_unit({"match": "exact", "prefix": "0.0.0.0/0"}), ("permit", "0.0.0.0/0", 0, 0))
        self.assertEqual(
            _route_filter_unit({"match": "prefix-length-range", "arg": "/25-/32", "prefix": "0.0.0.0/0"}),
            ("permit", "0.0.0.0/0", 25, 32),
        )
        self.assertEqual(
            _route_filter_unit({"match": "orlonger", "prefix": "10.0.0.0/8"}), ("permit", "10.0.0.0/8", 8, 32)
        )
        self.assertEqual(
            _route_filter_unit({"match": "orlonger", "prefix": "2001:db8::/32"}), ("permit", "2001:db8::/32", 32, 128)
        )
        # Unhandled type round-trips its raw spelling (a 4-tuple with a -1 sentinel length) so it
        # can never falsely equate a clean range AND stays sortable alongside clean int units.
        self.assertEqual(
            _route_filter_unit({"match": "through", "arg": "1.2.3.0/24", "prefix": "1.0.0.0/8"}),
            ("permit", "1.0.0.0/8", -1, "raw:through:1.2.3.0/24"),
        )

    def test_mixed_recognised_and_raw_units_are_sortable(self):
        """A set mixing a recognised route-filter (4-tuple of ints) and an unrecognised one (raw)
        must remain sortable — a 3-tuple raw unit made sorted() compare str vs int → TypeError,
        aborting the whole route-map reconcile for the device."""
        from netbox_nso_plugin.route_policy_structure import _route_filter_unit

        clean = _route_filter_unit({"match": "exact", "prefix": "10.0.0.0/8"})
        raw = _route_filter_unit({"match": "through", "arg": "10.255.0.0/16", "prefix": "10.0.0.0/8"})
        self.assertEqual(len(sorted({clean, raw})), 2)  # no TypeError; both distinct

    def test_prefix_list_entry_unit_bare_prefix_is_exact(self):
        from netbox_nso_plugin.route_policy_structure import prefix_list_entry_unit

        self.assertEqual(prefix_list_entry_unit({"prefix": "0.0.0.0/0"}), ("permit", "0.0.0.0/0", 0, 0))
        self.assertEqual(
            prefix_list_entry_unit({"prefix": "10.0.0.0/8", "ge": 8, "le": 32}), ("permit", "10.0.0.0/8", 8, 32)
        )


class TestCanonicalRouteMapPrefixExpansion(SimpleTestCase):
    """BOGONS: a Junos term that INLINES a route-filter set converges with the Nokia term that
    references the equivalent NAMED prefix-lists — and only when the content truly matches.

    Mirrors live rc1↔ra1 BOGONS-EXT-V4-out (converges) vs -V4-in (rc1 also filters /1-7 that
    ra1's inbound list lacks → genuine drift). Resolver fakes the global prefix-list content.
    """

    _PL = {
        "MARTIANS_V4": [("permit", "0.0.0.0/8", 8, 32), ("permit", "10.0.0.0/8", 8, 32)],
        "DEFAULT_ROUTE_IPv4": [("permit", "0.0.0.0/0", 0, 0), ("permit", "0.0.0.0/0", 25, 32)],
        "DEFAULT_ROUTE_IPv4_2": [("permit", "0.0.0.0/0", 1, 7)],
    }

    def _resolver(self, name):
        return self._PL.get(name, [])

    def _canon(self, captured):
        from netbox_nso_plugin.route_policy_structure import canonical_route_map

        return canonical_route_map(captured, self._resolver)

    def _junos_inline_term(self, length_ranges):
        # length_ranges: list of route-filter dicts for the default-route filters (besides MARTIANS).
        rf = [{"match": "exact", "prefix": "0.0.0.0/0"}] + length_ranges
        match = {
            "_junos_family": "inet",
            "_junos_prefix_list_filter": [{"list": "MARTIANS_V4", "match": "orlonger"}],
            "_junos_route_filter": rf,
            "_junos_term": "prefix",
            "protocol": "bgp",
        }
        return {
            "name": "RM",
            "entries": [
                {"sequence": 10, "action": "deny", "match": json.dumps(match), "set": '{"_junos_terminal": "reject"}'}
            ],
        }

    def _nokia_named_term(self, names):
        match = {"_timos_description": "prefix", "family": ["ipv4"], "protocol": ["bgp"]}
        return {
            "name": "RM",
            "entries": [
                {"sequence": 1, "action": "deny", "match_prefix_lists": names, "match": json.dumps(match), "set": "{}"}
            ],
        }

    def test_inline_route_filter_converges_with_named_lists(self):
        # -out: rc1 inline {exact, /25-32, /1-7} == ra1 [MARTIANS, DEFAULT, DEFAULT_2].
        junos = self._junos_inline_term(
            [
                {"match": "prefix-length-range", "arg": "/25-/32", "prefix": "0.0.0.0/0"},
                {"match": "prefix-length-range", "arg": "/1-/7", "prefix": "0.0.0.0/0"},
            ]
        )
        nokia = self._nokia_named_term(["DEFAULT_ROUTE_IPv4", "DEFAULT_ROUTE_IPv4_2", "MARTIANS_V4"])
        self.assertEqual(self._canon(junos), self._canon(nokia))

    def test_extra_inline_filter_stays_distinct(self):
        # -in: rc1 still inlines /1-7, but ra1 inbound references only DEFAULT_ROUTE_IPv4 (no _2).
        junos = self._junos_inline_term(
            [
                {"match": "prefix-length-range", "arg": "/25-/32", "prefix": "0.0.0.0/0"},
                {"match": "prefix-length-range", "arg": "/1-/7", "prefix": "0.0.0.0/0"},
            ]
        )
        nokia = self._nokia_named_term(["MARTIANS_V4", "DEFAULT_ROUTE_IPv4"])
        self.assertNotEqual(self._canon(junos), self._canon(nokia))

    def test_without_resolver_prefix_lists_stay_by_name(self):
        # No resolver → pure DB-free projection keeps the by-name prefix_lists (used by unit tests
        # that don't exercise expansion); the entry carries no expanded prefix_match.
        from netbox_nso_plugin.route_policy_structure import canonical_route_map

        nokia = self._nokia_named_term(["MARTIANS_V4", "DEFAULT_ROUTE_IPv4"])
        c = canonical_route_map(nokia)
        self.assertIn("prefix_lists", c["entries"][0])
        self.assertNotIn("prefix_match", c["entries"][0])
