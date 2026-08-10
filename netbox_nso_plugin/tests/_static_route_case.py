# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared fixtures for the #1396 R3 static-route suites (P2 transition, P6 push errors).

Defined once, here, so the two suites cannot drift on what a device, a brownfield route or
an owned overlay looks like. Same role as :mod:`._settlement_case` for the Appendix S suites.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.utils import timezone

PUT = "netbox_nso_plugin.adapter_client.put_static_route_intent"


def _make_device(tag: str, index: int = 1):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"Sr{tag}Mfg", slug=f"sr{tag}mfg")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"Sr{tag}Dev", slug=f"sr{tag}dev")
    role, _ = DeviceRole.objects.get_or_create(name=f"Sr{tag}Role", slug=f"sr{tag}role")
    site, _ = Site.objects.get_or_create(name=f"Sr{tag}Site", slug=f"sr{tag}site")
    return Device.objects.create(name=f"sr-{tag}-rtr-{index}", device_type=dt, role=role, site=site)


def _make_mgmt(device, tag: str, adapter_device_id: int):
    from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

    inst, _ = NSOInstance.objects.get_or_create(
        name=f"sr-{tag}-inst", defaults={"adapter_instance_id": f"sr-{tag}-inst"}
    )
    return NSODeviceManagement.objects.create(
        device=device,
        nso_instance=inst,
        nso_device_name=f"nso-sr-{tag}-{device.pk}",
        adapter_device_id=adapter_device_id,
    )


@contextlib.contextmanager
def _fixtures():
    """Build fixtures with the adapter patched out, then clear the coalescer.

    Creating an overlay fires its own push, which inside a ``TestCase``'s ambient atomic
    block lands in the thread-local pending map that only an ``on_commit`` drain clears.
    That drain is registered outside the assertion's ``captureOnCommitCallbacks()``, so it
    never runs there — and the still-populated map then suppresses the registration the
    transition's own push needs (the pre-existing rollback leak; Appendix O owns the fix).

    The reset runs in a ``finally``: a fixture body that raises would otherwise leak the
    thread-local pending map into every later test on this worker.
    """
    from netbox_nso_plugin.signals import reset_intent_push_state

    try:
        with patch(PUT):
            yield
    finally:
        reset_intent_push_state()


def _route(prefix, next_hop, *, vrf=None, metric=1, devices=()):
    """Create a route already assigned to *devices*, without owning it (brownfield shape)."""
    from netbox_routing.models import StaticRoute

    from netbox_nso_plugin.signals import suppress_intent_push

    sr = StaticRoute.objects.create(prefix=prefix, next_hop=next_hop, vrf=vrf, metric=metric)
    if devices:
        with suppress_intent_push():
            sr.devices.add(*devices)
    return sr


def _own(sr, mgmt, *, status="in_sync", mirror_vrf=None):
    """Create the overlay the reconciler would have, already carrying a generation."""
    from netbox_nso_plugin.intent_generation import allocate_intent_generation
    from netbox_nso_plugin.models import NSOStaticRouteState

    return NSOStaticRouteState.objects.create(
        management=mgmt,
        static_route=sr,
        status=status,
        nso_vrf=mirror_vrf if mirror_vrf is not None else (sr.vrf.name if sr.vrf else ""),
        nso_prefix=str(sr.prefix or ""),
        nso_next_hop=str(sr.next_hop or ""),
        accepted_at=timezone.now(),
        intent_generation=allocate_intent_generation(),
        generation_started_at=timezone.now(),
    )
