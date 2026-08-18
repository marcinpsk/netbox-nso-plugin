# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Onboarding dashboard computation — the three tiles.

Compares the NSO device inventory (from the adapter) against NetBox to produce:

- **onboarded**  — NSO devices matched to a NetBox device (+ which NED they use).
- **candidates** — NetBox devices NOT in NSO that are onboardable now: status=active
  and a management IP — primary, or OOB when there is no primary yet (NSO needs an
  address). A platform→NED mapping is only the default NED suggestion, not a requirement —
  the operator picks the NED on onboard.
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


def device_mgmt_addresses(device) -> tuple[str | None, str | None]:
    """Resolve a device's ``(primary, OOB)`` management host strings.

    The **single** source of management-address resolution, shared by onboarding and the
    failover scope push (``signals.sync_scope_to_adapter`` → ``set_scope``). Keeping both on
    this one helper guarantees the address NSO is provisioned over and the addresses the
    failover loop probes can never diverge — there is no second selection code path. Either
    element may be ``None`` (a freshly-deployed box with no primary yet; a device with no OOB).
    """
    return (
        _ip_host(getattr(device, "primary_ip", None)),
        _ip_host(getattr(device, "oob_ip", None)),
    )


def _index_netbox_devices():
    """Return (all_devices, by_id, by_name, by_primary_ip) for matching."""
    from dcim.models import Device

    devices = list(Device.objects.select_related("platform", "primary_ip4", "primary_ip6", "oob_ip", "site"))
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
        logger.exception("build_onboarding_dashboard: listing devices for instance %s failed", instance.pk)
        out["error"] = f"Could not list NSO devices ({type(exc).__name__}); see the server log."
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
    mapping; resolves the management address via :func:`device_mgmt_addresses` (primary,
    or OOB when there is no primary yet) and passes BOTH to the adapter so its failover
    bootstrap picks the reachable one; **enqueues** the adapter's ``/devices/provision``
    job (create node → fetch-host-keys → unlock → sync-from) and immediately creates the
    NSODeviceManagement row in ``provisioning`` status. The row's post_save signal is
    *gated* on that status, so the adapter mapping + scope push + sync-notify do NOT fire
    until the background job succeeds and the status-advance view flips the row to ready.

    Provisioning is async because it can take minutes (probe an unreachable primary,
    bootstrap over OOB, then a full sync-from) — far longer than the plugin's adapter
    read timeout, which previously aborted the onboard mid-flight while NSO kept going.

    The platform→NED mapping is only a *default*: passing ``ned_id`` overrides it,
    so an operator can onboard with a different NED (software version / testing) or
    onboard a device whose platform has no mapping at all.

    Returns ``{"ok", "error", "provisioning", "job_id", "managed"}``. ``ok=False`` with a
    populated ``error`` for the pre-flight failures (no NED / no primary IP / already
    managed) or an adapter enqueue failure; ``ok=True, provisioning=True`` once the job is
    queued and the row exists (the device is not yet managed — the job is still running).
    """
    from . import adapter_client as client
    from .models import NSODeviceManagement, NSOPlatformNedMapping

    result = {"ok": False, "error": None, "provisioning": False, "job_id": None, "managed": False}

    chosen_ned = (ned_id or "").strip()
    if not chosen_ned and device.platform_id is not None:
        mapping = NSOPlatformNedMapping.objects.filter(platform_id=device.platform_id).first()
        if mapping is not None:
            chosen_ned = mapping.ned_id
    if not chosen_ned:
        result["error"] = "No NED selected — pick a NED (or add a Platform → NED mapping for a default)."
        return result
    # Default to the primary (in-band) address; fall back to OOB when a freshly-deployed box
    # has no primary yet. Both are resolved by the shared device_mgmt_addresses helper and sent
    # to the adapter, whose failover bootstrap probes the primary and switches to OOB if it is
    # unreachable — the plugin never decides reachability, so onboarding can't diverge from the
    # failover loop. Require at least one address (no way to reach the device otherwise).
    primary_address, oob_address = device_mgmt_addresses(device)
    address = primary_address or oob_address
    if address is None:
        result["error"] = "Device has no primary or OOB IP — NSO needs an address to reach it."
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

    # OOB rides along as the failover fallback — a fresh device's primary (in-band) loopback is
    # usually unreachable until NSO configures it, so the adapter onboards over OOB when primary
    # is down (and when there is no primary at all, ``address`` already IS the OOB above).
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
        logger.exception("onboard_candidate: provision request failed for %s", nso_name)
        result["error"] = f"Provisioning request failed ({type(exc).__name__}); see the server log."
        return result

    job_id = str((prov or {}).get("job_id") or "")
    if not job_id:
        result["error"] = "Adapter did not return a provision job id."
        return result

    # Create the management row in 'provisioning' — its post_save signal is GATED on this
    # status (signals.sync_scope_to_adapter) so it does NOT map/scope/sync while the NSO
    # node is still being built. The dashboard polls the job (NSOOnboardStatusView): on
    # success the status flips to "" (ready), re-firing the signal to map/scope/sync; on
    # failure it records provision_failed + the steps.
    from django.utils import timezone

    try:
        NSODeviceManagement.objects.create(
            device=device,
            nso_instance=instance,
            nso_device_name=nso_name,
            onboarded_at=timezone.now(),
            onboard_status="provisioning",
            onboard_job_id=job_id,
        )
    except Exception as exc:  # noqa: BLE001 — the adapter job is already running; surface it for recovery
        # provision_device() already enqueued job_id, so NSO is building the node. Without a
        # tracking row it would become an untracked "ghost" onboard (a later re-onboard is then
        # blocked by the name clash), so record the job id prominently instead of raising.
        logger.error(
            "onboard_candidate: provision job %s started but tracking-row create failed for %s (%s)",
            job_id,
            nso_name,
            exc,
        )
        result["error"] = (
            f"Provision job {job_id} started, but the NetBox tracking row could not be created "
            f"({type(exc).__name__}; see the server log). The NSO node may still be provisioning: "
            f"recover via job {job_id}."
        )
        result["job_id"] = job_id
        return result
    # Learn the platform→NED mapping from this onboard: the first device of a
    # platform is onboarded with an explicit NED; record it so future devices of
    # the same platform appear as onboardable candidates (no-op if one exists).
    if device.platform_id is not None:
        _, created = NSOPlatformNedMapping.objects.get_or_create(
            platform_id=device.platform_id, defaults={"ned_id": chosen_ned}
        )
        result["mapping_created"] = created
    result["ok"] = True
    result["provisioning"] = True
    result["job_id"] = job_id
    return result


def _summarize_provision_failure(steps) -> str:
    """Build a one-line summary of the first failed step in a provision result."""
    for step in steps or []:
        if step.get("status") == "failed":
            detail = step.get("detail")
            return f"{step.get('step')} failed" + (f": {detail}" if detail else "")
    return "Provisioning failed."


def advance_provisioning(mgmt) -> dict:
    """Poll a provisioning row's adapter job and advance it. Idempotent + best-effort.

    Shared by three callers so a stranded async onboard self-heals no matter which runs
    first: the dashboard status poll (:class:`~netbox_nso_plugin.views.NSOOnboardStatusView`),
    the device NSO tab render, and the hourly
    :class:`~netbox_nso_plugin.jobs.AdvanceStaleOnboardingJob` sweep. On success it flips
    ``onboard_status`` to "" and re-saves — re-firing the (now un-gated)
    ``sync_scope_to_adapter`` signal → adapter mapping + scope + sync-notify.

    Returns the dict shape the poll endpoint serialises: ``{"status": ...}`` with the onboard
    state ("ready"/"provisioning"/"provision_failed"), plus ``error`` on failure or
    ``poll_error`` on a transient adapter outage (the row is kept provisioning so callers retry).
    """
    from . import adapter_client as client
    from .adapter_client import AdapterError

    # Terminal or ready row → just report it (never re-poll). Keeps the sweep/tab cheap and
    # the poll endpoint idempotent.
    if mgmt.onboard_status != "provisioning":
        return {"status": mgmt.onboard_status or "ready", "error": mgmt.onboard_error}

    if not mgmt.onboard_job_id:
        mgmt.onboard_status = "provision_failed"
        mgmt.onboard_error = "No provision job id recorded."
        mgmt.save(update_fields=["onboard_status", "onboard_error"])
        return {"status": "provision_failed", "error": mgmt.onboard_error}

    try:
        job = client.get_job(mgmt.onboard_job_id)
    except AdapterError as exc:
        # Transient — leave the row provisioning so the next poll/tab/sweep retries.
        return {"status": "provisioning", "poll_error": str(exc)}

    job_status = (job or {}).get("status")
    if job_status in ("queued", "running"):
        return {"status": "provisioning"}

    if job_status == "succeeded":
        result = (job or {}).get("result") or {}
        steps = result.get("steps") or []
        if result.get("ok"):
            # NSO node is up — flip to ready; the full save() re-fires the un-gated
            # sync_scope_to_adapter signal (adapter mapping + scope + sync-notify).
            mgmt.onboard_status = ""
            mgmt.onboard_steps = steps
            mgmt.onboard_error = ""
            mgmt.save()
            return {"status": "ready"}
        mgmt.onboard_status = "provision_failed"
        mgmt.onboard_steps = steps
        mgmt.onboard_error = _summarize_provision_failure(steps)
        mgmt.save(update_fields=["onboard_status", "onboard_steps", "onboard_error"])
        return {"status": "provision_failed", "error": mgmt.onboard_error}

    # failed / timeout / unknown-terminal
    err = (job or {}).get("error") or {}
    mgmt.onboard_status = "provision_failed"
    mgmt.onboard_error = err.get("message") or "Provision job failed."
    mgmt.save(update_fields=["onboard_status", "onboard_error"])
    return {"status": "provision_failed", "error": mgmt.onboard_error}


def advance_stale_onboarding_rows() -> tuple:
    """Advance every row still in 'provisioning' by polling its job — the periodic backstop.

    The dashboard/device-tab poll advances a row the moment its provision job finishes, but
    only while someone has that page open. This sweep (run hourly by
    :class:`~netbox_nso_plugin.jobs.AdvanceStaleOnboardingJob`) catches rows stranded because
    no page was open when the job completed. Returns ``(checked, advanced)``.
    """
    from .models import NSODeviceManagement

    rows = list(NSODeviceManagement.objects.filter(onboard_status="provisioning"))
    advanced = 0
    for mgmt in rows:
        try:
            res = advance_provisioning(mgmt)
        except Exception:  # noqa: BLE001 — one bad row must not abort the whole sweep
            logger.exception("advance_stale_onboarding_rows: failed for mgmt %s", mgmt.pk)
            continue
        if res.get("status") != "provisioning":
            advanced += 1
    if rows:
        logger.info("advance_stale_onboarding_rows: %d checked, %d advanced", len(rows), advanced)
    return len(rows), advanced


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
        logger.exception("manage_existing: creating the management row for %s failed", name)
        result["error"] = f"Could not create the management row ({type(exc).__name__}); see the server log."
        return result

    result["ok"] = True
    result["managed"] = True
    return result


def _candidates(by_id, matched_ids, mappings) -> list[dict]:
    """NetBox devices onboardable now: active + a management IP + not in NSO + mappable.

    A management IP is the primary OR the OOB address (a freshly-deployed box may only have
    OOB yet) — resolved by the shared :func:`device_mgmt_addresses`, so candidacy uses the same
    addresses onboarding will provision over. The device's platform must have a platform→NED
    mapping. The mapping filters to devices we plausibly have a NED for (so servers /
    unmanageable platforms are excluded) — it is NOT a hard NED choice: the operator can still
    pick any NED in the picker. ``ned_id`` is the mapped default shown pre-selected there;
    ``oob_only`` flags a device that would onboard over OOB (no primary yet).
    """
    candidates = []
    for d in by_id.values():
        if d.id in matched_ids:
            continue
        if (d.status or "") != "active":
            continue
        primary, oob = device_mgmt_addresses(d)
        mgmt_ip = primary or oob
        if mgmt_ip is None:
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
                "mgmt_ip": mgmt_ip,
                "oob_only": primary is None,
            }
        )
    candidates.sort(key=lambda e: e["device"].name or "")
    return candidates
