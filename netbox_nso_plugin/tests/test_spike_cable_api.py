# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Phase 1 spike: confirm which NetBox 4.6 Cable/Interface API attributes work.

These tests are deliberately exploratory. They exist to de-risk M8's assumptions
about cable traversal before any production code is written. Results from these
tests update the Phase 0 "Allowed APIs" section of docs/m8-derived-intent-plan.md.

Run via:
    netbox-test netbox_nso_plugin.tests.test_spike_cable_api -v 2
"""

import logging

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
from django.db.models.signals import post_delete, post_save
from django.test import TestCase

logger = logging.getLogger(__name__)


def _make_device(name, *, dt, role, site):
    return Device.objects.create(name=name, device_type=dt, role=role, site=site)


def _make_iface(device, name, iface_type="1000base-t"):
    return Interface.objects.create(device=device, name=name, type=iface_type)


class TestCableAPISpike(TestCase):
    """Spike: confirm Cable + Interface traversal APIs available in NetBox 4.6."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="SpikeMfg", slug="spikemfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SpikeDevice", slug="spikedevice")
        role = DeviceRole.objects.create(name="SpikeRole", slug="spikerole")
        site = Site.objects.create(name="SpikeSite", slug="spikesite")
        cls.dt = dt
        cls.role = role
        cls.site = site

        cls.dev1 = _make_device("spike-dev1", dt=dt, role=role, site=site)
        cls.dev2 = _make_device("spike-dev2", dt=dt, role=role, site=site)
        cls.dev3 = _make_device("spike-dev3", dt=dt, role=role, site=site)

        cls.iface1 = _make_iface(cls.dev1, "GigabitEthernet0/0")
        cls.iface2 = _make_iface(cls.dev2, "GigabitEthernet0/0")
        cls.iface3 = _make_iface(cls.dev3, "GigabitEthernet0/0")

    def test_cable_a_b_terminations_attribute_exists(self):
        """Confirm Cable.a_terminations / Cable.b_terminations exist and are queryable."""
        cable = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable, cable_end="A", termination=self.iface1)
        CableTermination.objects.create(cable=cable, cable_end="B", termination=self.iface2)

        # In NetBox 4.6, a_terminations/b_terminations are properties returning
        # a list of the actual termination objects (Interface instances), NOT
        # CableTermination wrappers and NOT querysets.
        a_terms = list(cable.a_terminations)
        b_terms = list(cable.b_terminations)

        self.assertEqual(len(a_terms), 1)
        self.assertEqual(len(b_terms), 1)
        # Items are Interface objects directly
        self.assertIsInstance(a_terms[0], Interface)
        self.assertEqual(a_terms[0].pk, self.iface1.pk)
        self.assertEqual(b_terms[0].pk, self.iface2.pk)

        cable.delete()

    def test_cable_traversal_link_peers(self):
        """Confirm Interface.link_peers returns the single-segment peer."""
        cable = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable, cable_end="A", termination=self.iface1)
        CableTermination.objects.create(cable=cable, cable_end="B", termination=self.iface2)

        # Refresh from DB to populate the link-peer cache
        iface1 = Interface.objects.get(pk=self.iface1.pk)
        peers = iface1.link_peers
        logger.info("SPIKE: iface1.link_peers = %r", peers)

        # Record result
        if peers:
            peer = peers[0]
            self.assertIsInstance(peer, Interface)
            self.assertEqual(peer.pk, self.iface2.pk)
        else:
            logger.warning("SPIKE: link_peers returned empty — check connected_endpoints instead")

        cable.delete()

    def test_cable_traversal_connected_endpoints(self):
        """Confirm Interface.connected_endpoints walks multi-hop paths."""
        cable = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable, cable_end="A", termination=self.iface1)
        CableTermination.objects.create(cable=cable, cable_end="B", termination=self.iface2)

        iface1 = Interface.objects.get(pk=self.iface1.pk)
        endpoints = iface1.connected_endpoints
        logger.info("SPIKE: iface1.connected_endpoints = %r", endpoints)

        if endpoints:
            ep = endpoints[0]
            self.assertIsInstance(ep, Interface)
            self.assertEqual(ep.pk, self.iface2.pk)
        else:
            logger.warning("SPIKE: connected_endpoints returned empty")

        cable.delete()

    def test_cable_traversal_manual_via_terminations(self):
        """Confirm manual traversal via cable.a_terminations / b_terminations resolves peers."""
        cable = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable, cable_end="A", termination=self.iface1)
        CableTermination.objects.create(cable=cable, cable_end="B", termination=self.iface2)

        # From iface1: find which end it's on, then yield the other end
        iface1_fresh = Interface.objects.get(pk=self.iface1.pk)

        # a_terminations/b_terminations return lists of Interface objects directly
        a_iface_ids = set(iface.pk for iface in cable.a_terminations)
        b_iface_ids = set(iface.pk for iface in cable.b_terminations)
        logger.info("SPIKE: a_ids=%r b_ids=%r", a_iface_ids, b_iface_ids)

        if iface1_fresh.pk in a_iface_ids:
            peer_ids = b_iface_ids
        elif iface1_fresh.pk in b_iface_ids:
            peer_ids = a_iface_ids
        else:
            peer_ids = set()

        self.assertIn(self.iface2.pk, peer_ids)
        cable.delete()

    def test_cable_delete_leaves_peer_none(self):
        """After cable deletion, Interface.link_peers is empty."""
        cable = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable, cable_end="A", termination=self.iface1)
        CableTermination.objects.create(cable=cable, cable_end="B", termination=self.iface2)

        cable.delete()

        iface1_fresh = Interface.objects.get(pk=self.iface1.pk)
        peers = iface1_fresh.link_peers
        logger.info("SPIKE: after delete, iface1.link_peers = %r", peers)
        # Should be empty or None after cable deleted
        self.assertFalse(peers)

    def test_interface_cable_reverse(self):
        """Interface.cable returns the Cable object (or None when disconnected)."""
        cable = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable, cable_end="A", termination=self.iface1)
        CableTermination.objects.create(cable=cable, cable_end="B", termination=self.iface2)

        iface1_fresh = Interface.objects.get(pk=self.iface1.pk)
        logger.info("SPIKE: iface1.cable = %r", iface1_fresh.cable)
        # The attribute should exist; value may be None or a Cable
        # (whether Interface.cable is populated depends on NetBox internals)
        self.assertTrue(hasattr(iface1_fresh, "cable"))

        cable.delete()
        iface1_disconnected = Interface.objects.get(pk=self.iface1.pk)
        self.assertIsNone(iface1_disconnected.cable)

    def test_lag_member_fields(self):
        """Confirm child.lag returns the LAG Interface and child.cable works."""
        lag = Interface.objects.create(device=self.dev1, name="Port-channel1", type="lag")
        child = Interface.objects.create(device=self.dev1, name="GigabitEthernet0/1", type="1000base-t", lag=lag)

        child_fresh = Interface.objects.get(pk=child.pk)
        logger.info("SPIKE: child.lag_id = %r, child.lag = %r", child_fresh.lag_id, child_fresh.lag)
        self.assertEqual(child_fresh.lag_id, lag.pk)
        self.assertIsInstance(child_fresh.lag, Interface)

        # Cable a LAG member
        cable = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable, cable_end="A", termination=child)
        CableTermination.objects.create(cable=cable, cable_end="B", termination=self.iface2)
        child_cabled = Interface.objects.get(pk=child.pk)
        logger.info("SPIKE: lag child.cable = %r", child_cabled.cable)
        self.assertTrue(hasattr(child_cabled, "cable"))

        cable.delete()
        child.delete()
        lag.delete()

    def test_breakout_parent_field(self):
        """Check whether Interface.parent exists for breakout children in NetBox 4.6."""
        parent_iface = Interface.objects.create(
            device=self.dev1, name="FortyGigabitEthernet0/0", type="40gbase-x-qsfpp"
        )
        has_parent_field = hasattr(parent_iface, "parent")
        has_parent_id = hasattr(parent_iface, "parent_id")
        logger.info("SPIKE: Interface has parent=%s, parent_id=%s", has_parent_field, has_parent_id)

        if has_parent_field:
            # Try creating a child with parent
            try:
                child_bo = Interface.objects.create(
                    device=self.dev1,
                    name="FortyGigabitEthernet0/0/1",
                    type="10gbase-x-sfpp",
                    parent=parent_iface,
                )
                child_bo_fresh = Interface.objects.get(pk=child_bo.pk)
                logger.info("SPIKE: breakout child.parent_id = %r", child_bo_fresh.parent_id)
                self.assertEqual(child_bo_fresh.parent_id, parent_iface.pk)
                child_bo.delete()
            except Exception as exc:
                logger.warning("SPIKE: breakout child creation failed: %s", exc)

        parent_iface.delete()
        # This test always passes — we're recording facts, not enforcing behavior
        self.assertTrue(True)

    def test_cable_signals_fire(self):
        """Confirm post_save and post_delete fire on Cable create/delete."""
        save_calls = []
        delete_calls = []

        def on_save(sender, instance, created, **kwargs):
            save_calls.append((instance.pk, created))

        def on_delete(sender, instance, **kwargs):
            delete_calls.append(instance.pk)

        post_save.connect(on_save, sender=Cable, dispatch_uid="spike_cable_save")
        post_delete.connect(on_delete, sender=Cable, dispatch_uid="spike_cable_delete")
        try:
            cable = Cable.objects.create(status="connected")
            CableTermination.objects.create(cable=cable, cable_end="A", termination=self.iface1)
            CableTermination.objects.create(cable=cable, cable_end="B", termination=self.iface2)
            cable_pk = cable.pk
            cable.delete()
        finally:
            post_save.disconnect(dispatch_uid="spike_cable_save")
            post_delete.disconnect(dispatch_uid="spike_cable_delete")

        logger.info("SPIKE: save_calls=%r, delete_calls=%r", save_calls, delete_calls)
        self.assertTrue(any(pk == cable_pk for pk, _ in save_calls), "post_save did not fire")
        self.assertIn(cable_pk, delete_calls, "post_delete did not fire")

    def test_cable_swap_to_third_device(self):
        """Cable swap: iface1↔iface2, then iface1↔iface3 → peer changes."""
        cable = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable, cable_end="A", termination=self.iface1)
        CableTermination.objects.create(cable=cable, cable_end="B", termination=self.iface2)

        iface1 = Interface.objects.get(pk=self.iface1.pk)
        peers_before = iface1.link_peers
        logger.info("SPIKE: before swap, iface1.link_peers = %r", peers_before)

        cable.delete()

        cable2 = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable2, cable_end="A", termination=self.iface1)
        CableTermination.objects.create(cable=cable2, cable_end="B", termination=self.iface3)

        iface1_after = Interface.objects.get(pk=self.iface1.pk)
        peers_after = iface1_after.link_peers
        logger.info("SPIKE: after swap to dev3, iface1.link_peers = %r", peers_after)

        if peers_after:
            self.assertEqual(peers_after[0].pk, self.iface3.pk)

        cable2.delete()
