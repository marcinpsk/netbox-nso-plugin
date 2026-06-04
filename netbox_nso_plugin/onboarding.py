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


def _default_authgroup() -> str:
    """Resolve the onboarding authgroup from the AdapterConnection setting."""
    try:
        from .models import AdapterConnection

        conn = AdapterConnection.objects.filter(enabled=True).first()
        if conn and conn.onboard_authgroup:
            return conn.onboard_authgroup
    except Exception:
        pass
    return "network"


def onboard_candidate(device, instance, *, admin_state="unlocked", sync=True) -> dict:
    """Onboard one NetBox device into NSO (the write action).

    Resolves the NED from the device's platform mapping and the address from its
    primary IP, calls the adapter's ``/devices/provision`` (create node →
    fetch-host-keys → unlock → sync-from), and — on success — creates the
    NSODeviceManagement row, whose post_save signal performs the adapter mapping +
    scope push + sync-notify (so we never double-onboard).

    Returns ``{"ok", "error", "steps", "managed"}``. ``ok=False`` with a populated
    ``error`` for the pre-flight failures (no mapping / no primary IP / already
    managed) the operator must fix first.
    """
    from . import adapter_client as client
    from .models import NSODeviceManagement, NSOPlatformNedMapping

    result = {"ok": False, "error": None, "steps": [], "managed": False}

    if device.platform_id is None:
        result["error"] = "Device has no platform — set a platform and add a Platform → NED mapping."
        return result
    mapping = NSOPlatformNedMapping.objects.filter(platform_id=device.platform_id).first()
    if mapping is None:
        result["error"] = f"No NED mapping for platform '{device.platform}'. Add one under Platform → NED Mappings."
        return result
    ip = device.primary_ip
    if ip is None:
        result["error"] = "Device has no primary IP — NSO needs an address to reach it."
        return result
    if NSODeviceManagement.objects.filter(device=device).exists():
        result["error"] = "Device is already managed by NSO."
        return result

    # ip.address is a netaddr IPNetwork when DB-loaded (.ip = host), but can be the
    # raw "x.x.x.x/yy" string on an unsaved/in-memory instance — handle both.
    addr = ip.address
    address = str(getattr(addr, "ip", None) or str(addr).split("/")[0])

    try:
        prov = client.provision_device(
            nso_instance=instance.adapter_instance_id,
            device_name=device.name,
            address=address,
            ned_id=mapping.ned_id,
            authgroup=_default_authgroup(),
            admin_state=admin_state,
            sync=sync,
        )
    except Exception as exc:
        result["error"] = repr(exc)
        return result

    result["steps"] = prov.get("steps", [])
    if not prov.get("ok"):
        result["error"] = "NSO provisioning failed — see steps."
        return result

    # NSO node is up; create the management row (its signal does adapter mapping + scope + sync).
    NSODeviceManagement.objects.create(device=device, nso_instance=instance, nso_device_name=device.name)
    result["ok"] = True
    result["managed"] = True
    return result


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
