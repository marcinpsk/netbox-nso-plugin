# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Thin wrapper around the nso-adapter REST API (see api-contract.md).

Config resolution (per call, ~30 s in-process cache):
  - URL + non-secret settings: AdapterConnection singleton (if enabled) → PLUGINS_CONFIG/env.
  - Bearer token: PLUGINS_CONFIG/env ONLY — never the database.
"""

import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_CACHE_TTL = 30  # seconds
_cfg_cache: dict = {}
# Connect phase is capped well below the (longer) read timeout so a genuinely
# unreachable adapter fails fast, while a connected-but-slow adapter still gets
# the full read window before we conclude it is hung.
_CONNECT_TIMEOUT = 5  # seconds


class AdapterError(Exception):
    """Raised when the nso-adapter returns an error or is unreachable."""

    def __init__(self, message, code=None, detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _resolve_config() -> dict:
    """Return resolved config dict, using a short in-process cache."""
    now = time.monotonic()
    cached = _cfg_cache.get("data")
    if cached and (_cfg_cache.get("ts", 0) + _CACHE_TTL > now):
        return cached

    plugin_cfg = settings.PLUGINS_CONFIG.get("netbox_nso_plugin", {})
    # Token is ALWAYS from env/PLUGINS_CONFIG — never the DB.
    token = plugin_cfg.get("adapter_token", "")

    # Try AdapterConnection singleton for URL and non-secret settings.
    conn = None
    try:
        from .models import AdapterConnection  # noqa: PLC0415

        conn = AdapterConnection.objects.filter(enabled=True).first()
    except Exception:
        pass

    if conn:
        url = conn.url or plugin_cfg.get("adapter_url", "")
        verify_tls = conn.verify_tls
        ca_cert_path = conn.ca_cert_path or None
        timeout = conn.timeout_seconds
    else:
        url = plugin_cfg.get("adapter_url", "")
        verify_tls = True
        ca_cert_path = None
        timeout = 30

    data = {
        "url": url.rstrip("/") if url else "",
        "token": token,
        "verify_tls": verify_tls,
        "ca_cert_path": ca_cert_path,
        "timeout": timeout,
    }
    _cfg_cache["data"] = data
    _cfg_cache["ts"] = now
    return data


def _request(method, path, **kwargs):
    cfg = _resolve_config()

    if not cfg["url"]:
        raise AdapterError("Adapter URL is not configured.", code="configuration_error")
    if not cfg["token"]:
        raise AdapterError("Adapter token is not configured.", code="configuration_error")

    url = f"{cfg['url']}{path}"
    headers = {"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"}

    if not cfg["verify_tls"]:
        verify = False
    elif cfg["ca_cert_path"]:
        verify = cfg["ca_cert_path"]
    else:
        verify = True

    read_timeout = cfg["timeout"]
    connect_timeout = min(_CONNECT_TIMEOUT, read_timeout)
    try:
        session = requests.Session()
        session.trust_env = False  # Adapter is always internal — never route through system proxy.
        resp = session.request(
            method, url, headers=headers, timeout=(connect_timeout, read_timeout), verify=verify, **kwargs
        )
    except requests.exceptions.ReadTimeout as exc:
        # Connected but no response within the read window — the adapter is up but
        # hung (e.g. blocked event loop). Surface this distinctly so it is NOT
        # mistaken for "unreachable" and is visible in logs.
        logger.warning(
            "nso-adapter accepted the connection but did not respond within %ss for %s %s — it may be hung",
            read_timeout,
            method,
            path,
        )
        raise AdapterError(
            f"Adapter did not respond within {read_timeout}s (it may be hung).", code="nso_timeout"
        ) from exc
    except requests.exceptions.ConnectTimeout as exc:
        raise AdapterError(f"Adapter connect timed out after {connect_timeout}s.", code="nso_unreachable") from exc
    except requests.RequestException as exc:
        raise AdapterError(f"Adapter unreachable: {exc}", code="nso_unreachable") from exc

    if not resp.ok:
        try:
            err = resp.json().get("error", {})
        except Exception:
            err = {}
        raise AdapterError(
            err.get("message", resp.text),
            code=err.get("code", str(resp.status_code)),
            detail=err.get("detail"),
        )
    return resp.json() if resp.content else None


def onboard_device(nso_instance, nso_device_name, netbox_device_id):
    """POST /api/v1/devices — onboard a device."""
    return _request(
        "POST",
        "/api/v1/devices",
        json={
            "nso_instance": nso_instance,
            "nso_device_name": nso_device_name,
            "netbox_device_id": netbox_device_id,
        },
    )


def set_scope(adapter_device_id, attributes, auto_apply=False):
    """PUT /api/v1/devices/{id}/scope — update managed attributes and settings."""
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/scope",
        json={"attributes": attributes, "auto_apply": auto_apply},
    )


def patch_device(adapter_device_id, nso_instance=None, nso_device_name=None):
    """PATCH /api/v1/devices/{id} — re-key device mapping."""
    payload = {}
    if nso_instance is not None:
        payload["nso_instance"] = nso_instance
    if nso_device_name is not None:
        payload["nso_device_name"] = nso_device_name
    return _request("PATCH", f"/api/v1/devices/{adapter_device_id}", json=payload)


def delete_device(adapter_device_id):
    """DELETE /api/v1/devices/{id} — offboard device."""
    _request("DELETE", f"/api/v1/devices/{adapter_device_id}")


def get_device(adapter_device_id):
    """GET /api/v1/devices/{id}."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}")


def get_interfaces(adapter_device_id):
    """GET /api/v1/devices/{id}/interfaces."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/interfaces")


def get_lag_topology(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/lag-topology."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/lag-topology")


def get_interface_ips(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/interface-ips."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/interface-ips")


def get_snmp_config(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/snmp-config."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/snmp-config")


def get_static_routes(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/static-routes."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/static-routes")


def get_isis_interfaces(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/isis-interfaces → dict with 'processes' and 'interfaces' lists.

    Returns an empty dict when the device has no IS-IS state (404).
    Raises AdapterError on transport or server errors.
    """
    try:
        data = _request("GET", f"/api/v1/devices/{adapter_device_id}/isis-interfaces")
    except AdapterError as exc:
        if exc.code in ("not_found", "404"):
            return {"processes": [], "interfaces": []}
        raise
    return {"processes": data.get("processes", []), "interfaces": data.get("interfaces", [])}


def get_bgp_config(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/bgp-config → BGP router hierarchy dict.

    Returns empty dict with ``routers: []`` when the device has no BGP state (404).
    Raises AdapterError on transport or server errors.
    """
    try:
        return _request("GET", f"/api/v1/devices/{adapter_device_id}/bgp-config")
    except AdapterError as exc:
        if exc.code in ("not_found", "404"):
            return {"device_id": adapter_device_id, "routers": []}
        raise


def get_route_policy(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/route-policy → route-policy objects dict.

    Returns an empty payload when the device has no route-policy state (404).
    Raises AdapterError on transport or server errors.
    """
    _empty: dict = {
        "device_id": adapter_device_id,
        "prefix_lists": [],
        "community_lists": [],
        "as_paths": [],
        "route_maps": [],
    }
    try:
        return _request("GET", f"/api/v1/devices/{adapter_device_id}/route-policy")
    except AdapterError as exc:
        if exc.code in ("not_found", "404"):
            return _empty
        raise


def get_ospf(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/ospf → dict with 'instances' and 'interfaces' lists.

    Returns an empty payload when the device has no OSPF state (404).
    Raises AdapterError on transport or server errors.
    """
    _empty: dict = {"device_id": adapter_device_id, "instances": [], "interfaces": []}
    try:
        return _request("GET", f"/api/v1/devices/{adapter_device_id}/ospf")
    except AdapterError as exc:
        if exc.code in ("not_found", "404"):
            return _empty
        raise


def get_redistribution(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/redistribution → dict with 'entries' list.

    Returns an empty payload when the device has no redistribution state (404).
    Raises AdapterError on transport or server errors.
    """
    _empty: dict = {"device_id": adapter_device_id, "entries": []}
    try:
        return _request("GET", f"/api/v1/devices/{adapter_device_id}/redistribution")
    except AdapterError as exc:
        if exc.code in ("not_found", "404"):
            return _empty
        raise


def put_ospf_intent(adapter_device_id: int, payload: dict) -> dict:
    """PUT /api/v1/devices/{id}/ospf-intent → push full OSPF intent snapshot."""
    return _request("PUT", f"/api/v1/devices/{adapter_device_id}/ospf-intent", json=payload)


def get_state(adapter_device_id):
    """GET /api/v1/devices/{id}/state."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/state")


def trigger_sync(adapter_device_id):
    """POST /api/v1/devices/{id}/actions/sync → job_id."""
    return _request("POST", f"/api/v1/devices/{adapter_device_id}/actions/sync")


def trigger_detect_drift(adapter_device_id):
    """POST /api/v1/devices/{id}/actions/detect-drift → job_id."""
    return _request("POST", f"/api/v1/devices/{adapter_device_id}/actions/detect-drift")


def trigger_connect(adapter_device_id):
    """POST /api/v1/devices/{id}/actions/connect → job_id."""
    return _request("POST", f"/api/v1/devices/{adapter_device_id}/actions/connect")


def sync_notify(adapter_device_id):
    """POST /api/v1/devices/{id}/sync-notify — notify adapter of scope/intent change.

    Triggers an immediate sync so the user sees results without waiting for the
    scheduled poll. Returns the job dict, or None if no job was started (e.g. 409).
    A 409 (job already running) is not an error — log it and return the existing job id.
    """
    try:
        return _request("POST", f"/api/v1/devices/{adapter_device_id}/sync-notify")
    except AdapterError as exc:
        if exc.code == "conflict":
            return exc.detail  # existing job info — caller may log it
        raise


def get_job(job_id):
    """GET /api/v1/jobs/{id}."""
    return _request("GET", f"/api/v1/jobs/{job_id}")


def list_jobs(adapter_device_id):
    """GET /api/v1/jobs?device_id={id} — the device's jobs, most-recent-first."""
    return _request("GET", f"/api/v1/jobs?device_id={adapter_device_id}")


def put_intent(adapter_device_id, attributes):
    """PUT /api/v1/devices/{id}/intent — push full intent snapshot.

    ``attributes`` is a list of dicts:
      [{"interface": "...", "attribute": "...", "intent_value": ..., "accepted_at": "...Z"}, ...]
    Empty list clears the mirror.
    Returns {"device_id": ..., "attribute_count": N, "updated_at": "...Z"}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/intent",
        json={"attributes": attributes},
    )


def put_ip_intent(adapter_device_id, addresses):
    """PUT /api/v1/devices/{id}/ip-intent — push full IP intent snapshot.

    ``addresses`` is a list of dicts:
      [{"interface": "...", "address": "ip/plen", "family": "ipv4|ipv6",
        "secondary": bool, "vrf": "...", "accepted_at": "...Z"}, ...]
    Empty list clears the mirror.
    Returns {"device_id": ..., "address_count": N, "updated_at": "...Z"}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/ip-intent",
        json={"addresses": addresses},
    )


def put_snmp_intent(adapter_device_id, communities, v3_users, hosts, system_info):
    """PUT /api/v1/devices/{id}/snmp-intent — push full SNMP intent snapshot.

    ``communities`` list of dicts: {label, vault_ref, access, acl?, accepted_at?}
    ``v3_users``    list of dicts: {username, auth_vault_ref?, priv_vault_ref?, accepted_at?}
    ``hosts``       list of dicts: {address, version, notify_type, community_or_user, accepted_at?}
    ``system_info`` dict or None:  {location?, contact?, accepted_at?}

    Empty lists / None clears the respective sections.
    Returns {"device_id": ..., "community_count": N, ..., "updated_at": "...Z"}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/snmp-intent",
        json={
            "communities": communities,
            "v3_users": v3_users,
            "hosts": hosts,
            "system_info": system_info,
        },
    )


def put_static_route_intent(adapter_device_id, routes):
    """PUT /api/v1/devices/{id}/static-route-intent — push full static route intent.

    ``routes`` is a list of dicts:
      [{"vrf": "", "prefix": "10.0.0.0/8", "next_hop": "192.168.1.1",
        "metric": None, "permanent": None, "tag": None,
        "accepted_at": "...Z"}, ...]
    Empty list clears all static route intent for the device.
    Returns {"device_id": ..., "count": N}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/static-route-intent",
        json={"routes": routes},
    )


def put_isis_interface_intent(adapter_device_id, interfaces, processes=None):
    """PUT /api/v1/devices/{id}/isis-interface-intent — push full IS-IS intent.

    ``interfaces`` is a list of interface intent dicts.
    ``processes`` is an optional list of process intent dicts (M18 B3):
      [{"process_tag": "", "net": "49.0001.0001.0001.00", "is_type": "level-2",
        "metric_style": "wide", "overload_bit": None,
        "area_auth_type": None, "area_auth_key": None,
        "domain_auth_type": None, "domain_auth_key": None,
        "accepted_at": "...Z"}, ...]
    Empty lists clear all intent of the respective type for the device.
    Returns {"device_id": ..., "interface_count": N, "process_count": M}.
    """
    payload = {"interfaces": interfaces, "processes": processes or []}
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/isis-interface-intent",
        json=payload,
    )


def put_bgp_intent(adapter_device_id, routers):
    """PUT /api/v1/devices/{id}/bgp-intent — push full BGP intent snapshot (M16 B3).

    ``routers`` is a list of dicts following the BgpIntentUpdate schema:
      [{"asn": "65100", "scopes": [{"vrf": "", "address_families": [...], "peers": [...]}]}]
    Empty list clears all BGP intent for the device.
    Returns {"device_id": ..., "router_count": N}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/bgp-intent",
        json={"routers": routers},
    )


def put_route_policy_intent(adapter_device_id, objects):
    """PUT /api/v1/devices/{id}/route-policy-intent — push accepted route-policy objects (M17 B3).

    ``objects`` is a list of dicts:
      [{"family": "prefix_list", "name": "PL-RFC1918", "entries": [...], "accepted": true}]
    Returns the updated intent state for all objects on this device.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/route-policy-intent",
        json={"objects": objects},
    )


def trigger_apply(adapter_device_id, force=True):
    """POST /api/v1/devices/{id}/actions/apply → job_id.

    ``force=True`` (default) pushes all eligible attributes including in_sync.
    Returns the job dict, or raises AdapterError on conflict (existing job running).
    """
    return _request(
        "POST",
        f"/api/v1/devices/{adapter_device_id}/actions/apply",
        json={"force": force},
    )


def list_nso_devices(nso_instance_id: str) -> list[dict]:
    """GET /api/v1/nso-instances/{id}/devices — enriched device list.

    Returns a list of dicts, one per NSO device, with the fields:
      name, address, ned_id, platform, auth_group, admin_state,
      onboarded, onboarded_device_id, onboarded_netbox_device_id.
    All fields are always present; nullable fields are null when absent.
    Returns [] if the adapter returns an empty list.
    """
    result = _request("GET", f"/api/v1/nso-instances/{nso_instance_id}/devices")
    return result or []


def get_device_by_nso(nso_instance_id: str, nso_device_name: str) -> dict | None:
    """GET /api/v1/devices/by-nso — resolve (instance, name) → adapter device.

    Returns the device dict (same shape as GET /api/v1/devices/{id}) on hit.
    Returns None on 404 (device not onboarded).
    Raises AdapterError on any other error.
    """
    try:
        return _request(
            "GET",
            "/api/v1/devices/by-nso",
            params={"instance": nso_instance_id, "name": nso_device_name},
        )
    except AdapterError as exc:
        if exc.code == "not_found":
            return None
        raise
