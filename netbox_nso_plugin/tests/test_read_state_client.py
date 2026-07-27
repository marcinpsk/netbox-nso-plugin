# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""READSEM S4 Slice B1: the vendored family registry + the client's read_state contract.

Pins: the 19-key vocabulary + category→families display map (D8); the REMOVAL of the
routing-family 404→empty fabrication (D4 — a fabricated empty carries no read_state and
would masquerade as authoritative-empty under the gate); read_state passthrough for the
shape-rebuilding fetchers; and get_interfaces_doc's route-404 legacy fallback with the
capability TTL cache (R2-4/R5-7 — route-404 is client code "404"/"route_not_found",
device-404 is the envelope's "not_found" and must still raise).
"""

import unittest
from unittest.mock import patch

from ._adapter_http import make_response, make_session

_BASE_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}

_READ_STATE = {
    "outcome": "present",
    "reason": None,
    "freshness": "fresh",
    "result": "replaced",
    "succeeded": True,
    "read_at": "2026-06-01T10:00:00Z",
    "attempt_id": 7,
    "incarnation": "00000000-0000-0000-0000-000000000001",
    "incarnation_born": "2026-06-01T00:00:00Z",
}

_DEVICE_404 = {"error": {"code": "not_found", "message": "Device not found", "detail": {}}}


class TestFamilyRegistry(unittest.TestCase):
    """The vendored registry mirrors the adapter's canonical vocabulary (D8)."""

    def test_all_19_keys(self):
        from netbox_nso_plugin.families import ALL_FAMILY_KEYS, FAMILIES_VERSION

        self.assertEqual(len(ALL_FAMILY_KEYS), 19)
        self.assertEqual(len(set(ALL_FAMILY_KEYS)), 19)
        self.assertTrue(all("-" not in k for k in ALL_FAMILY_KEYS))
        self.assertIn("redistribution", ALL_FAMILY_KEYS)
        self.assertIn("interface_attributes", ALL_FAMILY_KEYS)
        self.assertGreaterEqual(FAMILIES_VERSION, 1)

    def test_category_map_matches_summary_categories(self):
        """Every tab category maps to ≥1 canonical family; keys track summary._CATEGORIES
        exactly (a new category must decide its display families explicitly)."""
        from netbox_nso_plugin import summary
        from netbox_nso_plugin.families import ALL_FAMILY_KEYS, CATEGORY_FAMILIES

        category_keys = {c[0] for c in summary._CATEGORIES}
        self.assertEqual(set(CATEGORY_FAMILIES), category_keys)
        for cat, families in CATEGORY_FAMILIES.items():
            self.assertTrue(families, f"{cat}: empty family set")
            for fam in families:
                self.assertIn(fam, ALL_FAMILY_KEYS, f"{cat}: {fam} not canonical")

    def test_display_accurate_special_cases(self):
        """R2-6: interface = the 4 families its headline actually renders; lacp displays
        lag_config only (the lag topology outcome is an explicit non-goal)."""
        from netbox_nso_plugin.families import CATEGORY_FAMILIES

        self.assertEqual(
            set(CATEGORY_FAMILIES["interface"]),
            {"interface_attributes", "interface_ip", "interface_mtu", "switchport"},
        )
        self.assertEqual(CATEGORY_FAMILIES["lacp"], ("lag_config",))


class TestFabricationRemoved(unittest.TestCase):
    """D4: a 404 raises AdapterError for every routing fetcher — no fabricated empties."""

    def _assert_404_raises(self, fn_name, *args):
        from netbox_nso_plugin import adapter_client

        session = make_session(status_code=404, json_data=_DEVICE_404)
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
        ):
            fn = getattr(adapter_client, fn_name)
            with self.assertRaises(adapter_client.AdapterError) as ctx:
                fn(*args)
            self.assertEqual(ctx.exception.code, "not_found")

    def test_isis_404_raises(self):
        self._assert_404_raises("get_isis_interfaces", 99)

    def test_l2_services_404_raises(self):
        self._assert_404_raises("get_l2_services", 99)

    def test_bfd_404_raises(self):
        self._assert_404_raises("get_bfd", 99)

    def test_bgp_404_raises(self):
        self._assert_404_raises("get_bgp_config", 99)

    def test_route_policy_404_raises(self):
        self._assert_404_raises("get_route_policy", 99)

    def test_ospf_404_raises(self):
        self._assert_404_raises("get_ospf", 99)

    def test_redistribution_404_raises(self):
        self._assert_404_raises("get_redistribution", 99)


class TestReadStatePassthrough(unittest.TestCase):
    """Every fetcher must RETAIN a served read_state — including the shape-rebuilders
    (isis/l2/bfd), whose dict reconstruction silently dropped unknown keys."""

    def _fetch(self, fn_name, body, *args):
        from netbox_nso_plugin import adapter_client

        session = make_session(status_code=200, json_data=body)
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
        ):
            return getattr(adapter_client, fn_name)(*args)

    def test_isis_rebuilder_keeps_read_state(self):
        out = self._fetch(
            "get_isis_interfaces",
            {"device_id": 9, "read_state": _READ_STATE, "processes": [], "interfaces": []},
            9,
        )
        self.assertEqual(out.get("read_state"), _READ_STATE)

    def test_l2_rebuilder_keeps_read_state(self):
        out = self._fetch("get_l2_services", {"device_id": 9, "read_state": _READ_STATE, "services": []}, 9)
        self.assertEqual(out.get("read_state"), _READ_STATE)

    def test_bfd_rebuilder_keeps_read_state(self):
        out = self._fetch("get_bfd", {"device_id": 9, "read_state": _READ_STATE, "interfaces": []}, 9)
        self.assertEqual(out.get("read_state"), _READ_STATE)

    def test_passthrough_fetcher_keeps_read_state(self):
        out = self._fetch("get_static_routes", {"device_id": 9, "read_state": _READ_STATE, "routes": []}, 9)
        self.assertEqual(out.get("read_state"), _READ_STATE)


class TestInterfacesDoc(unittest.TestCase):
    """get_interfaces_doc: object doc from an S4 adapter; route-404 → legacy list wrapped
    as a key-absent doc (S3 floor); device-404 raises on BOTH paths; capability cached."""

    def setUp(self):
        from netbox_nso_plugin import adapter_client

        adapter_client.reset_interfaces_doc_capability()

    def _session_with(self, responses):
        session = make_session()
        session.request.side_effect = responses
        return session

    def _patches(self, session):
        return (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
        )

    def test_doc_served_directly(self):
        from netbox_nso_plugin import adapter_client

        doc = {"device_id": 9, "read_state": _READ_STATE, "interfaces": []}
        session = self._session_with([make_response(200, doc)])
        p1, p2 = self._patches(session)
        with p1, p2:
            out = adapter_client.get_interfaces_doc(9)
        self.assertEqual(out, doc)
        self.assertEqual(session.request.call_count, 1)
        self.assertIn("/interfaces-doc", session.request.call_args_list[0][0][1])

    def test_route_404_falls_back_and_caches(self):
        from netbox_nso_plugin import adapter_client

        legacy = [{"name": "eth0", "netbox_interface_id": 1, "attrs": {}}]
        session = self._session_with(
            [
                make_response(404, {"detail": "Not Found"}),  # route-level 404: no ErrorEnvelope
                make_response(200, legacy),
                make_response(200, legacy),  # second get_interfaces_doc: straight to legacy
            ]
        )
        p1, p2 = self._patches(session)
        with p1, p2:
            out = adapter_client.get_interfaces_doc(9)
            again = adapter_client.get_interfaces_doc(9)
        self.assertEqual(out, {"device_id": 9, "interfaces": legacy})
        self.assertNotIn("read_state", out)  # key-absent doc = legacy semantics (D3)
        self.assertEqual(again, {"device_id": 9, "interfaces": legacy})
        self.assertEqual(session.request.call_count, 3, "capability cached — no re-probe")
        self.assertIn("/interfaces-doc", session.request.call_args_list[0][0][1])
        self.assertTrue(session.request.call_args_list[2][0][1].endswith("/interfaces"))

    def test_device_404_on_doc_raises(self):
        from netbox_nso_plugin import adapter_client

        session = self._session_with([make_response(404, _DEVICE_404)])
        p1, p2 = self._patches(session)
        with p1, p2:
            with self.assertRaises(adapter_client.AdapterError) as ctx:
                adapter_client.get_interfaces_doc(9)
        self.assertEqual(ctx.exception.code, "not_found")

    def test_device_404_on_fallback_raises(self):
        from netbox_nso_plugin import adapter_client

        session = self._session_with(
            [
                make_response(404, {"detail": "Not Found"}),
                make_response(404, _DEVICE_404),
            ]
        )
        p1, p2 = self._patches(session)
        with p1, p2:
            with self.assertRaises(adapter_client.AdapterError) as ctx:
                adapter_client.get_interfaces_doc(9)
        self.assertEqual(ctx.exception.code, "not_found")
