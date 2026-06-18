# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Onboarding dashboard computation — the three tiles.

Compares the NSO device inventory (from the adapter) against NetBox to produce:

- **onboarded**  — NSO devices matched to a NetBox device (+ which NED they use).
- **candidates** — NetBox devices NOT in NSO that are onboardable now: status=active
  and a primary IP (NSO needs an address). A platform→NED mapping is only the default
  NED suggestion, not a requirement — the operator picks the NED on onboard.
- **orphans**    — NSO devices that cannot be matched to any NetBox device.

Device identity NSO↔NetBox is resolved **plugin-link → name → primary IP** so a
device onboarded outside the plugin is not shown as a false orphan.

Pure-ish: one adapter call + a few NetBox queries; no writes. Shared by the HTML
dashboard view and the CICD-facing candidates API.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def _ip_host(ip) -> str | None:
    """Return the bare host string of a NetBox IPAddress (v4 or v6), or ``None``.

    ``ip.address`` is a netaddr ``IPNetwork`` when DB-loaded (``.ip`` is the host) but can be
    the raw ``"x.x.x.x/yy"`` string on an unsaved/in-memory instance — handle both. Used to
    feed NSO/adapter a plain host string for both the primary and the OOB management address.
    """
    if ip is None:
        return None
    addr = ip.address
    return str(getattr(addr, "ip", None) or str(addr).split("/")[0])


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

    out: dict = {
        "instance": instance.name,
        "error": None,
        "onboarded": [],
        "candidates": [],
        "orphans": [],
        "neds": [],
        "ned_by_nso_name": {},
    }

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

    # Available NEDs on this instance — drives the onboard NED picker. Best-effort:
    # the dashboard still works (free-text/mapping default) if the lookup fails.
    try:
        out["neds"] = [n.get("ned_id") for n in client.get_neds(instance.adapter_instance_id) if n.get("ned_id")]
    except Exception:
        out["neds"] = []

    # NED-in-use per NSO device name — lets the Managed tab show which NED a
    # managed device actually runs on (from the live NSO inventory).
    out["ned_by_nso_name"] = {
        nd.get("name"): nd.get("ned_id") for nd in nso_devices if isinstance(nd, dict) and nd.get("name")
    }

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


_NSO_NAME_INVALID = re.compile(r"[^A-Za-z0-9._-]+")


def normalize_nso_device_name(name: str) -> str:
    """Normalize a NetBox device name into a valid NSO device name.

    NSO device names are a single token — keep ``[A-Za-z0-9._-]``, replace any run
    of other characters (spaces, slashes, etc.) with a single ``-``, and trim
    leading/trailing separators (a leading/trailing ``.``/``-`` is invalid). Falls
    back to ``device`` if nothing usable remains.
    """
    cleaned = _NSO_NAME_INVALID.sub("-", (name or "").strip())
    cleaned = cleaned.strip("-.")
    return cleaned or "device"


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


def onboard_candidate(device, instance, *, ned_id=None, admin_state="unlocked", sync=True) -> dict:
    """Onboard one NetBox device into NSO (the write action).

    Resolves the NED from the explicit *ned_id* if given, else the device's platform
    mapping; resolves the address from the device's primary IP; calls the adapter's
    ``/devices/provision`` (create node → fetch-host-keys → unlock → sync-from), and
    — on success — creates the NSODeviceManagement row, whose post_save signal
    performs the adapter mapping + scope push + sync-notify.

    The platform→NED mapping is only a *default*: passing ``ned_id`` overrides it,
    so an operator can onboard with a different NED (software version / testing) or
    onboard a device whose platform has no mapping at all.

    Returns ``{"ok", "error", "steps", "managed"}``. ``ok=False`` with a populated
    ``error`` for the pre-flight failures (no NED / no primary IP / already managed).
    """
    from . import adapter_client as client
    from .models import NSODeviceManagement, NSOPlatformNedMapping

    result = {"ok": False, "error": None, "steps": [], "managed": False}

    chosen_ned = (ned_id or "").strip()
    if not chosen_ned and device.platform_id is not None:
        mapping = NSOPlatformNedMapping.objects.filter(platform_id=device.platform_id).first()
        if mapping is not None:
            chosen_ned = mapping.ned_id
    if not chosen_ned:
        result["error"] = "No NED selected — pick a NED (or add a Platform → NED mapping for a default)."
        return result
    ip = device.primary_ip
    if ip is None:
        result["error"] = "Device has no primary IP — NSO needs an address to reach it."
        return result
    if NSODeviceManagement.objects.filter(device=device).exists():
        result["error"] = "Device is already managed by NSO."
        return result

    # NSO device name = normalized NetBox name (NSO names can't hold spaces/slashes/etc.).
    nso_name = normalize_nso_device_name(device.name)
    clash = (
        NSODeviceManagement.objects.filter(nso_instance=instance, nso_device_name=nso_name)
        .exclude(device=device)
        .first()
    )
    if clash is not None:
        result["error"] = f"NSO device name '{nso_name}' is already used by {clash.device} on this instance."
        return result

    address = _ip_host(ip)
    # OOB is the failover fallback — a fresh device's primary (in-band) loopback is usually
    # unreachable until NSO configures it, so the adapter onboards over OOB when primary is
    # down. ``oob_ip`` is optional: a device without one simply has no fallback.
    oob_address = _ip_host(getattr(device, "oob_ip", None))

    try:
        prov = client.provision_device(
            nso_instance=instance.adapter_instance_id,
            device_name=nso_name,
            address=address,
            ned_id=chosen_ned,
            authgroup=_default_authgroup(),
            admin_state=admin_state,
            sync=sync,
            oob_ip=oob_address,
        )
    except Exception as exc:
        result["error"] = repr(exc)
        return result

    result["steps"] = prov.get("steps", [])
    if not prov.get("ok"):
        result["error"] = "NSO provisioning failed — see steps."
        return result

    # NSO node is up; create the management row (its signal does adapter mapping + scope + sync).
    from django.utils import timezone

    NSODeviceManagement.objects.create(
        device=device,
        nso_instance=instance,
        nso_device_name=nso_name,
        onboarded_at=timezone.now(),
        onboard_steps=result["steps"],
    )
    # Learn the platform→NED mapping from this onboard: the first device of a
    # platform is onboarded with an explicit NED; record it so future devices of
    # the same platform appear as onboardable candidates (no-op if one exists).
    if device.platform_id is not None:
        _, created = NSOPlatformNedMapping.objects.get_or_create(
            platform_id=device.platform_id, defaults={"ned_id": chosen_ned}
        )
        result["mapping_created"] = created
    result["ok"] = True
    result["managed"] = True
    return result


def manage_existing(device, instance, nso_device_name) -> dict:
    """Bring an already-in-NSO device under plugin management (no provisioning).

    For a device that exists in BOTH NSO and NetBox but has no NSODeviceManagement
    record ("external"): just create the management row. Its post_save signal
    registers the device with the adapter + pushes scope + sync-notify — there is
    nothing to provision because the NSO node already exists.

    Returns ``{"ok", "error", "managed"}``.
    """
    from .models import NSODeviceManagement

    result = {"ok": False, "error": None, "managed": False}

    name = (nso_device_name or "").strip()
    if not name:
        result["error"] = "Missing NSO device name."
        return result
    if NSODeviceManagement.objects.filter(device=device).exists():
        result["error"] = "Device is already managed by NSO."
        return result
    clash = (
        NSODeviceManagement.objects.filter(nso_instance=instance, nso_device_name=name).exclude(device=device).first()
    )
    if clash is not None:
        result["error"] = f"NSO device name '{name}' is already managed for {clash.device} on this instance."
        return result

    try:
        NSODeviceManagement.objects.create(
            device=device,
            nso_instance=instance,
            nso_device_name=name,
        )
    except Exception as exc:
        result["error"] = repr(exc)
        return result

    result["ok"] = True
    result["managed"] = True
    return result


def _candidates(by_id, matched_ids, mappings) -> list[dict]:
    """NetBox devices onboardable now: active + primary IP + not in NSO + mappable.

    The device's platform must have a platform→NED mapping. The mapping is used to
    filter to devices we plausibly have a NED for (so servers / unmanageable
    platforms are excluded) — it is NOT a hard NED choice: the operator can still
    pick any NED in the picker (e.g. a different NED for a software version, or to
    test). ``ned_id`` is the mapped default shown pre-selected in that picker.
    """
    candidates = []
    for d in by_id.values():
        if d.id in matched_ids:
            continue
        if (d.status or "") != "active":
            continue
        ip = d.primary_ip
        if ip is None:
            continue
        # Require a platform→NED mapping — filters out platforms we have no NED for
        # (servers, etc.). The picker still lets the operator override the NED.
        if d.platform_id is None or d.platform_id not in mappings:
            continue
        candidates.append(
            {
                "device": d,
                "platform": d.platform,
                "ned_id": mappings.get(d.platform_id, "") if d.platform_id is not None else "",
                "primary_ip": _ip_host(ip),
            }
        )
    candidates.sort(key=lambda e: e["device"].name or "")
    return candidates
