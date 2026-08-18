# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import logging
from types import MappingProxyType

from dcim.models import Device
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.views import View
from netbox.object_actions import AddObject, BulkDelete, BulkExport
from netbox.views import generic
from utilities.views import ViewTab, register_model_view

from .adapter_client import AdapterError
from .deployment import DeploymentQuiesced
from .filters import (
    NSODerivedIntentTemplateFilterSet,
    NSODeviceManagementFilterSet,
    NSOInstanceFilterSet,
    NSOInterfaceStateFilterSet,
    NSOLinkRoleAssignmentFilterSet,
    NSOLinkRoleFilterSet,
    NSOPlatformNedMappingFilterSet,
)
from .forms import (
    AdapterConnectionForm,
    NSOBgpPeerGreenfieldForm,
    NSODerivedIntentTemplateForm,
    NSODeviceManagementForm,
    NSOFailoverSettingsForm,
    NSOInstanceForm,
    NSOInterfaceMtuStateForm,
    NSOLinkRoleAssignmentForm,
    NSOLinkRoleForm,
    NSOLoggingHostStateForm,
    NSOLoggingLevelStateForm,
    NSOPlatformNedMappingForm,
    NSOSnmpCommunityStateForm,
    NSOSnmpHostStateForm,
    NSOSnmpSystemInfoStateForm,
    NSOSnmpV3UserStateForm,
    NSOVaultSettingsForm,
)
from .models import (
    AdapterConnection,
    NSOBFDInterfaceState,
    NSOBGPPeerState,
    NSODerivedIntentTemplate,
    NSODeviceManagement,
    NSOFailoverSettings,
    NSOInstance,
    NSOInterfaceMtuState,
    NSOInterfaceState,
    NSOISISInstanceState,
    NSOISISInterfaceState,
    NSOL2SapState,
    NSOLinkRole,
    NSOLinkRoleAssignment,
    NSOLoggingHostState,
    NSOLoggingLevelState,
    NSOOSPFInstanceState,
    NSOOSPFInterfaceState,
    NSOPlatformNedMapping,
    NSORedistributionState,
    NSORoutePolicyState,
    NSOSnmpCommunityState,
    NSOSnmpHostState,
    NSOSnmpSystemInfoState,
    NSOSnmpV3UserState,
    NSOStaticRouteState,
    NSOSubinterfaceState,
    NSOSVIState,
    NSOVaultSettings,
    NSOVLANState,
)
from .signals import _STATIC_ROUTE_ARMED_FIELDS, _schedule_intent_push
from .tables import (
    NSODerivedIntentTemplateTable,
    NSODeviceManagementTable,
    NSOInstanceTable,
    NSOInterfaceStateTable,
    NSOLinkRoleAssignmentTable,
    NSOLinkRoleTable,
    NSOPlatformNedMappingTable,
)

logger = logging.getLogger(__name__)


def _device_nso_tab_url(device_pk):
    """Return the URL for the NSO tab on a device detail page."""
    return reverse("dcim:device_nso", kwargs={"pk": device_pk})


def _device_capabilities_url(device_pk):
    """Return the URL for a device's route-policy capabilities page."""
    return reverse("plugins:netbox_nso_plugin:route_policy_capabilities", kwargs={"device_pk": device_pk})


def _observe_live_read_state(device, mgmt):
    """READSEM S4 (D8b): fetch + observe the live per-family read-state on tab render.

    Runs beside ``get_device`` on its own SHORT budget (R5-5) with SEPARATE failure
    handling — a hung/absent read-state endpoint (incl. the S3 route-404) leaves the
    sync cache valid while family chips fall back to persisted rows. On success the
    observed states upsert through the R6-3 observe-only protocol (never adopts; a
    newer incarnation only sets the durable reset-pending marker). Returns
    ``(unknown_families, families_version_mismatch)`` for the tab's banners.
    """
    from . import adapter_client as client
    from .adapter_client import AdapterError
    from .families import ALL_FAMILY_KEYS, FAMILIES_VERSION
    from .read_gate import observe_aggregate

    try:
        rs_doc = client.get_device_read_state(mgmt.adapter_device_id)
    except AdapterError as exc:
        logger.debug("read-state unavailable for device %s: %s", device.pk, exc)
        return [], None
    families = rs_doc.get("families") or {}
    try:
        observe_aggregate(mgmt, families, epoch=mgmt.adapter_device_id)
    except Exception:  # noqa: BLE001 — the tab must never 500 on read-state bookkeeping
        logger.exception("read-state observation failed for device %s", device.pk)
    mgmt.refresh_from_db()
    unknown = [{"family": f, "read_state": families[f]} for f in sorted(set(families) - set(ALL_FAMILY_KEYS))]
    mismatch = None
    served_version = rs_doc.get("families_version")
    if served_version is not None and served_version != FAMILIES_VERSION:
        mismatch = {"served": served_version, "expected": FAMILIES_VERSION}
        logger.warning(
            "adapter families_version %s != plugin %s — update the older side", served_version, FAMILIES_VERSION
        )
    return unknown, mismatch


# ── Authorization for NSO action views ───────────────────────────────────────


class NSOActionPermissionMixin(LoginRequiredMixin):
    """Require a NetBox permission (not just authentication) to invoke an NSO action.

    The NSO action views are RPC-style (Accept / Apply / onboard / sync / re-point)
    rather than object CRUD, so — unlike NetBox's generic ``ObjectEditView`` etc. — they
    carry no single restrictable queryset for ``ObjectPermissionRequiredMixin`` to filter.
    Gating them on ``LoginRequiredMixin`` alone let *any* authenticated user push intent or
    apply config to a device. Each view names the NetBox permission it needs in
    ``required_permission``; it is evaluated against the user's ObjectPermissions by
    NetBox's auth backend (a superuser and any holder of a matching ObjectPermission pass).
    An authenticated user lacking it gets 403; an anonymous user still follows the normal
    login redirect (so the login-required behaviour is unchanged).

    ``required_permission`` may name a single permission or a sequence of them — ALL are
    required. A view that mints objects in ANOTHER app (the in-tab BGP-peer create builds a
    netbox_routing graph) must name that app's permission too, or the NSO permission alone
    would silently become a grant to create routing objects.
    """

    required_permission = "netbox_nso_plugin.change_nsodevicemanagement"

    def dispatch(self, request, *args, **kwargs):
        """Enforce every ``required_permission`` for authenticated users before the handler runs."""
        required = self.required_permission
        if isinstance(required, str):
            required = (required,)
        if request.user.is_authenticated and not all(request.user.has_perm(perm) for perm in required):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


# ── Device NSO Tab (registered into dcim.Device detail) ──────────────────────


@register_model_view(Device, name="nso", path="nso")
class DeviceNSOTabView(generic.ObjectView):
    """NSO tab on the Device detail page — shows management status, actions, and compliance."""

    queryset = Device.objects.all()
    template_name = "netbox_nso_plugin/device_nso_tab.html"
    tab = ViewTab(
        label="NSO",
        weight=551,  # right after Console Ports (550)
    )

    def get_extra_context(self, request, instance):
        """Counts-first, read-only tab render.

        Per-category counts come from persisted NSO*State (cheap aggregates — NO
        adapter calls, NO reconcile writes), so opening the tab is instant even for a
        2000+ interface device. Rows load lazily when a category is expanded
        (:class:`NSOCategoryView`). The persisted cache is refreshed off-render by the
        sync-complete background job. A single cheap ``get_device()`` drives the
        connection / last-sync banner and is the only adapter touch here.
        """
        from .adapter_client import AdapterError
        from .summary import category_summaries
        from .template_content import _STATUS_BADGE

        device = instance
        mgmt = getattr(device, "nso_management", None)

        # Self-heal a stranded async onboard: if the provision job already finished but no
        # dashboard/status poll was open to catch it, advance the row now so simply opening
        # this tab completes onboarding (flip to ready → the un-gated signal maps/scopes/syncs).
        # Best-effort: a poll error leaves the row provisioning and never breaks the render.
        if mgmt is not None and mgmt.onboard_status == "provisioning":
            from .onboarding import advance_provisioning

            try:
                advance_provisioning(mgmt)
            except Exception:  # noqa: BLE001 — the tab must never 500 on a self-heal poll
                logger.debug("NSO tab self-heal failed for device %s", device.pk, exc_info=True)

        adapter_error = None
        adapter_error_code = None
        intent_drift = []
        failover = None
        device_capability = None
        read_state_unknown = []
        families_version_mismatch = None
        if mgmt is not None and mgmt.adapter_device_id is not None:
            from . import adapter_client as client
            from .intent_drift import compute_intent_drift
            from .sync_cache import parse_adapter_timestamp, refresh_sync_cache

            try:
                adapter_device = client.get_device(mgmt.adapter_device_id)
                refresh_sync_cache(mgmt, adapter_device)
                # Mgmt-IP failover status (active address / last probe / OOB health) — None when
                # the device has no failover row (no primary/OOB IPs pushed yet). Parse the ISO
                # timestamps to datetimes so the template's |date filter can format them.
                failover = adapter_device.get("failover")
                if failover:
                    for key in ("last_probe_at", "last_switch_at", "oob_health_checked_at"):
                        if failover.get(key):
                            failover[key] = parse_adapter_timestamp(failover[key], key)
                # Surface adapter↔NetBox split-brain (orphaned intent) — only renders if any.
                intent_drift = compute_intent_drift(device, mgmt)
            except AdapterError as exc:
                adapter_error = str(exc)
                adapter_error_code = exc.code
                logger.debug("Adapter unavailable for device %s: %s", device.pk, exc)

            # Capability transparency (I2): the device_capability matrix's unsupported/skipped
            # rows for this device's (ned, sw) — recorded reactively when a prior apply's intent
            # was rejected by the NED. A cheap cache-only read that fails open, so it never breaks
            # the tab; only surfaced when the adapter is reachable and there are real gaps.
            # source='read' rows (H3: per-scope read-support fed by the live read probe) describe
            # mirror completeness, not rejected applies — this banner's wording is apply-specific,
            # so they render on the capabilities page instead (and still warn via apply-preflight).
            if adapter_error is None:
                read_state_unknown, families_version_mismatch = _observe_live_read_state(device, mgmt)

            if adapter_error is None:
                cap = client.get_device_capability(mgmt.adapter_device_id, refresh=False)
                if cap.get("known"):
                    gaps = [
                        e
                        for e in cap.get("elements", [])
                        if e.get("status") in ("unsupported", "skipped") and e.get("source") != "read"
                    ]
                    if gaps:
                        device_capability = {
                            "ned_id": cap.get("ned_id", ""),
                            "sw_version": cap.get("sw_version", ""),
                            "gaps": gaps,
                        }

        from .drain import degraded_deletions

        try:
            deletion_records = degraded_deletions(device.pk)
        except Exception:  # noqa: BLE001 (optional tab data must not break the render)
            logger.debug("Degraded deletion read failed for device %s", device.pk, exc_info=True)
            deletion_records = []

        return {
            "mgmt": mgmt,
            "nso_categories": category_summaries(device, mgmt),
            "adapter_error": adapter_error,
            "adapter_error_code": adapter_error_code,
            "intent_drift": intent_drift,
            # §4.3(c): a deletion that left the device configured. Durable, adapter-free and
            # cleared by the acknowledgement command alone, so it renders on every tab load
            # until an operator answers for it.
            "degraded_deletions": deletion_records,
            "failover": failover,
            "device_capability": device_capability,
            # READSEM S4 (D8/D10): honored by EVERY render path — including the
            # adapter-down persisted-rows fallback (R12: durable reset knowledge).
            "read_reset_pending": bool(
                mgmt is not None
                and (
                    mgmt.source_rekey_pending
                    or mgmt.reset_pending_born is not None
                    or mgmt.reset_pending_source_epoch is not None
                )
            ),
            "read_state_unknown": read_state_unknown,
            "families_version_mismatch": families_version_mismatch,
            "status_badge": _STATUS_BADGE,
        }


# ── Lazy category load: rows for one expanded category (HTML fragment) ─────────

# "Accept" makes NetBox the source of truth, so it applies to values NetBox does not
# yet own (imported) and to drift (resolve). Already-owned states (in_sync, accepted,
# deploying, apply_failed) offer no Accept — that was the repeatable-no-op bug.
_UNOWNED_STATUSES = ("imported", "changed", "conflict")


def _ctx_has_unowned(ctx) -> bool:
    """Return True if any reconciled routing row in *ctx* is unowned/drifted (gates 'Accept All')."""
    lists = (
        "static_routes",
        "isis_interfaces",
        "isis_processes",
        "bgp_peers",
        "route_policy_states",
        "redistribution_states",
    )
    for key in lists:
        if any(getattr(r, "status", "") in _UNOWNED_STATUSES for r in ctx.get(key) or []):
            return True
    ospf = ctx.get("ospf_data") or {}
    for key in ("instances", "interfaces"):
        if any(getattr(r, "status", "") in _UNOWNED_STATUSES for r in ospf.get(key) or []):
            return True
    return False


def _persisted_category_context(device, mgmt, key: str) -> dict:
    """Build a reconcile-on-expand category's display context from persisted state only.

    What renders when the reconcile cannot run: the device is unlinked
    (adapter_device_id is None) or the adapter errored. reconcile_category
    returns an empty context in both cases, and a panel keyed off that context
    reads "nothing configured" while NetBox is holding rows for it — an operator
    who unlinks a device watches their accepted config vanish. The grid
    categories already render from persisted state for exactly this reason
    (_grid_payload); these are the remaining server-rendered panels. Every value
    the six templates render is a model column, so the DB can serve all of it.
    """
    from .models import (
        NSOInterfaceIPState,
        NSOInterfaceMtuState,
        NSOLACPBundleState,
        NSOLoggingHostState,
        NSOSnmpCommunityState,
        NSOSnmpHostState,
        NSOSnmpSystemInfoState,
        NSOSnmpV3UserState,
        NSOSwitchportState,
    )

    def by_mgmt(model, *related):
        if mgmt is None:
            return []
        qs = model.objects.filter(management=mgmt)
        return list(qs.select_related(*related) if related else qs)

    if key == "interface_ips":
        # Keyed on interface, not management — the one overlay that survives even
        # a deleted management row.
        return {
            "interface_ips": list(
                NSOInterfaceIPState.objects.filter(interface__device=device).select_related("interface")
            )
        }
    if key == "interface_mtu":
        return {"interface_mtu_states": by_mgmt(NSOInterfaceMtuState, "interface")}
    if key == "lacp":
        bundles = (
            []
            if mgmt is None
            else list(
                NSOLACPBundleState.objects.filter(management=mgmt)
                .select_related("interface")
                # The template lists members via bundle.interface.nso_lacp_member_bundles.
                .prefetch_related("interface__nso_lacp_member_bundles__interface")
            )
        )
        return {"lacp_bundle_states": bundles}
    if key == "logging":
        return {
            "logging_data": {
                "hosts": by_mgmt(NSOLoggingHostState),
                "local_levels": (
                    NSOLoggingLevelState.objects.filter(management=mgmt).first() if mgmt is not None else None
                ),
            }
        }
    if key == "snmp":
        # snmp_value_compare is device-platform-derived, not payload-derived — without
        # it the Vault/Harvest affordances silently degrade.
        from .template_content import _snmp_value_compare_supported

        return {
            "snmp_data": {
                "communities": by_mgmt(NSOSnmpCommunityState),
                "v3_users": by_mgmt(NSOSnmpV3UserState),
                "hosts": by_mgmt(NSOSnmpHostState),
                "system_info": (
                    NSOSnmpSystemInfoState.objects.filter(management=mgmt).first() if mgmt is not None else None
                ),
                "snmp_value_compare": _snmp_value_compare_supported(device),
            }
        }
    if key == "switchport":
        states = (
            []
            if mgmt is None
            else list(
                NSOSwitchportState.objects.filter(management=mgmt)
                .select_related("interface", "untagged_vlan")
                .prefetch_related("tagged_vlans")
            )
        )
        return {"switchport_states": states}
    return {}


class NSOCategoryCountsView(LoginRequiredMixin, View):
    """JSON of every category's live counts for the device NSO tab.

    The tab renders the category header badges (total / drift / pending apply / in sync)
    server-side at page load. After a Sync/Detect-Drift/Apply, the rows can clear but the
    headers stay stale until a full reload — so the JS re-fetches these counts and rewrites
    the badges in place. Read-only aggregate over NSO*State (same source as the tab render).

    URL: /plugins/nso/devices/<pk>/category-counts/
    """

    def get(self, request, device_pk):
        """Return {categories: {key: {total, drift, pending, read}}, reset_pending}.

        ``read`` is the D10 per-category read chip (None = healthy/no chip) so the
        dynamic renderBadges path keeps chips honest across the
        ``nso:refresh-categories`` rebuild; ``reset_pending`` is the device-wide
        R11/R12 marker (the JS shows/keeps the reset banner from it).
        """
        from .summary import category_summaries

        device = get_object_or_404(Device, pk=device_pk)
        mgmt = getattr(device, "nso_management", None)
        out = {
            c["key"]: {
                "total": c["counts"].get("total", 0),
                "drift": c["counts"].get("drift", 0),
                "pending": c["counts"].get("pending", 0),
                "read": c.get("read"),
            }
            for c in category_summaries(device, mgmt)
        }
        return JsonResponse(
            {
                "categories": out,
                "reset_pending": bool(
                    mgmt is not None
                    and (
                        mgmt.source_rekey_pending
                        or mgmt.reset_pending_born is not None
                        or mgmt.reset_pending_source_epoch is not None
                    )
                ),
            }
        )


_PENDING_KINDS = {"pending", "apply_failed"}

# Worst-first ordering of per-cell state kinds, for rolling an interface's cells up
# into the single row-level state the grid sorts and quick-filters on.
_KIND_SEVERITY = ("apply_failed", "drift", "pending", "deploying", "unknown", "in_sync")

# Grid category → the intent-push scope whose rejection record belongs on its banner.
# Only the scopes whose push failures are persisted appear here (see
# signals._record_push_outcome); a category with no entry simply renders no banner.
_CATEGORY_PUSH_SCOPES = {"static": "static_route"}


# How definite a recorded push failure is. Only `configuration_error` is raised before a
# request is ever built, so it is the only code that PROVES nothing was sent.
# `nso_unreachable` does not: the client maps every generic RequestException to it,
# including a socket that drops after the body went out, and `nso_timeout` likewise leaves
# a PUT that may have committed and auto-applied. Both are unknown, not unsent — claiming
# either way would state an outcome nobody observed.
_PUSH_UNSENT_CODES = frozenset({"configuration_error"})
_PUSH_UNKNOWN_CODES = frozenset({"nso_unreachable", "nso_timeout", ""})
_PUSH_HEADLINES = {
    "rejected": "The adapter rejected the last intent push for this category — NetBox holds the edit, the device does not.",
    "unsent": "The last intent push for this category never reached the adapter — NetBox holds the edit, the device does not.",
    "unknown": "The last intent push for this category did not complete — whether the adapter stored it is unknown.",
}


def _push_error_kind(code):
    """Classify a recorded push failure as rejected / unsent / unknown."""
    if code in _PUSH_UNSENT_CODES:
        return "unsent"
    if code in _PUSH_UNKNOWN_CODES:
        return "unknown"
    return "rejected"


def _category_push_error(key, mgmt):
    """Return this category's persisted intent-push failure, classified for the banner.

    A failed push is not an adapter READ error: the operator's edit was saved and the
    device was never told. Without this the only trace is a log line — the grid would show
    a green, freshly-accepted row over intent that never landed. The classification is
    derived here rather than stored, so a record written before this existed still renders
    honestly.
    """
    scope = _CATEGORY_PUSH_SCOPES.get(key)
    if scope is None or mgmt is None:
        return None
    entry = (mgmt.intent_push_errors or {}).get(scope)
    if not isinstance(entry, dict):
        return None
    kind = _push_error_kind(entry.get("code") or "")
    return {**entry, "kind": kind, "headline": _PUSH_HEADLINES[kind]}


def _row_state(kinds) -> str:
    """Collapse one interface's cell kinds to the single worst-first row state.

    THE row state: the grid sorts on it and every quick-filter pill matches on it, so the
    chip counts must be derived from this same value. Counting by set-membership instead
    (``"drift" in kinds``) double-classified an interface with both a drifted cell and an
    apply_failed cell: it collapses to ``apply_failed``, so the Drift filter (state == drift)
    hid it — while the Drift chip still counted it. The chip promised a row the filter would
    not show.
    """
    return next((k for k in _KIND_SEVERITY if k in kinds), "in_sync")


def _merged_iface_kinds(iface, attr_states, mtu_states, sw_states, ip_states) -> set[str]:
    """Aggregate the per-attribute state kinds for one interface (matrix view).

    Each attribute cell classifies independently: enabled/description are value-aware
    (interface_row_state), the rest go through display_state. Returns the SET of kinds
    across all of the interface's cells so the view can bucket it as drift/pending/in_sync.
    """
    from .status_machine import OWNED_STATES
    from .summary import display_state, interface_row_state

    kinds: set[str] = set()
    for attr in ("enabled", "description"):
        st = attr_states.get((iface.id, attr))
        if st is not None:
            kinds.add(interface_row_state(st, iface)[0])
    for st in (mtu_states.get(iface.id), sw_states.get(iface.id)):
        if st is not None:
            kinds.add(display_state(st.status, st.status in OWNED_STATES)[0])
    for st in ip_states.get(iface.id, []):
        kinds.add(display_state(st.status, st.status in OWNED_STATES)[0])
    return kinds


def _matching_peer_ip_state(state, candidates):
    """Return the unambiguous far-end address corresponding to *state*.

    A link-role allocation records ``peer_state`` explicitly. Brownfield imports do
    not, so fall back to a single same-family/VRF address, then to the one address in
    the local subnet. Ambiguous multi-address peers deliberately yield ``None``: the
    compact editor must never guess which far-end address an operator meant to change.
    """
    from ipaddress import ip_interface

    eligible = [
        candidate for candidate in candidates if candidate.family == state.family and candidate.vrf == state.vrf
    ]
    if state.peer_state_id:
        direct = next((candidate for candidate in eligible if candidate.pk == state.peer_state_id), None)
        if direct is not None:
            return direct
    if len(eligible) == 1:
        return eligible[0]
    try:
        network = ip_interface(state.address).network
        in_subnet = [candidate for candidate in eligible if ip_interface(candidate.address).ip in network]
    except ValueError:
        return None
    return in_subnet[0] if len(in_subnet) == 1 else None


def _interface_topology_payload(interfaces, ip_states):
    """Build cable/peer row metadata and an optional peer state per local IP.

    ``Interface.link_peers`` is NetBox's canonical cable-path traversal API. Only
    cabled interfaces invoke it, and peer Interface/Device plus Cable objects are
    then bulk-loaded for serialization. Far-end IP states are also loaded in one
    query so a peer on another device can participate in the two-ended editor.
    """
    from dcim.models import Cable, Interface

    from .derived_intent import find_peer
    from .models import NSOInterfaceIPState

    raw_peers = {}
    for iface in interfaces:
        if iface.cable_id is not None:
            peer = find_peer(iface)
            if peer is not None:
                raw_peers[iface.pk] = peer.pk

    peers = Interface.objects.filter(pk__in=set(raw_peers.values())).select_related("device").in_bulk()
    cables = Cable.objects.in_bulk({iface.cable_id for iface in interfaces if iface.cable_id is not None})
    peer_states = {}
    states_by_peer = {}
    for state in (
        NSOInterfaceIPState.objects.filter(interface_id__in=set(raw_peers.values()))
        .select_related("interface", "interface__device")
        .order_by("address")
    ):
        states_by_peer.setdefault(state.interface_id, []).append(state)

    links = {}
    for iface in interfaces:
        if iface.cable_id is None:
            continue
        cable = cables.get(iface.cable_id)
        peer = peers.get(raw_peers.get(iface.pk))
        links[iface.pk] = {
            "cable": (
                {"label": str(cable), "url": cable.get_absolute_url()}
                if cable is not None
                else {"label": f"Cable {iface.cable_id}", "url": None}
            ),
            "peer": (
                {
                    "id": peer.pk,
                    "name": peer.name,
                    "url": peer.get_absolute_url(),
                    "device": peer.device.name,
                    "device_url": peer.device.get_absolute_url(),
                }
                if peer is not None
                else None
            ),
        }
        if peer is None:
            continue
        candidates = states_by_peer.get(peer.pk, [])
        for state in ip_states.get(iface.pk, []):
            match = _matching_peer_ip_state(state, candidates)
            if match is not None:
                peer_states[state.pk] = match
    return links, peer_states


def _native_ip_by_state(ip_states):
    """Resolve each overlay to the matching native IPAddress, if one exists.

    Prefer an object assigned to the reporting interface, then an unassigned object,
    then any matching object (the conflict case). The latter is linkable for diagnosis
    but is not considered editable by :func:`_editable_native_ip` below.
    """
    from dcim.models import Interface
    from django.contrib.contenttypes.models import ContentType
    from ipam.models import IPAddress

    states = [state for values in ip_states.values() for state in values]
    if not states:
        return {}
    interface_type_id = ContentType.objects.get_for_model(Interface).pk
    candidates = {}
    for ip_obj in IPAddress.objects.filter(address__in={state.address for state in states}).select_related("vrf"):
        key = (str(ip_obj.address), ip_obj.vrf.name if ip_obj.vrf else "")
        candidates.setdefault(key, []).append(ip_obj)

    resolved = {}
    for state in states:
        matches = candidates.get((state.address, state.vrf), [])
        own = next(
            (
                ip_obj
                for ip_obj in matches
                if ip_obj.assigned_object_type_id == interface_type_id
                and ip_obj.assigned_object_id == state.interface_id
            ),
            None,
        )
        unassigned = next((ip_obj for ip_obj in matches if ip_obj.assigned_object_id is None), None)
        linked = own or unassigned or (matches[0] if matches else None)
        if linked is not None:
            resolved[state.pk] = linked
    return resolved


def _paged_row_bucket(status: str, owned: bool) -> str:
    """Bucket one paged-overlay row into the drift / pending / in_sync quick-filter group.

    Mirrors ``summary.display_state`` + the ``_table_filter.html`` ``kindOf`` JS so the
    server-side paged filter and the client-side non-paged filter agree exactly.
    """
    if status in ("imported", "in_sync", "deploying"):
        return "in_sync"
    if status == "apply_failed":
        return "pending"
    return "pending" if owned else "drift"  # changed / conflict / accepted


def _filter_ifaces_by_state(ordered, kinds_by_iface, state):
    """Filter the interface list to those matching the quick-select *state* bucket."""
    if state == "drift":
        return [i for i in ordered if "drift" in kinds_by_iface[i.id]]
    if state == "pending":
        return [i for i in ordered if kinds_by_iface[i.id] & _PENDING_KINDS]
    if state == "in_sync":
        return [
            i
            for i in ordered
            if kinds_by_iface[i.id]
            and "drift" not in kinds_by_iface[i.id]
            and not (kinds_by_iface[i.id] & _PENDING_KINDS)
        ]
    return ordered  # "all" (or unknown) → no filter


class NSOCategoryView(LoginRequiredMixin, View):
    """Return one category's rows for the device NSO tab, fetched on expand.

    The tab renders counts-first; when an operator expands a category, the browser
    GETs this view, which does a push-suppressed scoped reconcile of just that
    category and renders its partial. Keeps the page render itself counts-only.

    URL: /plugins/nso/devices/<pk>/category/<key>/
    """

    # interfaces is handled by _render_interfaces_page (paginated); the rest reconcile-on-expand.
    _PARTIALS = {
        "interface_ips": "netbox_nso_plugin/categories/interface_ips.html",
        "lacp": "netbox_nso_plugin/categories/lacp.html",
        "vlan": "netbox_nso_plugin/categories/vlan.html",
        "switchport": "netbox_nso_plugin/categories/switchport.html",
        "svi": "netbox_nso_plugin/categories/svi.html",
        "subinterface": "netbox_nso_plugin/categories/subinterface.html",
        "interface_mtu": "netbox_nso_plugin/categories/interface_mtu.html",
        "static": "netbox_nso_plugin/categories/static.html",
        "isis": "netbox_nso_plugin/categories/isis.html",
        "ospf": "netbox_nso_plugin/categories/ospf.html",
        "bgp": "netbox_nso_plugin/categories/bgp.html",
        "bfd": "netbox_nso_plugin/categories/bfd.html",
        "route_policy": "netbox_nso_plugin/categories/route_policy.html",
        "redistribution": "netbox_nso_plugin/categories/redistribution.html",
        "snmp": "netbox_nso_plugin/categories/snmp.html",
        "logging": "netbox_nso_plugin/categories/logging.html",
        "l2_services": "netbox_nso_plugin/categories/l2_services.html",
    }

    # Rows-per-page for the (potentially huge) interfaces table.
    _INTERFACES_PER_PAGE = 50

    def get(self, request, pk, key):
        """Render one category's rows.

        Interfaces (potentially thousands) are served read-only and **paginated +
        name-filterable** straight from persisted NSOInterfaceState — no reconcile,
        so a page loads in milliseconds. The cache is refreshed off-render by the
        sync-complete job or the Refresh button. Routing categories are small, so
        they keep the on-expand suppressed scoped reconcile.
        """
        device = get_object_or_404(Device, pk=pk)
        if key == "interface":
            return self._render_interface_merged(request, device)
        if key == "interfaces":
            return self._render_interfaces_page(request, device)

        mgmt = getattr(device, "nso_management", None)

        # Client-side grids: all rows at once, filtered/sorted in the browser. Must come
        # before the paginated path — static/redistribution used to render from it.
        if key in self._GRID_CATEGORIES:
            return self._render_grid_category(request, device, mgmt, key)

        # Large single-table categories render paginated from last-synced state
        # (fast); ?refresh=1 (the Refresh icon) forces a live reconcile first.
        paged = self._render_paged_category(request, device, mgmt, key)
        if paged is not None:
            return paged

        # Remaining (small / multi-table) categories keep the on-expand reconcile.
        from .reconcile import reconcile_category
        from .template_content import _STATUS_BADGE

        partial = self._PARTIALS.get(key)
        if partial is None:
            # ``key`` is a raw URL segment reflected into an HTML body: escape it.
            return HttpResponseBadRequest(f"unknown category: {escape(key)}")

        ctx = {"object": device, "mgmt": mgmt, "status_badge": _STATUS_BADGE}
        if mgmt is not None and mgmt.adapter_device_id is not None:
            try:
                ctx.update(reconcile_category(device, mgmt, key))
            except (AdapterError, DeploymentQuiesced) as exc:
                ctx["adapter_error"] = str(exc)
                ctx["adapter_error_code"] = getattr(exc, "code", "deployment_quiesced")
                # The banner explains the failed refresh; the rows must not vanish
                # with it — render last-synced state underneath.
                ctx.update(_persisted_category_context(device, mgmt, key))
            else:
                # READSEM S4 (D9): any skip disposition means the reconciler body
                # never ran — its ctx entries are still empty defaults. Fill THOSE
                # from persisted rows so last-known state stays visible (fresh
                # results from families that DID run are kept as-is).
                gate = ctx.get("_gate") or {}
                if any(str(d).startswith("skipped_") for d in gate.values()):
                    from .reconcile import _empty_context

                    defaults = _empty_context()
                    for pkey, pval in _persisted_category_context(device, mgmt, key).items():
                        if pkey in defaults and ctx.get(pkey) == defaults[pkey]:
                            ctx[pkey] = pval
        else:
            # Unlinked device: nothing to reconcile, but NetBox still holds state.
            ctx.update(_persisted_category_context(device, mgmt, key))
        _annotate_residue_rows(ctx, key, mgmt)
        ctx["category_has_unowned"] = _ctx_has_unowned(ctx)
        return render(request, partial, ctx)

    # Single-table overlay categories that render paginated from last-synced state.
    # spec: model, ctx var the partial loops, partial, search fields, order, FKs to
    # select_related, and the filter-box placeholder. Freshness comes from the
    # sync-complete / scheduler reconcile (reconcile_device covers every one of these).
    def _paged_category_specs(self):
        from .models import (
            NSOL2SapState,
            NSOSubinterfaceState,
            NSOSVIState,
            NSOVLANState,
        )

        base = "netbox_nso_plugin/categories/"
        return {
            # route_policy + static + redistribution used to live here. They are
            # client-side grids now (_GRID_CATEGORIES) — all rows at once, sorted/
            # filtered in the browser — so they no longer paginate or search server-side.
            "vlan": dict(
                model=NSOVLANState,
                ctx="vlan_states",
                partial=base + "vlan.html",
                search=["vlan__name", "device_name"],
                order=["vlan__vid"],
                sr=["vlan"],
                ph="Filter by name…",
            ),
            "svi": dict(
                model=NSOSVIState,
                ctx="svi_states",
                partial=base + "svi.html",
                search=["interface__name", "vrf"],
                order=["interface__name"],
                sr=["interface", "vlan"],
                ph="Filter by interface / VRF…",
            ),
            "subinterface": dict(
                model=NSOSubinterfaceState,
                ctx="subinterface_states",
                partial=base + "subinterface.html",
                search=["interface__name", "vrf"],
                order=["interface__name"],
                sr=["interface", "parent_interface"],
                ph="Filter by interface / VRF…",
            ),
            "l2_services": dict(
                model=NSOL2SapState,
                ctx="l2_sap_states",
                partial=base + "l2_services.html",
                search=["service_name", "port", "sap_id"],
                order=["service_name", "sap_id"],
                sr=["l2vpn", "termination"],
                ph="Filter by service / port / tag…",
            ),
        }

    def _render_paged_category(self, request, device, mgmt, key):
        """Render a single-table category paginated from last-synced NSO*State.

        Returns None if *key* isn't a paginated category (caller falls back to the
        reconcile-on-expand path). Reads persisted rows (fast); ?refresh=1 forces a
        live reconcile first. Server-side ?q filter + ?page, driven by the shared
        pager JS so paging/search keep the card open.
        """
        from django.core.paginator import Paginator
        from django.db.models import Q

        from .status_machine import OWNED_STATES
        from .template_content import _STATUS_BADGE

        spec = self._paged_category_specs().get(key)
        if spec is None:
            return None

        adapter_error = None
        if request.GET.get("refresh") and mgmt is not None and mgmt.adapter_device_id is not None:
            from .reconcile import reconcile_category

            try:
                reconcile_category(device, mgmt, key)
            except (AdapterError, DeploymentQuiesced) as exc:
                adapter_error = str(exc)

        qs = spec["model"].objects.filter(management=mgmt)
        if spec["sr"]:
            qs = qs.select_related(*spec["sr"])
        qs = qs.order_by(*spec["order"])

        q = (request.GET.get("q") or "").strip()
        if q:
            cond = Q()
            for field in spec["search"]:
                cond |= Q(**{f"{field}__icontains": q})
            qs = qs.filter(cond)

        # Server-side state quick-filter — applied HERE (the one renderer for every paged
        # category) so route_policy / static / redistribution / vlan / svi / subinterface /
        # l2_services all get the drift/pending/in-sync quick-select without per-template work.
        rows = list(qs)
        bucketed = [(_paged_row_bucket(r.status, r.status in OWNED_STATES), r) for r in rows]
        state_counts = {"all": len(rows), "drift": 0, "pending": 0, "in_sync": 0}
        for bucket, _row in bucketed:
            state_counts[bucket] += 1
        state = request.GET.get("state") or "all"
        if state in ("drift", "pending", "in_sync"):
            rows = [r for bucket, r in bucketed if bucket == state]
        else:
            state = "all"

        has_unowned = any(r.status in _UNOWNED_STATUSES for _b, r in bucketed)
        paginator = Paginator(rows, self._INTERFACES_PER_PAGE)
        page = paginator.get_page(request.GET.get("page") or 1)
        page_rows = list(page.object_list)
        if key == "l2_services":
            from dcim.models import Interface

            port_urls = {
                interface.name: interface.get_absolute_url()
                for interface in Interface.objects.filter(
                    device=device,
                    name__in={row.port for row in page_rows if row.port},
                )
            }
            for row in page_rows:
                row.port_url = port_urls.get(row.port)

        ctx = {
            "object": device,
            "mgmt": mgmt,
            "status_badge": _STATUS_BADGE,
            spec["ctx"]: page_rows,
            "page": page,
            "q": q,
            "state": state,
            "state_counts": state_counts,
            "placeholder": spec["ph"],
            "category_has_unowned": has_unowned,
            "adapter_error": adapter_error,
            "paged": True,
        }
        _annotate_residue_rows(ctx, key, mgmt)
        return render(request, spec["partial"], ctx)

    def _render_interface_merged(self, request, device):
        """Consolidated per-interface view: one row per interface, a column per attribute.

        Folds the four scattered per-interface scalar overlays (enabled/description,
        IPs, MTU, switchport) into a single client-side grid. Pivots the persisted
        NSO*State rows by interface; each attribute cell reuses that overlay's own
        status badge + Accept endpoint, so per-attribute Accept/Apply still works.
        Sorting, filtering and the drift/pending quick-filter all happen in the
        browser, so every row ships at once (json_script on first paint,
        ?format=json for post-action reloads).
        """
        from .models import (
            NSOInterfaceIPState,
            NSOInterfaceMtuState,
            NSOSwitchportState,
        )
        from .reconcile import reconcile_category

        mgmt = getattr(device, "nso_management", None)

        # Read from last-synced state (fast); the Refresh icon (?refresh=1) forces a
        # live reconcile. Freshness otherwise comes from the sync-complete reconcile.
        adapter_error = None
        if request.GET.get("refresh") and mgmt is not None and mgmt.adapter_device_id is not None:
            try:
                reconcile_category(device, mgmt, "interface")
            except (AdapterError, DeploymentQuiesced) as exc:
                adapter_error = str(exc)

        dev_filter = {"interface__device": device}
        # Index every overlay by interface id (enabled/description keyed by attribute).
        attr_states: dict[tuple[int, str], NSOInterfaceState] = {}
        for st in NSOInterfaceState.objects.filter(**dev_filter).select_related("interface"):
            attr_states[(st.interface_id, st.attribute)] = st
        mtu_states = {
            st.interface_id: st for st in NSOInterfaceMtuState.objects.filter(**dev_filter).select_related("interface")
        }
        sw_states = {
            st.interface_id: st
            for st in NSOSwitchportState.objects.filter(**dev_filter)
            .select_related("interface", "untagged_vlan")
            .prefetch_related("tagged_vlans")
        }
        ip_states: dict[int, list] = {}
        ifaces: dict[int, object] = {}
        for st in NSOInterfaceIPState.objects.filter(**dev_filter).select_related("interface").order_by("address"):
            ip_states.setdefault(st.interface_id, []).append(st)
            ifaces[st.interface_id] = st.interface
        for (iface_id, _attr), st in attr_states.items():
            ifaces[iface_id] = st.interface
        for iface_id, st in mtu_states.items():
            ifaces[iface_id] = st.interface
        for iface_id, st in sw_states.items():
            ifaces[iface_id] = st.interface

        # The drift modal compares the observed switchport against the LIVE native
        # Interface value. Reload the small interface set with both VLAN relations
        # prefetched so serialising that second side does not become an N+1 query.
        from dcim.models import Interface

        ordered = sorted(
            Interface.objects.filter(pk__in=ifaces).select_related("untagged_vlan").prefetch_related("tagged_vlans"),
            key=lambda i: i.name,
        )
        links_by_iface, peer_ip_states = _interface_topology_payload(ordered, ip_states)
        native_ips = _native_ip_by_state(ip_states)

        # Per-interface aggregate state for the grid's row-level rollup and the
        # drift/pending quick-filter counts.
        kinds_by_iface = {i.id: _merged_iface_kinds(i, attr_states, mtu_states, sw_states, ip_states) for i in ordered}
        # Counted off the COLLAPSED row state — the exact value each quick-filter pill matches
        # on — so a chip can never promise rows its own filter hides (see _row_state).
        counts = {"all": len(ordered), "drift": 0, "pending": 0}
        for ks in kinds_by_iface.values():
            state = _row_state(ks)
            counts["drift"] += 1 if state == "drift" else 0
            counts["pending"] += 1 if state in _PENDING_KINDS else 0

        # The client-side grid sorts, filters and quick-filters in the browser, so the
        # payload always carries every row — no server state filter, no pagination.
        # ?format=json serves the grid's post-action reloads; the plain fragment embeds
        # the same payload via json_script so first paint needs no second request.
        payload = self._interface_merged_payload(
            ordered,
            kinds_by_iface,
            counts,
            attr_states,
            mtu_states,
            sw_states,
            ip_states,
            links_by_iface,
            peer_ip_states,
            native_ips,
            adapter_error,
        )
        if request.GET.get("format") == "json":
            return JsonResponse(payload)

        return render(
            request,
            "netbox_nso_plugin/categories/interface.html",
            {
                "object": device,
                "grid_payload": payload,
                "counts": counts,
                "adapter_error": adapter_error,
            },
        )

    # Statuses whose rows offer an Accept action — mirrors _accept_cell.html exactly.
    _ACCEPTABLE_STATUSES = ("imported", "changed", "conflict", "drifted")

    # ── Category grids (nso-grid.js) ─────────────────────────────────────────────
    #
    # Categories rendered as a client-side grid instead of a server-rendered table.
    # Unlike Interfaces — where each attribute is its own overlay row and Accept is per
    # CELL — every routing overlay owns the whole ROW: one status, one accept_url. So
    # they all share one row serializer and differ only in their display fields.
    #
    # The payload is ALWAYS built from persisted overlay state, never from the reconcile's
    # return value. reconcile_category yields an empty context for a device with no
    # adapter_device_id, and a panel keyed off that context claims the category is empty
    # while NetBox is holding rows for it — an operator who unlinks a device watches their
    # accepted config vanish from the UI. Reading the DB is also what lets ?format=json
    # serve the grid's post-action reload without re-reconciling: the grid re-fetches after
    # every action, so reconciling there would mean a fresh device read per Accept click.
    #
    # reconcile_on_expand: these categories reconcile when first expanded (they used to
    # live on the reconcile-on-expand path). static/redistribution came off the paginated
    # path and reconcile only when the Refresh icon asks (?refresh=1).
    _GRID_CATEGORIES = ("lacp", "bfd", "ospf", "isis", "bgp", "static", "redistribution", "route_policy")

    def _grid_specs(self):
        """Per-category grid spec: sub-tables, their rows, and their display fields.

        sections maps section name -> (queryset, accept route, display fields). A
        single-section category ships a flat {rows, counts} payload; a multi-section one
        ships {"<section>": {rows, counts}, ...} and each grid picks its own section
        client-side (nso-grid.js opts.extract).
        """
        from .models import (
            NSOBFDInterfaceState,
            NSOBGPPeerState,
            NSOBGPPeerTemplateState,
            NSOISISInstanceState,
            NSOISISInterfaceState,
            NSOLACPBundleState,
            NSOOSPFInstanceState,
            NSOOSPFInterfaceState,
            NSORedistributionState,
            NSORoutePolicyState,
            NSOStaticRouteState,
        )

        r = "plugins:netbox_nso_plugin:"

        def iface(st):
            return {"name": st.interface.name, "url": st.interface.get_absolute_url()}

        def linked(obj):
            """Render a resolved netbox-routing object, or None when it never matched."""
            return {"label": str(obj), "url": obj.get_absolute_url()} if obj else None

        def by_device(model, device, *sr):
            qs = model.objects.filter(management__device=device)
            return qs.select_related(*sr) if sr else qs

        def bgp_source(peer):
            if peer is None:
                return None
            if peer.update_source_id:
                return peer.update_source.name
            if peer.source_id:
                return str(peer.source.address).split("/")[0]
            return None

        def bgp_address_families(owner):
            if owner is None:
                return []
            out = []
            for paf in owner.address_families.all():
                inbound = [obj.name for obj in (paf.prefixlist_in, paf.routemap_in) if obj is not None]
                outbound = [obj.name for obj in (paf.prefixlist_out, paf.routemap_out) if obj is not None]
                out.append(
                    {
                        "af": paf.address_family.address_family,
                        "enabled": paf.enabled is not False,
                        "inbound": ", ".join(inbound) or None,
                        "outbound": ", ".join(outbound) or None,
                    }
                )
            return sorted(out, key=lambda item: item["af"])

        def redistribution_metric_types(state):
            options = {
                "ospf": (("1", "Type 1"), ("2", "Type 2")),
                "isis": (("internal", "Internal"), ("external", "External")),
            }
            return [
                {"value": "", "label": "Default"},
                *({"value": value, "label": label} for value, label in options.get(state.dest_protocol, ())),
            ]

        def lacp_member_states(state):
            return [
                member
                for member in state.interface.nso_lacp_member_bundles.all()
                if member.management_id == state.management_id
            ]

        def lacp_members(state):
            return [
                {
                    "interface": iface(member),
                    "mode": member.mode or None,
                    "port_priority": member.port_priority,
                    "edit_url": reverse(r + "overlay_field_edit", args=["lacp_member", member.pk]),
                }
                for member in lacp_member_states(state)
            ]

        return {
            "lacp": {
                "reconcile_on_expand": True,
                "sections": {
                    None: dict(
                        ctx="lacp_bundle_states",
                        qs=lambda d: (
                            by_device(NSOLACPBundleState, d, "interface")
                            .prefetch_related("interface__nso_lacp_member_bundles__interface")
                            .order_by("interface__name")
                        ),
                        accept=r + "lacp_accept_bundle",
                        related=lacp_member_states,
                        fields={
                            "bundle": iface,
                            "lag_id": lambda st: st.lag_id,
                            "min_links": lambda st: st.min_links,
                            "system_priority": lambda st: st.system_priority,
                            "system_id": lambda st: st.system_id or None,
                            "timer": lambda st: st.timer or None,
                            "admin_key": lambda st: st.admin_key,
                            # NX-P2: surface the vPC-protected flag so the operator sees which
                            # bundles are not onboardable (Accept is refused for them).
                            "vpc_sensitive": lambda st: st.vpc_sensitive,
                            "members": lacp_members,
                            "edit_url": lambda st: reverse(r + "overlay_field_edit", args=["lacp_bundle", st.pk]),
                        },
                    )
                },
            },
            "bfd": {
                "reconcile_on_expand": True,
                "sections": {
                    None: dict(
                        ctx="bfd_states",
                        qs=lambda d: by_device(NSOBFDInterfaceState, d, "interface").order_by("interface__name"),
                        accept=r + "bfd_accept",
                        fields={
                            "iface": iface,
                            "micro_bfd": lambda st: st.micro_bfd,
                            "min_tx": lambda st: st.min_tx,
                            "min_rx": lambda st: st.min_rx,
                            "multiplier": lambda st: st.multiplier,
                            "edit_url": lambda st: reverse(r + "overlay_field_edit", args=["bfd", st.pk]),
                        },
                    )
                },
            },
            "ospf": {
                "reconcile_on_expand": True,
                "sections": {
                    "instances": dict(
                        ctx="ospf_data.instances",
                        qs=lambda d: by_device(NSOOSPFInstanceState, d, "ospf_instance").order_by("process_id"),
                        accept=r + "routing_accept_ospf_instance",
                        fields={
                            "process_id": lambda st: st.process_id,
                            "vrf": lambda st: st.vrf or "global",
                            "router_id": lambda st: st.router_id or None,
                            "instance": lambda st: linked(st.ospf_instance),
                            "edit_url": lambda st: (
                                reverse(r + "overlay_field_edit", args=["ospf_instance", st.pk])
                                if st.ospf_instance_id
                                else None
                            ),
                        },
                    ),
                    "interfaces": dict(
                        ctx="ospf_data.interfaces",
                        qs=lambda d: by_device(NSOOSPFInterfaceState, d, "interface").order_by("interface__name"),
                        accept=r + "routing_accept_ospf_interface",
                        fields={
                            "iface": iface,
                            "process_id": lambda st: st.process_id,
                            "area_id": lambda st: st.area_id or None,
                            "network_type": lambda st: st.network_type or None,
                            "cost": lambda st: st.cost,
                            "passive": lambda st: st.passive,
                            "edit_url": lambda st: reverse(r + "overlay_field_edit", args=["ospf_interface", st.pk]),
                        },
                    ),
                },
            },
            "isis": {
                "reconcile_on_expand": True,
                "sections": {
                    "interfaces": dict(
                        ctx="isis_interfaces",
                        qs=lambda d: by_device(NSOISISInterfaceState, d, "interface").order_by("interface__name", "af"),
                        accept=r + "routing_accept_isis_interface",
                        fields={
                            "iface": iface,
                            "af": lambda st: st.af,
                            "process_tag": lambda st: st.process_tag or "default",
                            "circuit_type": lambda st: st.circuit_type or None,
                            "network_type": lambda st: st.network_type or None,
                            "metric": lambda st: st.metric,
                            "passive": lambda st: st.passive,
                            "bfd_enabled": lambda st: st.bfd_enabled,
                            "frr_enabled": lambda st: st.frr_enabled,
                            "frr_protection": lambda st: st.frr_protection or None,
                            "hello_auth": lambda st: (
                                (st.hello_auth_type or "on") if (st.hello_auth_present or st.hello_auth_type) else None
                            ),
                            "edit_url": lambda st: (
                                reverse(r + "overlay_field_edit", args=["isis_interface", st.pk])
                                if st.isis_interface_id
                                else None
                            ),
                        },
                    ),
                    "instances": dict(
                        ctx="isis_processes",
                        qs=lambda d: by_device(NSOISISInstanceState, d, "isis_instance").order_by("process_tag"),
                        accept=r + "routing_accept_isis_instance",
                        fields={
                            "process_tag": lambda st: st.process_tag or "default",
                            "instance": lambda st: linked(st.isis_instance),
                            "net": lambda st: st.net or None,
                            "is_type": lambda st: st.is_type or None,
                            "metric_style": lambda st: st.metric_style or None,
                            "overload_bit": lambda st: st.overload_bit,
                            "fast_reroute": lambda st: st.fast_reroute or None,
                            "microloop_avoidance": lambda st: st.microloop_avoidance,
                            "area_auth": lambda st: f"{st.area_auth_type or '—'}{' ✓' if st.area_auth_present else ''}",
                            "domain_auth": lambda st: (
                                f"{st.domain_auth_type or '—'}{' ✓' if st.domain_auth_present else ''}"
                            ),
                            "edit_url": lambda st: (
                                reverse(r + "overlay_field_edit", args=["isis_instance", st.pk])
                                if st.isis_instance_id
                                else None
                            ),
                        },
                    ),
                },
            },
            "bgp": {
                "reconcile_on_expand": True,
                "sections": {
                    "peers": dict(
                        ctx="bgp_peers",
                        qs=lambda d: (
                            by_device(
                                NSOBGPPeerState,
                                d,
                                "bgp_peer",
                                "bgp_peer__local_as",
                                "bgp_peer__peer_group",
                                "bgp_peer__source",
                                "bgp_peer__update_source",
                            )
                            .prefetch_related(
                                "bgp_peer__address_families__address_family",
                                "bgp_peer__address_families__prefixlist_in",
                                "bgp_peer__address_families__prefixlist_out",
                                "bgp_peer__address_families__routemap_in",
                                "bgp_peer__address_families__routemap_out",
                            )
                            .order_by("asn_str", "vrf_name", "peer_address_str")
                        ),
                        accept=r + "routing_accept_bgp_peer",
                        fields={
                            "asn": lambda st: st.asn_str,
                            "vrf": lambda st: st.vrf_name or "global",
                            "peer_address": lambda st: st.peer_address_str,
                            "remote_as": lambda st: st.remote_as_str or None,
                            # Junos deactivate / admin-down: the row exists but is inert.
                            "disabled": lambda st: st.enabled is False,
                            "peer": lambda st: linked(st.bgp_peer),
                            "enabled": lambda st: st.enabled,
                            "local_as": lambda st: (
                                str(st.bgp_peer.local_as.asn) if st.bgp_peer_id and st.bgp_peer.local_as_id else None
                            ),
                            "peer_group": lambda st: (
                                st.bgp_peer.peer_group.name if st.bgp_peer_id and st.bgp_peer.peer_group_id else None
                            ),
                            "source": lambda st: bgp_source(st.bgp_peer),
                            "ttl": lambda st: st.bgp_peer.ttl if st.bgp_peer_id else None,
                            "bfd_enabled": lambda st: st.bgp_peer.bfd_enabled if st.bgp_peer_id else None,
                            "address_families": lambda st: bgp_address_families(st.bgp_peer),
                            "edit_url": lambda st: (
                                reverse(r + "overlay_field_edit", args=["bgp_peer", st.pk]) if st.bgp_peer_id else None
                            ),
                        },
                    ),
                    "templates": dict(
                        ctx="bgp_peer_templates",
                        qs=lambda d: (
                            by_device(NSOBGPPeerTemplateState, d, "template")
                            .prefetch_related(
                                "template__address_families__address_family",
                                "template__address_families__prefixlist_in",
                                "template__address_families__prefixlist_out",
                                "template__address_families__routemap_in",
                                "template__address_families__routemap_out",
                            )
                            .order_by("template_name")
                        ),
                        accept=r + "routing_accept_bgp_peer_template",
                        fields={
                            "template_name": lambda st: st.template_name,
                            "remote_as": lambda st: st.remote_as_str or None,
                            # BGPPeerTemplate intentionally has no detail view in the fork.
                            "template": lambda st: {"label": str(st.template)} if st.template else None,
                            "address_families": lambda st: bgp_address_families(st.template),
                        },
                    ),
                },
            },
            "static": {
                "reconcile_on_expand": False,
                "sections": {
                    None: dict(
                        ctx="static_routes",
                        qs=lambda d: by_device(NSOStaticRouteState, d, "static_route").order_by("nso_prefix"),
                        accept=r + "routing_accept_static_route",
                        # Static routes are the one family that settles per route with a
                        # per-route reason, so an owned apply_failed here renders as its
                        # own red state carrying the message instead of a blue
                        # "pending apply" chip promising an Apply that already lost.
                        distinguish_failed=True,
                        fields={
                            "vrf": lambda st: st.nso_vrf or "global",
                            "prefix": lambda st: st.nso_prefix,
                            "next_hop": lambda st: st.nso_next_hop or None,
                            "metric": lambda st: st.static_route.metric if st.static_route else None,
                            "permanent": lambda st: st.static_route.permanent if st.static_route else None,
                            "tag": lambda st: st.static_route.tag if st.static_route else None,
                            "route": lambda st: linked(st.static_route),
                            "error": lambda st: st.last_apply_error or None,
                            # An `unproven` verdict's reason: the apply landed and nothing
                            # proves it. Not a status — a qualifier on one.
                            "advisory": lambda st: st.last_result_advisory or None,
                            "edit_url": lambda st: (
                                reverse(r + "overlay_field_edit", args=["static_route", st.pk])
                                if st.static_route_id
                                else None
                            ),
                        },
                    )
                },
            },
            "redistribution": {
                "reconcile_on_expand": False,
                "sections": {
                    None: dict(
                        ctx="redistribution_states",
                        qs=lambda d: by_device(NSORedistributionState, d, "redistribution").order_by(
                            "dest_protocol", "source_protocol"
                        ),
                        accept=r + "routing_accept_redistribution",
                        fields={
                            "dest_protocol": lambda st: st.dest_protocol,
                            "dest_ref": lambda st: st.dest_ref or None,
                            "source_protocol": lambda st: st.source_protocol,
                            "source_ref": lambda st: st.source_ref or None,
                            "route_map": lambda st: st.route_map or None,
                            "metric": lambda st: st.metric,
                            "metric_type": lambda st: st.metric_type or None,
                            "metric_type_options": redistribution_metric_types,
                            "edit_url": lambda st: (
                                reverse(r + "overlay_field_edit", args=["redistribution", st.pk])
                                if st.redistribution_id
                                else None
                            ),
                            "diff_url": lambda st: reverse(r + "routing_redistribution_diff", args=[st.pk]),
                        },
                    )
                },
            },
            "route_policy": {
                # Came off the paginated path (like static/redistribution): reconciles
                # only when the Refresh icon asks (?refresh=1), renders from persisted
                # state otherwise. Diff / Versions only exist once a NetBox object is
                # matched, so their urls are None until then and the client skips them.
                "reconcile_on_expand": False,
                "sections": {
                    None: dict(
                        ctx="route_policy_states",
                        qs=lambda d: (
                            by_device(NSORoutePolicyState, d, "content_type")
                            .prefetch_related("assigned_object")
                            .order_by("family", "object_name")
                        ),
                        accept=r + "routing_accept_route_policy",
                        fields={
                            "family": lambda st: st.family,
                            "name": lambda st: st.object_name,
                            "per_device": lambda st: st.classification_mode == "local",
                            "unsupported": lambda st: list(st.unsupported_members or []),
                            "obj": lambda st: linked(st.assigned_object),
                            "edit_url": lambda st: (
                                reverse(r + "overlay_field_edit", args=["route_map_name", st.pk])
                                if st.family == "route_map" and st.assigned_object
                                else None
                            ),
                            "diff_url": lambda st: (
                                reverse(r + "routing_route_policy_diff", args=[st.pk]) if st.assigned_object else None
                            ),
                            "versions_url": lambda st: (
                                reverse(r + "routing_route_policy_versions", args=[st.pk])
                                if st.assigned_object
                                else None
                            ),
                        },
                    )
                },
            },
        }

    def _grid_section(self, states, accept_route, fields, related=None, distinguish_failed=False):
        """Serialize one grid sub-table: its rows plus the quick-filter counts.

        kind/label come from summary.display_state — the same helper the server-rendered
        tables used — so badges, quick-filter buckets and Accept visibility keep their
        established meaning, and the client never re-derives them (a second, drifting
        implementation would show a row that its own filter then hides).

        The counts are taken off the SAME kind the pills filter on, so a chip can never
        promise rows its own filter would hide.
        """
        from .status_machine import OWNED_STATES
        from .summary import display_state

        rows = []
        counts = {"all": 0, "drift": 0, "pending": 0}
        for st in states:
            status_rows = [st, *(list(related(st)) if related else [])]
            displayed = [
                display_state(row.status, row.status in OWNED_STATES, distinguish_failed=distinguish_failed)
                for row in status_rows
            ]
            kind = _row_state({item[0] for item in displayed})
            label = next(item[1] for item in displayed if item[0] == kind)
            counts["all"] += 1
            if kind == "drift":
                counts["drift"] += 1
            elif kind in _PENDING_KINDS:
                counts["pending"] += 1
            row = {
                "pk": st.pk,
                "status": st.status,
                "state": kind,
                "kind": kind,
                "label": label,
                "residue": bool(getattr(st, "residue_survivor", False)),
                "residue_job": getattr(st, "residue_job_id", None),
                "last_sync": st.last_sync_at.strftime("%Y-%m-%d %H:%M") if st.last_sync_at else None,
                "accept_url": (
                    reverse(accept_route, args=[st.pk])
                    if any(row.status in self._ACCEPTABLE_STATUSES for row in status_rows)
                    else None
                ),
            }
            row.update({name: fn(st) for name, fn in fields.items()})
            rows.append(row)
        return {"rows": rows, "counts": counts}

    def _grid_payload(self, key, device, mgmt, adapter_error=None):
        """Build a grid category's whole payload from persisted overlay state."""
        spec = self._grid_specs()[key]
        states = {name: list(section["qs"](device)) for name, section in spec["sections"].items()}

        # Residue decoration reads the device's adapter jobs, so annotate the WHOLE
        # category in one pass rather than once per sub-table. _annotate_residue_rows
        # looks for each sub-table under the ctx path its matcher names (_residue_matchers,
        # e.g. "ospf_data.interfaces"), which is why every section carries that path
        # verbatim — get it wrong and the rows silently lose their residue badge.
        ctx: dict = {}
        for name, section in spec["sections"].items():
            path = section["ctx"]
            if "." in path:
                head, leaf = path.split(".", 1)
                ctx.setdefault(head, {})[leaf] = states[name]
            else:
                ctx[path] = states[name]
        _annotate_residue_rows(ctx, key, mgmt)

        # The key is emitted ONLY for a category that owns a banner. A null on every other
        # category would let that category's own grid reload hide an active failure
        # belonging to a different, still-expanded one.
        payload: dict = {"adapter_error": adapter_error}
        if key in _CATEGORY_PUSH_SCOPES:
            payload["push_error"] = _category_push_error(key, mgmt)
        for name, section in spec["sections"].items():
            built = self._grid_section(
                states[name],
                section["accept"],
                section["fields"],
                related=section.get("related"),
                distinguish_failed=section.get("distinguish_failed", False),
            )
            if name is None:
                payload.update(built)
            else:
                payload[name] = built
        return payload

    def _render_grid_category(self, request, device, mgmt, key):
        """Render a grid category — or, for ?format=json, serve just its rows."""
        spec = self._grid_specs()[key]
        want_json = request.GET.get("format") == "json"

        adapter_error = None
        # Never reconcile for the JSON reload: the grid re-fetches after every action, so
        # that would be a device read per Accept click.
        may_reconcile = not want_json and mgmt is not None and mgmt.adapter_device_id is not None
        if may_reconcile and (spec["reconcile_on_expand"] or request.GET.get("refresh")):
            from .reconcile import reconcile_category

            try:
                reconcile_category(device, mgmt, key)
            except (AdapterError, DeploymentQuiesced) as exc:
                adapter_error = str(exc)

        payload = self._grid_payload(key, device, mgmt, adapter_error)
        if want_json:
            return JsonResponse(payload)

        # Bulk-accept shows only when something is still unowned — i.e. some row offers
        # an Accept, which is exactly the accept_url the grid itself renders.
        sections = [payload] if None in spec["sections"] else [payload[n] for n in spec["sections"]]
        has_unowned = any(row["accept_url"] for s in sections for row in s["rows"])

        from .template_content import _STATUS_BADGE

        return render(
            request,
            self._PARTIALS[key],
            {
                "object": device,
                "mgmt": mgmt,
                "status_badge": _STATUS_BADGE,
                "grid_payload": payload,
                "counts": payload.get("counts"),
                "adapter_error": adapter_error,
                "push_error": payload.get("push_error"),
                "category_has_unowned": has_unowned,
            },
        )

    def _interface_merged_payload(
        self,
        ordered,
        kinds_by_iface,
        counts,
        attr_states,
        mtu_states,
        sw_states,
        ip_states,
        links_by_iface,
        peer_ip_states,
        native_ips,
        adapter_error,
    ):
        """Build the merged per-interface matrix payload for the Tabulator grid.

        Cell classification reuses the exact helpers the old server-rendered table
        used (interface_row_state / display_state), so grid badges, quick-filter
        buckets and Accept visibility keep the established semantics.
        """
        from dcim.models import Interface
        from django.contrib.contenttypes.models import ContentType

        from .status_machine import OWNED_STATES
        from .summary import _netbox_value_for, display_state, interface_row_state

        interface_type_id = ContentType.objects.get_for_model(Interface).pk

        def attr_cell(st, iface):
            if st is None:
                return None
            kind, label, owned = interface_row_state(st, iface)
            return {
                "pk": st.pk,
                "value": st.nso_value,
                "netbox_value": _netbox_value_for(st.attribute, iface),
                "status": st.status,
                "kind": kind,
                "label": label,
                "owned": owned,
                "accept_url": (
                    reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept", args=[st.pk])
                    if st.status in self._ACCEPTABLE_STATUSES
                    else None
                ),
                "edit_url": reverse("plugins:netbox_nso_plugin:nsointerfacestate_edit_field", args=[st.pk]),
            }

        def plain_cell(st, accept_route, extra):
            if st is None:
                return None
            owned = st.status in OWNED_STATES
            kind, label = display_state(st.status, owned)
            cell = {
                "pk": st.pk,
                "status": st.status,
                "kind": kind,
                "label": label,
                "owned": owned,
                "accept_url": (reverse(accept_route, args=[st.pk]) if st.status in self._ACCEPTABLE_STATUSES else None),
            }
            cell.update(extra)
            return cell

        def ip_cell(st):
            owned = st.status in OWNED_STATES
            kind, label = display_state(st.status, owned)
            native = native_ips.get(st.pk)
            peer = peer_ip_states.get(st.pk)
            if native is None:
                assignment = "absent"
            elif native.assigned_object_id is None:
                assignment = "unassigned"
            elif native.assigned_object_type_id == interface_type_id and native.assigned_object_id == st.interface_id:
                assignment = st.interface.name
            else:
                assignment = "another object"
            return {
                "pk": st.pk,
                "address": str(st.address),
                "vrf": st.vrf,
                "secondary": st.secondary,
                "status": st.status,
                "kind": kind,
                "label": label,
                "owned": owned,
                "url": native.get_absolute_url() if native is not None else None,
                # A changed IP row is retained only when the latest device snapshot
                # stopped reporting it; conflict/imported rows are still present.
                "device_present": st.status != "changed",
                "netbox": {
                    "present": native is not None,
                    "address": str(native.address) if native is not None else None,
                    "vrf": native.vrf.name if native is not None and native.vrf else "",
                    "assignment": assignment,
                },
                "accept_url": (
                    reverse("plugins:netbox_nso_plugin:nsointerfaceipstate_accept", args=[st.pk])
                    if st.status in self._ACCEPTABLE_STATUSES
                    else None
                ),
                "edit_url": reverse("plugins:netbox_nso_plugin:nsointerfaceipstate_edit", args=[st.pk]),
                "peer": (
                    {
                        "pk": peer.pk,
                        "address": peer.address,
                        "interface": f"{peer.interface.device.name} / {peer.interface.name}",
                    }
                    if peer is not None
                    else None
                ),
            }

        rows = []
        for iface in ordered:
            mtu = mtu_states.get(iface.id)
            sw = sw_states.get(iface.id)
            kinds = kinds_by_iface[iface.id]
            rows.append(
                {
                    "iface": {"id": iface.id, "name": iface.name, "url": iface.get_absolute_url()},
                    "link": links_by_iface.get(iface.id),
                    "enabled": attr_cell(attr_states.get((iface.id, "enabled")), iface),
                    "description": attr_cell(attr_states.get((iface.id, "description")), iface),
                    "mtu": plain_cell(
                        mtu,
                        "plugins:netbox_nso_plugin:interface_mtu_accept",
                        {"l2": mtu.l2_mtu, "ip": mtu.ip_mtu, "mpls": mtu.mpls_mtu, "bound_port": mtu.bound_port}
                        if mtu
                        else {},
                    ),
                    "ips": [ip_cell(st) for st in ip_states.get(iface.id, [])],
                    "switchport": plain_cell(
                        sw,
                        "plugins:netbox_nso_plugin:switchport_accept",
                        {
                            "mode": sw.mode,
                            "untagged": sw.untagged_vlan.vid if sw and sw.untagged_vlan else None,
                            "tagged": sorted(v.vid for v in sw.tagged_vlans.all()) if sw else [],
                            "netbox": {
                                "mode": iface.mode or "",
                                "untagged": iface.untagged_vlan.vid if iface.untagged_vlan else None,
                                "tagged": sorted(v.vid for v in iface.tagged_vlans.all()),
                            },
                        }
                        if sw
                        else {},
                    ),
                    "state": _row_state(kinds),
                }
            )
        return {"rows": rows, "counts": counts, "adapter_error": adapter_error}

    def _render_interfaces_page(self, request, device):
        """Per-(interface, attribute) drift/pending view — paginated, filterable, read-only.

        One row per managed attribute showing NetBox value vs Device (NSO) value and the
        state (drift / pending apply / in sync). Filter by interface name (?q=) and by
        state (?state=drift|pending|in_sync|all). Read straight from persisted
        NSOInterfaceState — no reconcile; the cache is refreshed off-render.
        """
        from django.core.paginator import Paginator

        from .models import NSOInterfaceState
        from .summary import interface_row_state

        q = (request.GET.get("q") or "").strip()
        state = request.GET.get("state") or "all"

        qs = (
            NSOInterfaceState.objects.filter(interface__device=device)
            .select_related("interface")
            .order_by("interface__name", "attribute")
        )
        if q:
            qs = qs.filter(interface__name__icontains=q)

        # Classify every row value-aware (NetBox value vs device value), not by the
        # adapter's status — which lags and is blind to a value typed straight into
        # NetBox. Filtering/counts therefore happen in Python over the classified rows
        # so the chips, totals and badges always agree. The per-device row count is
        # bounded (≤2 attrs × interfaces), so this is cheap on tab load.
        classified = []
        counts = {"all": 0, "drift": 0, "pending": 0}
        for st in qs:
            kind, label, owned = interface_row_state(st, st.interface)
            counts["all"] += 1
            if kind in ("pending", "deploying", "apply_failed"):
                counts["pending"] += 1
            elif kind == "drift":
                counts["drift"] += 1
            classified.append((st, kind, label, owned))

        if state == "drift":
            filtered = [c for c in classified if c[1] == "drift"]
        elif state == "pending":
            filtered = [c for c in classified if c[1] in ("pending", "deploying", "apply_failed")]
        elif state == "in_sync":
            filtered = [c for c in classified if c[1] in ("in_sync", "unknown")]
        else:
            filtered = classified

        paginator = Paginator(filtered, self._INTERFACES_PER_PAGE)
        page = paginator.get_page(request.GET.get("page") or 1)

        rows = []
        for st, kind, label, owned in page.object_list:
            iface = st.interface
            if st.attribute == "description":
                netbox_value = iface.description or "—"
            elif st.attribute == "enabled":
                netbox_value = "Yes" if iface.enabled else "No"
            else:
                netbox_value = "—"
            rows.append(
                {
                    "state": st,
                    "iface_name": iface.name,
                    "iface_url": iface.get_absolute_url(),
                    "iface_enabled": iface.enabled,
                    "attribute": st.attribute,
                    "netbox_value": netbox_value,
                    "device_value": st.nso_value or "—",
                    "label": label,
                    "kind": kind,
                    "owned": owned,
                }
            )

        # If interface management is on but neither attribute leaf (description/enabled)
        # is selected, the adapter scope is empty and a sync can never produce rows.
        # Flag it so the empty state tells the operator that — instead of the misleading
        # "wait for the next sync".
        mgmt = getattr(device, "nso_management", None)
        no_attrs_in_scope = bool(mgmt and mgmt.manage_interfaces and not mgmt.managed_attributes)

        return render(
            request,
            "netbox_nso_plugin/categories/interfaces_page.html",
            {
                "object": device,
                "rows": rows,
                "page": page,
                "q": q,
                "state": state,
                "counts": counts,
                "no_attrs_in_scope": no_attrs_in_scope,
            },
        )


# ── AJAX: NSO device names for match form datalist ────────────────────────────


class NSODeviceNamesView(LoginRequiredMixin, View):
    """Return enriched JSON device list for a given NSOInstance (for match form datalist)."""

    def get(self, request, instance_pk):
        """Return JSON list of enriched NSO device dicts for the match form."""
        from . import adapter_client as client

        nso_instance = get_object_or_404(NSOInstance, pk=instance_pk)
        try:
            device_names = client.list_nso_devices(nso_instance.adapter_instance_id)
            return JsonResponse({"devices": device_names})
        except AdapterError as exc:
            return JsonResponse({"error": str(exc)}, status=502)


# ── Adapter Connection (singleton) ───────────────────────────────────────────


class AdapterConnectionEditView(generic.ObjectEditView):
    """Singleton edit view for AdapterConnection (URL + non-secret settings)."""

    queryset = AdapterConnection.objects.all()
    form = AdapterConnectionForm
    template_name = "netbox_nso_plugin/adapterconnection.html"

    def get_object(self, **kwargs):
        """Return the existing singleton or a blank instance for first-time creation."""
        return AdapterConnection.objects.first() or AdapterConnection()

    def get_extra_context(self, request, instance):
        """Surface derived-intent templates + the env-sourced bearer token status.

        Everything except the token is the editable DB row; the bearer token is read
        ONLY from PLUGINS_CONFIG / env (never the DB), so show its source + whether
        it is configured — otherwise the page is misleading about the effective config.
        """
        from . import adapter_client
        from .derived_intent import get_sentinel_templates

        resolved = adapter_client._resolve_config()
        db_url = (getattr(instance, "url", "") or "") if getattr(instance, "enabled", False) else ""
        return {
            "derived_intent_templates": get_sentinel_templates(),
            "token_configured": bool(resolved.get("token")),
            "effective_url": resolved.get("url") or "",
            "url_source": "Adapter Connection (DB)" if db_url else "PLUGINS_CONFIG / env",
        }


class NSOFailoverSettingsEditView(generic.ObjectEditView):
    """Singleton edit view for NSOFailoverSettings (global mgmt-IP failover tuning)."""

    template_name = "netbox_nso_plugin/settings_object_edit.html"
    queryset = NSOFailoverSettings.objects.all()
    form = NSOFailoverSettingsForm

    def get_object(self, **kwargs):
        """Return the existing singleton or a blank instance for first-time creation."""
        return NSOFailoverSettings.objects.first() or NSOFailoverSettings()

    def get(self, request, *args, **kwargs):
        """Render the form, warning first if failover is off at the adapter deployment level."""
        self._warn_if_deployment_failover_disabled(request)
        return super().get(request, *args, **kwargs)

    @staticmethod
    def _warn_if_deployment_failover_disabled(request):
        """Surface when the adapter's deployment master switch (enable_failover) is off.

        Without this, enabling failover here is a silent no-op: the adapter gates the whole
        feature (probe loop + onboarding OOB bootstrap) on its static ``enable_failover``, and
        the runtime toggle these settings drive has no effect until that is on. Best-effort —
        an unreachable adapter must not block the settings page.
        """
        from . import adapter_client as client

        try:
            cfg = client.get_failover_config()
        except Exception as exc:  # noqa: BLE001 — adapter may be down; never block the page
            logger.debug("Could not read adapter failover config for deployment-state hint: %s", exc)
            return
        if cfg and cfg.get("deployment_enabled") is False:
            messages.warning(
                request,
                "Mgmt-IP failover is disabled at the adapter deployment level "
                "(enable_failover is off in the adapter config). These settings are saved but "
                "have no effect — the failover probe loop is not running and onboarding will not "
                "fall back to OOB — until the adapter operator sets enable_failover: true and "
                "restarts the adapter.",
            )


class NSOVaultSettingsEditView(generic.ObjectEditView):
    """Singleton edit view for NSOVaultSettings (Vault KV layout for generated refs)."""

    template_name = "netbox_nso_plugin/settings_object_edit.html"
    queryset = NSOVaultSettings.objects.all()
    form = NSOVaultSettingsForm

    def get_object(self, **kwargs):
        """Return the existing singleton or a blank instance for first-time creation."""
        return NSOVaultSettings.objects.first() or NSOVaultSettings()


# ── NSO Instance CRUD ────────────────────────────────────────────────────────


class NSOInstanceListView(generic.ObjectListView):
    """List view for NSO instances."""

    template_name = "netbox_nso_plugin/settings_object_list.html"
    queryset = NSOInstance.objects.all()
    table = NSOInstanceTable
    filterset = NSOInstanceFilterSet
    # Only advertise the bulk/single actions we actually wire up — the NetBox
    # default tuple includes Import / Bulk-Edit / Bulk-Rename, whose buttons would
    # render with formaction="None" (NoReverseMatch → None) and POST to a 404.
    actions = (AddObject, BulkExport, BulkDelete)


class NSOInstanceBulkDeleteView(generic.BulkDeleteView):
    """Bulk-delete view for NSO instances."""

    template_name = "netbox_nso_plugin/settings_bulk_delete.html"
    queryset = NSOInstance.objects.all()
    table = NSOInstanceTable
    filterset = NSOInstanceFilterSet


class NSOInstanceView(generic.ObjectView):
    """Detail view for an NSO instance."""

    queryset = NSOInstance.objects.all()


class NSOInstanceEditView(generic.ObjectEditView):
    """Create/edit view for an NSO instance."""

    template_name = "netbox_nso_plugin/settings_object_edit.html"
    queryset = NSOInstance.objects.all()
    form = NSOInstanceForm


class NSOInstanceDeleteView(generic.ObjectDeleteView):
    """Delete view for an NSO instance."""

    template_name = "netbox_nso_plugin/settings_object_delete.html"
    queryset = NSOInstance.objects.all()


# ── Link-role provisioning ─────────────────────────────────────────────────────


class NSOLinkRoleListView(generic.ObjectListView):
    """List view for configurable link roles."""

    template_name = "netbox_nso_plugin/links_object_list.html"
    queryset = NSOLinkRole.objects.all()
    table = NSOLinkRoleTable
    filterset = NSOLinkRoleFilterSet
    actions = (AddObject, BulkExport, BulkDelete)


class NSOLinkRoleView(generic.ObjectView):
    """Detail view for a link role."""

    queryset = NSOLinkRole.objects.all()


class NSOLinkRoleEditView(generic.ObjectEditView):
    """Create/edit view for a link role."""

    template_name = "netbox_nso_plugin/links_object_edit.html"
    queryset = NSOLinkRole.objects.all()
    form = NSOLinkRoleForm


class NSOLinkRoleDeleteView(generic.ObjectDeleteView):
    """Delete view for a link role."""

    template_name = "netbox_nso_plugin/links_object_delete.html"
    queryset = NSOLinkRole.objects.all()


class NSOLinkRoleBulkDeleteView(generic.BulkDeleteView):
    """Bulk-delete view for link roles."""

    template_name = "netbox_nso_plugin/links_bulk_delete.html"
    queryset = NSOLinkRole.objects.all()
    table = NSOLinkRoleTable
    filterset = NSOLinkRoleFilterSet


class NSOLinkRoleAssignmentListView(generic.ObjectListView):
    """List view for link-role assignments."""

    template_name = "netbox_nso_plugin/links_object_list.html"
    queryset = NSOLinkRoleAssignment.objects.select_related("role", "cable", "interface")
    table = NSOLinkRoleAssignmentTable
    filterset = NSOLinkRoleAssignmentFilterSet
    actions = (AddObject, BulkExport, BulkDelete)


class NSOLinkRoleAssignmentView(generic.ObjectView):
    """Detail view for a link-role assignment."""

    queryset = NSOLinkRoleAssignment.objects.select_related("role", "cable", "interface")


class NSOLinkRoleAssignmentEditView(generic.ObjectEditView):
    """Create/edit view for a link-role assignment."""

    template_name = "netbox_nso_plugin/links_object_edit.html"
    queryset = NSOLinkRoleAssignment.objects.all()
    form = NSOLinkRoleAssignmentForm


class NSOLinkRoleAssignmentDeleteView(generic.ObjectDeleteView):
    """Delete view for a link-role assignment."""

    template_name = "netbox_nso_plugin/links_object_delete.html"
    queryset = NSOLinkRoleAssignment.objects.all()


class NSOLinkRoleAssignmentBulkDeleteView(generic.BulkDeleteView):
    """Bulk-delete view for link-role assignments."""

    template_name = "netbox_nso_plugin/links_bulk_delete.html"
    queryset = NSOLinkRoleAssignment.objects.all()
    table = NSOLinkRoleAssignmentTable
    filterset = NSOLinkRoleAssignmentFilterSet


class NSOOnboardingDashboardView(LoginRequiredMixin, View):
    """Combined NSO devices view (tabbed): Managed / Onboarded / Onboardable / Unmatched.

    Reads the NSO device inventory for the selected instance (default unless
    ``?instance=<adapter_instance_id>``) and compares it to NetBox. Read-only.
    URL: /plugins/nso/onboarding/
    """

    template_name = "netbox_nso_plugin/onboarding_dashboard.html"

    def get(self, request):
        """Render the tabbed view for the selected (or default) NSO instance."""
        from .onboarding import build_onboarding_dashboard

        instances = list(NSOInstance.objects.all())
        selected = request.GET.get("instance")
        instance = None
        if selected:
            instance = next((i for i in instances if i.adapter_instance_id == selected), None)
        if instance is None:
            instance = NSOInstance.get_default() or (instances[0] if instances else None)

        if instance is None:
            data = {
                "instance": "—",
                "error": "No NSO instance configured.",
                "onboarded": [],
                "candidates": [],
                "orphans": [],
            }
            managed = []
        else:
            data = build_onboarding_dashboard(instance)
            # nso_instance: the refresh below classifies every row through it, so without the
            # join the page costs one more query per managed device.
            managed = list(
                NSODeviceManagement.objects.filter(nso_instance=instance).select_related("device", "nso_instance")
            )
            # Mirror the adapter's current last-sync state onto the rows before rendering.
            # The periodic job keeps them fresh with nobody watching; this makes the page
            # the operator is actually looking at current to the second. Best-effort: the
            # last-sync columns are a display nicety, never worth 500ing the dashboard.
            from .sync_cache import refresh_sync_caches

            try:
                refresh_sync_caches(managed)
            except Exception:  # noqa: BLE001 — see above
                logger.debug("Dashboard last-sync refresh failed", exc_info=True)
            # Annotate each managed row with the NED it actually runs on (live NSO
            # inventory), so the Managed tab shows it without a second page.
            ned_by_name = data.get("ned_by_nso_name") or {}
            for m in managed:
                m.ned_in_use = ned_by_name.get(m.nso_device_name)

        return render(
            request,
            self.template_name,
            {
                "data": data,
                "managed": managed,
                "instances": instances,
                "selected": instance.adapter_instance_id if instance else None,
            },
        )


class NSOOnboardView(NSOActionPermissionMixin, View):
    """POST action: onboard one candidate device into NSO, then redirect to the dashboard.

    URL: POST /plugins/nso/onboard/  body: device=<pk>, instance=<adapter_instance_id>
    """

    required_permission = "netbox_nso_plugin.add_nsodevicemanagement"

    def post(self, request):
        """Onboard the posted device into the selected (or default) NSO instance."""
        from .onboarding import onboard_candidate

        device = get_object_or_404(Device, pk=request.POST.get("device"))
        selected = request.POST.get("instance")
        instance = None
        if selected:
            instance = NSOInstance.objects.filter(adapter_instance_id=selected).first()
        instance = instance or NSOInstance.get_default()

        redirect_url = reverse("plugins:netbox_nso_plugin:onboarding_dashboard")
        if instance is None:
            messages.error(request, "No NSO instance configured.")
            return redirect(redirect_url)

        ned_id = (request.POST.get("ned_id") or "").strip() or None
        try:
            result = onboard_candidate(device, instance, ned_id=ned_id)
        except Exception as exc:  # never 500 the action
            logger.exception("onboard action failed for device %s", device.pk)
            messages.error(request, f"Onboarding {device} failed ({type(exc).__name__}); see the server log.")
            return redirect(f"{redirect_url}?instance={instance.adapter_instance_id}")

        if result["ok"]:
            messages.success(
                request,
                f"Provisioning {device} into NSO ({instance.name})… this list updates automatically.",
            )
        else:
            messages.error(request, f"Could not onboard {device}: {result['error']}")
        return redirect(f"{redirect_url}?instance={instance.adapter_instance_id}")


class NSOQuickManageView(NSOActionPermissionMixin, View):
    """POST action: bring an already-in-NSO ('external') device under plugin management.

    The device exists in both NSO and NetBox but has no NSODeviceManagement record.
    Creates that record (no re-provisioning) and redirects to the dashboard.

    URL: POST /plugins/nso/manage/  body: device=<pk>, instance=<adapter_instance_id>,
    nso_name=<NSO device name>
    """

    required_permission = "netbox_nso_plugin.add_nsodevicemanagement"

    def post(self, request):
        """Create the management record for the posted external device."""
        from .onboarding import manage_existing

        device = get_object_or_404(Device, pk=request.POST.get("device"))
        selected = request.POST.get("instance")
        instance = None
        if selected:
            instance = NSOInstance.objects.filter(adapter_instance_id=selected).first()
        instance = instance or NSOInstance.get_default()

        redirect_url = reverse("plugins:netbox_nso_plugin:onboarding_dashboard")
        if instance is None:
            messages.error(request, "No NSO instance configured.")
            return redirect(redirect_url)

        nso_name = request.POST.get("nso_name") or device.name
        try:
            result = manage_existing(device, instance, nso_name)
        except Exception as exc:  # never 500 the action
            logger.exception("manage action failed for device %s", device.pk)
            messages.error(request, f"Managing {device} failed ({type(exc).__name__}); see the server log.")
            return redirect(f"{redirect_url}?instance={instance.adapter_instance_id}")

        if result["ok"]:
            messages.success(request, f"{device} is now managed by NSO ({instance.name}).")
        else:
            messages.error(request, f"Could not manage {device}: {result['error']}")
        return redirect(f"{redirect_url}?instance={instance.adapter_instance_id}")


class NSOOnboardStatusView(NSOActionPermissionMixin, View):
    """Advance + report an async onboarding job — polled by the dashboard while a row provisions.

    Provisioning runs as a background adapter job; the NSODeviceManagement row sits in
    ``provisioning`` (its adapter-push signal gated) until this view, polled client-side,
    sees the job finish and advances the row:

      * job succeeded + result.ok  → ``onboard_status=""`` (ready). Saving re-fires
        ``sync_scope_to_adapter`` (no longer gated) → adapter mapping + scope + sync-notify.
      * job succeeded + ``ok=False`` (a blocking step failed) or job failed/timeout →
        ``provision_failed`` + recorded steps/error (no adapter push).

    Idempotent: once the row is terminal (``""`` / ``provision_failed``) it just reports that.
    A transient adapter error while polling keeps the row provisioning so the client retries.

    URL: POST /plugins/nso/onboard-status/<pk>/  (pk = NSODeviceManagement id)
    """

    required_permission = "netbox_nso_plugin.add_nsodevicemanagement"

    def post(self, request, pk):
        """Poll the provision job and advance the row; return its onboarding status as JSON."""
        from .onboarding import advance_provisioning

        mgmt = get_object_or_404(NSODeviceManagement, pk=pk)
        return JsonResponse(advance_provisioning(mgmt))


class NSODerivedIntentTemplateListView(generic.ObjectListView):
    """List database-managed interface-description templates."""

    template_name = "netbox_nso_plugin/settings_object_list.html"
    queryset = NSODerivedIntentTemplate.objects.all()
    table = NSODerivedIntentTemplateTable
    filterset = NSODerivedIntentTemplateFilterSet
    actions = (AddObject, BulkExport, BulkDelete)


class NSODerivedIntentTemplateBulkDeleteView(generic.BulkDeleteView):
    """Bulk-delete derived-intent templates."""

    template_name = "netbox_nso_plugin/settings_bulk_delete.html"
    queryset = NSODerivedIntentTemplate.objects.all()
    table = NSODerivedIntentTemplateTable
    filterset = NSODerivedIntentTemplateFilterSet


class NSODerivedIntentTemplateView(generic.ObjectView):
    """Display a derived-intent template."""

    queryset = NSODerivedIntentTemplate.objects.all()


class NSODerivedIntentTemplateEditView(generic.ObjectEditView):
    """Create or edit a derived-intent template."""

    template_name = "netbox_nso_plugin/settings_object_edit.html"
    queryset = NSODerivedIntentTemplate.objects.all()
    form = NSODerivedIntentTemplateForm


class NSODerivedIntentTemplateDeleteView(generic.ObjectDeleteView):
    """Delete a derived-intent template."""

    template_name = "netbox_nso_plugin/settings_object_delete.html"
    queryset = NSODerivedIntentTemplate.objects.all()


class NSOPlatformNedMappingListView(generic.ObjectListView):
    """List view for Platform→NED mappings."""

    template_name = "netbox_nso_plugin/settings_object_list.html"
    queryset = NSOPlatformNedMapping.objects.all()
    table = NSOPlatformNedMappingTable
    filterset = NSOPlatformNedMappingFilterSet
    actions = (AddObject, BulkExport, BulkDelete)


class NSOPlatformNedMappingBulkDeleteView(generic.BulkDeleteView):
    """Bulk-delete view for Platform→NED mappings."""

    template_name = "netbox_nso_plugin/settings_bulk_delete.html"
    queryset = NSOPlatformNedMapping.objects.all()
    table = NSOPlatformNedMappingTable
    filterset = NSOPlatformNedMappingFilterSet


class NSOPlatformNedMappingView(generic.ObjectView):
    """Detail view for a Platform→NED mapping."""

    queryset = NSOPlatformNedMapping.objects.all()


class NSOPlatformNedMappingEditView(generic.ObjectEditView):
    """Create/edit view for a Platform→NED mapping."""

    template_name = "netbox_nso_plugin/settings_object_edit.html"
    queryset = NSOPlatformNedMapping.objects.all()
    form = NSOPlatformNedMappingForm


class NSOPlatformNedMappingDeleteView(generic.ObjectDeleteView):
    """Delete view for a Platform→NED mapping."""

    template_name = "netbox_nso_plugin/settings_object_delete.html"
    queryset = NSOPlatformNedMapping.objects.all()


# ── NSO Device Management CRUD ───────────────────────────────────────────────


class NSODevicesReturnMixin:
    """Return device-management forms to their originating NSO surface."""

    default_return_url = "plugins:netbox_nso_plugin:onboarding_dashboard"

    def get_return_url(self, request, obj=None):
        """Honor an explicit safe return URL, otherwise use the NSO Devices dashboard."""
        return super().get_return_url(request)


class NSODeviceManagementListView(generic.ObjectListView):
    """List view for managed NSO devices.

    Refreshes the cached ``last_sync_*`` columns on each render via one bulk adapter
    call, so the list reflects current sync state without the operator first having to
    open each device's NSO tab. Compliance and per-protocol reconcile are NOT run here
    (those stay on the tab) — only the lightweight last-sync fields are polled.
    """

    queryset = NSODeviceManagement.objects.select_related("device", "nso_instance")
    table = NSODeviceManagementTable
    filterset = NSODeviceManagementFilterSet
    actions = (AddObject, BulkExport, BulkDelete)

    def get_queryset(self, request):
        """Poll the adapter for last-sync state before the table is built."""
        qs = super().get_queryset(request)
        from .sync_cache import refresh_sync_caches

        refresh_sync_caches(qs)
        return qs


class NSODeviceManagementBulkDeleteView(NSODevicesReturnMixin, generic.BulkDeleteView):
    """Bulk-delete view for managed NSO devices."""

    template_name = "netbox_nso_plugin/nsodevicemanagement_bulk_delete.html"
    queryset = NSODeviceManagement.objects.select_related("device", "nso_instance")
    table = NSODeviceManagementTable
    filterset = NSODeviceManagementFilterSet


class NSODeviceManagementView(generic.ObjectView):
    """Detail view for an NSO device management record."""

    queryset = NSODeviceManagement.objects.select_related("device", "nso_instance")

    def get_extra_context(self, request, instance):
        """Inject live adapter data, falling back to cached snapshot on error."""
        from . import adapter_client as client

        interfaces = None
        compliance = None
        adapter_error = None
        adapter_error_code = None

        if instance.adapter_device_id is not None:
            try:
                interfaces = client.get_interfaces_doc(instance.adapter_device_id).get("interfaces", [])
                compliance = client.get_state(instance.adapter_device_id)
            except AdapterError as exc:
                adapter_error = str(exc)
                adapter_error_code = exc.code
                # Fall back to snapshot so the page remains useful
                snapshot = instance.state_snapshot or {}
                interfaces = snapshot.get("interfaces")
                compliance = snapshot.get("compliance")

        return {
            "interfaces": interfaces,
            "compliance": compliance,
            "adapter_error": adapter_error,
            "adapter_error_code": adapter_error_code,
        }


class NSODeviceManagementEditView(NSODevicesReturnMixin, generic.ObjectEditView):
    """Create/edit view for an NSO device management record."""

    queryset = NSODeviceManagement.objects.all()
    form = NSODeviceManagementForm
    template_name = "netbox_nso_plugin/nsodevicemanagement_edit.html"

    def get_extra_context(self, request, instance):
        """Label the persistent return control for the actual originating surface."""
        context = super().get_extra_context(request, instance)
        return_url = self.get_return_url(request, instance)
        if return_url.startswith("/dcim/devices/") and return_url.endswith("/nso/"):
            context["nso_return_label"] = "Back to device NSO tab"
        else:
            context["nso_return_label"] = "Back to NSO Devices"
        return context


class NSODeviceManagementDeleteView(NSODevicesReturnMixin, generic.ObjectDeleteView):
    """Delete view for an NSO device management record."""

    template_name = "netbox_nso_plugin/nsodevicemanagement_delete.html"
    queryset = NSODeviceManagement.objects.all()


# ── Adapter actions ──────────────────────────────────────────────────────────

_ACTION_LABELS = {
    "sync": "Sync",
    "sync-from-nso": "Sync from NSO",
    "detect-drift": "Detect Drift",
    "connect": "Test Connection",
    "apply": "Apply Intent",
}


class ApplyRefused(Exception):
    """A precondition of the Apply is unmet, so no job is triggered and no row is promoted.

    A refusal carries delivery-registry keys and a phrase from the vocabulary below, never
    text: :func:`_apply_refusal_message` rebuilds the operator wording from those fields at
    the boundary, so nothing a raise site was told can be serialized into a response.
    """


class ApplyDeadlineExpired(ApplyRefused):
    """The preparation budget ran out before every scope was shipped."""


class ApplySnmpRefused(ApplyRefused):
    """The store-only SNMP refresh was refused, so this Apply would commit stale intent."""


class ApplyPreparationRefused(ApplyRefused):
    """One in-protocol scope's store-only preparation did not land."""

    def __init__(self, key, failure):
        super().__init__()
        self.key = key
        self.failure = failure


class ApplyDirectRefused(ApplyRefused):
    """One direct-config snapshot did not land, after *applied* already reached the device."""

    def __init__(self, key, applied, failure):
        super().__init__()
        self.key = key
        self.applied = tuple(applied)
        self.failure = failure


class ApplyPromotionFailed(ApplyRefused):
    """NetBox could not mark the selected intent as deploying, so nothing was submitted."""


#: What a preparation step did, in the operator's words. A closed vocabulary: with the
#: delivery-registry labels it is everything a refusal may say.
_PREPARE_FAILED = "failed"
_PREPARE_NOT_SETTLED = "did not settle successfully"
_PREPARE_NOT_STARTED = "did not start before the preparation deadline expired"

_APPLY_DEADLINE_MESSAGE = "Apply stopped before submission because the intent preparation deadline expired."
_APPLY_PROMOTION_MESSAGE = (
    "Apply stopped because NetBox could not mark the selected intent as deploying. "
    "No Apply job was enqueued and all local promotion marks were rolled back."
)
_APPLY_REFUSED_MESSAGE = (
    "Apply stopped before submission because a precondition was unmet. Nothing was applied and no row was promoted."
)


def _push_error_message(mgmt, scope) -> str:
    """Read back the cause the claim recorded for *scope*, which the NSO tab also renders."""
    mgmt.refresh_from_db(fields=["intent_push_errors"])
    return (mgmt.intent_push_errors or {}).get(scope, {}).get("message") or "no cause was recorded"


def _snmp_refusal_message(mgmt) -> str:
    """Operator wording for the SNMP-refusal stop, rebuilt from the recorded cause."""
    return (
        f"Apply stopped: this device's SNMP intent refresh was refused "
        f"({_push_error_message(mgmt, 'snmp')}), so applying now would commit the SNMP intent the "
        "adapter still holds. Nothing was applied and no row was promoted."
    )


def _delivery_label(key) -> str:
    """Return one delivery key's operator label, which only the registry may supply."""
    from . import delivery

    return delivery.delivery_keys()[key].label


def _prepare_failure_message(key, failure) -> str:
    """Describe an in-protocol preparation failure that promoted nothing."""
    return (
        f"Apply stopped: {_delivery_label(key)} intent preparation {failure}. "
        "Nothing was applied and no row was promoted."
    )


def _direct_prepare_failure_message(key, applied_keys, failure) -> str:
    """Describe a direct preparation failure without hiding completed device writes."""
    if applied_keys:
        applied = ", ".join(_delivery_label(applied_key) for applied_key in applied_keys)
        completed = f"Direct-config snapshots already applied to the device: {applied}."
    else:
        completed = "No direct-config snapshot completed before this failure."
    return (
        f"Apply stopped: {_delivery_label(key)} direct configuration {failure}. {completed} "
        "No Apply job was enqueued and no row was promoted."
    )


def _apply_refusal_message(exc, mgmt) -> str:
    """Rebuild the operator wording from the refusal's TYPE and its registry-keyed fields.

    CodeQL py/stack-trace-exposure: nothing here reads the exception's own text, and an
    unrecognised refusal says only that a precondition was unmet.
    """
    if isinstance(exc, ApplySnmpRefused):
        return _snmp_refusal_message(mgmt)
    if isinstance(exc, ApplyDirectRefused):
        return _direct_prepare_failure_message(exc.key, exc.applied, exc.failure)
    if isinstance(exc, ApplyPreparationRefused):
        return _prepare_failure_message(exc.key, exc.failure)
    if isinstance(exc, ApplyPromotionFailed):
        return _APPLY_PROMOTION_MESSAGE
    if isinstance(exc, ApplyDeadlineExpired):
        return _APPLY_DEADLINE_MESSAGE
    return _APPLY_REFUSED_MESSAGE


def _push_direct_snapshots(mgmt, registry, remaining_budget) -> None:
    """Force-push the out-of-protocol device snapshots, last, and abort truthfully.

    These are synchronous device writes with no rollback, so they run only after every
    store-only push succeeded, and a failure names the snapshots already applied.
    """
    from . import delivery, drain

    applied_direct_keys = []
    for entry in (candidate for candidate in registry.values() if not candidate.in_protocol):
        try:
            deadline = remaining_budget()
        except ApplyDeadlineExpired as exc:
            raise ApplyDirectRefused(entry.key, applied_direct_keys, _PREPARE_NOT_STARTED) from exc
        try:
            response = drain.push_now(
                mgmt.device_id,
                entry.key,
                mode=delivery.MODE_NORMAL,
                force=True,
                deadline=deadline,
            )
        except Exception as exc:  # noqa: BLE001 (the direct write may already have happened)
            logger.warning("Apply direct push failed for device %s: %s", mgmt.device_id, exc)
            raise ApplyDirectRefused(entry.key, applied_direct_keys, _PREPARE_FAILED) from exc
        if response is None:
            raise ApplyDirectRefused(entry.key, applied_direct_keys, _PREPARE_NOT_SETTLED)
        applied_direct_keys.append(entry.key)


def _prepare_apply(mgmt):
    """Pre-Apply bookkeeping for one device's single Apply.

    Refresh every adapter intent mirror with store-only pushes first. LACP and
    switchport are owned in NetBox, so push their device snapshots only after
    every store-only push succeeds. Then move owned 'accepted' overlays →
    'deploying' so they read as "applying" and settle to 'in_sync' on the next
    reconcile once the device reflects them (VLAN value-aware; SVI/subif/BFD when
    re-reported). ``.update()`` avoids firing the per-row push signal.

    Returns the rows moved to 'deploying' and an immutable adapter-stream selector. The
    caller can roll the rows back via :func:`_rollback_prepare_apply` if no job is enqueued.
    Raises :class:`ApplyRefused` when a preparation call escapes or the SNMP refresh is
    refused. Preparation completes before promotion, so an abort leaves no rows to roll back.
    Completed direct writes cannot be rolled back.
    """
    from . import delivery, drain
    from .signals import stored_static_route_count

    prepare_deadline = drain._send_clock() + drain.SEND_DEADLINE.total_seconds()

    def remaining_budget():
        remaining = prepare_deadline - drain._send_clock()
        if remaining <= 0:
            raise ApplyDeadlineExpired
        return remaining

    # Each of these takes its OWN forced claim, so Apply re-ships the operator's intent
    # whatever the acknowledged baseline says and whatever a queued claim was carrying:
    #   - LACP / switchport: owned in NetBox, never mirrored as adapter intent.
    #   - VLAN: the name lives on ipam.VLAN; renaming it fires no plugin signal, so a
    #     post-accept rename would otherwise be stranded in NetBox (the row stays
    #     'in_sync' and the stale old name is what gets applied).
    #   - interface description/enabled: an owned attribute (status in OWNED_STATES)
    #     whose adapter intent went stale is re-sent so Apply actually re-applies it.
    #     Ownership is status-based and kept durable by the reconciler's owned-guard,
    #     so this no longer re-pushes a row that genuinely drifted back to 'imported'.
    #   - route-policy / SVI / subinterface / BFD / MTU: mirrored as adapter intent (reactive
    #     push on accept/edit), but that mirror can go stale/empty (a failed push, an
    #     out-of-band adapter reset). These are all marked accepted->deploying below, so
    #     re-send the owned snapshot too — otherwise Apply applies nothing and the row
    #     sticks 'deploying' forever (observed on rg03 for route-policy: an owned as-path
    #     with no adapter intent row; SVI/subinterface/BFD/MTU share the same failure mode).
    #   - SNMP: mirrored reactively on accept and a failed push is swallowed, so the adapter
    #     mirror can be stale or absent. Refreshed store-only below — the Apply commits it.
    registry = delivery.delivery_keys()
    static_route_stored = False
    with drain.capture_successful_pushes() as pushed:
        for entry in (candidate for candidate in registry.values() if candidate.in_protocol):
            if entry.key == "snmp":
                continue
            deadline = remaining_budget()
            try:
                response = drain.push_now(
                    mgmt.device_id,
                    entry.key,
                    mode=delivery.MODE_STORE_ONLY,
                    force=True,
                    deadline=deadline,
                )
            except Exception as exc:  # noqa: BLE001 (a partial selector must never be applied)
                logger.warning("Apply push failed for device %s: %s", mgmt.device_id, exc)
                raise ApplyPreparationRefused(entry.key, _PREPARE_FAILED) from exc
            if response is None:
                raise ApplyPreparationRefused(entry.key, _PREPARE_NOT_SETTLED)
            if entry.key == "static_route":
                # A forced claim is dropped only on a real rejection, and a static route settles
                # on a generation the adapter has to be holding. Promoting on a push the adapter
                # refused would create a 'deploying' row no result can ever name, stuck until
                # the backstop calls it failed.
                static_route_stored = stored_static_route_count(response) is not None

        # A normal SNMP push can enqueue a shrink-removal job before this Apply. Store the
        # snapshot without executing it, then select that exact receipt for promotion below.
        deadline = remaining_budget()
        try:
            outcome = drain.drain_key(
                mgmt.device_id,
                "snmp",
                mode=delivery.MODE_STORE_ONLY,
                force=True,
                deadline=deadline,
            )
        except Exception as exc:  # noqa: BLE001 (a partial selector must never be applied)
            logger.warning("Apply push failed for device %s: %s", mgmt.device_id, exc)
            raise ApplyPreparationRefused("snmp", _PREPARE_FAILED) from exc
        if outcome == drain.REFUSED:
            # A store-only claim may not carry deletion authority (§4.3(d)), so the adapter still
            # holds the SNMP intent the operator deleted and this Apply would commit it.
            # Delivering that authority takes a NORMAL claim, whose removal and auto-apply jobs
            # are exactly what the store-only push exists to avoid ahead of trigger_apply and
            # would 409 this Apply anyway. So the precondition is reported rather than worked
            # around: the tick drains the pending claim and the operator re-applies.
            raise ApplySnmpRefused
        if outcome != drain.SUCCEEDED:
            raise ApplyPreparationRefused("snmp", _PREPARE_NOT_SETTLED)

        _push_direct_snapshots(mgmt, registry, remaining_budget)

    selected = MappingProxyType({registry[scope].section: push_seq for scope, push_seq in pushed.items()})

    moved: list[tuple] = []  # (adapter section, model, [pks]) actually moved, for selective rollback
    try:
        with transaction.atomic():
            for scope, model in (
                ("vlan", NSOVLANState),
                ("svi", NSOSVIState),
                ("subinterface", NSOSubinterfaceState),
                ("bfd", NSOBFDInterfaceState),
                ("interface_mtu", NSOInterfaceMtuState),
                ("route_policy", NSORoutePolicyState),
                ("static_route", NSOStaticRouteState),
                ("l2_sap", NSOL2SapState),
                ("logging", NSOLoggingLevelState),
            ):
                if model is NSOStaticRouteState and not static_route_stored:
                    logger.warning(
                        "Apply: the static-route intent push was not acknowledged for device %s: "
                        "leaving those rows accepted because the adapter does not hold their intent",
                        mgmt.device_id,
                    )
                    continue
                section = registry[scope].section
                pks = list(model.objects.filter(management=mgmt, status="accepted").values_list("pk", flat=True))
                if pks:
                    model.objects.filter(pk__in=pks).update(status="deploying")
                    moved.append((section, model, pks))
    except Exception as exc:  # noqa: BLE001 (abort before the adapter can promote a partial local state)
        logger.warning("Apply deploying-mark transaction failed for device %s: %s", mgmt.device_id, exc)
        raise ApplyPromotionFailed from exc
    return moved, selected


def _rollback_prepare_apply(moved, *, keep_streams=()) -> None:
    """Revert accepted→deploying marks for streams without an enqueued generation.

    Only the rows THIS Apply moved are reverted (by pk), so a genuinely in-flight 'deploying'
    row from a prior Apply is left untouched. Reverting to 'accepted' is safe — an accepted row
    still settles to in_sync on the next reconcile once the device matches; leaving it 'deploying'
    with no apply job would strand it as 'applying' forever (nothing settles it).
    """
    keep_streams = set(keep_streams)
    for section, model, pks in moved or []:
        if section in keep_streams:
            continue
        try:
            model.objects.filter(pk__in=pks, status="deploying").update(status="accepted")
        except Exception as exc:  # noqa: BLE001 — best-effort rollback; log and move on
            logger.warning("Apply rollback failed: %s", exc)


def _stream_reason_message(prefix, reasons) -> str:
    """Format adapter stream reasons for an operator-visible Apply message."""
    if not isinstance(reasons, dict) or not reasons:
        return prefix
    detail = ", ".join(f"{stream} ({reason})" for stream, reason in sorted(reasons.items()))
    return f"{prefix}: {detail}."


def _apply_stream_partition(result, selected):
    """Validate the selector echo and return its selected and skipped stream sets."""
    expected_selected = dict(selected)
    returned_selected = result.get("selected")
    skipped = result.get("skipped")
    if not isinstance(returned_selected, dict) or returned_selected != expected_selected:
        return None, None, "Adapter returned an invalid Apply selector."
    if not isinstance(skipped, dict) or not set(skipped).issubset(expected_selected):
        return None, None, "Adapter returned invalid Apply skip results."
    return expected_selected, skipped, None


_APPLY_GENERATION_FIELDS = frozenset(
    {"generation_id", "seq", "job_id", "mode", "source_push_seq", "stream_revisions", "digest"}
)


def _promoted_stream_coverage(generations, expected_selected, skipped):
    """Return the promoted stream union when every generation proves its selector."""
    promoted_streams = set()
    previous_seq = None
    for link in generations:
        if not isinstance(link, dict) or not _APPLY_GENERATION_FIELDS.issubset(link):
            return None, "Adapter returned an invalid Apply generation."
        generation_id = link.get("generation_id")
        seq = link.get("seq")
        job_id = link.get("job_id")
        mode = link.get("mode")
        sources = link.get("source_push_seq")
        revisions = link.get("stream_revisions")
        digest = link.get("digest")
        valid_shape = (
            type(generation_id) is int
            and generation_id > 0
            and type(seq) is int
            and seq > 0
            and (previous_seq is None or seq > previous_seq)
            and (job_id is None or (type(job_id) is int and job_id > 0))
            and isinstance(mode, str)
            and mode in {"networked", "detach"}
            and isinstance(sources, dict)
            and isinstance(revisions, dict)
            and bool(revisions)
            and isinstance(digest, str)
        )
        if not valid_shape:
            return None, "Adapter returned an invalid Apply generation."
        streams = set(revisions)
        if streams != set(sources) or not streams.issubset(expected_selected):
            return None, "Adapter returned an invalid Apply generation."
        if any(type(revision) is not int for revision in revisions.values()) or any(
            type(sources[stream]) is not int or sources[stream] != expected_selected[stream] for stream in streams
        ):
            return None, "Adapter returned an invalid Apply generation provenance."
        promoted_streams.update(revisions)
        previous_seq = seq
    if promoted_streams != set(expected_selected) - set(skipped):
        return None, "Adapter returned incomplete Apply generation coverage."
    return promoted_streams, None


class NSODeviceActionView(NSOActionPermissionMixin, View):
    """Trigger an adapter action (sync / detect-drift / connect) via POST."""

    def _incumbent_job(self, request, mgmt, exc, *, is_ajax):
        """Report a 409 under the name of the job that HOLDS the device (S5a C, codex R1-F7).

        Without the incumbent's type the UI polls the running job under the CLICKED action's
        label ("Sync from NSO running…" while an Apply runs). Best-effort: a failed lookup
        degrades to the generic wording rather than losing the conflict.
        """
        from . import adapter_client as client

        detail = exc.detail if isinstance(exc.detail, dict) else {}
        job_id = detail.get("job_id")
        incumbent_type = None
        if job_id:
            try:
                incumbent_type = (client.get_job(job_id) or {}).get("type")
            except AdapterError:
                incumbent_type = None
        if is_ajax:
            # 409, not 200: the action did not happen, and a poller that reads only the
            # status line must not record this as a started job.
            return JsonResponse({"status": "conflict", "job_id": job_id, "job_type": incumbent_type}, status=409)
        msg = (
            f"Another job is already running: {incumbent_type}."
            if incumbent_type
            else "A job is already running for this device."
        )
        if job_id:
            msg += f" (Job ID: {job_id})"
        messages.warning(request, msg)
        return redirect(_device_nso_tab_url(mgmt.device.pk))

    def _apply_unreadable_response(self, request, mgmt, message, *, is_ajax):
        """Report an unreadable successful Apply response without reverting promoted rows."""
        logger.error(
            "Adapter accepted Apply for device %s but returned an unreadable response: %s", mgmt.device_id, message
        )
        if is_ajax:
            return JsonResponse({"status": "error", "message": message}, status=502)
        messages.error(request, message)
        return redirect(_device_nso_tab_url(mgmt.device.pk))

    def _apply_result(self, request, mgmt, result, prepared, selected, *, label, is_ajax):
        """Surface one successful Apply response and its complete generation chain."""
        if not isinstance(result, dict):
            return self._apply_unreadable_response(
                request, mgmt, "Adapter returned an invalid Apply response.", is_ajax=is_ajax
            )
        outcome = result.get("outcome")
        expected_selected, skipped, partition_error = _apply_stream_partition(result, selected)
        if partition_error:
            return self._apply_unreadable_response(request, mgmt, partition_error, is_ajax=is_ajax)
        if outcome == "no_op":
            if set(skipped) != set(expected_selected) or result.get("generations") != []:
                return self._apply_unreadable_response(
                    request, mgmt, "Adapter returned incomplete Apply skip results.", is_ajax=is_ajax
                )
            _rollback_prepare_apply(prepared)
            msg = _stream_reason_message("Apply did not enqueue a job. Skipped streams", skipped)
            if is_ajax:
                return JsonResponse(
                    {
                        "status": "no_op",
                        "message": msg,
                        "skipped": skipped,
                        "generations": [],
                    }
                )
            messages.info(request, msg)
            return redirect(_device_nso_tab_url(mgmt.device.pk))
        if outcome != "promoted":
            return self._apply_unreadable_response(
                request, mgmt, "Adapter returned an invalid Apply outcome.", is_ajax=is_ajax
            )
        generations = result.get("generations")
        if not isinstance(generations, list) or not generations:
            return self._apply_unreadable_response(
                request, mgmt, "Adapter promoted Apply without a generation chain.", is_ajax=is_ajax
            )
        promoted_streams, coverage_error = _promoted_stream_coverage(generations, expected_selected, skipped)
        if coverage_error:
            return self._apply_unreadable_response(request, mgmt, coverage_error, is_ajax=is_ajax)
        job_id = generations[0]["job_id"]
        response_job_id = result.get("job_id")
        if job_id is None or type(response_job_id) is not int or response_job_id != job_id:
            msg = (
                f"Apply promoted {len(generations)} generation(s), but the adapter reported an invalid head job. "
                "The promoted rows remain applying until a result settles them."
            )
            if is_ajax:
                return JsonResponse(
                    {"status": "error", "message": msg, "generations": generations},
                    status=502,
                )
            messages.error(request, msg)
            return redirect(_device_nso_tab_url(mgmt.device.pk))
        _rollback_prepare_apply(prepared, keep_streams=promoted_streams)
        msg = f"{label} triggered. Tracking {len(generations)} generation(s) from Job ID {job_id}."
        if skipped:
            msg = f"{msg} {_stream_reason_message('Skipped streams', skipped)}"
        if is_ajax:
            return JsonResponse(
                {
                    "status": "ok",
                    "message": msg,
                    "job_id": job_id,
                    "skipped": skipped,
                    "generations": generations,
                }
            )
        messages.success(request, msg)
        return redirect(_device_nso_tab_url(mgmt.device.pk))

    def _adapter_error(self, request, mgmt, exc, prepared, *, action, label, is_ajax):
        """Roll back an unenqueued Apply and surface the adapter's failure."""
        if prepared is not None:
            _rollback_prepare_apply(prepared)
        if exc.code == "conflict":
            return self._incumbent_job(request, mgmt, exc, is_ajax=is_ajax)
        if action == "apply" and exc.code == "apply_unexecutable":
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            msg = _stream_reason_message(
                "Apply cannot execute the selected streams",
                detail.get("streams"),
            )
            if is_ajax:
                return JsonResponse({"status": "error", "message": msg}, status=409)
            messages.error(request, msg)
            return redirect(_device_nso_tab_url(mgmt.device.pk))
        # No deployment-gate branch here: the gate is the plugin's OWN middleware, which
        # answers the POST before this view runs. An adapter 503 is an ordinary adapter
        # failure and keeps the adapter's own message.
        if is_ajax:
            return JsonResponse({"status": "error", "message": str(exc)}, status=502)
        messages.error(request, f"Adapter error triggering {label}: {exc}")
        return redirect(_device_nso_tab_url(mgmt.device.pk))

    def post(self, request, pk, action):
        """Fire the requested action against the nso-adapter and redirect back."""
        from . import adapter_client as client

        mgmt = get_object_or_404(NSODeviceManagement, pk=pk)
        label = _ACTION_LABELS.get(action, action)
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        if action not in _ACTION_LABELS:
            if is_ajax:
                return JsonResponse({"status": "error", "message": f"Unknown action: {action}"}, status=400)
            messages.error(request, f"Unknown action: {action}")
            return redirect(mgmt.device.get_absolute_url())

        if mgmt.adapter_device_id is None:
            msg = "Device is not yet onboarded to the adapter."
            if is_ajax:
                return JsonResponse({"status": "error", "message": msg}, status=409)
            messages.warning(request, msg)
            return redirect(_device_nso_tab_url(mgmt.device.pk))

        action_fn = {
            "sync": client.trigger_sync,
            "sync-from-nso": client.trigger_sync_from_nso,
            "detect-drift": client.trigger_detect_drift,
            "connect": client.trigger_connect,
            "apply": client.trigger_apply,
        }[action]

        # One Apply commits everything: the adapter worker applies the intent-stored
        # scopes (attrs/IP/SNMP/routing/L2), and here we force-commit the LACP +
        # switchport snapshots, which are owned in NetBox rather than mirrored in the
        # adapter. Accept itself only marks rows owned (no immediate device write).
        prepared = None
        selected = None
        try:
            if action == "apply":
                prepared, selected = _prepare_apply(mgmt)
        except ApplyRefused as exc:
            # CodeQL py/stack-trace-exposure: rebuild the wording from the refusal type and
            # its registry-keyed fields, never from the exception object.
            msg = _apply_refusal_message(exc, mgmt)
            logger.warning("Apply refused for device %s (%s): %s", mgmt.device_id, type(exc).__name__, msg)
            if is_ajax:
                return JsonResponse({"status": "error", "message": msg}, status=409)
            messages.error(request, msg)
            return redirect(_device_nso_tab_url(mgmt.device.pk))

        try:
            result = (
                action_fn(mgmt.adapter_device_id, selected) if action == "apply" else action_fn(mgmt.adapter_device_id)
            )
            if action == "apply":
                return self._apply_result(request, mgmt, result, prepared, selected, label=label, is_ajax=is_ajax)

            job_id = result.get("job_id") if result else None
            if is_ajax:
                return JsonResponse({"status": "ok", "job_id": job_id})
            if job_id:
                messages.success(request, f"{label} triggered — Job ID: {job_id}. Refresh the page to see results.")
            else:
                messages.success(request, f"{label} triggered.")
        except AdapterError as exc:
            # The Apply never enqueued a job (adapter unreachable/500, or a conflict rejected
            # ours), so roll back the deploying marks THIS Apply made — otherwise the rows are
            # stuck 'applying' with nothing to ever settle them.
            return self._adapter_error(
                request,
                mgmt,
                exc,
                prepared,
                action=action,
                label=label,
                is_ajax=is_ajax,
            )

        return redirect(_device_nso_tab_url(mgmt.device.pk))


class NSOIntentResyncView(NSOActionPermissionMixin, View):
    """POST: re-sync orphaned adapter intent to NetBox ownership (clears split-brain).

    Re-pushes the device's current owned intent for every orphaned scope; the adapter's
    full-replace then drops the rows NetBox no longer owns. Never writes to the device.
    """

    def post(self, request, pk):
        """Re-sync orphaned adapter intent for the device, then redirect to the NSO tab."""
        from .intent_drift import resync_intent

        mgmt = get_object_or_404(NSODeviceManagement, pk=pk)
        if mgmt.adapter_device_id is None:
            messages.warning(request, "Device is not yet onboarded to the adapter.")
            return redirect(_device_nso_tab_url(mgmt.device.pk))
        try:
            done, failed = resync_intent(mgmt.device, mgmt)
            if done:
                messages.success(request, f"Re-synced adapter intent — cleared orphaned: {', '.join(done)}.")
            if failed:
                # A refused or unanswered push cleared nothing, so it is reported as the
                # failure it is; the NSO tab renders the per-scope cause the claim recorded.
                messages.error(
                    request,
                    f"The adapter did not acknowledge: {', '.join(failed)}. That intent is still "
                    "orphaned. See the per-scope push error on this tab, then retry.",
                )
            if not done and not failed:
                messages.info(request, "No orphaned adapter intent to clear.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Intent re-sync failed for device %s: %s", mgmt.device_id, exc)
            messages.error(request, f"Intent re-sync failed: {exc}")
        return redirect(_device_nso_tab_url(mgmt.device.pk))


class NSOAdapterLinkRetryView(NSOActionPermissionMixin, View):
    """POST: retry linking a managed device to the adapter after a failed onboard/scope/sync.

    Re-fires ``sync_scope_to_adapter`` by re-saving the management row (the same recovery the
    async-onboarding status advance uses), which onboards/adopts the adapter device, pushes scope
    and sends sync-notify. On success the row's ``adapter_link_error`` is cleared and
    ``adapter_device_id`` set; on a repeated failure the error is refreshed for the tab banner.
    """

    def post(self, request, pk):
        """Re-attempt the adapter link for the device, then redirect to the NSO tab."""
        mgmt = get_object_or_404(NSODeviceManagement, pk=pk)
        mgmt.save()  # re-fires sync_scope_to_adapter (onboard → scope → sync-notify)
        mgmt.refresh_from_db()
        if mgmt.adapter_link_error:
            messages.error(request, f"Still couldn't link this device to the adapter: {mgmt.adapter_link_error}")
        else:
            messages.success(request, "Device linked to the adapter.")
        return redirect(_device_nso_tab_url(mgmt.device.pk))


class NSOForceRemovalView(NSOActionPermissionMixin, View):
    """POST: re-run a blocked removal with the adapter's collateral guard disabled.

    The operator override for a ``removal_blocked_collateral`` failure: the tab banner
    shows the orphaned service rows and the dry-run device delta the adapter refused to
    commit; this deliberately flushes them (the adapter re-runs the scope's PUT-replace
    with ``force=true``). Destructive by design — the button carries a confirm dialog.
    """

    def post(self, request, pk):
        """Queue the forced removal for the POSTed scope and report the job id."""
        from . import adapter_client as client

        mgmt = get_object_or_404(NSODeviceManagement, pk=pk)
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        scope = (request.POST.get("scope") or "").strip()

        if not scope:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Missing removal scope."}, status=400)
            messages.error(request, "Missing removal scope.")
            return redirect(_device_nso_tab_url(mgmt.device.pk))
        if mgmt.adapter_device_id is None:
            msg = "Device is not yet onboarded to the adapter."
            if is_ajax:
                return JsonResponse({"status": "error", "message": msg}, status=409)
            messages.warning(request, msg)
            return redirect(_device_nso_tab_url(mgmt.device.pk))

        try:
            result = client.trigger_force_removal(mgmt.adapter_device_id, scope)
            job_id = result.get("job_id") if result else None
            if is_ajax:
                return JsonResponse({"status": "ok", "job_id": job_id})
            messages.success(
                request,
                f"Force removal ({scope}) queued — Job ID: {job_id}. "
                "The orphaned service rows will be retracted from the device.",
            )
        except AdapterError as exc:
            if is_ajax:
                return JsonResponse({"status": "error", "message": str(exc)}, status=502)
            messages.error(request, f"Adapter error triggering force removal: {exc}")
        return redirect(_device_nso_tab_url(mgmt.device.pk))


class NSOJobStatusView(LoginRequiredMixin, View):
    """Return JSON status of an adapter job — used for client-side polling."""

    def get(self, request, job_id):
        """Proxy a GET /api/v1/jobs/{job_id} call to the adapter and return the result."""
        from . import adapter_client as client

        try:
            job = client.get_job(job_id)
            return JsonResponse(job)
        except AdapterError as exc:
            return JsonResponse({"error": str(exc)}, status=502)


def _removal_job_scope(job):
    """Best-effort scope attribution for a removal job.

    The queue context carries the scope for every job state; older adapter jobs
    predate context serialization, so fall back to the terminal result (succeeded)
    or the error detail (blocked/failed).
    """
    # context/result are objects by contract, but ``error.detail`` is free-form JSON.
    for part in (job.get("context"), job.get("result"), (job.get("error") or {}).get("detail")):
        if isinstance(part, dict) and part.get("scope"):
            return part["scope"]
    return None


def _blocked_removals(jobs):
    """Collect scopes whose LATEST removal job was blocked on collateral.

    Newest-first scan: the most recent removal job per scope decides — a later
    removal for the same scope (queued force re-run, or a clean success after the
    orphans were re-accepted) masks a stale block, while other scopes' jobs and
    non-removal jobs never do. A blocked removal means the intent retraction is NOT
    enforced on the device, so the entry carries everything the operator needs to
    resolve it: the orphan keys and the dry-run preview the adapter refused to commit.
    """
    blocked = []
    seen_scopes = set()
    for job in jobs:
        if job.get("type") != "removal":
            continue
        scope = _removal_job_scope(job)
        if not scope or scope in seen_scopes:
            continue
        seen_scopes.add(scope)
        error = job.get("error") or {}
        if job.get("status") == "failed" and error.get("code") == "removal_blocked_collateral":
            detail = error.get("detail")
            detail = detail if isinstance(detail, dict) else {}
            orphans = detail.get("orphans")
            # The block itself is the operator-critical fact, so a junk orphans map
            # empties the list rather than hiding the banner that reports the block.
            orphans = orphans if isinstance(orphans, dict) else {}
            if not orphans:
                # pre-generalization job shape (isis-only guard era)
                orphans = {
                    label: value
                    for label, value in (
                        ("interface-config", detail.get("orphan_interfaces")),
                        ("process-config", detail.get("orphan_processes")),
                    )
                    if value
                }
            blocked.append(
                {
                    "scope": scope,
                    "job_id": job.get("id"),
                    "orphans": orphans,
                    "preview": detail.get("preview") or "",
                    "blocked_at": job.get("updated_at"),
                }
            )
    return blocked


def _residue_removals(jobs):
    """Collect scopes whose LATEST removal job SUCCEEDED but left device residue (#104-A).

    FASTMAP's reverse diff keeps service entries that picked up foreign leaves (the
    sw03 Vlan987 husk), so a removal can report success while its keys survive on the
    device; the adapter records survivors in ``job.result.residue``. Same newest-first
    per-scope masking as :func:`_blocked_removals`: a later removal for the scope
    (e.g. a clean re-run) displaces a stale residue report. The entry carries the
    surviving keys so the operator can attribute the re-imported unowned rows to the
    retraction instead of mistaking them for new device config.
    """
    found = []
    seen_scopes = set()
    for job in jobs:
        if job.get("type") != "removal":
            continue
        scope = _removal_job_scope(job)
        if not scope or scope in seen_scopes:
            continue
        seen_scopes.add(scope)
        result = job.get("result") or {}
        if job.get("status") == "succeeded" and result.get("residue"):
            found.append(
                {
                    "scope": scope,
                    "job_id": job.get("id"),
                    "residue": result["residue"],
                    "detected_at": job.get("updated_at"),
                }
            )
    return found


# ── #104 phase-2: per-row residue badges in the category grids ────────────────


def _norm_ip_triple(t):
    """Canonicalize an (interface, address, vrf) key for residue matching.

    The adapter reports the trigger's NetBox text form while the re-imported row
    carries the device form of the same address — IPv6 case and zero-compression
    can differ. Unparseable addresses fall back to the raw string.
    """
    import ipaddress

    iface, address, vrf = (tuple(t) + ("", "", ""))[:3]
    try:
        address = str(ipaddress.ip_interface(address))
    except ValueError:
        pass
    return (iface, address, vrf)


def _residue_matchers():
    """Category key → (adapter removal scope, row matchers[, key normalizer]) for residue badging.

    Each matcher is (ctx path — ``var`` or ``var.subkey`` —, the residue YANG-list
    label — a per-row callable for route_policy's family bucketing —, row → key
    tuple). Key tuples mirror the adapter's removed-key grain verbatim; both sides
    are str-normalized before comparing, then passed through the spec's optional
    normalizer (interface_ips: IPv6 text forms). snmp communities match on the
    opaque SHA-256 label the export publishes (never the community string), so no
    secret is involved anywhere in the residue path.
    """
    from .delivery import delivery_keys

    def _iface(r):
        return (r.interface.name,)

    rp_family_lists = {  # mirrors the adapter's _ROUTE_POLICY_FAMILY_LISTS
        "prefix_list": "prefix-list",
        "community_list": "community-list",
        "as_path": "as-path",
        "route_map": "route-map",
    }

    return {
        "svi": ("svi", [("svi_states", "interface", _iface)]),
        "subinterface": ("subinterface", [("subinterface_states", "interface", _iface)]),
        "interface_mtu": ("interface_mtu", [("interface_mtu_states", "interface", _iface)]),
        "bfd": ("bfd", [("bfd_states", "interface", _iface)]),
        "static": (
            "static_route",
            [("static_routes", "route", lambda r: (r.nso_vrf or "", r.nso_prefix or "", r.nso_next_hop or ""))],
        ),
        "vlan": ("vlan", [("vlan_states", "vlan", lambda r: (r.vlan.vid,))]),
        "logging": ("logging", [("logging_data.hosts", "host", lambda r: (r.address,))]),
        "l2_services": ("l2_sap", [("l2_sap_states", "sap", lambda r: (r.service_name or "", r.sap_id or ""))]),
        "bgp": ("bgp", [("bgp_peers", "peer", lambda r: (r.peer_address_str or "",))]),
        "isis": (
            "isis",
            [
                ("isis_interfaces", "interface-config", lambda r: (r.interface.name, r.af)),
                ("isis_processes", "process-config", lambda r: (r.process_tag or "",)),
            ],
        ),
        "ospf": (
            "ospf",
            [
                ("ospf_data.interfaces", "interface-config", _iface),
                ("ospf_data.instances", "process-config", lambda r: (r.process_id,)),
            ],
        ),
        "route_policy": (
            "route_policy",
            [("route_policy_states", lambda r: rp_family_lists.get(r.family or ""), lambda r: (r.object_name or "",))],
        ),
        "snmp": (
            "snmp",
            [
                ("snmp_data.communities", "community", lambda r: (r.community_hash or "",)),
                ("snmp_data.v3_users", "v3-user", lambda r: (r.username or "",)),
                ("snmp_data.hosts", "host", lambda r: (r.address or "",)),
            ],
        ),
        # #104 phase-3: interface receipt residue is value-grain. The adapter reports
        # the removed (interface, address, vrf) triples that survived the retraction.
        # The key is the ADAPTER's removal scope (VALID_REMOVAL_SCOPES), not the plugin's
        # outbound delivery key "ip"; card #1591 owns unifying the two vocabularies.
        "interface_ips": (
            delivery_keys()["interface"].section,
            [("interface_ips", "address", lambda r: (r.interface.name, r.address, r.vrf or ""))],
            _norm_ip_triple,
        ),
    }


def _annotate_residue_rows(ctx: dict, key: str, mgmt) -> None:
    """Mark grid rows that are a retraction's on-device residue (#104 phase-2).

    Reads the device's adapter jobs and reuses :func:`_residue_removals`' newest-
    first per-scope masking, then flags each row of *key*'s grids whose key is in
    the scope's latest residue report (``row.residue_survivor`` +
    ``row.residue_job_id``) — so a re-imported husk is attributable in place
    instead of reading as new device config. Best-effort decoration: adapter
    trouble or a half-linked row means no badge, never a broken grid (the tab
    banner remains the primary surface).
    """
    spec = _residue_matchers().get(key)
    if spec is None or mgmt is None or mgmt.adapter_device_id is None:
        return
    scope, matchers = spec[0], spec[1]
    norm = spec[2] if len(spec) > 2 else (lambda t: t)
    from . import adapter_client as client

    try:
        jobs = client.list_jobs(mgmt.adapter_device_id)
    except Exception:  # noqa: BLE001 — best-effort decoration only
        return
    entry = next((e for e in _residue_removals(jobs) if e.get("scope") == scope), None)
    if not entry:
        return
    # ``result`` is an object by contract; the residue map inside it is free-form JSON,
    # and this badge is decoration: a junk report costs the badge, never the grid.
    residue = entry.get("residue")
    keys_by_label = {
        label: {norm(tuple(str(p) for p in (k if isinstance(k, (list, tuple)) else (k,)))) for k in klist}
        for label, klist in (residue if isinstance(residue, dict) else {}).items()
        if isinstance(klist, (list, tuple))
    }
    for ctx_path, label, keyfn in matchers:
        rows = ctx
        for part in ctx_path.split("."):
            rows = rows.get(part) if isinstance(rows, dict) else None
        for row in rows or []:
            try:
                row_label = label(row) if callable(label) else label
                targets = keys_by_label.get(row_label)
                if targets and norm(tuple(str(p) for p in keyfn(row))) in targets:
                    row.residue_survivor = True
                    row.residue_job_id = entry.get("job_id")
            except Exception:  # noqa: BLE001 — one unmatched row must not kill the grid
                continue


class NSODeviceJobsView(LoginRequiredMixin, View):
    """JSON summary of a device's adapter jobs for the tab's status strip.

    Returns the currently-active job (queued/running) if any, the most recent
    finished job (succeeded/failed) so an operator returning to the tab can see at a
    glance whether work is in flight and how the last run went, and any removals
    blocked by the adapter's collateral guard — those persist until resolved rather
    than being displaced by later jobs. Polled client-side while a job is active.
    """

    _ACTIVE = ("queued", "running")
    _TERMINAL = ("succeeded", "failed")

    def get(self, request, pk):
        """Return the device's adapter jobs and the requested Apply generation chain."""
        device = get_object_or_404(Device, pk=pk)
        mgmt = getattr(device, "nso_management", None)
        if mgmt is None or mgmt.adapter_device_id is None:
            return JsonResponse(
                {
                    "onboarded": False,
                    "running": None,
                    "last": None,
                    "jobs": [],
                    "generations": [],
                    "blocked_removals": [],
                    "residue_removals": [],
                }
            )

        from . import adapter_client as client

        try:
            generation_ids = {int(value) for value in request.GET.getlist("generation_id")}
        except ValueError:
            return JsonResponse({"error": "generation_id must be an integer"}, status=400)
        raw_since_seq = request.GET.get("since_seq")
        try:
            since_seq = None if raw_since_seq is None else int(raw_since_seq)
        except ValueError:
            return JsonResponse({"error": "since_seq must be an integer"}, status=400)
        if since_seq is not None and since_seq < 0:
            return JsonResponse({"error": "since_seq must be non-negative"}, status=400)
        try:
            jobs = client.list_jobs(mgmt.adapter_device_id)
            generations = (
                client.list_device_generations(mgmt.adapter_device_id, since_seq=since_seq) if generation_ids else []
            )
        except AdapterError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        generations = [row for row in generations if row.get("generation_id") in generation_ids]
        serialized_jobs = jobs
        if generation_ids:
            generation_job_ids = {row.get("job_id") for row in generations if row.get("job_id") is not None}
            serialized_jobs = [job for job in jobs if job.get("id") in generation_job_ids]

        # list_jobs is most-recent-first, so the first match in each bucket is newest.
        running = next((j for j in jobs if j.get("status") in self._ACTIVE), None)
        last = next((j for j in jobs if j.get("status") in self._TERMINAL), None)
        return JsonResponse(
            {
                "onboarded": True,
                "running": running,
                "last": last,
                "jobs": serialized_jobs,
                "generations": generations,
                "blocked_removals": _blocked_removals(jobs),
                "residue_removals": _residue_removals(jobs),
            }
        )


class NSORefreshStateView(NSOActionPermissionMixin, View):
    """Fetch live compliance + interface data from the adapter and cache it."""

    def post(self, request, pk):
        """Call the adapter and update state_snapshot on the management record."""
        from . import adapter_client as client

        mgmt = get_object_or_404(NSODeviceManagement, pk=pk)

        if mgmt.adapter_device_id is None:
            messages.warning(request, "Device is not yet onboarded to the adapter.")
            return redirect(_device_nso_tab_url(mgmt.device.pk))

        try:
            compliance = client.get_state(mgmt.adapter_device_id)
            from .read_gate import _is_authoritative

            doc = client.get_interfaces_doc(mgmt.adapter_device_id)
            interfaces = doc.get("interfaces", [])
            # The FULL gate tuple decides authoritativeness (codex B5-R2-4) — an
            # outcome=present with succeeded=false/result=error is a failed read.
            # Key absent = pre-S4 adapter (legacy, replace); explicit null = malformed.
            read_state = doc.get("read_state")
            authoritative = "read_state" not in doc or (isinstance(read_state, dict) and _is_authoritative(read_state))
            if not authoritative:
                # a non-authoritative doc (e.g. not_ready after a store reset) serves a
                # legitimately EMPTY list — keep the last-known interfaces (codex B5-F5)
                interfaces = (mgmt.state_snapshot or {}).get("interfaces", [])
                messages.warning(request, "Interface read unavailable — kept last-known interface data.")
            NSODeviceManagement.objects.filter(pk=mgmt.pk).update(
                state_snapshot={
                    "compliance": compliance,
                    "interfaces": interfaces,
                    "refreshed_at": timezone.now().isoformat(),
                }
            )
            messages.success(request, "Compliance data refreshed.")
        except AdapterError as exc:
            messages.error(request, f"Could not reach adapter: {exc}")

        return redirect(_device_nso_tab_url(mgmt.device.pk))


class NSODeviceReconcileView(NSOActionPermissionMixin, View):
    """POST: queue a background refresh of the plugin's NSO*State display cache.

    Lighter than 'Sync Now' — it does NOT run an NSO/device sync; it just re-pulls
    the adapter's current state into the plugin cache so the tab counts update. The
    same reconcile the adapter's sync-complete callback runs automatically.
    """

    def post(self, request, pk):
        """Enqueue the reconcile and redirect back to the device NSO tab."""
        from .reconcile import enqueue_device_reconcile

        mgmt = get_object_or_404(NSODeviceManagement, pk=pk)
        if mgmt.adapter_device_id is None:
            messages.warning(request, "Device is not yet onboarded to the adapter.")
        else:
            enqueue_device_reconcile(mgmt.device_id)
            messages.success(request, "Refresh overlays queued — category counts will update shortly.")
        return redirect(_device_nso_tab_url(mgmt.device_id))


# ── NSO Interface State CRUD ─────────────────────────────────────────────────


class NSOInterfaceStateListView(generic.ObjectListView):
    """Cross-device interface-attribute *drift dashboard*.

    Not a dump of every overlay row — it shows only attributes that need attention
    (drift / pending apply / apply-failed) across all devices, with the device
    column, so an operator can triage from one place. In-sync/imported rows are
    excluded (the per-device NSO tab is the full value-aware surface). Status-based
    filter: the adapter status lags a value typed straight into NetBox, so click
    through to the device tab for the value-aware truth.
    """

    template_name = "netbox_nso_plugin/links_object_list.html"
    queryset = NSOInterfaceState.objects.exclude(status__in=("imported", "in_sync")).select_related(
        "interface", "interface__device"
    )
    table = NSOInterfaceStateTable
    filterset = NSOInterfaceStateFilterSet
    # Sync-managed: no add/import/edit — just export + bulk-delete (cleanup).
    actions = (BulkExport, BulkDelete)


class NSOInterfaceStateBulkDeleteView(generic.BulkDeleteView):
    """Bulk-delete view for NSOInterfaceState rows (cleanup)."""

    template_name = "netbox_nso_plugin/links_bulk_delete.html"
    queryset = NSOInterfaceState.objects.all()
    table = NSOInterfaceStateTable
    filterset = NSOInterfaceStateFilterSet


class NSOInterfaceStateView(generic.ObjectView):
    """Detail view for an NSOInterfaceState record."""

    queryset = NSOInterfaceState.objects.select_related("interface")


class NSOInterfaceStateDeleteView(generic.ObjectDeleteView):
    """Delete view for an NSOInterfaceState record."""

    template_name = "netbox_nso_plugin/links_object_delete.html"
    queryset = NSOInterfaceState.objects.all()


# ── Accept workflow ───────────────────────────────────────────────────────────


def _push_intent_for_device(device_id: int) -> None:
    """Record the device's interface intent in the outbox, which drains it once.

    Appends rather than pushes (#1503 Appendix O): every in-protocol send is a claimed,
    sequenced logical operation, so the view-level bulk accept goes through the same outbox
    as the accept signal and the Decision-G edit signal rather than calling the builder
    around it. The caller must hold a writer transaction, as ``NSOBulkAcceptView.post`` does.
    """
    try:
        mgmt = NSODeviceManagement.objects.select_related("nso_instance").get(device_id=device_id)
    except NSODeviceManagement.DoesNotExist:
        logger.warning("No NSODeviceManagement for device %s, skipping intent push", device_id)
        return

    if mgmt.adapter_device_id is None:
        return

    _schedule_intent_push((device_id, "interface"))


# Statuses where the NetBox value already matches the device — accepting them
# leaves nothing to apply.
_MATCHING_SOURCE_STATUSES = ("imported", "in_sync")


def _status_after_accept(source_status: str) -> str:
    """Status a row should take when the operator accepts it.

    Accepting a value that already matches the device (imported / in_sync) means
    NetBox simply owns what's already there — nothing to push, so it stays
    ``in_sync``. Accepting a *differing* value (changed / drifted / conflict) creates
    real intent that the device doesn't have yet → ``accepted`` ("pending apply").
    """
    return "in_sync" if source_status in _MATCHING_SOURCE_STATUSES else "accepted"


class NSOAcceptAttributeView(NSOActionPermissionMixin, View):
    """Accept a single interface attribute as NetBox intent."""

    def post(self, request, pk):
        """Accept the interface state.

        Accepting a value that matches the device → in_sync (nothing to apply);
        a differing value → accepted, and the post_save signal pushes intent.
        """
        state = get_object_or_404(NSOInterfaceState, pk=pk)
        state.status = _status_after_accept(state.status)
        state.accepted_at = timezone.now()
        with transaction.atomic():
            state.save(update_fields=["status", "accepted_at"])

        msg = f"Accepted {state.attribute} on {state.interface}."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"status": "ok", "message": msg})
        messages.success(request, msg)
        return redirect(_device_nso_tab_url(state.interface.device_id))


class NSOAcceptDeviceView(NSOActionPermissionMixin, View):
    """Accept the DEVICE's value into NetBox for a drifted interface attribute.

    The opposite of 'Keep NetBox': when the device changed out-of-band, this pulls the
    device (NSO) value onto the dcim.Interface so NetBox matches reality again → the
    attribute becomes in_sync. The write is suppressed so it is NOT pushed back as
    intent (the device already has the value — nothing to apply).
    """

    def post(self, request, pk):
        """Copy the device value onto the interface and mark the state in_sync."""
        from .signals import suppress_intent_push

        state = get_object_or_404(NSOInterfaceState, pk=pk)
        iface = state.interface
        dev_val = state.nso_value
        with suppress_intent_push():
            if state.attribute == "description":
                iface.description = dev_val or ""
                iface.save(update_fields=["description"])
            elif state.attribute == "enabled":
                iface.enabled = str(dev_val).lower() == "true"
                iface.save(update_fields=["enabled"])
            state.status = "in_sync"
            state.accepted_at = timezone.now()
            state.save(update_fields=["status", "accepted_at"])

        msg = f"Adopted device value for {state.attribute} on {iface}."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"status": "ok", "message": msg})
        messages.success(request, msg)
        return redirect(_device_nso_tab_url(iface.device_id))


class NSOInterfaceEditFieldView(NSOActionPermissionMixin, View):
    """Inline-edit a managed interface attribute (description / enabled) from the NSO tab.

    Writes the new value onto the ``dcim.Interface`` and saves it, which fires the
    Decision-G signal chain (:func:`signals._push_intent_on_interface_edit`) exactly
    as editing the interface through the NetBox UI would: the attribute becomes
    NetBox-owned intent and is pushed to the adapter. AJAX-only — returns JSON so the
    tab's inline editor can refresh just the rows without collapsing the category.
    """

    _EDITABLE = ("description", "enabled")
    _TRUE = ("true", "1", "on", "yes")

    def post(self, request, pk):
        """Apply the new value to the interface; Decision-G handles ownership + push."""
        state = get_object_or_404(NSOInterfaceState, pk=pk)
        attribute = state.attribute
        if attribute not in self._EDITABLE:
            return JsonResponse({"status": "error", "message": f"{attribute} is not editable here."}, status=400)

        iface = state.interface
        raw = request.POST.get("value", "")
        if attribute == "description":
            iface.description = raw.strip()
        else:  # enabled
            iface.enabled = raw.strip().lower() in self._TRUE
        with transaction.atomic():
            iface.save(update_fields=[attribute])

        return JsonResponse({"status": "ok", "message": f"Updated {attribute} on {iface.name}."})


def _unique_collision_response(obj, editable):
    """400 JSON when *obj* would violate a unique constraint, else None.

    ``field.clean()`` is FIELD-level only: it never checks unique / unique_together. So an
    inline edit to a value that collides with a sibling row — ``logging_host`` exposes
    ``address``, half of NSOLoggingHostState's (management, address) unique_together — sailed
    through validation, reached ``obj.save()``, and raised an unhandled IntegrityError: HTTP
    500, and a popover that just spins. Report the collision like any other field error.
    """
    from django.core.exceptions import ValidationError

    try:
        obj.validate_unique()
    except ValidationError as exc:
        collisions = {f: [str(m) for m in msgs] for f, msgs in exc.message_dict.items() if f in editable}
        elsewhere = [str(m) for f, msgs in exc.message_dict.items() if f not in editable for m in msgs]
        payload: dict = {"status": "error"}
        if collisions:
            payload["errors"] = collisions
        if elsewhere:
            payload["message"] = " ".join(elsewhere)
        return JsonResponse(payload, status=400)
    return None


def _bfd_field_errors(obj):
    """Return netbox-routing's BFD timer bounds as popover field errors."""
    errors = {}
    for name in ("min_tx", "min_rx"):
        value = getattr(obj, name)
        if value is not None and not 60 <= value <= 60000:
            errors[name] = ["Must be between 60 and 60000 ms."]
    if obj.multiplier is not None and not 0 <= obj.multiplier <= 255:
        errors["multiplier"] = ["Must be between 0 and 255."]
    return errors


def _logging_host_errors(obj):
    """Validate values against the logging-reconciler service contract."""
    errors = {}
    if obj.port is not None and not 1 <= obj.port <= 65_535:
        errors["port"] = ["Enter a port between 1 and 65535."]
    if obj.transport not in ("", "udp", "tcp"):
        errors["transport"] = ["Transport must be UDP, TCP, or device default."]
    return errors


def _snmp_community_errors(obj):
    """Validate community policy against the adapter's strict access enum."""
    if obj.access not in ("RO", "RW", "ro", "rw"):
        return {"access": ["Access must be RO or RW."]}
    return {}


def _snmp_host_errors(obj):
    """Validate trap/inform settings before the row becomes owned intent."""
    errors = {}
    if obj.version not in ("1", "v1", "2", "2c", "v2c", "3", "v3"):
        errors["version"] = ["Version must be v1, v2c, or v3."]
    if obj.notify_type not in ("trap", "traps", "inform", "informs"):
        errors["notify_type"] = ["Notification must be trap or inform."]
    if obj.port is not None and not 1 <= obj.port <= 65_535:
        errors["port"] = ["Enter a port between 1 and 65535."]
    if obj.version in ("3", "v3") and not obj.username:
        errors["username"] = ["A v3 security user is required for an SNMPv3 host."]
    return errors


def _ospf_instance_errors(obj):
    """Validate an inline router-ID edit against the linked native instance."""
    from django.core.exceptions import ValidationError

    native = obj.ospf_instance
    if native is None:
        return {"router_id": ["Only a linked NetBox OSPF instance can be edited inline."]}
    errors = {}
    try:
        native._meta.get_field("router_id").clean(obj.router_id, native)
    except ValidationError as exc:
        errors["router_id"] = [str(message) for message in exc.messages]
    if obj.vrf and not native._meta.get_field("vrf").remote_field.model.objects.filter(name=obj.vrf).exists():
        errors.setdefault("router_id", []).append(f"The overlay VRF {obj.vrf!r} does not exist in NetBox.")
    return errors


def _ospf_interface_errors(obj):
    """Validate editable OSPF knobs using netbox-routing's real model rules."""
    from django.core.exceptions import ValidationError
    from netbox_routing.models import OSPFArea, OSPFInstance, OSPFInterface

    errors = {}
    if not OSPFInterface.objects.filter(interface=obj.interface).exists():
        return {"area_id": ["Only a linked NetBox OSPF interface can be edited inline."]}
    if not OSPFInstance.objects.filter(device=obj.interface.device, process_id=obj.process_id).exists():
        errors["area_id"] = [f"OSPF process {obj.process_id!r} does not exist on this device in NetBox."]
    if not obj.area_id:
        errors.setdefault("area_id", []).append("Area is required.")
    else:
        try:
            OSPFArea(area_id=obj.area_id, area_type="standard").full_clean()
        except ValidationError as exc:
            messages = exc.message_dict.get("area_id", exc.messages)
            errors.setdefault("area_id", []).extend(str(message) for message in messages)

    native = OSPFInterface.objects.filter(interface=obj.interface).first()
    if native is not None:
        for overlay_field, native_field in (("network_type", "network_type"), ("cost", "cost")):
            try:
                native._meta.get_field(native_field).clean(getattr(obj, overlay_field), native)
            except ValidationError as exc:
                errors[overlay_field] = [str(message) for message in exc.messages]
    return errors


def _isis_instance_errors(obj):
    """Validate safe process knobs without exposing authentication keys inline."""
    from django.core.exceptions import ValidationError
    from netbox_routing.helpers.isis import NET_RE

    native = obj.isis_instance
    if native is None:
        return {"net": ["Only a linked NetBox IS-IS instance can be edited inline."]}
    errors = {}
    if obj.net and not NET_RE.match(obj.net):
        errors["net"] = ["Enter a valid NET, e.g. 49.0001.0000.0000.0001.00."]
    for name in ("is_type", "metric_style", "overload_bit", "fast_reroute", "microloop_avoidance"):
        try:
            native._meta.get_field(name).clean(getattr(obj, name), native)
        except ValidationError as exc:
            errors[name] = [str(message) for message in exc.messages]
    return errors


def _isis_interface_errors(obj):
    """Validate safe interface knobs against the linked native IS-IS object."""
    from django.core.exceptions import ValidationError
    from netbox_routing.models import ISISInstance

    native = obj.isis_interface
    if native is None:
        return {"circuit_type": ["Only a linked NetBox IS-IS interface can be edited inline."]}
    if native.address_family != obj.af:
        return {"circuit_type": ["The linked NetBox IS-IS interface uses a different address family."]}
    errors = {}
    if not ISISInstance.objects.filter(device=obj.interface.device, process_tag=obj.process_tag).exists():
        errors["circuit_type"] = [f"IS-IS process {obj.process_tag!r} does not exist on this device in NetBox."]
    for name in (
        "circuit_type",
        "network_type",
        "metric",
        "passive",
        "bfd_enabled",
        "frr_enabled",
        "frr_protection",
    ):
        try:
            native._meta.get_field(name).clean(getattr(obj, name), native)
        except ValidationError as exc:
            errors[name] = [str(message) for message in exc.messages]
    if obj.frr_protection and obj.frr_enabled is not True:
        errors.setdefault("frr_protection", []).append("FRR protection requires FRR to be enabled.")
    return errors


def _bgp_peer_errors(obj):
    """Validate the editable BGP peer fields without changing peer identity."""
    if obj.bgp_peer is None:
        return {"remote_as_str": ["Only a linked NetBox BGP peer can be edited inline."]}
    if not obj.remote_as_str:
        return {}
    try:
        asn = int(obj.remote_as_str)
    except (TypeError, ValueError):
        return {"remote_as_str": ["Enter a valid ASN."]}
    if not 1 <= asn <= 4_294_967_295:
        return {"remote_as_str": ["ASN must be between 1 and 4294967295."]}
    return {}


def _redistribution_errors(obj):
    """Validate policy knobs against the linked native redistribution scope."""
    from django.core.exceptions import ValidationError
    from netbox_routing.models import RouteMap

    native = obj.redistribution
    if native is None:
        return {"route_map": ["Only a linked NetBox redistribution can be edited inline."]}

    errors = {}
    route_map = None
    if obj.route_map:
        route_map = RouteMap.objects.filter(name=obj.route_map).first()
        if route_map is None:
            errors["route_map"] = [f"Route map {obj.route_map!r} does not exist in NetBox."]

    native.route_map = route_map if route_map is not None or not obj.route_map else native.route_map
    native.metric = obj.metric
    native.metric_type = obj.metric_type
    try:
        native.full_clean()
    except ValidationError as exc:
        for field, messages in exc.message_dict.items():
            target = field if field in ("route_map", "metric", "metric_type") else "metric_type"
            errors.setdefault(target, []).extend(str(message) for message in messages)
    return errors


def _static_route_errors(obj):
    """Validate native static-route policy without exposing route identity inline."""
    from django.core.exceptions import ValidationError

    native = obj.static_route
    if native is None:
        return {"metric": ["Only a linked NetBox static route can be edited inline."]}
    if native.metric is None:
        return {"metric": ["Metric is required."]}

    errors = {}
    try:
        native.full_clean()
    except ValidationError as exc:
        for field, messages in exc.message_dict.items():
            target = field if field in ("metric", "permanent", "tag") else "metric"
            errors.setdefault(target, []).extend(str(message) for message in messages)
    return errors


def _lacp_errors(key, obj):
    """Validate LACP knobs against the uint16/string contract exposed by NSO."""
    errors = {}
    if key == "lacp_bundle":
        for field in ("min_links", "system_priority", "admin_key"):
            value = getattr(obj, field)
            if value is not None and not 0 <= value <= 65_535:
                errors[field] = ["Enter a value between 0 and 65535."]
        if obj.timer not in ("", "fast", "slow"):
            errors["timer"] = ["Timer must be fast, slow, or default."]
        return errors

    from .models import NSOLACPBundleState

    if (
        obj.lag_bundle_id is None
        or not NSOLACPBundleState.objects.filter(management=obj.management, interface=obj.lag_bundle).exists()
    ):
        errors["mode"] = ["This member is not linked to a tracked LACP bundle."]
    if obj.mode not in ("", "active", "passive", "on"):
        errors["mode"] = ["Mode must be active, passive, on, or default."]
    if obj.port_priority is not None and not 0 <= obj.port_priority <= 65_535:
        errors["port_priority"] = ["Enter a value between 0 and 65535."]
    return errors


def _vlan_name_errors(obj):
    """Validate a shared native VLAN rename, including its group/name constraint."""
    from django.core.exceptions import ValidationError

    errors = {}
    try:
        obj.vlan.full_clean()
    except ValidationError as exc:
        for messages in exc.message_dict.values():
            errors.setdefault("name", []).extend(str(message) for message in messages)
    return errors


def _subinterface_errors(obj):
    """Validate inline L3 values before owning a pushable subinterface row."""
    errors = {}
    tag = obj.dot1q_vlan
    if tag is None:
        errors["dot1q_vlan"] = ["A dot1q VLAN tag is required."]
    elif not 1 <= tag <= 4094:
        errors["dot1q_vlan"] = ["Must be between 1 and 4094."]

    parent = obj.parent_interface
    if parent is None or parent.device_id != obj.management.device_id:
        message = "A parent interface on this managed device is required before this row can be owned."
        errors.setdefault("dot1q_vlan", []).append(message)
        errors.setdefault("vrf", []).append(message)
    elif tag is not None and (
        type(obj)
        .objects.filter(
            management=obj.management,
            parent_interface=parent,
            dot1q_vlan=tag,
        )
        .exclude(pk=obj.pk)
        .exists()
    ):
        errors.setdefault("dot1q_vlan", []).append(
            f"dot1q VLAN {tag} is already used by another subinterface on {parent.name}."
        )
    return errors


def _sync_native_bfd(obj):
    """Keep netbox-routing's native BFD row aligned with an edited overlay."""
    try:
        from netbox_routing.models import BFDInterface, BFDProfile
    except ImportError:
        return

    from .bfd_reconciler import _get_or_create_bfd_profile

    profile = _get_or_create_bfd_profile(
        {"min_tx": obj.min_tx, "min_rx": obj.min_rx, "multiplier": obj.multiplier},
        BFDProfile,
        {},
    )
    native, created = BFDInterface.objects.get_or_create(
        interface=obj.interface,
        defaults={"bfd_profile": profile, "micro_bfd": obj.micro_bfd, "enabled": True},
    )
    if not created and (
        native.bfd_profile_id != (profile.pk if profile else None) or native.micro_bfd != obj.micro_bfd
    ):
        native.bfd_profile = profile
        native.micro_bfd = obj.micro_bfd
        native.save(update_fields=["bfd_profile", "micro_bfd"])


def _sync_native_ospf_instance(obj):
    """Keep the native OSPF instance aligned with an edited overlay."""
    from .signals import suppress_intent_push

    native = obj.ospf_instance
    vrf_model = native._meta.get_field("vrf").remote_field.model
    vrf = vrf_model.objects.filter(name=obj.vrf).first() if obj.vrf else None
    native.router_id = obj.router_id
    native.vrf = vrf
    with suppress_intent_push():
        native.save(update_fields=["router_id", "vrf"])


def _sync_native_ospf_interface(obj):
    """Mirror the whole owned overlay row into its native OSPF interface."""
    from netbox_routing.models import OSPFArea, OSPFInstance, OSPFInterface

    from .signals import suppress_intent_push
    from .template_content import _OSPF_AUTH_MAP, _resolve_ospf_area

    native = OSPFInterface.objects.get(interface=obj.interface)
    native.instance = OSPFInstance.objects.get(device=obj.interface.device, process_id=obj.process_id)
    native.area = _resolve_ospf_area(OSPFArea, obj.area_id)
    native.passive = obj.passive
    native.priority = obj.priority
    native.cost = obj.cost
    native.network_type = obj.network_type or None
    native.authentication = _OSPF_AUTH_MAP.get(obj.auth_type or "")
    with suppress_intent_push():
        native.save(
            update_fields=[
                "instance",
                "area",
                "passive",
                "priority",
                "cost",
                "network_type",
                "authentication",
            ]
        )


def _sync_native_isis_instance(obj):
    """Mirror editable process intent into the native IS-IS instance."""
    from .signals import suppress_intent_push

    native = obj.isis_instance
    fields = ("net", "is_type", "metric_style", "overload_bit", "fast_reroute", "microloop_avoidance")
    for name in fields:
        setattr(native, name, getattr(obj, name))
    with suppress_intent_push():
        native.save(update_fields=list(fields))


def _sync_native_isis_interface(obj):
    """Mirror editable interface intent into the native IS-IS interface."""
    from netbox_routing.models import ISISInstance

    from .signals import suppress_intent_push

    native = obj.isis_interface
    native.instance = ISISInstance.objects.get(device=obj.interface.device, process_tag=obj.process_tag)
    fields = (
        "circuit_type",
        "network_type",
        "metric",
        "passive",
        "bfd_enabled",
        "frr_enabled",
        "frr_protection",
    )
    for name in fields:
        setattr(native, name, getattr(obj, name))
    with suppress_intent_push():
        native.save(update_fields=["instance", *fields])


def _sync_native_bgp_peer(obj):
    """Mirror remote-AS and admin state into the linked native BGP peer."""
    from ipam.models import ASN

    from .bgp_reconciler import _get_or_create_asn
    from .signals import suppress_intent_push

    native = obj.bgp_peer
    native.remote_as = _get_or_create_asn(obj.remote_as_str, ASN) if obj.remote_as_str else None
    native.enabled = obj.enabled
    with suppress_intent_push():
        native.save(update_fields=["remote_as", "enabled"])


def _sync_native_redistribution(obj):
    """Mirror route-map and metric policy into the linked native redistribution."""
    from netbox_routing.models import RouteMap

    from .signals import suppress_intent_push

    native = obj.redistribution
    native.route_map = RouteMap.objects.filter(name=obj.route_map).first() if obj.route_map else None
    native.metric = obj.metric
    native.metric_type = obj.metric_type
    with suppress_intent_push():
        native.save(update_fields=["route_map", "metric", "metric_type"])


def _save_owned_overlay_edit(obj, key):
    """Claim an edited overlay and update its matching native NetBox object atomically."""
    from django.db import transaction

    from . import status_machine as sm

    with transaction.atomic():
        if not sm.is_owned(obj.status):
            obj.accepted_at = timezone.now()
        if obj.status != "deploying":  # don't stomp an apply already in flight
            obj.status = "accepted"
        if key == "interface_mtu" and obj.l2_mtu is not None:
            iface = obj.interface
            clamped = min(int(obj.l2_mtu), NSOInterfaceMtuStateAcceptView._NETBOX_MTU_MAX)
            if iface.mtu != clamped:
                iface.mtu = clamped
                iface.save(update_fields=["mtu"])
        if key == "bfd":
            _sync_native_bfd(obj)
        if key == "ospf_instance":
            _sync_native_ospf_instance(obj)
        if key == "ospf_interface":
            _sync_native_ospf_interface(obj)
        if key == "isis_instance":
            _sync_native_isis_instance(obj)
        if key == "isis_interface":
            _sync_native_isis_interface(obj)
        if key == "bgp_peer":
            _sync_native_bgp_peer(obj)
        if key == "redistribution":
            _sync_native_redistribution(obj)
        if key == "static_route":
            from .signals import suppress_intent_push

            with suppress_intent_push():
                obj.static_route.save(update_fields=["metric", "permanent", "tag"])
        obj.save()
        if key == "static_route":
            from .signals import _transition_static_route_content

            # The native save above ran suppressed and only THIS overlay was saved, but the
            # fork object is shared by every device the route is on — editing through one
            # device's row silently changes the others' content. Re-arm them all.
            _transition_static_route_content(obj.static_route)
            obj.refresh_from_db()


def _route_map_name_errors(state, old_name):
    """Validate a route-map rename across its native and shared-overlay identities."""
    from django.core.exceptions import ValidationError

    from .models import NSORoutePolicyObjectClass, NSORoutePolicyState

    route_map = state.assigned_object
    if state.family != "route_map" or route_map is None or route_map._meta.label_lower != "netbox_routing.routemap":
        return {"object_name": ["Only a linked NetBox route map can be renamed inline."]}

    errors = {}
    try:
        route_map._meta.get_field("name").clean(state.object_name, route_map)
    except ValidationError as exc:
        errors["object_name"] = [str(message) for message in exc.messages]
    if type(route_map).objects.filter(name__iexact=state.object_name).exclude(pk=route_map.pk).exists():
        errors.setdefault("object_name", []).append("A route map with this name already exists.")

    attached = NSORoutePolicyState.objects.filter(
        content_type_id=state.content_type_id,
        object_id=state.object_id,
    )
    attached_mgmt_ids = attached.values_list("management_id", flat=True)
    if (
        NSORoutePolicyState.objects.filter(
            management_id__in=attached_mgmt_ids,
            family="route_map",
            object_name__iexact=state.object_name,
        )
        .exclude(content_type_id=state.content_type_id, object_id=state.object_id)
        .exists()
    ):
        errors.setdefault("object_name", []).append("A route-map row with this name already exists on a device.")

    old_class = NSORoutePolicyObjectClass.objects.filter(family="route_map", object_name__iexact=old_name)
    if old_class.exists() and (
        NSORoutePolicyObjectClass.objects.filter(family="route_map", object_name__iexact=state.object_name)
        .exclude(pk__in=old_class.values("pk"))
        .exists()
    ):
        errors.setdefault("object_name", []).append("A route-map classification with this name already exists.")
    return errors


def _route_map_dependent_pushes(route_map, old_name):
    """Return dependent BGP/redistribution intent targets for a route-map rename."""
    from django.db.models import Q

    from . import signals
    from .models import NSOBGPPeerState, NSORedistributionState

    owned = signals._OWNED_PUSH_STATUSES
    bgp_targets = set(
        NSOBGPPeerState.objects.filter(
            Q(bgp_peer__address_families__routemap_in=route_map)
            | Q(bgp_peer__address_families__routemap_out=route_map),
            status__in=owned,
            management__adapter_device_id__isnull=False,
        ).values_list("management__device_id", flat=True)
    )

    redistribution = NSORedistributionState.objects.filter(
        Q(redistribution__route_map=route_map) | Q(redistribution__isnull=True, route_map__iexact=old_name),
        status__in=owned,
    )
    redistribution_targets = set(
        redistribution.filter(management__adapter_device_id__isnull=False).values_list(
            "management__device_id", "dest_protocol"
        )
    )
    redistribution.filter(redistribution__isnull=True, route_map__iexact=old_name).update(route_map=route_map.name)
    return bgp_targets, redistribution_targets


def _save_route_map_name_edit(state, old_name):
    """Atomically rename a shared route map and refresh every dependent intent scope."""
    from django.db import transaction

    from . import signals
    from . import status_machine as sm
    from .models import NSORoutePolicyObjectClass, NSORoutePolicyState

    route_map = state.assigned_object
    new_name = state.object_name
    with transaction.atomic():
        attached = NSORoutePolicyState.objects.select_for_update().filter(
            content_type_id=state.content_type_id,
            object_id=state.object_id,
        )
        attached.exclude(pk=state.pk).update(object_name=new_name)
        NSORoutePolicyObjectClass.objects.select_for_update().filter(
            family="route_map", object_name__iexact=old_name
        ).update(object_name=new_name)

        if not sm.is_owned(state.status):
            state.accepted_at = timezone.now()
        if state.status != "deploying":
            state.status = "accepted"
        state.save()

        route_map.name = new_name
        route_map.save(update_fields=["name"])
        bgp_targets, redistribution_targets = _route_map_dependent_pushes(route_map, old_name)
        # Appended here, not on commit: the entry belongs to the transaction that renamed
        # the map, and the drain it schedules still runs after that transaction commits.
        for device_id in bgp_targets:
            signals._schedule_intent_push((device_id, "bgp"))
        for device_id, dest_protocol in redistribution_targets:
            signals._schedule_redistribution_push(device_id, dest_protocol)


def _save_lacp_edit(obj, key):
    """Own a complete LACP bundle while preserving which member actually changed."""
    from django.db import transaction

    from . import status_machine as sm
    from .models import NSOLACPBundleState, NSOLACPMemberState

    with transaction.atomic():
        if key == "lacp_bundle":
            bundle = obj
        else:
            bundle = NSOLACPBundleState.objects.select_for_update().get(
                management=obj.management, interface=obj.lag_bundle
            )
        members = list(
            NSOLACPMemberState.objects.select_for_update().filter(
                management=bundle.management, lag_bundle=bundle.interface
            )
        )
        now = timezone.now()
        for member in members:
            if member.pk == getattr(obj, "pk", None) and key == "lacp_member":
                member = obj
                target_status = "accepted"
            else:
                target_status = _status_after_accept(member.status)
            if not sm.is_owned(member.status):
                member.accepted_at = now
            if member.status != "deploying":
                member.status = target_status
            member.save()

        if not sm.is_owned(bundle.status):
            bundle.accepted_at = now
        if bundle.status != "deploying":
            bundle.status = "accepted"
        bundle.save()


def _save_vlan_name_edit(obj):
    """Rename one shared VLAN and take ownership on every attached managed device."""
    from django.db import transaction

    from . import status_machine as sm
    from .models import NSOVLANState
    from .signals import suppress_intent_push
    from .vlan_reconciler import is_placeholder_vlan_name

    with transaction.atomic():
        vlan = obj.vlan
        states = list(NSOVLANState.objects.select_for_update(of=("self",)).filter(vlan_id=vlan.pk))
        with suppress_intent_push():
            vlan.save(update_fields=["name"])

        now = timezone.now()
        for state in states:
            state.vlan = vlan
            if not sm.is_owned(state.status):
                state.accepted_at = now
            matches = state.device_name == vlan.name if state.device_name else is_placeholder_vlan_name(state)
            if state.status != "deploying":
                state.status = "in_sync" if matches else "accepted"
            state.save()


def _logging_levels_errors(obj, old_values):
    """Reject an inline edit that would clear the LAST managed severity.

    An all-blank owned row pushes ``local_levels: null`` — the un-manage wire shape
    that retracts every owned leaf (on NX: destination DISABLED). That retract must
    only be reachable through the Un-accept flow, whose confirm dialog states the
    consequence; a casual popover clear must not trigger it silently.
    """
    if obj.set_severities():
        return {}
    cleared = [f for f in obj.SEVERITY_FIELDS if not getattr(obj, f) and old_values.get(f)]
    if not cleared:
        return {}
    msg = (
        "Clearing the last managed severity would un-manage local levels and RETRACT them "
        "from the device (on NX this disables the destination). Use Un-accept instead."
    )
    return {f: [msg] for f in cleared}


def _overlay_family_errors(key, obj, old_values):
    """Run validation that spans fields or the linked native object."""
    simple_validator = {
        "logging_host": _logging_host_errors,
        "snmp_community": _snmp_community_errors,
        "snmp_host": _snmp_host_errors,
    }.get(key)
    if simple_validator is not None:
        return simple_validator(obj)
    if key == "logging_levels":
        return _logging_levels_errors(obj, old_values)
    if key == "bfd":
        return _bfd_field_errors(obj)
    if key == "ospf_instance":
        return _ospf_instance_errors(obj)
    if key == "ospf_interface":
        return _ospf_interface_errors(obj)
    if key == "isis_instance":
        return _isis_instance_errors(obj)
    if key == "isis_interface":
        return _isis_interface_errors(obj)
    if key == "bgp_peer":
        return _bgp_peer_errors(obj)
    if key == "redistribution":
        return _redistribution_errors(obj)
    if key == "static_route":
        return _static_route_errors(obj)
    if key in ("lacp_bundle", "lacp_member"):
        return _lacp_errors(key, obj)
    if key == "vlan_name":
        return _vlan_name_errors(obj)
    if key == "subinterface":
        return _subinterface_errors(obj)
    if key == "route_map_name":
        return _route_map_name_errors(obj, old_values["object_name"])
    return {}


def _save_overlay_edit(obj, key, old_values):
    """Dispatch a validated edit to its family-specific atomic save path."""
    if key == "route_map_name":
        _save_route_map_name_edit(obj, old_values["object_name"])
    elif key in ("lacp_bundle", "lacp_member"):
        _save_lacp_edit(obj, key)
    elif key == "vlan_name":
        _save_vlan_name_edit(obj)
    else:
        _save_owned_overlay_edit(obj, key)


class NSOOverlayFieldEditView(NSOActionPermissionMixin, View):
    """Inline (popover) field edit on an overlay row from the NSO tab.

    One endpoint for every family the tab edits in place; each family names an
    explicit field whitelist (anything else is rejected, not ignored — a silent
    ignore would hide client bugs). Values are validated by the model field
    itself, then saved with a normal ``save()`` so the same post_save push
    signals fire as for the full-page edit forms.

    An inline edit TAKES OWNERSHIP (status → accepted, accepted_at set) — the
    tab's documented inline-edit semantic ("NetBox will own this value — same
    as Accept"). Anything weaker is silently futile: the category reconcile
    refreshes unowned rows from the device mirror, so an unowned edit evaporates
    on the next expand (caught live). Vault-touching fields (community
    secret/ref) stay in the full form — their validation writes to Vault and
    cannot run per-field.
    """

    _FAMILIES = {
        "snmp_system_info": ("NSOSnmpSystemInfoState", ("location", "contact")),
        "snmp_community": ("NSOSnmpCommunityState", ("access", "acl")),
        "snmp_host": ("NSOSnmpHostState", ("version", "notify_type", "port", "username")),
        "logging_host": (
            "NSOLoggingHostState",
            ("address", "port", "severity", "facility", "transport", "vrf", "source"),
        ),
        "logging_levels": (
            "NSOLoggingLevelState",
            ("console_severity", "monitor_severity", "module_severity"),
        ),
        "interface_mtu": ("NSOInterfaceMtuState", ("l2_mtu", "ip_mtu", "mpls_mtu")),
        "bfd": ("NSOBFDInterfaceState", ("min_tx", "min_rx", "multiplier", "micro_bfd")),
        "ospf_instance": ("NSOOSPFInstanceState", ("router_id",)),
        "ospf_interface": ("NSOOSPFInterfaceState", ("area_id", "network_type", "cost", "passive")),
        "isis_instance": (
            "NSOISISInstanceState",
            ("net", "is_type", "metric_style", "overload_bit", "fast_reroute", "microloop_avoidance"),
        ),
        "isis_interface": (
            "NSOISISInterfaceState",
            ("circuit_type", "network_type", "metric", "passive", "bfd_enabled", "frr_enabled", "frr_protection"),
        ),
        "bgp_peer": ("NSOBGPPeerState", ("remote_as_str", "enabled")),
        "redistribution": ("NSORedistributionState", ("route_map", "metric", "metric_type")),
        "static_route": ("NSOStaticRouteState", ("metric", "permanent", "tag")),
        "lacp_bundle": ("NSOLACPBundleState", ("min_links", "system_priority", "timer", "admin_key")),
        "lacp_member": ("NSOLACPMemberState", ("mode", "port_priority")),
        "vlan_name": ("NSOVLANState", ("name",)),
        "svi": ("NSOSVIState", ("vrf",)),
        "subinterface": ("NSOSubinterfaceState", ("dot1q_vlan", "vrf")),
        "route_map_name": ("NSORoutePolicyState", ("object_name",)),
    }

    def post(self, request, key, pk):
        """Validate and apply whitelisted field values; JSON in, JSON out."""
        from django.apps import apps
        from django.core.exceptions import ValidationError

        spec = self._FAMILIES.get(key)
        if spec is None:
            return JsonResponse({"status": "error", "message": f"unknown overlay family: {key}"}, status=400)
        model_name, editable = spec

        rejected = sorted(f for f in request.POST if f not in editable and f != "csrfmiddlewaretoken")
        if rejected:
            return JsonResponse(
                {"status": "error", "message": f"field(s) not editable here: {', '.join(rejected)}"}, status=400
            )
        supplied = [f for f in editable if f in request.POST]
        if not supplied:
            return JsonResponse({"status": "error", "message": "no editable field supplied"}, status=400)

        obj = get_object_or_404(apps.get_model("netbox_nso_plugin", model_name), pk=pk)
        if key == "vlan_name":
            self._require_vlan_name_permissions(request, obj)
        edit_obj = obj.static_route if key == "static_route" else obj.vlan if key == "vlan_name" else obj
        old_values = {field: getattr(edit_obj, field) for field in editable}
        errors: dict[str, list[str]] = {}
        changed = []
        for f in supplied:
            field = edit_obj._meta.get_field(f)
            raw = request.POST.get(f, "")
            if key in (
                "route_map_name",
                "bgp_peer",
                "redistribution",
                "lacp_bundle",
                "lacp_member",
                "vlan_name",
                "svi",
                "subinterface",
                "snmp_host",
            ):
                raw = raw.strip()
            value = raw if raw != "" else (None if field.null else "")
            try:
                value = field.clean(value, edit_obj)
            except ValidationError as exc:
                errors[f] = [str(m) for m in exc.messages]
                continue
            if getattr(edit_obj, f) != value:
                setattr(edit_obj, f, value)
                changed.append(f)
        if errors:
            return JsonResponse({"status": "error", "errors": errors}, status=400)

        errors = _overlay_family_errors(key, obj, old_values)
        if errors:
            return JsonResponse({"status": "error", "errors": errors}, status=400)

        if changed:
            collision = None if key in ("static_route", "vlan_name") else _unique_collision_response(obj, editable)
            if collision is not None:
                return collision

            # Claim ownership (same transition as Accept on a differing value):
            # the edited value is intent the device doesn't have yet.
            _save_overlay_edit(obj, key, old_values)
        return JsonResponse({"status": "ok", "changed": changed})

    @staticmethod
    def _require_vlan_name_permissions(request, state):
        """Require native VLAN access and every device affected by a shared rename."""
        from ipam.models import VLAN

        if not request.user.has_perm("ipam.change_vlan"):
            raise PermissionDenied
        if not VLAN.objects.restrict(request.user, "change").filter(pk=state.vlan_id).exists():
            raise PermissionDenied

        from .models import NSODeviceManagement, NSOVLANState

        management_ids = set(NSOVLANState.objects.filter(vlan_id=state.vlan_id).values_list("management_id", flat=True))
        permitted_ids = set(
            NSODeviceManagement.objects.restrict(request.user, "change")
            .filter(pk__in=management_ids)
            .values_list("pk", flat=True)
        )
        if permitted_ids != management_ids:
            raise PermissionDenied


class NSOBulkAcceptView(NSOActionPermissionMixin, View):
    """Bulk-accept all 'changed' interface states for a device and push a single intent snapshot."""

    def post(self, request, device_pk):
        """Accept all acceptable states for the given device.

        Matching (imported) values become in_sync (nothing to apply); differing
        (changed) values become accepted and trigger a single intent push.
        """
        device = get_object_or_404(Device, pk=device_pk)  # 404 a bad pk BEFORE mutating anything
        now = timezone.now()
        base = NSOInterfaceState.objects.filter(interface__device_id=device_pk)
        # One transaction for the ownership and the entry that records it: appended after
        # the commit, the entry could be lost while the rows read as owned.
        with transaction.atomic():
            settled = base.filter(status="imported").update(status="in_sync", accepted_at=now)
            pending = base.filter(status="changed").update(status="accepted", accepted_at=now)
            updated = settled + pending

            # Push whenever anything became owned — the snapshot is by status (OWNED_STATES),
            # and matching rows settle to in_sync (an owned status), so even owned-but-matching
            # rows are recorded in the adapter to persist ownership.
            if updated:
                _push_intent_for_device(device_pk)
        if updated:
            messages.success(request, f"Accepted {updated} interface attribute(s).")
        else:
            messages.info(request, "No attributes to accept.")

        return redirect(_device_nso_tab_url(device.pk))


def _join_props(parts):
    """Render the non-empty property fragments as a single ' · '-joined string."""
    return " · ".join(p for p in parts if p)


def _ospf_iface_detail(r):
    """Pushed OSPF-interface properties for the Apply preview.

    Prefer the live netbox-routing OSPFInterface (the actual values we push) — the
    overlay's value columns are refreshed from the device on reconcile and can read
    stale (None) before an owned change has been applied. Fall back to the overlay.
    """
    src = None
    try:
        from netbox_routing.models import OSPFInterface

        src = OSPFInterface.objects.filter(interface=r.interface).first()
    except Exception:
        src = None
    area = (getattr(getattr(src, "area", None), "area_id", "") or r.area_id) if src else r.area_id
    cost = src.cost if (src and src.cost is not None) else r.cost
    net = (src.network_type if src else "") or r.network_type
    prio = src.priority if (src and src.priority is not None) else r.priority
    passive = src.passive if src else r.passive
    return _join_props(
        [
            f"area {area}" if area else "",
            f"cost {cost}" if cost is not None else "",
            net,
            f"prio {prio}" if prio is not None else "",
            "passive" if passive else "",
            f"auth {r.auth_type}".strip() if r.auth_present else "",
        ]
    )


def _isis_iface_detail(r):
    """Pushed IS-IS-interface properties for the Apply preview.

    Prefer the linked netbox-routing ISISInterface (the values we push); fall back to
    the overlay's cached fields when it is not linked yet.
    """
    src = getattr(r, "isis_interface", None)
    metric = getattr(src, "metric", None) if src else None
    metric = metric if metric is not None else r.metric
    net = (getattr(src, "network_type", "") if src else "") or r.network_type
    ctype = (getattr(src, "circuit_type", "") if src else "") or r.circuit_type
    # tri-state BFD (#77): None = no opinion → silent; True/False IS pushed intent and
    # must be listed (the dry-run diff showed bfd-enabled while this stayed silent —
    # operator caught the mismatch on the first live preview).
    bfd = getattr(src, "bfd_enabled", None) if src else None
    bfd = bfd if bfd is not None else r.bfd_enabled
    return _join_props(
        [
            r.process_tag,
            r.af,
            ctype,
            net,
            f"metric {metric}" if metric is not None else "",
            "passive" if r.passive else "",
            "bfd on" if bfd is True else ("bfd off" if bfd is False else ""),
            "hello-auth" if r.hello_auth_present else "",
        ]
    )


def _apply_preview_interface_changes(device_pk):
    """Interface attributes Apply would actually push: OWNED status + value differs from device.

    Apply pushes only intent in an OWNED status (accepted / deploying / in_sync / apply_failed) —
    that's what the intent-push mirrors to the adapter and what the NSO dry-run reflects. Keying
    the preview off ``accepted_at`` instead over-reported: an attribute that drifted back to
    ``imported`` (un-owned) keeps a stale ``accepted_at`` from a past acceptance, so it was listed
    as "what we push" even though Apply never pushes it and the dry-run shows no change. Filter by
    owned status so the left panel agrees with the dry-run and the real Apply.
    """
    from .status_machine import OWNED_STATES
    from .summary import interface_row_state

    changes = []
    owned = (
        NSOInterfaceState.objects.filter(interface__device_id=device_pk, status__in=OWNED_STATES)
        .select_related("interface")
        .order_by("interface__name", "attribute")
    )
    for st in owned:
        iface = st.interface
        kind, _label, _owned = interface_row_state(st, iface)
        # 'deploying' (apply pushed, awaiting device confirmation) is counted as pending-apply
        # by the tab badges, so it must appear here too — else the preview total drops to 0 and
        # the confirm modal is silently skipped (openApply's `!d.total` short-circuit).
        if kind not in ("pending", "apply_failed", "deploying"):
            continue
        if st.attribute == "description":
            netbox_val = iface.description or "—"
        elif st.attribute == "enabled":
            netbox_val = "Yes" if iface.enabled else "No"
        else:
            netbox_val = "—"
        changes.append(
            {"interface": iface.name, "attribute": st.attribute, "device": st.nso_value or "—", "netbox": netbox_val}
        )
    return changes


class NSOApplyPreviewView(LoginRequiredMixin, View):
    """JSON preview of what 'Apply Intent' would push to the device.

    Lists the pending-apply changes (NetBox intent that differs from the device) so the
    operator can confirm before pushing. Drives the apply-confirmation modal.
    """

    def get(self, request, device_pk):
        """Return {auto_apply, changes:[{interface, attribute, device, netbox}], routing}."""
        from django.http import JsonResponse

        device = get_object_or_404(Device, pk=device_pk)
        mgmt = getattr(device, "nso_management", None)
        auto_apply = bool(mgmt and mgmt.auto_apply)

        changes = _apply_preview_interface_changes(device_pk)

        # Every other category is an NSO*State overlay committed by this same single
        # Apply. Itemise each pending row (category + item + detail) so the operator
        # sees exactly what will be pushed — not just a count.
        from .models import NSOLACPBundleState, NSOSwitchportState

        def _iface(r):
            try:
                return r.interface.name
            except Exception:
                return "—"

        def _vlan_item(r):
            return f"VLAN {r.vlan.vid}" if getattr(r, "vlan", None) else "VLAN"

        # (Model, category label, item fn, detail fn) — all read defensively.
        # 5th element = the adapter apply-diff SCOPE the row's push rides (None =
        # pushed out-of-band, no dry-run scope). The modal badges rows whose scope
        # produced no delta as "no device change" — a row staged long ago can be
        # already-satisfied on the device (the example-comm case). Redistribution rides
        # its destination protocol's scope.
        preview_specs = [
            (NSOVLANState, "VLAN", _vlan_item, lambda r: f"name {r.vlan.name}" if r.vlan else "", "vlan"),
            (NSOSwitchportState, "Switchport", _iface, lambda r: r.mode or "", None),
            (
                NSOSVIState,
                "SVI / IRB",
                _iface,
                lambda r: f"VLAN {r.vlan.vid}" if getattr(r, "vlan", None) else (r.vrf or ""),
                "svi",
            ),
            (
                NSOSubinterfaceState,
                "Subinterface",
                _iface,
                lambda r: f"dot1q {r.dot1q_vlan}" if r.dot1q_vlan else "",
                "subinterface",
            ),
            (
                NSOBFDInterfaceState,
                "BFD",
                _iface,
                lambda r: f"tx {r.min_tx or '?'} / rx {r.min_rx or '?'} x{r.multiplier or '?'}",
                "bfd",
            ),
            (NSOLACPBundleState, "LACP", _iface, lambda r: f"lag {r.lag_id}" if r.lag_id else "", None),
            (
                NSOStaticRouteState,
                "Static route",
                lambda r: r.nso_prefix or "",
                lambda r: f"→ {r.nso_next_hop}" if r.nso_next_hop else "",
                "static_route",
            ),
            (NSOISISInterfaceState, "IS-IS interface", _iface, _isis_iface_detail, "isis"),
            (NSOISISInstanceState, "IS-IS", lambda r: r.process_tag or "instance", lambda r: r.net or "", "isis"),
            (NSOOSPFInterfaceState, "OSPF interface", _iface, _ospf_iface_detail, "ospf"),
            (NSOOSPFInstanceState, "OSPF", lambda r: f"process {r.process_id}", lambda r: r.router_id or "", "ospf"),
            (
                NSOBGPPeerState,
                "BGP peer",
                lambda r: r.peer_address_str or "",
                lambda r: f"AS {r.remote_as_str}" if r.remote_as_str else "",
                "bgp",
            ),
            (
                NSORoutePolicyState,
                "Route policy",
                lambda r: r.object_name or "",
                lambda r: r.family or "",
                "route_policy",
            ),
            (
                NSORedistributionState,
                "Redistribution",
                lambda r: f"{r.source_protocol} → {r.dest_protocol}",
                lambda r: r.route_map or "",
                lambda r: r.dest_protocol or None,
            ),
            (NSOSnmpCommunityState, "SNMP community", lambda r: "community", lambda r: r.access or "", "snmp"),
            (NSOSnmpV3UserState, "SNMP v3 user", lambda r: r.username or "user", lambda r: "", "snmp"),
            (
                NSOSnmpHostState,
                "SNMP host",
                lambda r: r.address or "",
                lambda r: f"v{r.version}" if r.version else "",
                "snmp",
            ),
            (NSOSnmpSystemInfoState, "SNMP system", lambda r: "system-info", lambda r: "", "snmp"),
            (NSOLoggingHostState, "Logging host", lambda r: r.address or "", lambda r: r.severity or "", "logging"),
            (
                NSOLoggingLevelState,
                "Logging levels",
                lambda r: "local-levels",
                lambda r: ", ".join(f"{f.split('_')[0]}={v}" for f, v in r.set_severities().items()),
                "logging",
            ),
            (NSOL2SapState, "L2 SAP", lambda r: r.sap_id or "", lambda r: r.service_name or "", "l2_sap"),
        ]

        routing_changes = []
        if mgmt is not None:
            for model, label, item_fn, detail_fn, scope in preview_specs:
                # accepted/apply_failed (owned, differs) AND deploying (apply pushed, not yet
                # confirmed) are all "pending apply" on the tab — the preview must count the same
                # set or a device with only deploying rows previews total=0 and skips the modal.
                rows = model.objects.filter(management=mgmt, status__in=("accepted", "apply_failed", "deploying"))
                for r in rows:
                    try:
                        item = item_fn(r)
                    except Exception:
                        item = "—"
                    try:
                        detail = detail_fn(r)
                    except Exception:
                        detail = ""
                    scope_val = scope(r) if callable(scope) else scope
                    # Staleness: a row accepted long ago and never applied is staged-and-
                    # forgotten (example-comm sat silently for a month) — surface it.
                    accepted_at = getattr(r, "accepted_at", None)
                    staged_days = int((timezone.now() - accepted_at).days) if accepted_at else None
                    routing_changes.append(
                        {
                            "category": label,
                            "item": item,
                            "detail": detail,
                            "status": r.status,
                            "scope": scope_val,
                            "staged_days": staged_days,
                            "never_applied": getattr(r, "last_apply_at", None) is None,
                        }
                    )

        # Right panel: the diff the Apply would push (NSO dry-run, no commit).
        # outformat=cli → NSO's NED-uniform +/- tree diff (rendered via diff2html);
        # outformat=native → device syntax (CLI lines / edit-config XML). Best-effort —
        # a slow/unavailable adapter must not block the preview.
        outformat = request.GET.get("outformat", "native")
        if outformat not in ("native", "cli"):
            outformat = "native"
        device_diff = {}
        # An EMPTY diff and an UNAVAILABLE diff are both {} on the wire, but they mean
        # opposite things: "the device already matches this intent" vs "we have no idea".
        # Every consumer that reads meaning INTO emptiness (the per-row "no device change"
        # badge, the skip-the-confirm-modal gate) must therefore know which one it got.
        diff_available = False
        if mgmt is not None and mgmt.adapter_device_id is not None:
            from . import adapter_client as client

            try:
                device_diff = (client.get_apply_diff(mgmt.adapter_device_id, outformat=outformat) or {}).get(
                    "diffs", {}
                )
                diff_available = True
            except Exception as exc:  # noqa: BLE001
                logger.debug("apply-diff unavailable for device %s: %s", device_pk, exc)

        # #107: the Apply button auto-proceeds (no confirm modal) only when NOTHING would
        # be committed. The itemised total alone cannot prove that: accepting an imported
        # (matching) row lands it in_sync — invisible above — yet the first Apply still
        # commits the FASTMAP service adoption. The dry-run diff is the ground truth for
        # that whole class (any registry omission included), so total==0 must be backed
        # by an EMPTY diff before the modal may be skipped. Pending rows always require
        # confirmation — an unavailable adapter (device_diff={}) proves nothing, which is
        # why the gate demands diff_available and not merely an empty dict.
        total = len(changes) + len(routing_changes)
        return JsonResponse(
            {
                "auto_apply": auto_apply,
                "changes": changes,
                "routing_changes": routing_changes,
                "routing": len(routing_changes),
                "total": total,
                "nothing_pending": total == 0 and diff_available and not device_diff,
                "outformat": outformat,
                "device_diff": device_diff,
                "diff_available": diff_available,
            }
        )


# ── IP auto-assignment operator actions ──────────────────────────────────


class NSOAutoAssignIPView(NSOActionPermissionMixin, View):
    """Operator action: auto-assign IPs to one or more interfaces from purpose pools.

    POST body: ``interface_pks`` (comma-separated or repeated query param).
    Redirects back to the device NSO tab with a success/error flash.
    """

    def post(self, request, device_pk):
        """Auto-assign IPs from purpose pools to the requested interfaces."""
        from dcim.models import Interface

        from .ip_autoassign import auto_assign_ip

        device = get_object_or_404(Device, pk=device_pk)
        pks_raw = request.POST.getlist("interface_pks") or request.POST.get("interface_pks", "").split(",")
        pks = [int(p.strip()) for p in pks_raw if p.strip().isdigit()]

        if not pks:
            messages.warning(request, "No interfaces selected for IP auto-assignment.")
            return redirect(_device_nso_tab_url(device.pk))

        interfaces = Interface.objects.filter(pk__in=pks, device=device)
        allocated_total, skipped_total, error_total = 0, 0, 0

        for iface in interfaces:
            result = auto_assign_ip(iface)
            allocated_total += len(result["allocated"])
            skipped_total += len(result["skipped"])
            error_total += len(result["errors"])
            for entry in result["errors"]:
                messages.warning(
                    request,
                    f"{iface.name} ({entry.get('family', '')}): {entry['reason']}",
                )

        if allocated_total:
            messages.success(
                request,
                f"Auto-assigned {allocated_total} IP address(es) ({skipped_total} skipped, {error_total} error(s)).",
            )
        elif not error_total:
            messages.info(request, f"No IPs allocated ({skipped_total} interface(s) already have managed IPs).")

        return redirect(_device_nso_tab_url(device.pk))


class NSOProvisionLinkRoleView(NSOActionPermissionMixin, View):
    """Operator action: provision one or more interfaces from their link roles.

    POST body: ``interface_pks`` (comma-separated or repeated). For each interface
    the resolved ``NSOLinkRole`` drives IP + description + IGP on both ends (p2p) or
    the interface (single-ended), atomically. Redirects to the device NSO tab with a
    per-interface summary flash.
    """

    def post(self, request, device_pk):
        """Provision the requested interfaces from their assigned link roles."""
        from dcim.models import Interface

        from .link_role import provision_link_role

        device = get_object_or_404(Device, pk=device_pk)
        pks_raw = request.POST.getlist("interface_pks") or request.POST.get("interface_pks", "").split(",")
        pks = [int(p.strip()) for p in pks_raw if p.strip().isdigit()]

        if not pks:
            messages.warning(request, "No interfaces selected for link-role provisioning.")
            return redirect(_device_nso_tab_url(device.pk))

        provisioned, skipped, rolled_back = 0, 0, 0
        covered: set[int] = set()  # ends already provisioned this batch (p2p link dedup)
        for iface in Interface.objects.filter(pk__in=pks, device=device):
            if iface.pk in covered:
                continue  # far end of a link already provisioned from the other side
            summary = provision_link_role(iface)
            covered.update(summary.get("ends", []))
            if summary["provisioned"]:
                provisioned += 1
            elif summary["rolled_back"]:
                rolled_back += 1
                for err in summary["errors"]:
                    messages.warning(request, f"{iface.name}: {err.get('reason', 'provisioning error')}")
            elif summary["skipped"]:
                skipped += 1
                messages.info(request, f"{iface.name}: {summary['skipped']}")

        if provisioned:
            messages.success(
                request,
                f"Provisioned {provisioned} interface(s) from link roles "
                f"({skipped} skipped, {rolled_back} rolled back).",
            )
        elif rolled_back:
            messages.error(request, f"Link-role provisioning rolled back for {rolled_back} interface(s).")

        return redirect(_device_nso_tab_url(device.pk))


# ── Routing state accept views (Track A) ──────────────────────────────────────


class RoutingStateAcceptMixin(NSOActionPermissionMixin, View):
    """Per-row accept for a routing state model — sets status to 'accepted' and fires push signal."""

    model_class = None
    # Extra columns _arm_accept() writes, saved in the same UPDATE as the status.
    accept_extra_fields: tuple[str, ...] = ()

    def _arm_accept(self, state) -> None:
        """Set family-specific state on *state* before the accepted row is saved."""

    def post(self, request, pk):  # noqa: D102
        state = get_object_or_404(self.model_class, pk=pk)
        # Matching (imported/in_sync) → nothing to apply → in_sync; differing → accepted.
        state.status = _status_after_accept(state.status)
        # First acceptance only — staged_days measures waiting time since the operator
        # FIRST took ownership, so a re-accept must not reset it (#107 staleness badge).
        if state.accepted_at is None:
            state.accepted_at = timezone.now()
        self._arm_accept(state)
        # One transaction, so the row and the outbox entry it schedules commit together.
        with transaction.atomic():
            state.save(update_fields=["status", "accepted_at", *self.accept_extra_fields])
        messages.success(request, f"Accepted routing state {state.pk}.")
        return redirect(_device_nso_tab_url(state.management.device_id))


class NSOL2SapStateAcceptView(NSOActionPermissionMixin, View):
    """Accept one Nokia L2 SAP — mark owned (accepted_at) so NetBox is the source of truth.

    Saving the accepted row fires the post_save signal which pushes the device's full
    L2 SAP intent snapshot to the adapter (write path), mirroring static routes.
    """

    def post(self, request, pk):  # noqa: D102
        state = get_object_or_404(NSOL2SapState, pk=pk)
        if state.service_type not in ("epipe", "vpls"):
            messages.error(
                request,
                f"Cannot accept {state.service_type or 'unknown'} L2 service {state.service_name}: "
                "the current writer supports only epipe and vpls SAPs.",
            )
            return redirect(_device_nso_tab_url(state.management.device_id))
        state.status = _status_after_accept(state.status)
        if state.accepted_at is None:
            state.accepted_at = timezone.now()
        with transaction.atomic():
            state.save(update_fields=["status", "accepted_at"])
        messages.success(request, f"Accepted L2 SAP {state.service_name}:{state.sap_id}.")
        return redirect(_device_nso_tab_url(state.management.device_id))


class NSOLACPBundleStateAcceptView(NSOActionPermissionMixin, View):
    """Accept one LACP bundle (and its member rows) — mark owned so NetBox is the source of truth.

    Accept only marks the rows owned; the device commit is deferred to the single
    device Apply (one flow, like every other scope). In auto-apply mode the
    post_save signal still commits immediately (lag-reconciler write path).
    """

    def post(self, request, pk):  # noqa: D102
        from django.db import transaction

        from .models import NSOLACPBundleState, NSOLACPMemberState

        state = get_object_or_404(NSOLACPBundleState, pk=pk)
        # NX-P2 vPC preserve/REFUSE: a vPC-protected bundle cannot be onboarded — the
        # lag-reconciler refuses it zero-write (a retract of an adopted vPC peer-link would
        # delete it → dual-active split-brain). Refuse Accept so it never becomes owned/writable.
        if state.vpc_sensitive:
            messages.error(
                request,
                f"LACP bundle {state.interface.name} is vPC-protected (a vPC member/peer-link/"
                f"orphan port) — NSO refuses to write it, so it cannot be onboarded. Left unmanaged.",
            )
            return redirect(_device_nso_tab_url(state.management.device_id))
        now = timezone.now()
        # Accept the bundle + all its members in ONE transaction so the per-save intent
        # pushes coalesce (via _schedule_intent_push) into a single snapshot push at commit —
        # otherwise each non-atomic save fires its own push and the member-before-bundle order
        # emits a spurious bundle_count=0 push (FASTMAP briefly clears the bundle) before the
        # real bundle_count=1 one.
        with transaction.atomic():
            for m in NSOLACPMemberState.objects.filter(management=state.management, lag_bundle=state.interface):
                m.status = _status_after_accept(m.status)
                m.accepted_at = now
                m.save(update_fields=["status", "accepted_at"])
            state.status = _status_after_accept(state.status)
            state.accepted_at = now
            state.save(update_fields=["status", "accepted_at"])
        messages.success(request, f"Accepted LACP bundle {state.interface.name}.")
        return redirect(_device_nso_tab_url(state.management.device_id))


class NSOSwitchportStateAcceptView(NSOActionPermissionMixin, View):
    """Accept one L2 switchport: native-write the observed mode/VLANs + mark owned.

    Writes the NSO-observed mode/VLANs onto the native NetBox interface (NetBox
    becomes the source of truth). The device commit is deferred to the single
    device Apply (one flow); in auto-apply mode the post_save signal commits
    immediately (switchport-reconciler).
    """

    def post(self, request, pk):  # noqa: D102
        from django.db import transaction

        from .models import NSOSwitchportState

        state = get_object_or_404(NSOSwitchportState, pk=pk)
        with transaction.atomic():
            # native-write-on-accept: make the NetBox interface match what NSO observed.
            iface = state.interface
            iface.mode = state.mode or ""
            iface.untagged_vlan = state.untagged_vlan
            iface.save()
            iface.tagged_vlans.set(state.tagged_vlans.all())
            state.status = _status_after_accept(state.status)
            state.accepted_at = timezone.now()
            state.save(update_fields=["status", "accepted_at"])
        messages.success(request, f"Accepted switchport {state.interface.name}.")
        return redirect(_device_nso_tab_url(state.management.device_id))


def _editable_native_ip(state, vrf_obj):
    """Return the native IP backing *state*, or one safe unassigned candidate.

    A matching object assigned elsewhere is intentionally not editable through this
    state: it remains a conflict link for inspection, and the operator may instead
    enter a free address for this interface.
    """
    from dcim.models import Interface
    from django.contrib.contenttypes.models import ContentType
    from ipam.models import IPAddress

    interface_type_id = ContentType.objects.get_for_model(Interface).pk
    matches = list(IPAddress.objects.filter(address=state.address, vrf=vrf_obj))
    own = next(
        (
            ip_obj
            for ip_obj in matches
            if ip_obj.assigned_object_type_id == interface_type_id and ip_obj.assigned_object_id == state.interface_id
        ),
        None,
    )
    if own is not None:
        return own
    unassigned = [ip_obj for ip_obj in matches if ip_obj.assigned_object_id is None]
    return unassigned[0] if len(unassigned) == 1 else None


def _peer_ip_state_for_edit(state):
    """Resolve the unambiguous IP state on *state*'s cable peer, if any."""
    from .derived_intent import find_peer
    from .models import NSOInterfaceIPState

    if state.interface.cable_id is None:
        return None
    peer = find_peer(state.interface)
    if peer is None:
        return None
    candidates = list(
        NSOInterfaceIPState.objects.filter(interface=peer)
        .select_related("interface", "interface__device")
        .order_by("address")
    )
    return _matching_peer_ip_state(state, candidates)


def _prepare_ip_update(state, field, raw):
    """Normalize one submitted address and resolve its VRF/native object."""
    from ipaddress import ip_interface

    from ipam.models import VRF

    value = (raw or "").strip()
    if not value:
        return None, "Enter an IP address with a prefix length."
    try:
        parsed = ip_interface(value)
    except ValueError:
        return None, "Enter a valid IPv4 or IPv6 address with a prefix length."
    if "/" not in value:
        return None, "Include the prefix length (for example, /31 or /128)."
    vrf_obj = VRF.objects.filter(name=state.vrf).first() if state.vrf else None
    if state.vrf and vrf_obj is None:
        return None, f"VRF '{state.vrf}' does not exist in NetBox."
    return {
        "field": field,
        "state": state,
        "address": str(parsed),
        "host": str(parsed.ip),
        "family": "ipv6" if parsed.version == 6 else "ipv4",
        "vrf": vrf_obj,
        "native": _editable_native_ip(state, vrf_obj),
    }, None


def _ip_update_collision_errors(updates):
    """Return field errors for an IP/state host collision in the same VRF."""
    from ipaddress import ip_interface

    from ipam.models import IPAddress

    errors = {}
    for update in updates:
        native = update["native"]
        clash = IPAddress.objects.filter(address__net_host=update["host"], vrf=update["vrf"])
        if native is not None:
            clash = clash.exclude(pk=native.pk)
        if clash.exists():
            errors[update["field"]] = [
                f"IP address {update['host']} already exists in this VRF; choose an unused address."
            ]
            continue
        state = update["state"]
        siblings = state.__class__.objects.filter(interface=state.interface, vrf=state.vrf).exclude(pk=state.pk)
        for sibling in siblings:
            try:
                same_host = str(ip_interface(sibling.address).ip) == update["host"]
            except ValueError:
                same_host = False
            if same_host:
                errors[update["field"]] = [
                    f"IP address {update['host']} is already tracked on this interface; choose an unused address."
                ]
                break

    if len(updates) == 2 and updates[0]["vrf"] == updates[1]["vrf"] and updates[0]["host"] == updates[1]["host"]:
        errors[updates[0]["field"]] = ["The local and peer addresses must be different."]
        errors[updates[1]["field"]] = ["The local and peer addresses must be different."]
    return errors


def _apply_ip_update(update, now):
    """Re-key one overlay and create/update its assigned native IPAddress."""
    from ipam.models import IPAddress

    state = update["state"]
    changed = update["address"] != state.address
    state.address = update["address"]
    state.family = update["family"]
    state.status = "accepted" if changed else _status_after_accept(state.status)
    state.accepted_at = now
    update_fields = ["address", "family", "status", "accepted_at"]
    if changed and state.auto_assigned:
        if state.peer_state_id:
            state.__class__.objects.filter(pk=state.peer_state_id, peer_state_id=state.pk).update(peer_state=None)
        state.auto_assigned = False
        state.source_pool = None
        state.peer_state = None
        update_fields.extend(["auto_assigned", "source_pool", "peer_state"])
    state.full_clean()
    state.save(update_fields=update_fields)

    ip_obj = update["native"] or IPAddress(vrf=update["vrf"], status="active")
    ip_obj.address = update["address"]
    ip_obj.vrf = update["vrf"]
    ip_obj.assigned_object = state.interface
    ip_obj.full_clean()
    ip_obj.save()


class NSOInterfaceIPStateEditView(NSOActionPermissionMixin, View):
    """Edit/materialize an interface IP from the merged grid, optionally with its peer.

    The local address is always updated. ``peer_address`` is opt-in: the popover
    prefills it for context, but an unchanged/blank value leaves the far end alone.
    Both requested updates validate before writing and commit atomically.
    """

    def post(self, request, pk):
        """Validate, atomically write native IPAM + overlays, then push owned intent."""
        from django.core.exceptions import ValidationError
        from django.db import IntegrityError, transaction

        from .models import NSODeviceManagement, NSOInterfaceIPState
        from .signals import _schedule_intent_push, suppress_intent_push

        state = get_object_or_404(
            NSOInterfaceIPState.objects.select_related("interface", "interface__device"),
            pk=pk,
        )
        local, error = _prepare_ip_update(state, "address", request.POST.get("address"))
        if error:
            return JsonResponse({"status": "error", "errors": {"address": [error]}}, status=400)

        updates = [local]
        peer_raw = (request.POST.get("peer_address") or "").strip()
        if peer_raw:
            peer_state = _peer_ip_state_for_edit(state)
            if peer_state is None:
                return JsonResponse(
                    {
                        "status": "error",
                        "errors": {"peer_address": ["No unambiguous IP address exists on the cable peer."]},
                    },
                    status=400,
                )
            peer, error = _prepare_ip_update(peer_state, "peer_address", peer_raw)
            if error:
                return JsonResponse({"status": "error", "errors": {"peer_address": [error]}}, status=400)
            if peer["address"] != peer_state.address:
                updates.append(peer)

        errors = _ip_update_collision_errors(updates)
        if errors:
            return JsonResponse({"status": "error", "errors": errors}, status=400)

        for update in updates:
            permission = "ipam.change_ipaddress" if update["native"] is not None else "ipam.add_ipaddress"
            if not request.user.has_perm(permission):
                raise PermissionDenied

        try:
            with transaction.atomic():
                now = timezone.now()
                with suppress_intent_push():
                    for update in updates:
                        _apply_ip_update(update, now)

                device_ids = {update["state"].interface.device_id for update in updates}
                for mgmt in NSODeviceManagement.objects.filter(
                    device_id__in=device_ids,
                    adapter_device_id__isnull=False,
                ):
                    device_id = mgmt.device_id
                    _schedule_intent_push((device_id, "ip"))
        except (ValidationError, IntegrityError) as exc:
            messages_list = getattr(exc, "messages", None) or ["The address conflicts with an existing object."]
            return JsonResponse(
                {"status": "error", "errors": {updates[-1]["field"]: [str(message) for message in messages_list]}},
                status=400,
            )

        return JsonResponse(
            {
                "status": "ok",
                "message": f"Updated {len(updates)} interface address{'es' if len(updates) != 1 else ''}.",
            }
        )


class NSOInterfaceIPStateAcceptView(NSOActionPermissionMixin, View):
    """Resolve an interface-IP *conflict* by adopting the device's reality into NetBox.

    The IP reconciler flags an address that NSO reports on one interface but which
    NetBox has assigned to a *different* interface as ``conflict`` (it refuses to
    silently move an IP). Accepting is the operator override: reassign the existing
    IPAddress to the NED-reported interface — e.g. move the device's OOB mgmt IP off
    the onboarding ``me0`` stand-in onto the real ``vme.0``. The device already
    carries the address (NSO read it there), so NetBox now *matches* the device →
    status ``in_sync``, and NO device push happens: the reassignment fires the
    IPAddress signal while the row is still ``conflict``, which skips the push.
    """

    def post(self, request, pk):  # noqa: D102
        from django.db import transaction
        from ipam.models import IPAddress

        try:
            from ipam.models import VRF
        except ImportError:
            VRF = None

        from .models import NSOInterfaceIPState

        state = get_object_or_404(NSOInterfaceIPState, pk=pk)
        iface = state.interface
        vrf_obj = VRF.objects.filter(name=state.vrf).first() if state.vrf and VRF is not None else None

        with transaction.atomic():
            existing = IPAddress.objects.filter(address=state.address, vrf=vrf_obj).first()
            if existing is None:
                existing = IPAddress(address=state.address, vrf=vrf_obj, status="active")
            existing.assigned_object = iface
            existing.save()  # signal skips the push while this row is still `conflict`
            # Device already has the address on `iface`; NetBox now matches → in_sync, owned.
            state.status = "in_sync"
            state.accepted_at = timezone.now()
            state.save(update_fields=["status", "accepted_at"])

        messages.success(request, f"Adopted {state.address} onto {iface.name}.")
        return redirect(_device_nso_tab_url(iface.device_id))


class NSOStaticRouteStateAcceptView(RoutingStateAcceptMixin):
    """Accept one static route — and re-arm its generation even when nothing changed.

    A re-accept saves no native object, so no content transition can see it, yet it is a
    fresh statement of intent: without a new generation the result of the apply the
    operator is re-accepting away would still name this row and could settle it.
    """

    model_class = NSOStaticRouteState
    accept_extra_fields = _STATIC_ROUTE_ARMED_FIELDS

    def _arm_accept(self, state):  # noqa: D102
        from .signals import _arm_static_route_generation

        _arm_static_route_generation(state)


class NSOISISInterfaceStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSOISISInterfaceState


class NSOISISInstanceStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSOISISInstanceState


class NSOBGPPeerStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSOBGPPeerState


class NSOBGPPeerTemplateStateAcceptView(NSOActionPermissionMixin, View):
    """Accept one BGP peer-group template — take ownership (no device apply path).

    Peer-group templates have no apply path, so accepting just marks the row owned
    (status + accepted_at). The 3-way reconcile already preserves the operator edit;
    accepting acknowledges the drift so it stops showing as un-owned.
    """

    def post(self, request, pk):  # noqa: D102
        from .models import NSOBGPPeerTemplateState

        state = get_object_or_404(NSOBGPPeerTemplateState, pk=pk)
        state.status = _status_after_accept(state.status)
        state.accepted_at = timezone.now()
        state.save(update_fields=["status", "accepted_at"])
        messages.success(request, f"Accepted BGP peer-group template {state.template_name}.")
        return redirect(_device_nso_tab_url(state.management.device_id))


class NSORoutePolicyStateAcceptView(RoutingStateAcceptMixin):
    """Per-row accept for a route-policy object.

    Owns it, and cascades ownership to a route-map's referenced prefix-lists /
    community-lists / as-paths so they're pushed too.
    """

    model_class = NSORoutePolicyState

    def post(self, request, pk):  # noqa: D102
        from django.db import transaction

        from .signals import _own_route_map_contributors

        state = get_object_or_404(NSORoutePolicyState, pk=pk)
        with transaction.atomic():
            state.status = _status_after_accept(state.status)
            state.save(update_fields=["status"])
            if state.family == "route_map" and state.assigned_object is not None:
                _own_route_map_contributors(state.management, state.assigned_object)
        messages.success(request, f"Accepted routing state {state.pk}.")
        return redirect(_device_nso_tab_url(state.management.device_id))


# ── Shared-object versions + re-point (universal: route-policy now, ACL later) ──


class SharedObjectVersionsMixin(LoginRequiredMixin, View):
    """Show every device's captured version of one globally-deduped named object.

    Globally-deduped objects (a route-map / community-list / … shared by name across
    devices) hold ONE device's content in NetBox while every device keeps its own
    captured version.  This lists all of them — which device is the materialized owner,
    which match it, which diverge — so an operator can pick which version NetBox should
    mirror.  Family-agnostic: an ACL overlay subclasses this with its own ``model_class``.
    """

    model_class = None
    materialize_url_name = ""
    template_name = "netbox_nso_plugin/shared_object_versions.html"

    def get(self, request, pk):  # noqa: D102
        from . import shared_object_ownership as ownership

        state = get_object_or_404(self.model_class, pk=pk)
        items = ownership.version_items(self.model_class, state.family, state.object_name)
        self.decorate_items(items, state)
        return render(
            request,
            self.template_name,
            {
                "state": state,
                "object_name": state.object_name,
                "family": state.family.replace("_", "-"),
                "items": items,
                "device": getattr(state.management, "device", None),
                "materialize_url_name": self.materialize_url_name,
                **self.extra_context(state, items),
            },
        )

    def decorate_items(self, items, state):
        """Attach per-family display detail to each version item (hook; default no-op).

        The surface is family-agnostic; route-policy overrides this to attach a structured
        route-map summary so operators compare versions without reading raw JSON.
        """

    def extra_context(self, state, items) -> dict:
        """Per-family extra template context (hook; default none).

        Route-policy adds the MASTER/LOCAL classification + the diverging-group suggestion.
        """
        return {}


class SharedObjectMaterializeMixin(NSOActionPermissionMixin, View):
    """Re-point a shared object's content to a chosen device's captured version.

    Refills the NetBox object from this device's capture and flips ownership (the former
    owner becomes the conflict).  Writes only into NetBox — pushing the new content to
    other devices is a separate, explicit Accept.  Family-agnostic via ``model_class``.
    """

    model_class = None

    def post(self, request, pk):  # noqa: D102
        from . import shared_object_ownership as ownership

        state = get_object_or_404(self.model_class, pk=pk)
        try:
            ownership.rematerialize(state)
        except ValueError as exc:
            messages.error(request, f"Could not use this version: {exc}")
            return redirect(_device_nso_tab_url(state.management.device_id))
        dev = getattr(state.management, "device", None)
        messages.success(
            request,
            f"NetBox now mirrors {dev}'s version of {state.family.replace('_', '-')} “{state.object_name}”.",
        )
        return redirect(_device_nso_tab_url(state.management.device_id))


class NSORoutePolicyVersionsView(SharedObjectVersionsMixin):  # noqa: D101
    model_class = NSORoutePolicyState
    materialize_url_name = "plugins:netbox_nso_plugin:routing_materialize_route_policy"

    def decorate_items(self, items, state):
        """Attach a structured route-map summary to each device version (route_map only)."""
        if state.family != "route_map":
            return
        from .route_policy_structure import summarize_route_map

        for it in items:
            row = it.get("row")
            it["route_map"] = summarize_route_map(getattr(row, "captured", None)) if it.get("has_capture") else None

    def extra_context(self, state, items) -> dict:
        """MASTER/LOCAL classification + the heuristic 'diverging → suggest LOCAL' verdict.

        ``mode`` defaults MASTER (absence of a NSORoutePolicyObjectClass row); ``diverging`` is
        computed live from the per-device versions, and ``suggest_local`` flags a diverging
        MASTER group so the operator can mark it per-device in one click (never auto-applied).
        """
        from .models import NSORoutePolicyObjectClass

        row = NSORoutePolicyObjectClass.objects.filter(family=state.family, object_name=state.object_name).first()
        mode = row.mode if row else "master"
        diverging = any(it.get("comparable") and not it.get("matches_owner") and not it.get("is_owner") for it in items)
        return {
            "mode": mode,
            "diverging": diverging,
            "suggest_local": mode == "master" and diverging,
            "classify_url_name": "plugins:netbox_nso_plugin:routing_classify_route_policy",
        }


class NSORoutePolicyClassifyView(NSOActionPermissionMixin, View):
    """Classify a route-policy object group MASTER (shared, dedup) or LOCAL (per-device).

    POST ``mode=local`` clears the false drift of a group that legitimately differs per device
    (each device keeps its own version, NSO tab only); ``mode=master`` re-materializes a shared
    owner. Re-processes the stored captures immediately (no device read). Gated on the same
    change permission as the other route-policy actions.
    """

    def post(self, request, pk):  # noqa: D102
        from .route_policy_reconciler import set_classification

        state = get_object_or_404(NSORoutePolicyState, pk=pk)
        mode = request.POST.get("mode")
        if mode not in ("master", "local"):
            messages.error(request, "Invalid classification.")
            return redirect("plugins:netbox_nso_plugin:routing_route_policy_versions", pk=pk)
        set_classification(state.family, state.object_name, mode)
        label = "per-device (local)" if mode == "local" else "shared (master)"
        messages.success(request, f"Marked {state.family.replace('_', '-')} {state.object_name} as {label}.")
        return redirect("plugins:netbox_nso_plugin:routing_route_policy_versions", pk=pk)


class NSORoutePolicyClassifyBulkView(NSOActionPermissionMixin, View):
    """Bulk-mark a device's divergent route-policy groups as per-device (LOCAL).

    Lists this device's drifted (conflict/changed) shared route-policy objects with a Diff link
    so the operator can batch the genuinely-per-device ones (VRRP / per-region lists) in one POST
    instead of opening each Versions page. Marking is global per ``(family, name)`` group.
    """

    template_name = "netbox_nso_plugin/route_policy_classify_bulk.html"

    def _divergent_rows(self, mgmt):
        return (
            NSORoutePolicyState.objects.filter(
                management=mgmt, status__in=("conflict", "changed"), is_materialized=False
            )
            .select_related("management__device")
            .order_by("family", "object_name")
        )

    def get(self, request, device_pk):  # noqa: D102
        mgmt = get_object_or_404(NSODeviceManagement, device_id=device_pk)
        return render(
            request,
            self.template_name,
            {"mgmt": mgmt, "object": mgmt.device, "rows": self._divergent_rows(mgmt)},
        )

    def post(self, request, device_pk):  # noqa: D102
        from .route_policy_reconciler import set_classification

        mgmt = get_object_or_404(NSODeviceManagement, device_id=device_pk)
        wanted = set(request.POST.getlist("state"))
        seen: set[tuple] = set()
        for state in self._divergent_rows(mgmt):
            if str(state.pk) in wanted and (state.family, state.object_name) not in seen:
                set_classification(state.family, state.object_name, "local")
                seen.add((state.family, state.object_name))
        if seen:
            messages.success(request, f"Marked {len(seen)} object(s) as per-device (local).")
        else:
            messages.warning(request, "No objects selected.")
        return redirect(_device_nso_tab_url(device_pk))


class NSORoutePolicyMaterializeView(SharedObjectMaterializeMixin):  # noqa: D101
    model_class = NSORoutePolicyState


# ── Drift delta: what differs between the device and what NetBox holds ──────────


class NSORoutePolicyDiffView(LoginRequiredMixin, View):
    """Show the concrete delta between a device's captured route-map and the NetBox object.

    The status badge says *that* a route-map drifted (``conflict`` / ``changed``); this shows
    *what* — a per-entry, per-field comparison of the device's on-box capture against the
    materialised ``RouteMap``, so the operator can see exactly which match/set construct
    differs (e.g. a ``set as-path replace`` the device has but NetBox is missing).
    """

    template_name = "netbox_nso_plugin/route_policy_diff.html"
    modal_template_name = "netbox_nso_plugin/route_policy_diff_modal.html"

    def get(self, request, pk):  # noqa: D102
        from .route_policy_diff import route_policy_state_diff, unified_policy_diff

        state = get_object_or_404(NSORoutePolicyState, pk=pk)
        diff = route_policy_state_diff(state)
        template_name = self.modal_template_name if request.headers.get("HX-Request") == "true" else self.template_name
        return render(
            request,
            template_name,
            {
                "state": state,
                "object_name": state.object_name,
                "family": state.family.replace("_", "-"),
                "diff": diff,
                "unified_diff": unified_policy_diff(state),
                "device": getattr(state.management, "device", None),
            },
        )


class NSORedistributionDiffView(LoginRequiredMixin, View):
    """Show the device-vs-NetBox delta for a redistribution overlay row (field-level)."""

    template_name = "netbox_nso_plugin/redistribution_diff.html"

    def get(self, request, pk):  # noqa: D102
        from .route_policy_diff import redistribution_diff, unified_redistribution_diff

        state = get_object_or_404(NSORedistributionState.objects.select_related("redistribution__route_map"), pk=pk)
        return render(
            request,
            self.template_name,
            {
                "state": state,
                "diff": redistribution_diff(state),
                "unified_diff": unified_redistribution_diff(state),
                "device": getattr(state.management, "device", None),
            },
        )


class NSOOSPFInstanceStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSOOSPFInstanceState


class NSOOSPFInterfaceStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSOOSPFInterfaceState


class NSORedistributionStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSORedistributionState


# ── Routing bulk accept views (Track A) ───────────────────────────────────────


class RoutingBulkAcceptMixin(NSOActionPermissionMixin, View):
    """Bulk 'Keep NetBox' for all DRIFTED routing rows of a device, then push intent."""

    model_class = None

    def _push(self, mgmt):
        """Record the accepted rows in the outbox; override in subclasses.

        The bulk update writes with ``QuerySet.update()``, which fires no signal, so the
        subclass names the key its rows belong to. Appending is what makes the send a
        claimed, sequenced operation (#1503 Appendix O, §4.2) rather than a bare PUT whose
        failure the view would swallow with nothing left to retry.
        """

    def _after_accept(self, mgmt, accepted_pks):
        """Run after the bulk ownership update, before the push (override in subclasses).

        *accepted_pks* are the rows this call moved from drift into ownership — the ones
        that now carry intent the device does not have.
        """

    def post(self, request, device_pk):  # noqa: D102
        from django.db import transaction

        try:
            mgmt = NSODeviceManagement.objects.get(device_id=device_pk)
        except NSODeviceManagement.DoesNotExist:
            messages.warning(request, "Device is not NSO-managed.")
            return redirect(_device_nso_tab_url(device_pk))

        # Accept = make NetBox the source of truth for not-yet-owned rows (imported)
        # and for drift. Already-owned rows (in_sync/accepted) are skipped (accepting
        # them was a repeatable no-op). Matching (imported) -> in_sync (nothing to
        # push); drift -> accepted (pending apply). _push() sends the snapshot once.
        base = self.model_class.objects.filter(management=mgmt)
        # One transaction: a request is not wrapped in one, so committing the status ahead
        # of _after_accept() would publish rows that read as owned while still carrying the
        # state the previous apply named — which a concurrent Apply would then act on.
        with transaction.atomic():
            # Captured before the UPDATE: afterwards the two groups are indistinguishable,
            # and only the drift group is entering ownership.
            drift_pks = list(base.filter(status__in=["changed", "conflict"]).values_list("pk", flat=True))
            n_owned = base.filter(status="imported").update(status="in_sync")
            # The pks narrow the update, they do not replace its predicate: a reconcile that
            # re-classified one of them meanwhile owns that row's status, not this request.
            n_drift = base.filter(pk__in=drift_pks, status__in=["changed", "conflict"]).update(status="accepted")
            count = n_owned + n_drift
            if count and mgmt.adapter_device_id is not None:
                self._after_accept(mgmt, drift_pks)
                # Inside the same transaction as the ownership it records: appended after
                # the commit, the entry could be lost while the rows read as owned.
                self._push(mgmt)

        if count:
            messages.success(request, f"Accepted {count} routing state(s).")
        else:
            messages.info(request, "Nothing to accept — no drift.")
        return redirect(_device_nso_tab_url(device_pk))


class NSOStaticRouteBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSOStaticRouteState

    def _after_accept(self, mgmt, accepted_pks):
        """Arm a generation on every row this accept moved from drift into ownership.

        Suppressed: a request is not wrapped in a transaction, so each unsuppressed save
        would PUT the full snapshot on the spot — N adapter calls carrying half-armed
        intent. ``_push()`` sends the finished snapshot once, immediately after.
        """
        from .signals import _arm_static_route_generation, suppress_intent_push

        with suppress_intent_push():
            for state in self.model_class.objects.filter(pk__in=accepted_pks, status="accepted"):
                _arm_static_route_generation(state)
                state.save(update_fields=list(_STATIC_ROUTE_ARMED_FIELDS))

    def _push(self, mgmt):
        _schedule_intent_push((mgmt.device_id, "static_route"))


class NSOISISInterfaceBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSOISISInterfaceState

    def _push(self, mgmt):
        _schedule_intent_push((mgmt.device_id, "isis"))


class NSOISISInstanceBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSOISISInstanceState

    def _push(self, mgmt):
        _schedule_intent_push((mgmt.device_id, "isis"))


class NSOBGPPeerBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSOBGPPeerState

    def _push(self, mgmt):
        _schedule_intent_push((mgmt.device_id, "bgp"))


class NSORoutePolicyBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSORoutePolicyState

    def _after_accept(self, mgmt, accepted_pks):
        from .signals import _own_route_map_contributors

        # Owning a route-map owns its contributors — cascade for every now-owned route-map.
        owned = ("accepted", "deploying", "in_sync", "apply_failed")
        for st in NSORoutePolicyState.objects.filter(management=mgmt, family="route_map", status__in=owned):
            obj = st.assigned_object
            if obj is not None:
                _own_route_map_contributors(mgmt, obj)

    def _push(self, mgmt):
        _schedule_intent_push((mgmt.device_id, "route_policy"))


class NSOOSPFInstanceBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSOOSPFInstanceState

    def _push(self, mgmt):
        _schedule_intent_push((mgmt.device_id, "ospf"))


class NSOOSPFInterfaceBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSOOSPFInterfaceState

    def _push(self, mgmt):
        _schedule_intent_push((mgmt.device_id, "ospf"))


class NSORedistributionBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSORedistributionState

    def _push(self, mgmt):
        from .signals import _OWNED_PUSH_STATUSES, redistribution_destinations

        supported = redistribution_destinations()
        destinations = (
            self.model_class.objects.filter(
                management=mgmt,
                status__in=_OWNED_PUSH_STATUSES,
                dest_protocol__in=supported,
            )
            .order_by()
            .values_list("dest_protocol", flat=True)
            .distinct()
        )
        for scope in sorted(destinations):
            _schedule_intent_push((mgmt.device_id, scope))


# ── SNMP / Logging overlay accept + edit (operator modify → accept → push) ─────
# Accept marks the row owned (accepted_at + status); the device commit is deferred
# to the single device Apply — one flow, like every other scope. SNMP secrets are
# never stored; the push resolves them from Vault via each row's vault_ref.


class OverlayStateAcceptMixin(NSOActionPermissionMixin, View):
    """Per-row accept for an SNMP/logging overlay — mark owned (accepted_at + status)."""

    model_class = None

    def push_blocker(self, state) -> str:
        """Why accepting *state* could not be faithfully applied, or "" when it can.

        Owning a row that the push must then skip is worse than refusing the accept: the
        snapshot is a FULL-REPLACE, so the skipped row is a shrink the adapter detaches,
        and the operator is left with a row that reads 'accepted' but was never applied.
        """
        return ""

    def post(self, request, pk):  # noqa: D102
        state = get_object_or_404(self.model_class, pk=pk)
        blocker = self.push_blocker(state)
        if blocker:
            messages.error(request, f"Cannot accept {state}: {blocker}")
            return redirect(_device_nso_tab_url(state.management.device_id))
        state.status = _status_after_accept(state.status)
        state.accepted_at = timezone.now()
        # One transaction, so the row and the outbox entry it schedules commit together.
        with transaction.atomic():
            state.save(update_fields=["status", "accepted_at"])
        messages.success(request, f"Accepted {state}.")
        return redirect(_device_nso_tab_url(state.management.device_id))


class NSOSnmpCommunityStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOSnmpCommunityState


class NSOSnmpV3UserStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOSnmpV3UserState

    def push_blocker(self, state):
        """Never let an accept downgrade a device-held authPriv user to noAuthNoPriv."""
        from .signals import snmp_v3_user_push_blocker

        return snmp_v3_user_push_blocker(state)


class NSOSnmpHostStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOSnmpHostState

    def push_blocker(self, state):
        """v3 trap hosts have no username source on the overlay — not pushable."""
        from .signals import snmp_host_push_blocker

        return snmp_host_push_blocker(state)


class NSOSnmpSystemInfoStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOSnmpSystemInfoState


class NSOSnmpCommunityStateVerifyView(NSOActionPermissionMixin, View):
    """Resolve the row's Vault ref via the adapter and store the value fingerprint.

    Sets ``vault_secret_hash``/``vault_secret_version`` so the badge can state
    whether the Vault-held secret matches what the device reports. Values never
    leave the adapter — only sha256[:16] fingerprints travel.
    """

    def post(self, request, pk):  # noqa: D102
        from . import adapter_client
        from .vault_refs import VaultRefError, parse_vault_ref

        state = get_object_or_404(NSOSnmpCommunityState, pk=pk)
        redirect_url = _device_nso_tab_url(state.management.device_id)
        if not state.vault_ref:
            messages.error(request, "No Vault ref on this community — set one (or a secret value) first.")
            return redirect(redirect_url)
        try:
            key = parse_vault_ref(state.vault_ref, require_key=True).key
        except VaultRefError as exc:
            messages.error(request, f"Bad Vault ref: {exc}")
            return redirect(redirect_url)
        try:
            result = adapter_client.verify_secret(state.vault_ref)
        except AdapterError as exc:
            messages.error(request, f"Vault verify failed: {exc}")
            return redirect(redirect_url)

        hashes = result.get("hashes") or {}
        if result.get("exists") and key in hashes:
            state.vault_secret_hash = hashes[key]
            state.vault_secret_version = result.get("version")
            with transaction.atomic():
                state.save(update_fields=["vault_secret_hash", "vault_secret_version"])
            verdict = (
                "matches the device value"
                if state.vault_secret_hash == state.community_hash
                else "DIFFERS from the device value (apply pending, or the device changed out-of-band)"
            )
            messages.success(request, f"Vault secret verified (v{result.get('version')}) — {verdict}.")
        else:
            messages.warning(request, f"Vault has no {key!r} field at {state.vault_ref!r}.")
        return redirect(redirect_url)


class NSOSnmpV3UserStateVerifyView(NSOActionPermissionMixin, View):
    """Resolve the v3 user's Vault path and record which fields (auth/priv) exist."""

    def post(self, request, pk):  # noqa: D102
        from . import adapter_client

        state = get_object_or_404(NSOSnmpV3UserState, pk=pk)
        redirect_url = _device_nso_tab_url(state.management.device_id)
        if not state.vault_ref:
            messages.error(request, "No Vault ref on this v3 user — set one (or secret values) first.")
            return redirect(redirect_url)
        try:
            result = adapter_client.verify_secret(state.vault_ref)
        except AdapterError as exc:
            messages.error(request, f"Vault verify failed: {exc}")
            return redirect(redirect_url)

        fields = set(result.get("fields") or [])
        state.vault_has_auth = "auth" in fields
        state.vault_has_priv = "priv" in fields
        with transaction.atomic():
            state.save(update_fields=["vault_has_auth", "vault_has_priv"])
        if fields:
            messages.success(request, f"Vault holds: {', '.join(sorted(fields))} (v{result.get('version')}).")
        else:
            messages.warning(request, f"Vault has no secret at {state.vault_ref!r}.")
        return redirect(redirect_url)


class NSOSnmpCommunityStateHarvestView(NSOActionPermissionMixin, View):
    """Adopt the device-held community string into Vault by its fingerprint.

    The adapter reads the plaintext from NSO's config mirror (targeted per-NED
    subtree), writes it to Vault, and returns only ref + fingerprint — the
    secret is never displayed or stored in NetBox.
    """

    def post(self, request, pk):  # noqa: D102
        from . import adapter_client
        from .forms import _vault_settings_layout

        state = get_object_or_404(NSOSnmpCommunityState, pk=pk)
        redirect_url = _device_nso_tab_url(state.management.device_id)
        if state.management.adapter_device_id is None:
            messages.error(request, "Device is not linked to the adapter — cannot harvest.")
            return redirect(redirect_url)
        ref = state.vault_ref
        if not ref:
            kv_mount, base_path = _vault_settings_layout()
            if not (kv_mount and base_path):
                messages.error(
                    request,
                    "No Vault ref on this row and no enabled Vault settings to derive one — "
                    "configure Settings → Vault first.",
                )
                return redirect(redirect_url)
            ref = f"{kv_mount}/{base_path}/community/{state.community_hash}#community"
        try:
            result = adapter_client.harvest_community(state.management.adapter_device_id, state.community_hash, ref)
        except AdapterError as exc:
            messages.error(request, f"Harvest failed: {exc}")
            return redirect(redirect_url)

        state.vault_ref = result.get("vault_ref") or ref
        state.vault_secret_hash = result.get("secret_hash") or ""
        state.vault_secret_version = result.get("version")
        with transaction.atomic():
            state.save(update_fields=["vault_ref", "vault_secret_hash", "vault_secret_version"])
        messages.success(
            request,
            f"Community harvested into Vault at {state.vault_ref!r} (v{result.get('version')}).",
        )
        return redirect(redirect_url)


class NSOLoggingHostStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOLoggingHostState


class NSOLoggingLevelStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOLoggingLevelState

    def push_blocker(self, state):
        """Refuse ownership of an all-blank row — it manages nothing, so the push would un-manage."""
        if not state.set_severities():
            return "no severity level is set; set at least one destination level before accepting"
        return ""


class NSOLoggingLevelStateUnacceptView(NSOActionPermissionMixin, View):
    """Release ownership of the logging-levels singleton (un-accept = RETRACT).

    Dropping the row from the owned set makes the next snapshot push carry
    ``local_levels: null``, so the adapter deletes the levels intent and enqueues
    a PUT-replace retract — FASTMAP withdraws the owned severity leaves. On NX
    that renders ``no logging <dest>``, which DISABLES the destination (the NED
    default is not "revert to the previous severity"); the confirm dialog on the
    tab says so before this view is ever reached. ``deploying`` refuses: an
    in-flight Apply must settle before ownership can be released.
    """

    def post(self, request, pk):  # noqa: D102
        from . import status_machine as sm

        state = get_object_or_404(NSOLoggingLevelState, pk=pk)
        device_id = state.management.device_id
        if state.status == "deploying":
            messages.error(request, f"Cannot un-accept {state}: an Apply is in flight.")
            return redirect(_device_nso_tab_url(device_id))
        if not sm.is_owned(state.status):
            messages.error(request, f"Cannot un-accept {state}: it is not owned.")
            return redirect(_device_nso_tab_url(device_id))
        state.status = sm.advance(state.status, sm.REVERT, to=sm.IMPORTED)
        state.accepted_at = None
        with transaction.atomic():
            state.save(update_fields=["status", "accepted_at"])
        messages.warning(
            request,
            f"Un-accepted {state}: the managed levels are being retracted from the device — "
            "on NX this disables the affected destination(s) rather than reverting them.",
        )
        return redirect(_device_nso_tab_url(device_id))


class NSOSVIStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOSVIState


class NSOSubinterfaceStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOSubinterfaceState

    def push_blocker(self, state):
        """Refuse ownership when the full-replace serializer would omit the row."""
        messages_by_field = _subinterface_errors(state)
        return " ".join(dict.fromkeys(message for messages in messages_by_field.values() for message in messages))


class NSOInterfaceMtuStateAcceptView(OverlayStateAcceptMixin):
    """Accept a per-interface MTU overlay (Phase 2b).

    Marks the row owned AND adopts the native L2 MTU onto the dcim.Interface
    (clamped for NetBox), so NetBox's native field reflects the managed value.
    ip-mtu/mpls-mtu ride the overlay only.
    """

    model_class = NSOInterfaceMtuState

    # NetBox dcim.Interface.mtu max (and the read-side clamp ceiling).
    _NETBOX_MTU_MAX = 65536

    def post(self, request, pk):  # noqa: D102
        state = get_object_or_404(self.model_class, pk=pk)
        state.status = _status_after_accept(state.status)
        state.accepted_at = timezone.now()
        # One transaction, so the native adoption, the row and the outbox entry it
        # schedules commit together.
        with transaction.atomic():
            if state.l2_mtu is not None:
                iface = state.interface
                clamped = min(int(state.l2_mtu), self._NETBOX_MTU_MAX)
                if iface.mtu != clamped:
                    iface.mtu = clamped
                    iface.save(update_fields=["mtu"])
            state.save(update_fields=["status", "accepted_at"])
        messages.success(request, f"Accepted {state}.")
        return redirect(_device_nso_tab_url(state.management.device_id))


class NSOVLANStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOVLANState


class NSOVLANRescopeView(NSOActionPermissionMixin, View):
    """Re-scope a synced VLAN into a different (e.g. site-wide/shared) VLAN group.

    The device↔VLAN link is anchored on the overlay row, so a VLAN can leave its
    per-device group and stay synced. GET shows the target-group picker (annotated
    with whether each group already has this vid → a merge); POST performs the
    move-or-merge via :func:`vlan_reconciler.rescope_vlan`.
    """

    def get(self, request, pk):  # noqa: D102
        from ipam.models import VLAN, VLANGroup

        state = get_object_or_404(NSOVLANState, pk=pk)
        vid = state.vlan.vid
        groups = []
        for group in VLANGroup.objects.exclude(pk=state.vlan.group_id).order_by("name"):
            merges = VLAN.objects.filter(group=group, vid=vid).exclude(pk=state.vlan.pk).exists()
            groups.append({"group": group, "merges": merges})
        return render(
            request,
            "netbox_nso_plugin/rescope_vlan.html",
            {"state": state, "groups": groups, "object": state.management.device},
        )

    def post(self, request, pk):  # noqa: D102
        from ipam.models import VLANGroup

        from .vlan_reconciler import rescope_vlan

        state = get_object_or_404(NSOVLANState, pk=pk)
        group = get_object_or_404(VLANGroup, pk=request.POST.get("group"))
        device_id = state.management.device_id
        action, vlan = rescope_vlan(state, group)
        if action == "noop":
            messages.info(request, f"VLAN {vlan.vid} is already in group {group}.")
        elif action == "moved":
            messages.success(request, f"Moved VLAN {vlan.vid} to group {group} (still synced).")
        else:
            messages.success(request, f"Merged VLAN {vlan.vid} onto the shared VLAN in group {group} (still synced).")
        return redirect(_device_nso_tab_url(device_id))


_RP_ATTACH_FAMILIES = [
    ("prefix_list", "Prefix List", "PrefixList"),
    ("route_map", "Route Map", "RouteMap"),
    ("community_list", "Community List", "CommunityList"),
    ("as_path", "AS Path", "ASPath"),
]


class NSORoutePolicyAttachView(NSOActionPermissionMixin, View):
    """Attach an existing netbox-routing policy object to this device (greenfield write).

    Handles prefix-list / route-map / community-list / as-path.
    The device↔object link is the NSORoutePolicyState overlay (content-type GFK), so
    one policy object can be attached to several devices. GET lists objects not yet
    attached to this device; POST creates an *accepted* overlay (which pushes the
    owned route-policy intent), written on the next Apply.
    """

    def get(self, request, device_pk):  # noqa: D102
        from django.contrib.contenttypes.models import ContentType

        mgmt = get_object_or_404(NSODeviceManagement, device_id=device_pk)
        attached = set(NSORoutePolicyState.objects.filter(management=mgmt).values_list("content_type_id", "object_id"))
        candidates = []
        try:
            import netbox_routing.models as rm
        except ImportError:
            rm = None
        for family, label, model_name in _RP_ATTACH_FAMILIES:
            model = getattr(rm, model_name, None) if rm else None
            if model is None:
                continue
            ct = ContentType.objects.get_for_model(model)
            for obj in model.objects.all().order_by("name"):
                if (ct.id, obj.pk) in attached:
                    continue
                candidates.append({"value": f"{family}:{ct.id}:{obj.pk}", "label": label, "name": obj.name})
        return render(
            request,
            "netbox_nso_plugin/attach_route_policy.html",
            {"mgmt": mgmt, "candidates": candidates, "object": mgmt.device},
        )

    def post(self, request, device_pk):  # noqa: D102
        from django.contrib.contenttypes.models import ContentType
        from django.utils import timezone

        mgmt = get_object_or_404(NSODeviceManagement, device_id=device_pk)
        try:
            family, ct_id, obj_pk = request.POST.get("policy", "").split(":")
            ct = ContentType.objects.get_for_id(int(ct_id))
            obj = ct.get_object_for_this_type(pk=int(obj_pk))
        except (ValueError, ContentType.DoesNotExist, Exception):  # noqa: BLE001
            messages.error(request, "Invalid route-policy selection.")
            return redirect(_device_nso_tab_url(mgmt.device_id))

        # Capability pre-flight (block-with-override): if this device's (ned, sw) is KNOWN
        # not to support parts of the object, stop and show exactly what won't apply — unless
        # the operator already chose to override. Unknown / unreachable adapter → fail open
        # (never block on a verdict we don't have). See compatibility-matrix design.
        override = request.POST.get("override") == "1"
        preflight = self._preflight(mgmt, family, obj) if (mgmt.adapter_device_id and not override) else None
        if preflight and preflight.get("known") and not preflight.get("fully_supported"):
            return render(
                request,
                "netbox_nso_plugin/attach_route_policy.html",
                {
                    "mgmt": mgmt,
                    "object": mgmt.device,
                    "preflight": preflight,
                    "selected_policy": request.POST.get("policy", ""),
                    "selected_label": f"{family} {obj.name}",
                },
            )

        with transaction.atomic():
            state, created = NSORoutePolicyState.objects.get_or_create(
                management=mgmt,
                family=family,
                object_name=obj.name,
                defaults={
                    "content_type": ct,
                    "object_id": obj.pk,
                    "status": "accepted",
                    "accepted_at": timezone.now(),
                },
            )
            if not created and state.status not in ("accepted", "deploying", "in_sync", "apply_failed"):
                state.status = "accepted"
                state.accepted_at = timezone.now()
            state.content_type = ct
            state.object_id = obj.pk
            state.last_sync_at = timezone.now()
            state.save()  # → _on_route_policy_state_save schedules the push
            cascade = None
            if family == "route_map":
                # Owning a route-map owns its contributors too (else dangling device references).
                from .signals import _own_route_map_contributors

                cascade = _own_route_map_contributors(mgmt, obj)
        if cascade is not None:
            if cascade.drifted:
                # A referenced object the device already has but that diverges from NetBox was
                # NOT overwritten — tell the operator so they can resolve it explicitly.
                refs = ", ".join(f"{fam.replace('_', ' ')} {nm}" for fam, nm in cascade.drifted)
                messages.warning(
                    request,
                    f"Route-map {obj.name} references {len(cascade.drifted)} object(s) that differ on "
                    f"{mgmt.device.name} — left as-is (not overwritten); resolve their drift before "
                    f"they ship: {refs}.",
                )
            if cascade.cross_device:
                # A greenfield reference whose NetBox content came from another device — owning
                # the route-map here pushes that device's version. Make the provenance explicit.
                refs = ", ".join(f"{fam.replace('_', ' ')} {nm} (from {src})" for fam, nm, src in cascade.cross_device)
                messages.warning(
                    request,
                    f"Route-map {obj.name} references {len(cascade.cross_device)} shared object(s) whose "
                    f"NetBox version was sourced from another device — applying here pushes that version "
                    f"onto {mgmt.device.name}: {refs}.",
                )
        if override:
            # Operator overrode a known-negative verdict — be explicit about what won't land.
            messages.warning(
                request,
                f"Attached {family} {obj.name} to {mgmt.device.name} with override — "
                f"some parts won't apply on this device (see its capability check).",
            )
        else:
            messages.success(request, f"Attached {family} {obj.name} to {mgmt.device.name} — Apply to write it.")
        return redirect(_device_nso_tab_url(mgmt.device_id))

    @staticmethod
    def _preflight(mgmt, family, obj):
        """Run the adapter capability pre-flight for an attach (authoritative, probes once).

        Returns the adapter verdict dict, or ``None`` when there is nothing to check
        (prefix-list carries no flaggable constructs).
        """
        from . import adapter_client as client
        from .signals import _preflight_constructs

        community_members, set_keys, match_keys, aspath_names = _preflight_constructs(family, obj)
        if not (community_members or set_keys or match_keys or aspath_names):
            return None
        return client.preflight_route_policy(
            mgmt.adapter_device_id, community_members, set_keys, match_keys, aspath_names, refresh=True
        )


class NSOBgpPeerCreateView(NSOActionPermissionMixin, View):
    """Create a greenfield BGP peer scoped to one managed device (in-tab "Add BGP peer").

    Builds the netbox-routing object graph (BGPRouter → BGPScope → BGPPeer + address
    families) the reconciler would build for a brownfield peer, reusing the reconciler's
    own get_or_create helpers so a greenfield peer and a later reconcile of the same peer
    converge on one identity. The BGPPeer save fires the greenfield signal, which owns an
    accepted NSOBGPPeerState overlay and pushes the (owned-only) BGP intent — written to
    the device on the next Apply.

    Authorization is TWO-sided, because this view is a door into another app: the caller
    must hold the netbox_routing add permission for the object graph it mints (the NSO
    permission alone must not become a back-door grant to create routing objects), and the
    target device is looked up through ``restrict()`` so an ObjectPermission scoped to a
    subset of devices is honoured here as it is in NetBox's own object views.
    """

    required_permission = (
        "netbox_nso_plugin.change_nsodevicemanagement",
        "netbox_routing.add_bgppeer",
    )

    @staticmethod
    def _mgmt_for(request, device_pk):
        return get_object_or_404(
            NSODeviceManagement.objects.restrict(request.user, "change"),
            device_id=device_pk,
        )

    def get(self, request, device_pk):  # noqa: D102
        mgmt = self._mgmt_for(request, device_pk)
        form = NSOBgpPeerGreenfieldForm(device=mgmt.device)
        return render(request, "netbox_nso_plugin/bgp_peer_form.html", self._ctx(mgmt, form))

    def post(self, request, device_pk):  # noqa: D102
        mgmt = self._mgmt_for(request, device_pk)
        form = NSOBgpPeerGreenfieldForm(request.POST, device=mgmt.device)
        if not form.is_valid():
            return render(request, "netbox_nso_plugin/bgp_peer_form.html", self._ctx(mgmt, form))
        peer = self._create_peer(mgmt.device, form.cleaned_data)
        messages.success(
            request,
            f"Created BGP peer {peer.peer.address.ip} on {mgmt.device.name} — Apply to write it.",
        )
        return redirect(_device_nso_tab_url(mgmt.device_id))

    @staticmethod
    def _ctx(mgmt, form):
        return {"form": form, "object": mgmt.device, "mgmt": mgmt}

    @staticmethod
    def _create_peer(device, data):
        """Assemble the router→scope→peer→AF graph via the reconciler's helpers.

        The whole graph is built inside one ``transaction.atomic()`` block so the intent
        push — fired by the ``BGPPeer`` post_save signal — is DEFERRED to ``on_commit`` and
        runs only after the peer's address-families are attached. Without the wrapper the
        view runs outside any transaction (NetBox sets no ``ATOMIC_REQUESTS``), so
        ``_schedule_intent_push`` runs the push INLINE at ``BGPPeer.create()`` time — before
        ``_write_peer_afs`` — and the pushed intent carries an empty ``address_families``.
        The bgp-reconciler then writes a neighbor with no ``address-family`` activation, i.e.
        an inert session (device-caught on ra1.lab via a greenfield dry-run). Atomicity also
        makes the create all-or-nothing on error.
        """
        from dcim.models import Device
        from django.contrib.contenttypes.models import ContentType
        from django.db import transaction
        from netbox_routing.models import (
            BGPAddressFamily,
            BGPPeer,
            BGPPeerAddressFamily,
            BGPRouter,
            BGPScope,
        )

        from .bgp_reconciler import _get_or_create_router, _get_or_create_scope, _write_peer_afs

        with transaction.atomic():
            router = _get_or_create_router(device, data["local_asn"], BGPRouter, ContentType, Device)
            scope = _get_or_create_scope(router, data.get("vrf"), BGPScope)
            peer = BGPPeer.objects.create(
                scope=scope,
                peer=data["peer"],
                name=None,
                remote_as=data.get("remote_as"),
                local_as=data.get("peer_local_as"),
                ttl=data.get("ttl"),
                enabled=data.get("enabled", True),
                password=data.get("password") or None,
                peer_group=data.get("peer_group"),
                source=data.get("source"),
                update_source=data.get("update_source"),
            )
            af_list = [{"af": af, "enabled": True} for af in (data.get("address_families") or ["ipv4-unicast"])]
            _write_peer_afs(peer, af_list, scope, BGPAddressFamily, BGPPeerAddressFamily)
        return peer


class NSORoutePolicyCapabilityView(LoginRequiredMixin, View):
    """Operator-facing capability matrix for one device's route-policy support.

    Lists, per ``(ned_id, sw_version)``, which route-map / community constructs this device
    supports — native / translated / skipped / unsupported, with the source (probe vs a real
    device rejection). Read is cache-only (no live probe); ``?refresh=1`` (or the "Check now"
    POST) forces a fresh probe. This is the browsable companion to the attach-time block and
    the per-object panel badge.
    """

    #: A live NSO probe (POST, or GET ?refresh=1) is a device-touching action; a plain
    #: cache-only GET is a read (login-only). Gate only the probe on the change permission.
    _PROBE_PERMISSION = "netbox_nso_plugin.change_nsodevicemanagement"

    def _require_probe_perm(self, request):
        if request.user.is_authenticated and not request.user.has_perm(self._PROBE_PERMISSION):
            raise PermissionDenied

    def get(self, request, device_pk):  # noqa: D102
        mgmt = get_object_or_404(NSODeviceManagement, device_id=device_pk)
        refresh = request.GET.get("refresh") == "1"
        if refresh:
            self._require_probe_perm(request)  # a forced probe touches the device
        return render(request, "netbox_nso_plugin/route_policy_capabilities.html", self._context(mgmt, refresh))

    def post(self, request, device_pk):  # noqa: D102
        self._require_probe_perm(request)
        mgmt = get_object_or_404(NSODeviceManagement, device_id=device_pk)
        ctx = self._context(mgmt, refresh=True)
        if ctx["known"]:
            messages.success(request, f"Refreshed capabilities for {mgmt.device.name}.")
        else:
            messages.warning(request, "Could not determine capabilities (adapter unreachable or device not probed).")
        return redirect(_device_capabilities_url(mgmt.device_id))

    @staticmethod
    def _context(mgmt, refresh):
        from . import adapter_client as client

        result: dict = {"known": False, "ned_id": "", "sw_version": "", "elements": []}
        if mgmt.adapter_device_id:
            result = client.get_device_capability(mgmt.adapter_device_id, refresh=refresh)
        # The 'coverage' row is a meta marker ("not assessed"), not a flaggable construct —
        # lift it out of the per-scope table and into a banner.
        elements = [el for el in result.get("elements", []) if el.get("scope") != "coverage"]
        coverage_unknown = result.get("coverage_unknown") or any(
            el.get("scope") == "coverage" and el.get("status") == "unknown" for el in result.get("elements", [])
        )
        # source='read' rows (H3) are per-scope read-support facts fed by the live read probe —
        # a different axis from the route-policy constructs, so they get their own table and
        # never count into the flagged-construct summary.
        read_rows = sorted(
            (el for el in elements if el.get("source") == "read"), key=lambda el: str(el.get("scope", ""))
        )
        constructs = [el for el in elements if el.get("source") != "read"]
        scopes: dict[str, list] = {"community": [], "rm-set": [], "rm-match": []}
        for el in constructs:
            scopes.setdefault(el.get("scope", "other"), []).append(el)
        flagged = sum(1 for el in constructs if el.get("status") in ("skipped", "unsupported"))
        return {
            "mgmt": mgmt,
            "object": mgmt.device,
            "known": result.get("known", False),
            "coverage_unknown": coverage_unknown,
            "ned_id": result.get("ned_id", ""),
            "sw_version": result.get("sw_version", ""),
            "scopes": scopes,
            "total": len(constructs),
            "flagged": flagged,
            "read_rows": read_rows,
            "read_readable": sum(1 for el in read_rows if el.get("status") == "native"),
        }


class NSOVLANAttachView(NSOActionPermissionMixin, View):
    """Attach an existing (shared) ipam.VLAN to this device — greenfield write path.

    The device↔VLAN link is the NSOVLANState overlay, *not* the VLAN group, so the
    same shared VLAN can be attached to several devices; renaming the VLAN then
    propagates to all of them (one ipam.VLAN, N overlays). GET shows a picker of
    VLANs not yet attached to this device; POST creates an *accepted* overlay (which
    pushes the owned VLAN intent), so the vid+name is written on the next Apply.
    """

    def get(self, request, device_pk):  # noqa: D102
        from ipam.models import VLAN

        mgmt = get_object_or_404(NSODeviceManagement, device_id=device_pk)
        attached = NSOVLANState.objects.filter(management=mgmt).values_list("vlan_id", flat=True)
        vlans = VLAN.objects.exclude(pk__in=list(attached)).select_related("group").order_by("vid")
        return render(
            request,
            "netbox_nso_plugin/attach_vlan.html",
            {"mgmt": mgmt, "vlans": vlans, "object": mgmt.device},
        )

    def post(self, request, device_pk):  # noqa: D102
        from django.utils import timezone
        from ipam.models import VLAN

        mgmt = get_object_or_404(NSODeviceManagement, device_id=device_pk)
        vlan = get_object_or_404(VLAN, pk=request.POST.get("vlan"))
        with transaction.atomic():
            state, created = NSOVLANState.objects.get_or_create(
                management=mgmt,
                vlan=vlan,
                defaults={"status": "accepted", "accepted_at": timezone.now()},
            )
            if not created and state.status not in ("accepted", "deploying", "in_sync", "apply_failed"):
                state.status = "accepted"
                state.accepted_at = timezone.now()
            state.last_sync_at = timezone.now()
            state.save()  # → _on_vlan_state_save schedules the owned-VLAN intent push
        messages.success(
            request, f"Attached VLAN {vlan.vid} ({vlan.name or '—'}) to {mgmt.device.name} — Apply to write it."
        )
        return redirect(_device_nso_tab_url(mgmt.device_id))


class NSOBFDInterfaceStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOBFDInterfaceState


class NSOSnmpCommunityStateEditView(generic.ObjectEditView):  # noqa: D101
    queryset = NSOSnmpCommunityState.objects.all()
    form = NSOSnmpCommunityStateForm


class NSOSnmpV3UserStateEditView(generic.ObjectEditView):  # noqa: D101
    queryset = NSOSnmpV3UserState.objects.all()
    form = NSOSnmpV3UserStateForm


class NSOSnmpHostStateEditView(generic.ObjectEditView):  # noqa: D101
    queryset = NSOSnmpHostState.objects.all()
    form = NSOSnmpHostStateForm


class NSOSnmpSystemInfoStateEditView(generic.ObjectEditView):  # noqa: D101
    queryset = NSOSnmpSystemInfoState.objects.all()
    form = NSOSnmpSystemInfoStateForm


class NSOLoggingHostStateEditView(generic.ObjectEditView):  # noqa: D101
    queryset = NSOLoggingHostState.objects.all()
    form = NSOLoggingHostStateForm


class NSOLoggingLevelStateEditView(generic.ObjectEditView):  # noqa: D101
    queryset = NSOLoggingLevelState.objects.all()
    form = NSOLoggingLevelStateForm


class NSOInterfaceMtuStateEditView(generic.ObjectEditView):  # noqa: D101
    queryset = NSOInterfaceMtuState.objects.all()
    form = NSOInterfaceMtuStateForm
