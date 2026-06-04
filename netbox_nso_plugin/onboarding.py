# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Onboarding dashboard computation — the three tiles.

Compares the NSO device inventory (from the adapter) against NetBox to produce:

- **onboarded**  — NSO devices matched to a NetBox device (+ which NED they use).
- **candidates** — NetBox devices NOT in NSO that are onboardable now: status=active,
  a primary IP (NSO needs an address), and a platform with a configured NED mapping.
- **orphans**    — NSO devices that cannot be matched to any NetBox device.

Device identity NSO↔NetBox is resolved **plugin-link → name → primary IP** so a
device onboarded outside the plugin is not shown as a false orphan.

Pure-ish: one adapter call + a few NetBox queries; no writes. Shared by the HTML
dashboard view and the CICD-facing candidates API.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _index_netbox_devices():
    """Return (all_devices, by_id, by_name, by_primary_ip) for matching."""
    from dcim.models import Device

    devices = list(Device.objects.select_related("platform", "primary_ip4", "primary_ip6", "site"))
    by_id = {d.id: d for d in devices}
    by_name = {d.name: d for d in devices if d.name}
    by_ip: dict[str, object] = {}
    for d in devices:
        ip = d.primary_ip
        if ip is not None:
            by_ip[str(ip.address.ip)] = d
    return devices, by_id, by_name, by_ip


def _match_netbox_device(nso_dev, by_id, by_name, by_ip):
    """Resolve a NSO device dict to a NetBox device: plugin-link → name → IP."""
    nb_id = nso_dev.get("onboarded_netbox_device_id")
    if nb_id and nb_id in by_id:
        return by_id[nb_id], "link"
    name = nso_dev.get("name")
    if name and name in by_name:
        return by_name[name], "name"
    addr = nso_dev.get("address")
    if addr and addr in by_ip:
        return by_ip[addr], "ip"
    return None, None


def build_onboarding_dashboard(instance) -> dict:
    """Build the three-tile structure for *instance* (an NSOInstance).

    Returns ``{instance, error, onboarded, candidates, orphans}``. On adapter
    failure ``error`` is set and the lists are empty (the view renders the banner).
    """
    from . import adapter_client as client
    from .adapter_client import AdapterError
    from .models import NSOPlatformNedMapping

    out: dict = {"instance": instance.name, "error": None, "onboarded": [], "candidates": [], "orphans": []}

    try:
        nso_devices = client.list_instance_devices(instance.adapter_instance_id)
    except AdapterError as exc:
        out["error"] = str(exc)
        return out
    except Exception as exc:  # defensive — never 500 the dashboard
        out["error"] = repr(exc)
        return out

    _devices, by_id, by_name, by_ip = _index_netbox_devices()
    mappings = {m.platform_id: m.ned_id for m in NSOPlatformNedMapping.objects.all()}

    matched_ids: set[int] = set()
    for nd in nso_devices:
        if not isinstance(nd, dict) or not nd.get("name"):
            continue
        nb, how = _match_netbox_device(nd, by_id, by_name, by_ip)
        entry = {
            "nso_name": nd.get("name"),
            "ned_id": nd.get("ned_id"),
            "platform": nd.get("platform"),
            "address": nd.get("address"),
            "admin_state": nd.get("admin_state"),
            "netbox_device": nb,
            "matched_by": how,
            "plugin_managed": bool(nd.get("onboarded_netbox_device_id")),
        }
        if nb is not None:
            matched_ids.add(nb.id)
            out["onboarded"].append(entry)
        else:
            out["orphans"].append(entry)

    out["candidates"] = _candidates(by_id, matched_ids, mappings)
    out["onboarded"].sort(key=lambda e: e["nso_name"])
    out["orphans"].sort(key=lambda e: e["nso_name"])
    return out


def _candidates(by_id, matched_ids, mappings) -> list[dict]:
    """NetBox devices onboardable now: active + primary IP + mapped platform + not in NSO."""
    candidates = []
    for d in by_id.values():
        if d.id in matched_ids:
            continue
        if (d.status or "") != "active":
            continue
        ip = d.primary_ip
        if ip is None:
            continue
        if d.platform_id is None or d.platform_id not in mappings:
            continue
        candidates.append(
            {
                "device": d,
                "platform": d.platform,
                "ned_id": mappings[d.platform_id],
                "primary_ip": str(ip.address.ip),
            }
        )
    candidates.sort(key=lambda e: e["device"].name or "")
    return candidates
