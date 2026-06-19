# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import logging

from dcim.models import Device
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from netbox.object_actions import AddObject, BulkDelete, BulkExport
from netbox.views import generic
from utilities.views import ViewTab, register_model_view

from .adapter_client import AdapterError
from .filters import (
    NSODeviceManagementFilterSet,
    NSOInstanceFilterSet,
    NSOInterfaceStateFilterSet,
    NSOPlatformNedMappingFilterSet,
)
from .forms import (
    AdapterConnectionForm,
    NSODeviceManagementForm,
    NSOFailoverSettingsForm,
    NSOInstanceForm,
    NSOInterfaceMtuStateForm,
    NSOLoggingHostStateForm,
    NSOPlatformNedMappingForm,
    NSOSnmpCommunityStateForm,
    NSOSnmpHostStateForm,
    NSOSnmpSystemInfoStateForm,
    NSOSnmpV3UserStateForm,
)
from .models import (
    AdapterConnection,
    NSOBFDInterfaceState,
    NSOBGPPeerState,
    NSODeviceManagement,
    NSOFailoverSettings,
    NSOInstance,
    NSOInterfaceMtuState,
    NSOInterfaceState,
    NSOISISInstanceState,
    NSOISISInterfaceState,
    NSOL2SapState,
    NSOLoggingHostState,
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
    NSOVLANState,
)
from .tables import (
    NSODeviceManagementTable,
    NSOInstanceTable,
    NSOInterfaceStateTable,
    NSOPlatformNedMappingTable,
)

logger = logging.getLogger(__name__)


def _device_nso_tab_url(device_pk):
    """Return the URL for the NSO tab on a device detail page."""
    return reverse("dcim:device_nso", kwargs={"pk": device_pk})


def _device_capabilities_url(device_pk):
    """Return the URL for a device's route-policy capabilities page."""
    return reverse("plugins:netbox_nso_plugin:route_policy_capabilities", kwargs={"device_pk": device_pk})


def _refresh_sync_cache(mgmt, adapter_device):
    """Update an NSODeviceManagement row's cached last_sync_* from an adapter device dict.

    Writes only changed fields via a targeted .update() (no full save / no signals),
    so it is cheap enough to call per-row on the list view. Returns the list of
    fields actually changed (empty if already current).
    """
    update_fields = []
    raw_ts = adapter_device.get("last_sync_at")
    if raw_ts:
        from dateutil.parser import parse as parse_dt

        last_sync_at = parse_dt(raw_ts) if isinstance(raw_ts, str) else raw_ts
        if mgmt.last_sync_at != last_sync_at:
            mgmt.last_sync_at = last_sync_at
            update_fields.append("last_sync_at")
    last_sync_status = adapter_device.get("last_sync_status") or ""
    if mgmt.last_sync_status != last_sync_status:
        mgmt.last_sync_status = last_sync_status
        update_fields.append("last_sync_status")
    if update_fields:
        NSODeviceManagement.objects.filter(pk=mgmt.pk).update(**{f: getattr(mgmt, f) for f in update_fields})
    return update_fields


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
    """

    required_permission = "netbox_nso_plugin.change_nsodevicemanagement"

    def dispatch(self, request, *args, **kwargs):
        """Enforce ``required_permission`` for authenticated users before the handler runs."""
        if request.user.is_authenticated and not request.user.has_perm(self.required_permission):
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

        adapter_error = None
        adapter_error_code = None
        intent_drift = []
        failover = None
        if mgmt is not None and mgmt.adapter_device_id is not None:
            from . import adapter_client as client
            from .intent_drift import compute_intent_drift

            try:
                adapter_device = client.get_device(mgmt.adapter_device_id)
                _refresh_sync_cache(mgmt, adapter_device)
                # Mgmt-IP failover status (active address / last probe / OOB health) — None when
                # the device has no failover row (no primary/OOB IPs pushed yet). Parse the ISO
                # timestamps to datetimes so the template's |date filter can format them.
                failover = adapter_device.get("failover")
                if failover:
                    from dateutil.parser import parse as parse_dt

                    for key in ("last_probe_at", "last_switch_at", "oob_health_checked_at"):
                        if failover.get(key):
                            failover[key] = parse_dt(failover[key])
                # Surface adapter↔NetBox split-brain (orphaned intent) — only renders if any.
                intent_drift = compute_intent_drift(device, mgmt)
            except AdapterError as exc:
                adapter_error = str(exc)
                adapter_error_code = exc.code
                logger.debug("Adapter unavailable for device %s: %s", device.pk, exc)

        return {
            "mgmt": mgmt,
            "nso_categories": category_summaries(device, mgmt),
            "adapter_error": adapter_error,
            "adapter_error_code": adapter_error_code,
            "intent_drift": intent_drift,
            "failover": failover,
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


class NSOCategoryCountsView(LoginRequiredMixin, View):
    """JSON of every category's live counts for the device NSO tab.

    The tab renders the category header badges (total / drift / pending apply / in sync)
    server-side at page load. After a Sync/Detect-Drift/Apply, the rows can clear but the
    headers stay stale until a full reload — so the JS re-fetches these counts and rewrites
    the badges in place. Read-only aggregate over NSO*State (same source as the tab render).

    URL: /plugins/nso/devices/<pk>/category-counts/
    """

    def get(self, request, device_pk):
        """Return {categories: {key: {total, drift, pending}}} for the device."""
        from .summary import category_summaries

        device = get_object_or_404(Device, pk=device_pk)
        mgmt = getattr(device, "nso_management", None)
        out = {
            c["key"]: {
                "total": c["counts"].get("total", 0),
                "drift": c["counts"].get("drift", 0),
                "pending": c["counts"].get("pending", 0),
            }
            for c in category_summaries(device, mgmt)
        }
        return JsonResponse({"categories": out})


_PENDING_KINDS = {"pending", "apply_failed"}


def _merged_iface_kinds(iface, attr_states, mtu_states, sw_states, ip_states) -> set[str]:
    """Aggregate the per-attribute state kinds for one interface (matrix view).

    Each attribute cell classifies independently: enabled/description are value-aware
    (interface_row_state), the rest go through display_state. Returns the SET of kinds
    across all of the interface's cells so the view can bucket it as drift/pending/in_sync.
    """
    from .summary import display_state, interface_row_state

    kinds: set[str] = set()
    for attr in ("enabled", "description"):
        st = attr_states.get((iface.id, attr))
        if st is not None:
            kinds.add(interface_row_state(st, iface)[0])
    for st in (mtu_states.get(iface.id), sw_states.get(iface.id)):
        if st is not None:
            kinds.add(display_state(st.status, st.accepted_at is not None)[0])
    for st in ip_states.get(iface.id, []):
        kinds.add(display_state(st.status, st.accepted_at is not None)[0])
    return kinds


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
            return HttpResponseBadRequest(f"unknown category: {key}")

        ctx = {"object": device, "mgmt": mgmt, "status_badge": _STATUS_BADGE}
        if mgmt is not None and mgmt.adapter_device_id is not None:
            try:
                ctx.update(reconcile_category(device, mgmt, key))
            except AdapterError as exc:
                ctx["adapter_error"] = str(exc)
                ctx["adapter_error_code"] = exc.code
        ctx["category_has_unowned"] = _ctx_has_unowned(ctx)
        return render(request, partial, ctx)

    # Single-table overlay categories that render paginated from last-synced state.
    # spec: model, ctx var the partial loops, partial, search fields, order, FKs to
    # select_related, and the filter-box placeholder. Freshness comes from the
    # sync-complete / scheduler reconcile (reconcile_device covers every one of these).
    def _paged_category_specs(self):
        from .models import (
            NSOL2SapState,
            NSORedistributionState,
            NSORoutePolicyState,
            NSOStaticRouteState,
            NSOSubinterfaceState,
            NSOSVIState,
            NSOVLANState,
        )

        base = "netbox_nso_plugin/categories/"
        return {
            "route_policy": dict(
                model=NSORoutePolicyState,
                ctx="route_policy_states",
                partial=base + "route_policy.html",
                search=["object_name", "family"],
                order=["family", "object_name"],
                sr=["content_type"],
                ph="Filter by name / family…",
            ),
            "static": dict(
                model=NSOStaticRouteState,
                ctx="static_routes",
                partial=base + "static.html",
                search=["nso_prefix", "nso_vrf", "nso_next_hop"],
                order=["nso_prefix"],
                sr=["static_route"],
                ph="Filter by prefix / VRF / next hop…",
            ),
            "redistribution": dict(
                model=NSORedistributionState,
                ctx="redistribution_states",
                partial=base + "redistribution.html",
                search=["dest_protocol", "dest_ref", "source_protocol", "route_map"],
                order=["dest_protocol", "source_protocol"],
                sr=[],
                ph="Filter by protocol / ref / route-map…",
            ),
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

        from .template_content import _STATUS_BADGE

        spec = self._paged_category_specs().get(key)
        if spec is None:
            return None

        adapter_error = None
        if request.GET.get("refresh") and mgmt is not None and mgmt.adapter_device_id is not None:
            from .reconcile import reconcile_category

            try:
                reconcile_category(device, mgmt, key)
            except AdapterError as exc:
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
        bucketed = [(_paged_row_bucket(r.status, r.accepted_at is not None), r) for r in rows]
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

        ctx = {
            "object": device,
            "mgmt": mgmt,
            "status_badge": _STATUS_BADGE,
            spec["ctx"]: list(page.object_list),
            "page": page,
            "q": q,
            "state": state,
            "state_counts": state_counts,
            "placeholder": spec["ph"],
            "category_has_unowned": has_unowned,
            "adapter_error": adapter_error,
            "paged": True,
        }
        return render(request, spec["partial"], ctx)

    def _render_interface_merged(self, request, device):
        """Consolidated per-interface view: one row per interface, a column per attribute.

        Folds the four scattered per-interface scalar overlays (enabled/description,
        IPs, MTU, switchport) into a single table with a client-side column-select.
        Reconciles all four on expand (suppress-wrapped), then pivots the persisted
        NSO*State rows by interface. Each attribute cell reuses that overlay's own
        status badge + Accept endpoint, so per-attribute Accept/Apply still works.
        Filter by interface name (?q=), paginated like the interfaces page.
        """
        from django.core.paginator import Paginator

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
            except AdapterError as exc:
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

        q = (request.GET.get("q") or "").strip()
        ordered = sorted(ifaces.values(), key=lambda i: i.name)
        if q:
            ql = q.lower()
            ordered = [i for i in ordered if ql in i.name.lower()]

        # Per-interface aggregate state, so the matrix offers the same drift/pending
        # quick-filter the per-attribute interfaces page has. Counts are over the
        # name-filtered set (before the state filter); the chips then narrow the rows.
        kinds_by_iface = {i.id: _merged_iface_kinds(i, attr_states, mtu_states, sw_states, ip_states) for i in ordered}
        counts = {"all": len(ordered), "drift": 0, "pending": 0}
        for ks in kinds_by_iface.values():
            counts["drift"] += 1 if "drift" in ks else 0
            counts["pending"] += 1 if ks & _PENDING_KINDS else 0
        state = request.GET.get("state") or "all"
        ordered = _filter_ifaces_by_state(ordered, kinds_by_iface, state)

        paginator = Paginator(ordered, self._INTERFACES_PER_PAGE)
        page = paginator.get_page(request.GET.get("page") or 1)

        rows = []
        for iface in page.object_list:
            rows.append(
                {
                    "iface": iface,
                    "enabled": attr_states.get((iface.id, "enabled")),
                    "description": attr_states.get((iface.id, "description")),
                    "mtu": mtu_states.get(iface.id),
                    "ips": ip_states.get(iface.id, []),
                    "switchport": sw_states.get(iface.id),
                }
            )

        return render(
            request,
            "netbox_nso_plugin/categories/interface.html",
            {
                "object": device,
                "rows": rows,
                "page": page,
                "q": q,
                "state": state,
                "counts": counts,
                "adapter_error": adapter_error,
            },
        )

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
        from django.apps import apps

        from . import adapter_client

        app_cfg = apps.get_app_config("netbox_nso_plugin")
        resolved = adapter_client._resolve_config()
        db_url = (getattr(instance, "url", "") or "") if getattr(instance, "enabled", False) else ""
        return {
            "derived_intent_templates": getattr(app_cfg, "_derived_intent_templates", []),
            "token_configured": bool(resolved.get("token")),
            "effective_url": resolved.get("url") or "",
            "url_source": "Adapter Connection (DB)" if db_url else "PLUGINS_CONFIG / env",
        }


class NSOFailoverSettingsEditView(generic.ObjectEditView):
    """Singleton edit view for NSOFailoverSettings (global mgmt-IP failover tuning)."""

    queryset = NSOFailoverSettings.objects.all()
    form = NSOFailoverSettingsForm

    def get_object(self, **kwargs):
        """Return the existing singleton or a blank instance for first-time creation."""
        return NSOFailoverSettings.objects.first() or NSOFailoverSettings()


# ── NSO Instance CRUD ────────────────────────────────────────────────────────


class NSOInstanceListView(generic.ObjectListView):
    """List view for NSO instances."""

    queryset = NSOInstance.objects.all()
    table = NSOInstanceTable
    filterset = NSOInstanceFilterSet
    # Only advertise the bulk/single actions we actually wire up — the NetBox
    # default tuple includes Import / Bulk-Edit / Bulk-Rename, whose buttons would
    # render with formaction="None" (NoReverseMatch → None) and POST to a 404.
    actions = (AddObject, BulkExport, BulkDelete)


class NSOInstanceBulkDeleteView(generic.BulkDeleteView):
    """Bulk-delete view for NSO instances."""

    queryset = NSOInstance.objects.all()
    table = NSOInstanceTable
    filterset = NSOInstanceFilterSet


class NSOInstanceView(generic.ObjectView):
    """Detail view for an NSO instance."""

    queryset = NSOInstance.objects.all()


class NSOInstanceEditView(generic.ObjectEditView):
    """Create/edit view for an NSO instance."""

    queryset = NSOInstance.objects.all()
    form = NSOInstanceForm


class NSOInstanceDeleteView(generic.ObjectDeleteView):
    """Delete view for an NSO instance."""

    queryset = NSOInstance.objects.all()


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
            managed = list(NSODeviceManagement.objects.filter(nso_instance=instance).select_related("device"))
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
            messages.error(request, f"Onboarding {device} failed: {exc}")
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
            messages.error(request, f"Managing {device} failed: {exc}")
            return redirect(f"{redirect_url}?instance={instance.adapter_instance_id}")

        if result["ok"]:
            messages.success(request, f"{device} is now managed by NSO ({instance.name}).")
        else:
            messages.error(request, f"Could not manage {device}: {result['error']}")
        return redirect(f"{redirect_url}?instance={instance.adapter_instance_id}")


def _summarize_provision_failure(steps) -> str:
    """Build a one-line summary of the first failed step in a provision result."""
    for step in steps or []:
        if step.get("status") == "failed":
            detail = step.get("detail")
            return f"{step.get('step')} failed" + (f": {detail}" if detail else "")
    return "Provisioning failed."


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
        from . import adapter_client as client

        mgmt = get_object_or_404(NSODeviceManagement, pk=pk)
        if mgmt.onboard_status != "provisioning":
            return JsonResponse({"status": mgmt.onboard_status or "ready", "error": mgmt.onboard_error})

        if not mgmt.onboard_job_id:
            mgmt.onboard_status = "provision_failed"
            mgmt.onboard_error = "No provision job id recorded."
            mgmt.save(update_fields=["onboard_status", "onboard_error"])
            return JsonResponse({"status": "provision_failed", "error": mgmt.onboard_error})

        try:
            job = client.get_job(mgmt.onboard_job_id)
        except AdapterError as exc:
            # Transient — keep provisioning so the client keeps polling (200, not 502).
            return JsonResponse({"status": "provisioning", "poll_error": str(exc)})

        job_status = (job or {}).get("status")
        if job_status in ("queued", "running"):
            return JsonResponse({"status": "provisioning"})

        if job_status == "succeeded":
            result = (job or {}).get("result") or {}
            steps = result.get("steps") or []
            if result.get("ok"):
                # NSO node is up — flip to ready; the save re-fires the (now un-gated)
                # sync_scope_to_adapter signal → adapter mapping + scope + sync-notify.
                mgmt.onboard_status = ""
                mgmt.onboard_steps = steps
                mgmt.onboard_error = ""
                mgmt.save()
                return JsonResponse({"status": "ready"})
            mgmt.onboard_status = "provision_failed"
            mgmt.onboard_steps = steps
            mgmt.onboard_error = _summarize_provision_failure(steps)
            mgmt.save(update_fields=["onboard_status", "onboard_steps", "onboard_error"])
            return JsonResponse({"status": "provision_failed", "error": mgmt.onboard_error})

        # failed / timeout / unknown-terminal
        err = (job or {}).get("error") or {}
        mgmt.onboard_status = "provision_failed"
        mgmt.onboard_error = err.get("message") or "Provision job failed."
        mgmt.save(update_fields=["onboard_status", "onboard_error"])
        return JsonResponse({"status": "provision_failed", "error": mgmt.onboard_error})


class NSOPlatformNedMappingListView(generic.ObjectListView):
    """List view for Platform→NED mappings."""

    queryset = NSOPlatformNedMapping.objects.all()
    table = NSOPlatformNedMappingTable
    filterset = NSOPlatformNedMappingFilterSet
    actions = (AddObject, BulkExport, BulkDelete)


class NSOPlatformNedMappingBulkDeleteView(generic.BulkDeleteView):
    """Bulk-delete view for Platform→NED mappings."""

    queryset = NSOPlatformNedMapping.objects.all()
    table = NSOPlatformNedMappingTable
    filterset = NSOPlatformNedMappingFilterSet


class NSOPlatformNedMappingView(generic.ObjectView):
    """Detail view for a Platform→NED mapping."""

    queryset = NSOPlatformNedMapping.objects.all()


class NSOPlatformNedMappingEditView(generic.ObjectEditView):
    """Create/edit view for a Platform→NED mapping."""

    queryset = NSOPlatformNedMapping.objects.all()
    form = NSOPlatformNedMappingForm


class NSOPlatformNedMappingDeleteView(generic.ObjectDeleteView):
    """Delete view for a Platform→NED mapping."""

    queryset = NSOPlatformNedMapping.objects.all()


# ── NSO Device Management CRUD ───────────────────────────────────────────────


class NSODeviceManagementListView(generic.ObjectListView):
    """List view for managed NSO devices.

    Refreshes the cached ``last_sync_*`` columns on each render via a cheap
    per-row ``get_device`` call, so the list reflects current sync state without
    the operator first having to open each device's NSO tab. Compliance and
    per-protocol reconcile are NOT run here (those stay on the tab) — only the
    two lightweight last-sync fields are polled. Adapter errors are swallowed
    per row so one unreachable device never breaks the list.
    """

    queryset = NSODeviceManagement.objects.select_related("device", "nso_instance")
    table = NSODeviceManagementTable
    filterset = NSODeviceManagementFilterSet
    actions = (AddObject, BulkExport, BulkDelete)

    def get_queryset(self, request):
        """Poll the adapter for last-sync state before the table is built."""
        qs = super().get_queryset(request)
        from . import adapter_client as client

        for mgmt in qs:
            if mgmt.adapter_device_id is None:
                continue
            try:
                _refresh_sync_cache(mgmt, client.get_device(mgmt.adapter_device_id))
            except AdapterError as exc:
                logger.debug("List last-sync poll failed for device %s: %s", mgmt.pk, exc)
        return qs


class NSODeviceManagementBulkDeleteView(generic.BulkDeleteView):
    """Bulk-delete view for managed NSO devices."""

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
                interfaces = client.get_interfaces(instance.adapter_device_id)
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


class NSODeviceManagementEditView(generic.ObjectEditView):
    """Create/edit view for an NSO device management record."""

    queryset = NSODeviceManagement.objects.all()
    form = NSODeviceManagementForm
    template_name = "netbox_nso_plugin/nsodevicemanagement_edit.html"


class NSODeviceManagementDeleteView(generic.ObjectDeleteView):
    """Delete view for an NSO device management record."""

    queryset = NSODeviceManagement.objects.all()


# ── Adapter actions ──────────────────────────────────────────────────────────

_ACTION_LABELS = {
    "sync": "Sync",
    "detect-drift": "Detect Drift",
    "connect": "Test Connection",
    "apply": "Apply Intent",
}


def _prepare_apply(mgmt):
    """Pre-Apply bookkeeping for one device's single Apply.

    LACP + switchport are owned in NetBox (not mirrored as adapter intent), so
    force-push their snapshots now. Then move owned 'accepted' overlays →
    'deploying' so they read as "applying" and settle to 'in_sync' on the next
    reconcile once the device reflects them (VLAN value-aware; SVI/subif/BFD when
    re-reported). ``.update()`` avoids firing the per-row push signal.
    """
    from .signals import (
        _push_interface_intent_for_device,
        _push_lacp_intent_for_device,
        _push_switchport_intent_for_device,
        _push_vlan_intent_for_device,
    )

    # Force-push (bypass change-detection) the owned snapshots so Apply re-ships the
    # operator's intent even when the adapter's stored intent went stale:
    #   - LACP / switchport: owned in NetBox, never mirrored as adapter intent.
    #   - VLAN: the name lives on ipam.VLAN; renaming it fires no plugin signal, so a
    #     post-accept rename would otherwise be stranded in NetBox (the row stays
    #     'in_sync' and the stale old name is what gets applied).
    #   - interface description/enabled: an owned attribute that drifted back to
    #     'imported' (device differs again) is shown 'pending apply' but was being
    #     silently skipped — force-push so Apply actually re-applies it.
    for push in (
        _push_interface_intent_for_device,
        _push_lacp_intent_for_device,
        _push_switchport_intent_for_device,
        _push_vlan_intent_for_device,
    ):
        try:
            push(mgmt.device_id, mgmt.adapter_device_id, force=True)
        except Exception as exc:  # noqa: BLE001 — one scope's failure must not block the rest
            logger.warning("Apply push failed for device %s: %s", mgmt.device_id, exc)

    for model in (
        NSOVLANState,
        NSOSVIState,
        NSOSubinterfaceState,
        NSOBFDInterfaceState,
        NSOInterfaceMtuState,
        NSORoutePolicyState,
    ):
        try:
            model.objects.filter(management=mgmt, status="accepted").update(status="deploying")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Apply deploying-mark failed for device %s: %s", mgmt.device_id, exc)


class NSODeviceActionView(NSOActionPermissionMixin, View):
    """Trigger an adapter action (sync / detect-drift / connect) via POST."""

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
            "detect-drift": client.trigger_detect_drift,
            "connect": client.trigger_connect,
            "apply": client.trigger_apply,
        }[action]

        # One Apply commits everything: the adapter worker applies the intent-stored
        # scopes (attrs/IP/SNMP/routing/L2), and here we force-commit the LACP +
        # switchport snapshots, which are owned in NetBox rather than mirrored in the
        # adapter. Accept itself only marks rows owned (no immediate device write).
        if action == "apply":
            _prepare_apply(mgmt)

        try:
            result = action_fn(mgmt.adapter_device_id)
            job_id = result.get("job_id") if result else None
            if is_ajax:
                return JsonResponse({"status": "ok", "job_id": job_id})
            if job_id:
                messages.success(request, f"{label} triggered — Job ID: {job_id}. Refresh the page to see results.")
            else:
                messages.success(request, f"{label} triggered.")
        except AdapterError as exc:
            if exc.code == "conflict":
                job_id = (exc.detail or {}).get("job_id")
                if is_ajax:
                    return JsonResponse({"status": "conflict", "job_id": job_id})
                msg = "A job is already running for this device."
                if job_id:
                    msg += f" (Job ID: {job_id})"
                messages.warning(request, msg)
                return redirect(_device_nso_tab_url(mgmt.device.pk))
            if is_ajax:
                return JsonResponse({"status": "error", "message": str(exc)}, status=502)
            messages.error(request, f"Adapter error triggering {label}: {exc}")

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
            done = resync_intent(mgmt.device, mgmt)
            if done:
                messages.success(request, f"Re-synced adapter intent — cleared orphaned: {', '.join(done)}.")
            else:
                messages.info(request, "No orphaned adapter intent to clear.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Intent re-sync failed for device %s: %s", mgmt.device_id, exc)
            messages.error(request, f"Intent re-sync failed: {exc}")
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


class NSODeviceJobsView(LoginRequiredMixin, View):
    """JSON summary of a device's adapter jobs for the tab's status strip.

    Returns the currently-active job (queued/running) if any, and the most recent
    finished job (succeeded/failed) so an operator returning to the tab can see at a
    glance whether work is in flight and how the last run went. Polled client-side
    while a job is active.
    """

    _ACTIVE = ("queued", "running")
    _TERMINAL = ("succeeded", "failed")

    def get(self, request, pk):
        """Return {onboarded, running, last} for the device's adapter jobs."""
        device = get_object_or_404(Device, pk=pk)
        mgmt = getattr(device, "nso_management", None)
        if mgmt is None or mgmt.adapter_device_id is None:
            return JsonResponse({"onboarded": False, "running": None, "last": None})

        from . import adapter_client as client

        try:
            jobs = client.list_jobs(mgmt.adapter_device_id)
        except AdapterError as exc:
            return JsonResponse({"error": str(exc)}, status=502)

        # list_jobs is most-recent-first, so the first match in each bucket is newest.
        running = next((j for j in jobs if j.get("status") in self._ACTIVE), None)
        last = next((j for j in jobs if j.get("status") in self._TERMINAL), None)
        return JsonResponse({"onboarded": True, "running": running, "last": last})


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
            interfaces = client.get_interfaces(mgmt.adapter_device_id)
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
            messages.success(request, "Refresh from NSO queued — category counts will update shortly.")
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

    queryset = NSOInterfaceState.objects.exclude(status__in=("imported", "in_sync")).select_related(
        "interface", "interface__device"
    )
    table = NSOInterfaceStateTable
    filterset = NSOInterfaceStateFilterSet
    # Sync-managed: no add/import/edit — just export + bulk-delete (cleanup).
    actions = (BulkExport, BulkDelete)


class NSOInterfaceStateBulkDeleteView(generic.BulkDeleteView):
    """Bulk-delete view for NSOInterfaceState rows (cleanup)."""

    queryset = NSOInterfaceState.objects.all()
    table = NSOInterfaceStateTable
    filterset = NSOInterfaceStateFilterSet


class NSOInterfaceStateView(generic.ObjectView):
    """Detail view for an NSOInterfaceState record."""

    queryset = NSOInterfaceState.objects.select_related("interface")


class NSOInterfaceStateDeleteView(generic.ObjectDeleteView):
    """Delete view for an NSOInterfaceState record."""

    queryset = NSOInterfaceState.objects.all()


# ── Accept workflow ───────────────────────────────────────────────────────────


def _push_intent_for_device(device_id: int) -> None:
    """Push the full OWNED interface intent snapshot for a device to the adapter.

    Delegates to the single shared builder in ``signals`` so the view-level bulk
    accept, the accept signal, and the Decision-G edit signal all push the same
    snapshot and share the change-detection cache.
    """
    from .signals import _push_interface_intent_for_device

    try:
        mgmt = NSODeviceManagement.objects.select_related("nso_instance").get(device_id=device_id)
    except NSODeviceManagement.DoesNotExist:
        logger.warning("No NSODeviceManagement for device %s, skipping intent push", device_id)
        return

    if mgmt.adapter_device_id is None:
        return

    _push_interface_intent_for_device(device_id, mgmt.adapter_device_id)


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
        iface.save(update_fields=[attribute])

        return JsonResponse({"status": "ok", "message": f"Updated {attribute} on {iface.name}."})


class NSOBulkAcceptView(NSOActionPermissionMixin, View):
    """Bulk-accept all 'changed' interface states for a device and push a single intent snapshot."""

    def post(self, request, device_pk):
        """Accept all acceptable states for the given device.

        Matching (imported) values become in_sync (nothing to apply); differing
        (changed) values become accepted and trigger a single intent push.
        """
        now = timezone.now()
        base = NSOInterfaceState.objects.filter(interface__device_id=device_pk)
        settled = base.filter(status="imported").update(status="in_sync", accepted_at=now)
        pending = base.filter(status="changed").update(status="accepted", accepted_at=now)
        updated = settled + pending

        # Push whenever anything became owned — the snapshot is by accepted_at, so even
        # owned-but-matching rows must be recorded in the adapter to persist ownership.
        if updated:
            _push_intent_for_device(device_pk)
        if updated:
            messages.success(request, f"Accepted {updated} interface attribute(s).")
        else:
            messages.info(request, "No attributes to accept.")

        device = get_object_or_404(Device, pk=device_pk)
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
    return _join_props(
        [
            r.process_tag,
            r.af,
            ctype,
            net,
            f"metric {metric}" if metric is not None else "",
            "passive" if r.passive else "",
            "hello-auth" if r.hello_auth_present else "",
        ]
    )


def _apply_preview_interface_changes(device_pk):
    """Owned interface attributes whose NetBox value differs from the device.

    The value-aware 'pending' the matrix shows and the force-push applies — so the
    preview agrees with what Apply actually pushes (filtering by status==accepted alone
    missed an owned attribute that drifted back to 'imported').
    """
    from .summary import interface_row_state

    changes = []
    owned = (
        NSOInterfaceState.objects.filter(interface__device_id=device_pk, accepted_at__isnull=False)
        .select_related("interface")
        .order_by("interface__name", "attribute")
    )
    for st in owned:
        iface = st.interface
        kind, _label, _owned = interface_row_state(st, iface)
        if kind not in ("pending", "apply_failed"):
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
        preview_specs = [
            (NSOVLANState, "VLAN", _vlan_item, lambda r: f"name {r.vlan.name}" if r.vlan else ""),
            (NSOSwitchportState, "Switchport", _iface, lambda r: r.mode or ""),
            (
                NSOSVIState,
                "SVI / IRB",
                _iface,
                lambda r: f"VLAN {r.vlan.vid}" if getattr(r, "vlan", None) else (r.vrf or ""),
            ),
            (NSOSubinterfaceState, "Subinterface", _iface, lambda r: f"dot1q {r.dot1q_vlan}" if r.dot1q_vlan else ""),
            (
                NSOBFDInterfaceState,
                "BFD",
                _iface,
                lambda r: f"tx {r.min_tx or '?'} / rx {r.min_rx or '?'} x{r.multiplier or '?'}",
            ),
            (NSOLACPBundleState, "LACP", _iface, lambda r: f"lag {r.lag_id}" if r.lag_id else ""),
            (
                NSOStaticRouteState,
                "Static route",
                lambda r: r.nso_prefix or "",
                lambda r: f"→ {r.nso_next_hop}" if r.nso_next_hop else "",
            ),
            (NSOISISInterfaceState, "IS-IS interface", _iface, _isis_iface_detail),
            (NSOISISInstanceState, "IS-IS", lambda r: r.process_tag or "instance", lambda r: r.net or ""),
            (NSOOSPFInterfaceState, "OSPF interface", _iface, _ospf_iface_detail),
            (NSOOSPFInstanceState, "OSPF", lambda r: f"process {r.process_id}", lambda r: r.router_id or ""),
            (
                NSOBGPPeerState,
                "BGP peer",
                lambda r: r.peer_address_str or "",
                lambda r: f"AS {r.remote_as_str}" if r.remote_as_str else "",
            ),
            (NSORoutePolicyState, "Route policy", lambda r: r.object_name or "", lambda r: r.family or ""),
            (
                NSORedistributionState,
                "Redistribution",
                lambda r: f"{r.source_protocol} → {r.dest_protocol}",
                lambda r: r.route_map or "",
            ),
            (NSOSnmpCommunityState, "SNMP community", lambda r: "community", lambda r: r.access or ""),
            (NSOSnmpV3UserState, "SNMP v3 user", lambda r: r.username or "user", lambda r: ""),
            (NSOSnmpHostState, "SNMP host", lambda r: r.address or "", lambda r: f"v{r.version}" if r.version else ""),
            (NSOSnmpSystemInfoState, "SNMP system", lambda r: "system-info", lambda r: ""),
            (NSOLoggingHostState, "Logging host", lambda r: r.address or "", lambda r: r.severity or ""),
            (NSOL2SapState, "L2 SAP", lambda r: r.sap_id or "", lambda r: r.service_name or ""),
        ]

        routing_changes = []
        if mgmt is not None:
            for model, label, item_fn, detail_fn in preview_specs:
                rows = model.objects.filter(management=mgmt, status__in=("accepted", "apply_failed"))
                for r in rows:
                    try:
                        item = item_fn(r)
                    except Exception:
                        item = "—"
                    try:
                        detail = detail_fn(r)
                    except Exception:
                        detail = ""
                    routing_changes.append({"category": label, "item": item, "detail": detail, "status": r.status})

        # Right panel: the actual native device diff the Apply would push (NSO dry-run,
        # no commit). Best-effort — a slow/unavailable adapter must not block the preview.
        device_diff = {}
        if mgmt is not None and mgmt.adapter_device_id is not None:
            from . import adapter_client as client

            try:
                device_diff = (client.get_apply_diff(mgmt.adapter_device_id) or {}).get("diffs", {})
            except Exception as exc:  # noqa: BLE001
                logger.debug("apply-diff unavailable for device %s: %s", device_pk, exc)

        return JsonResponse(
            {
                "auto_apply": auto_apply,
                "changes": changes,
                "routing_changes": routing_changes,
                "routing": len(routing_changes),
                "total": len(changes) + len(routing_changes),
                "device_diff": device_diff,
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


# ── Routing state accept views (Track A) ──────────────────────────────────────


class RoutingStateAcceptMixin(NSOActionPermissionMixin, View):
    """Per-row accept for a routing state model — sets status to 'accepted' and fires push signal."""

    model_class = None

    def post(self, request, pk):  # noqa: D102
        state = get_object_or_404(self.model_class, pk=pk)
        # Matching (imported/in_sync) → nothing to apply → in_sync; differing → accepted.
        state.status = _status_after_accept(state.status)
        state.save(update_fields=["status"])
        messages.success(request, f"Accepted routing state {state.pk}.")
        return redirect(_device_nso_tab_url(state.management.device_id))


class NSOL2SapStateAcceptView(NSOActionPermissionMixin, View):
    """Accept one Nokia L2 SAP — mark owned (accepted_at) so NetBox is the source of truth.

    Saving the accepted row fires the post_save signal which pushes the device's full
    L2 SAP intent snapshot to the adapter (write path), mirroring static routes.
    """

    def post(self, request, pk):  # noqa: D102
        state = get_object_or_404(NSOL2SapState, pk=pk)
        state.status = _status_after_accept(state.status)
        state.accepted_at = timezone.now()
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


class NSOStaticRouteStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSOStaticRouteState


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
            },
        )

    def decorate_items(self, items, state):
        """Attach per-family display detail to each version item (hook; default no-op).

        The surface is family-agnostic; route-policy overrides this to attach a structured
        route-map summary so operators compare versions without reading raw JSON.
        """


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

    def get(self, request, pk):  # noqa: D102
        from .route_policy_diff import route_policy_state_diff

        state = get_object_or_404(NSORoutePolicyState, pk=pk)
        diff = route_policy_state_diff(state)
        return render(
            request,
            self.template_name,
            {
                "state": state,
                "object_name": state.object_name,
                "family": state.family.replace("_", "-"),
                "diff": diff,
                "device": getattr(state.management, "device", None),
            },
        )


class NSORedistributionDiffView(LoginRequiredMixin, View):
    """Show the device-vs-NetBox delta for a redistribution overlay row (field-level)."""

    template_name = "netbox_nso_plugin/redistribution_diff.html"

    def get(self, request, pk):  # noqa: D102
        from .route_policy_diff import redistribution_diff

        state = get_object_or_404(NSORedistributionState.objects.select_related("redistribution"), pk=pk)
        return render(
            request,
            self.template_name,
            {
                "state": state,
                "diff": redistribution_diff(state),
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
        """Trigger the appropriate intent push; override in subclasses."""

    def _after_accept(self, mgmt):
        """Run after the bulk ownership update, before the push (override in subclasses)."""

    def post(self, request, device_pk):  # noqa: D102
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
        n_owned = base.filter(status="imported").update(status="in_sync")
        n_drift = base.filter(status__in=["changed", "conflict"]).update(status="accepted")
        count = n_owned + n_drift

        if count and mgmt.adapter_device_id is not None:
            try:
                self._after_accept(mgmt)
                self._push(mgmt)
            except Exception as exc:
                logger.warning("Bulk accept push failed for device %s: %s", device_pk, exc)

        if count:
            messages.success(request, f"Accepted {count} routing state(s).")
        else:
            messages.info(request, "Nothing to accept — no drift.")
        return redirect(_device_nso_tab_url(device_pk))


class NSOStaticRouteBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSOStaticRouteState

    def _push(self, mgmt):
        from .signals import _push_static_route_intent_for_device

        _push_static_route_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


class NSOISISInterfaceBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSOISISInterfaceState

    def _push(self, mgmt):
        from .signals import _push_isis_intent_for_device

        _push_isis_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


class NSOISISInstanceBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSOISISInstanceState

    def _push(self, mgmt):
        from .signals import _push_isis_intent_for_device

        _push_isis_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


class NSOBGPPeerBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSOBGPPeerState

    def _push(self, mgmt):
        from .signals import _push_bgp_intent_for_device

        _push_bgp_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


class NSORoutePolicyBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSORoutePolicyState

    def _after_accept(self, mgmt):
        from .signals import _own_route_map_contributors

        # Owning a route-map owns its contributors — cascade for every now-owned route-map.
        owned = ("accepted", "deploying", "in_sync", "apply_failed")
        for st in NSORoutePolicyState.objects.filter(management=mgmt, family="route_map", status__in=owned):
            obj = st.assigned_object
            if obj is not None:
                _own_route_map_contributors(mgmt, obj)

    def _push(self, mgmt):
        from .signals import _push_route_policy_intent_for_device

        _push_route_policy_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


class NSOOSPFInstanceBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSOOSPFInstanceState

    def _push(self, mgmt):
        from .signals import _push_ospf_intent_for_device

        _push_ospf_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


class NSOOSPFInterfaceBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSOOSPFInterfaceState

    def _push(self, mgmt):
        from .signals import _push_ospf_intent_for_device

        _push_ospf_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


class NSORedistributionBulkAcceptView(RoutingBulkAcceptMixin):  # noqa: D101
    model_class = NSORedistributionState

    def _push(self, mgmt):
        from .signals import _push_bgp_intent_for_device, _push_isis_intent_for_device, _push_ospf_intent_for_device

        # Redistribution is distributed across destination protocols; push all three.
        for fn in (_push_ospf_intent_for_device, _push_isis_intent_for_device, _push_bgp_intent_for_device):
            fn(mgmt.device_id, mgmt.adapter_device_id)


# ── SNMP / Logging overlay accept + edit (operator modify → accept → push) ─────
# Accept marks the row owned (accepted_at + status); the device commit is deferred
# to the single device Apply — one flow, like every other scope. SNMP secrets are
# never stored; the push resolves them from Vault via each row's vault_ref.


class OverlayStateAcceptMixin(NSOActionPermissionMixin, View):
    """Per-row accept for an SNMP/logging overlay — mark owned (accepted_at + status)."""

    model_class = None

    def post(self, request, pk):  # noqa: D102
        state = get_object_or_404(self.model_class, pk=pk)
        state.status = _status_after_accept(state.status)
        state.accepted_at = timezone.now()
        state.save(update_fields=["status", "accepted_at"])
        messages.success(request, f"Accepted {state}.")
        return redirect(_device_nso_tab_url(state.management.device_id))


class NSOSnmpCommunityStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOSnmpCommunityState


class NSOSnmpV3UserStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOSnmpV3UserState


class NSOSnmpHostStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOSnmpHostState


class NSOSnmpSystemInfoStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOSnmpSystemInfoState


class NSOLoggingHostStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOLoggingHostState


class NSOSVIStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOSVIState


class NSOSubinterfaceStateAcceptView(OverlayStateAcceptMixin):  # noqa: D101
    model_class = NSOSubinterfaceState


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
        if state.l2_mtu is not None:
            iface = state.interface
            clamped = min(int(state.l2_mtu), self._NETBOX_MTU_MAX)
            if iface.mtu != clamped:
                iface.mtu = clamped
                iface.save(update_fields=["mtu"])
        state.status = _status_after_accept(state.status)
        state.accepted_at = timezone.now()
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
        if family == "route_map":
            # Owning a route-map owns its contributors too (else dangling device references).
            from .signals import _own_route_map_contributors

            _own_route_map_contributors(mgmt, obj)
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


class NSORoutePolicyCapabilityView(LoginRequiredMixin, View):
    """Operator-facing capability matrix for one device's route-policy support.

    Lists, per ``(ned_id, sw_version)``, which route-map / community constructs this device
    supports — native / translated / skipped / unsupported, with the source (probe vs a real
    device rejection). Read is cache-only (no live probe); ``?refresh=1`` (or the "Check now"
    POST) forces a fresh probe. This is the browsable companion to the attach-time block and
    the per-object panel badge.
    """

    def get(self, request, device_pk):  # noqa: D102
        mgmt = get_object_or_404(NSODeviceManagement, device_id=device_pk)
        refresh = request.GET.get("refresh") == "1"
        return render(request, "netbox_nso_plugin/route_policy_capabilities.html", self._context(mgmt, refresh))

    def post(self, request, device_pk):  # noqa: D102
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
        scopes: dict[str, list] = {"community": [], "rm-set": [], "rm-match": []}
        for el in elements:
            scopes.setdefault(el.get("scope", "other"), []).append(el)
        flagged = sum(1 for el in elements if el.get("status") in ("skipped", "unsupported"))
        return {
            "mgmt": mgmt,
            "object": mgmt.device,
            "known": result.get("known", False),
            "coverage_unknown": coverage_unknown,
            "ned_id": result.get("ned_id", ""),
            "sw_version": result.get("sw_version", ""),
            "scopes": scopes,
            "total": len(elements),
            "flagged": flagged,
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


class NSOInterfaceMtuStateEditView(generic.ObjectEditView):  # noqa: D101
    queryset = NSOInterfaceMtuState.objects.all()
    form = NSOInterfaceMtuStateForm
