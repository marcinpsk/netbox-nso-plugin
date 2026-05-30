# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for derived_intent compute functions (Phase 4).

Uses a real Django/NetBox DB via TestCase.setUpTestData.  Exercises
detect_skip, find_peer, render_template, compute_description, and
is_managed_description against real Interface and Cable objects.
"""

import logging
from unittest.mock import patch

from dcim.models import (
    Cable,
    CableTermination,
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Site,
)
from django.test import TestCase

from netbox_nso_plugin.derived_intent import (
    SentinelTemplate,
    SkipReason,
    compute_description,
    detect_skip,
    find_peer,
    is_managed_description,
    render_template,
)


def _make_device(name, dt, role, site):
    return Device.objects.create(name=name, device_type=dt, role=role, site=site)


def _make_iface(device, name, iface_type="1000base-t"):
    return Interface.objects.create(device=device, name=name, type=iface_type)


def _make_cable(iface_a, iface_b):
    cable = Cable.objects.create(status="connected")
    CableTermination.objects.create(cable=cable, cable_end="A", termination=iface_a)
    CableTermination.objects.create(cable=cable, cable_end="B", termination=iface_b)
    return cable


SENTINEL_AUTO = SentinelTemplate(
    sentinel="[auto]",
    template="[auto] to {peer_host}:{peer_iface}",
)
SENTINEL_SHORT = SentinelTemplate(
    sentinel="[short]",
    template="[short] {peer_host}",
)
TEMPLATES = [SENTINEL_AUTO, SENTINEL_SHORT]


class TestDetectSkip(TestCase):
    """Unit tests for detect_skip using real Interface objects."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="ComputeMfg", slug="computemfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="ComputeDev", slug="computedev")
        role = DeviceRole.objects.create(name="ComputeRole", slug="computerole")
        site = Site.objects.create(name="ComputeSite", slug="computesite")
        cls.dev = _make_device("dev-compute", dt, role, site)

    def test_no_skip_for_plain_interface(self):
        iface = _make_iface(self.dev, "Gi0/1")
        self.assertIsNone(detect_skip(iface))

    def test_skip_lag_member(self):
        lag = _make_iface(self.dev, "ae0", iface_type="lag")
        member = Interface.objects.create(device=self.dev, name="Gi0/2", type="1000base-t", lag=lag)
        result = detect_skip(member)
        self.assertIsInstance(result, SkipReason)
        self.assertEqual(result.reason, "lag_member")

    def test_skip_breakout_child(self):
        parent = _make_iface(self.dev, "Gi0/3")
        child = Interface.objects.create(device=self.dev, name="Gi0/3.1", type="1000base-t", parent=parent)
        result = detect_skip(child)
        self.assertIsInstance(result, SkipReason)
        self.assertEqual(result.reason, "breakout_child")


class TestFindPeer(TestCase):
    """Tests for find_peer against real cables."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="PeerMfg", slug="peermfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="PeerDev", slug="peerdev")
        role = DeviceRole.objects.create(name="PeerRole", slug="peerrole")
        site = Site.objects.create(name="PeerSite", slug="peersite")
        cls.dev1 = _make_device("peer-dev1", dt, role, site)
        cls.dev2 = _make_device("peer-dev2", dt, role, site)

    def test_find_peer_returns_interface(self):
        iface1 = _make_iface(self.dev1, "Gi0/1-peer")
        iface2 = _make_iface(self.dev2, "Gi0/2-peer")
        _make_cable(iface1, iface2)
        iface1_fresh = Interface.objects.get(pk=iface1.pk)
        peer = find_peer(iface1_fresh)
        self.assertIsNotNone(peer)
        self.assertEqual(peer.pk, iface2.pk)

    def test_find_peer_returns_none_when_no_cable(self):
        iface = _make_iface(self.dev1, "Gi0/10-solo")
        self.assertIsNone(find_peer(iface))

    def test_find_peer_multi_termination_returns_none(self):
        """If link_peers has > 1 entry (multi-termination), return None and log."""
        from unittest.mock import PropertyMock

        iface = _make_iface(self.dev1, "Gi0/11-multi")
        with patch.object(type(iface), "link_peers", new_callable=PropertyMock) as mock_lp:
            mock_lp.return_value = [object(), object()]
            with self.assertLogs("netbox_nso_plugin.derived_intent", level="INFO") as cm:
                result = find_peer(iface)
        self.assertIsNone(result)
        self.assertTrue(any("multi_termination" in line for line in cm.output))

    def test_find_peer_non_interface_peer_returns_none(self):
        """If the peer is not a dcim.Interface, return None and log."""
        from unittest.mock import PropertyMock

        iface = _make_iface(self.dev1, "Gi0/12-nonif")
        with patch.object(type(iface), "link_peers", new_callable=PropertyMock) as mock_lp:
            mock_lp.return_value = [object()]  # not a dcim.Interface
            with self.assertLogs("netbox_nso_plugin.derived_intent", level="INFO") as cm:
                result = find_peer(iface)
        self.assertIsNone(result)
        self.assertTrue(any("non_interface_peer" in line for line in cm.output))


class TestRenderTemplate(TestCase):
    """Tests for render_template (no DB needed, but uses Interface for convenience)."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RenderMfg", slug="rendermfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RenderDev", slug="renderdev")
        role = DeviceRole.objects.create(name="RenderRole", slug="renderrole")
        site = Site.objects.create(name="RenderSite", slug="rendersite")
        cls.dev1 = _make_device("render-dev1", dt, role, site)
        cls.dev2 = _make_device("render-dev2", dt, role, site)

    def test_render_canonical_template(self):
        iface1 = _make_iface(self.dev1, "Gi0/1-rend")
        iface2 = _make_iface(self.dev2, "Gi0/2-rend")
        result = render_template(
            "[auto] to {peer_host}:{peer_iface}",
            self_iface=iface1,
            peer_iface=iface2,
        )
        self.assertEqual(result, "to render-dev2:Gi0/2-rend".join(["[auto] ", ""]))
        self.assertEqual(result, "[auto] to render-dev2:Gi0/2-rend")

    def test_render_short_template(self):
        iface1 = _make_iface(self.dev1, "Gi0/3-rend")
        iface2 = _make_iface(self.dev2, "Gi0/4-rend")
        result = render_template("[short] {peer_host}", self_iface=iface1, peer_iface=iface2)
        self.assertEqual(result, "[short] render-dev2")


class TestComputeDescription(TestCase):
    """Integration tests for compute_description against real DB."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="CmpDescMfg", slug="cmpdescmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="CmpDescDev", slug="cmpdescdev")
        role = DeviceRole.objects.create(name="CmpDescRole", slug="cmpdescrole")
        site = Site.objects.create(name="CmpDescSite", slug="cmpdescsite")
        cls.dev1 = _make_device("cmpdesc-dev1", dt, role, site)
        cls.dev2 = _make_device("cmpdesc-dev2", dt, role, site)

    def test_two_devices_cabled_returns_peer_description(self):
        iface1 = _make_iface(self.dev1, "Gi0/1-cmp")
        iface2 = _make_iface(self.dev2, "Gi0/2-cmp")
        _make_cable(iface1, iface2)
        iface1_fresh = Interface.objects.get(pk=iface1.pk)
        result = compute_description(iface1_fresh, SENTINEL_AUTO)
        self.assertEqual(result, "[auto] to cmpdesc-dev2:Gi0/2-cmp")

    def test_short_sentinel_returns_peer_host_only(self):
        iface1 = _make_iface(self.dev1, "Gi0/3-cmp")
        iface2 = _make_iface(self.dev2, "Gi0/4-cmp")
        _make_cable(iface1, iface2)
        iface1_fresh = Interface.objects.get(pk=iface1.pk)
        result = compute_description(iface1_fresh, SENTINEL_SHORT)
        self.assertEqual(result, "[short] cmpdesc-dev2")

    def test_no_cable_returns_bare_sentinel(self):
        iface = _make_iface(self.dev1, "Gi0/5-cmp-solo")
        result = compute_description(iface, SENTINEL_AUTO)
        self.assertEqual(result, "[auto]")

    def test_lag_member_returns_none(self):
        lag = _make_iface(self.dev1, "ae0-cmp", iface_type="lag")
        member = Interface.objects.create(device=self.dev1, name="Gi0/6-lag-cmp", type="1000base-t", lag=lag)
        result = compute_description(member, SENTINEL_AUTO)
        self.assertIsNone(result)

    def test_lag_member_logs_skip(self):
        lag = _make_iface(self.dev1, "ae1-cmp", iface_type="lag")
        member = Interface.objects.create(device=self.dev1, name="Gi0/7-lag-log", type="1000base-t", lag=lag)
        with self.assertLogs("netbox_nso_plugin.derived_intent", level=logging.INFO) as cm:
            result = compute_description(member, SENTINEL_AUTO)
        self.assertIsNone(result)
        self.assertTrue(any("lag_member" in line for line in cm.output))

    def test_self_iface_no_device_returns_none(self):
        """Interface without a device (inventory-mode mock) is skipped gracefully."""
        from types import SimpleNamespace

        iface = SimpleNamespace(device=None, lag_id=None, parent_id=None, pk=9999)
        result = compute_description(iface, SENTINEL_AUTO)
        self.assertIsNone(result)

    def test_peer_iface_no_device_returns_bare_sentinel(self):
        """Peer without a device falls back to bare sentinel instead of crashing."""
        from types import SimpleNamespace

        peer_no_device = SimpleNamespace(device=None, lag_id=None, parent_id=None, pk=9998)
        self_iface = SimpleNamespace(device=object(), lag_id=None, parent_id=None, pk=9997, link_peers=[peer_no_device])
        with patch("netbox_nso_plugin.derived_intent.find_peer", return_value=peer_no_device):
            result = compute_description(self_iface, SENTINEL_AUTO)
        self.assertEqual(result, "[auto]")


class TestIsManagedDescription(TestCase):
    """Unit tests for is_managed_description (no DB needed)."""

    def test_managed_auto_returns_template(self):
        result = is_managed_description("[auto] to dev2:Gi0", TEMPLATES)
        self.assertEqual(result, SENTINEL_AUTO)

    def test_managed_short_returns_template(self):
        result = is_managed_description("[short] dev2", TEMPLATES)
        self.assertEqual(result, SENTINEL_SHORT)

    def test_unmanaged_returns_none(self):
        result = is_managed_description("hello world", TEMPLATES)
        self.assertIsNone(result)

    def test_empty_string_returns_none(self):
        result = is_managed_description("", TEMPLATES)
        self.assertIsNone(result)

    def test_bare_sentinel_is_managed(self):
        result = is_managed_description("[auto]", TEMPLATES)
        self.assertEqual(result, SENTINEL_AUTO)

    def test_empty_templates_returns_none(self):
        result = is_managed_description("[auto] something", [])
        self.assertIsNone(result)
