# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import logging

from dcim.models import Device
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from netbox.views import generic
from utilities.views import ViewTab, register_model_view

from .adapter_client import AdapterError
from .filters import (
    NSODeviceManagementFilterSet,
    NSOInstanceFilterSet,
    NSOInterfaceStateFilterSet,
    NSOPlatformNedMappingFilterSet,
)
from .forms import AdapterConnectionForm, NSODeviceManagementForm, NSOInstanceForm, NSOPlatformNedMappingForm
from .models import (
    AdapterConnection,
    NSOBGPPeerState,
    NSODeviceManagement,
    NSOInstance,
    NSOInterfaceState,
    NSOISISInstanceState,
    NSOISISInterfaceState,
    NSOOSPFInstanceState,
    NSOOSPFInterfaceState,
    NSOPlatformNedMapping,
    NSORedistributionState,
    NSORoutePolicyState,
    NSOStaticRouteState,
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
        try:
            mgmt = device.nso_management
        except Exception:
            mgmt = None

        adapter_error = None
        adapter_error_code = None
        if mgmt is not None and mgmt.adapter_device_id is not None:
            from . import adapter_client as client

            try:
                _refresh_sync_cache(mgmt, client.get_device(mgmt.adapter_device_id))
            except AdapterError as exc:
                adapter_error = str(exc)
                adapter_error_code = exc.code
                logger.debug("Adapter unavailable for device %s: %s", device.pk, exc)

        return {
            "mgmt": mgmt,
            "nso_categories": category_summaries(device, mgmt),
            "adapter_error": adapter_error,
            "adapter_error_code": adapter_error_code,
            "status_badge": _STATUS_BADGE,
        }


# ── Lazy category load: rows for one expanded category (HTML fragment) ─────────

# "Accept" makes NetBox the source of truth, so it applies to values NetBox does not
# yet own (imported) and to drift (resolve). Already-owned states (in_sync, accepted,
# deploying, apply_failed) offer no Accept — that was the repeatable-no-op bug.
_UNOWNED_STATUSES = ("imported", "changed", "conflict", "drifted")


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


class NSOCategoryView(LoginRequiredMixin, View):
    """Return one category's rows for the device NSO tab, fetched on expand.

    The tab renders counts-first; when an operator expands a category, the browser
    GETs this view, which does a push-suppressed scoped reconcile of just that
    category and renders its partial. Keeps the page render itself counts-only.

    URL: /plugins/nso/devices/<pk>/category/<key>/
    """

    # interfaces is handled by _render_interfaces_page (paginated); the rest reconcile-on-expand.
    _PARTIALS = {
        "static": "netbox_nso_plugin/categories/static.html",
        "isis": "netbox_nso_plugin/categories/isis.html",
        "ospf": "netbox_nso_plugin/categories/ospf.html",
        "bgp": "netbox_nso_plugin/categories/bgp.html",
        "bfd": "netbox_nso_plugin/categories/bfd.html",
        "route_policy": "netbox_nso_plugin/categories/route_policy.html",
        "redistribution": "netbox_nso_plugin/categories/redistribution.html",
        "snmp": "netbox_nso_plugin/categories/snmp.html",
        "logging": "netbox_nso_plugin/categories/logging.html",
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
        if key == "interfaces":
            return self._render_interfaces_page(request, device)

        from .reconcile import reconcile_category
        from .template_content import _STATUS_BADGE

        partial = self._PARTIALS.get(key)
        if partial is None:
            return HttpResponseBadRequest(f"unknown category: {key}")

        try:
            mgmt = device.nso_management
        except Exception:
            mgmt = None

        ctx = {"object": device, "mgmt": mgmt, "status_badge": _STATUS_BADGE}
        if mgmt is not None and mgmt.adapter_device_id is not None:
            try:
                ctx.update(reconcile_category(device, mgmt, key))
            except AdapterError as exc:
                ctx["adapter_error"] = str(exc)
                ctx["adapter_error_code"] = exc.code
        ctx["category_has_unowned"] = _ctx_has_unowned(ctx)
        return render(request, partial, ctx)

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

        return render(
            request,
            "netbox_nso_plugin/categories/interfaces_page.html",
            {"object": device, "rows": rows, "page": page, "q": q, "state": state, "counts": counts},
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
        """Return derived intent templates for display in the settings page."""
        from django.apps import apps

        cfg = apps.get_app_config("netbox_nso_plugin")
        return {"derived_intent_templates": getattr(cfg, "_derived_intent_templates", [])}


# ── NSO Instance CRUD ────────────────────────────────────────────────────────


class NSOInstanceListView(generic.ObjectListView):
    """List view for NSO instances."""

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


class NSOOnboardView(LoginRequiredMixin, View):
    """POST action: onboard one candidate device into NSO, then redirect to the dashboard.

    URL: POST /plugins/nso/onboard/  body: device=<pk>, instance=<adapter_instance_id>
    """

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
            messages.success(request, f"Onboarded {device} into NSO ({instance.name}).")
        else:
            messages.error(request, f"Could not onboard {device}: {result['error']}")
        return redirect(f"{redirect_url}?instance={instance.adapter_instance_id}")


class NSOQuickManageView(LoginRequiredMixin, View):
    """POST action: bring an already-in-NSO ('external') device under plugin management.

    The device exists in both NSO and NetBox but has no NSODeviceManagement record.
    Creates that record (no re-provisioning) and redirects to the dashboard.

    URL: POST /plugins/nso/manage/  body: device=<pk>, instance=<adapter_instance_id>,
    nso_name=<NSO device name>
    """

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


class NSOPlatformNedMappingListView(generic.ObjectListView):
    """List view for Platform→NED mappings."""

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


class NSODeviceActionView(LoginRequiredMixin, View):
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
        try:
            mgmt = device.nso_management
        except Exception:
            mgmt = None
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


class NSORefreshStateView(LoginRequiredMixin, View):
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


class NSODeviceReconcileView(LoginRequiredMixin, View):
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
    """List view for NSOInterfaceState records."""

    queryset = NSOInterfaceState.objects.select_related("interface")
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


class NSOAcceptAttributeView(LoginRequiredMixin, View):
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


class NSOAcceptDeviceView(LoginRequiredMixin, View):
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


class NSOInterfaceEditFieldView(LoginRequiredMixin, View):
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


class NSOBulkAcceptView(LoginRequiredMixin, View):
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


class NSOApplyPreviewView(LoginRequiredMixin, View):
    """JSON preview of what 'Apply Intent' would push to the device.

    Lists the pending-apply changes (NetBox intent that differs from the device) so the
    operator can confirm before pushing. Drives the apply-confirmation modal.
    """

    def get(self, request, device_pk):
        """Return {auto_apply, changes:[{interface, attribute, device, netbox}], routing}."""
        from django.http import JsonResponse

        device = get_object_or_404(Device, pk=device_pk)
        try:
            mgmt = device.nso_management
        except Exception:
            mgmt = None
        auto_apply = bool(mgmt and mgmt.auto_apply)

        changes = []
        pending = (
            NSOInterfaceState.objects.filter(
                interface__device_id=device_pk, status__in=("accepted", "apply_failed", "drifted")
            )
            .select_related("interface")
            .order_by("interface__name", "attribute")
        )
        for st in pending:
            iface = st.interface
            if st.attribute == "description":
                netbox_val = iface.description or "—"
            elif st.attribute == "enabled":
                netbox_val = "Yes" if iface.enabled else "No"
            else:
                netbox_val = "—"
            changes.append(
                {
                    "interface": iface.name,
                    "attribute": st.attribute,
                    "device": st.nso_value or "—",
                    "netbox": netbox_val,
                }
            )

        # Routing pending counts (overlays don't carry a simple value pair to diff).
        routing = 0
        for model in (
            NSOStaticRouteState,
            NSOISISInterfaceState,
            NSOISISInstanceState,
            NSOBGPPeerState,
            NSORoutePolicyState,
            NSOOSPFInstanceState,
            NSOOSPFInterfaceState,
            NSORedistributionState,
        ):
            if mgmt is not None:
                routing += model.objects.filter(
                    management=mgmt, status__in=("accepted", "apply_failed", "drifted")
                ).count()

        return JsonResponse(
            {"auto_apply": auto_apply, "changes": changes, "routing": routing, "total": len(changes) + routing}
        )


# ── M13: IP auto-assignment operator actions ──────────────────────────────────


class NSOAutoAssignIPView(LoginRequiredMixin, View):
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


class RoutingStateAcceptMixin(LoginRequiredMixin, View):
    """Per-row accept for a routing state model — sets status to 'accepted' and fires push signal."""

    model_class = None

    def post(self, request, pk):  # noqa: D102
        state = get_object_or_404(self.model_class, pk=pk)
        # Matching (imported/in_sync) → nothing to apply → in_sync; differing → accepted.
        state.status = _status_after_accept(state.status)
        state.save(update_fields=["status"])
        messages.success(request, f"Accepted routing state {state.pk}.")
        return redirect(_device_nso_tab_url(state.management.device_id))


class NSOStaticRouteStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSOStaticRouteState


class NSOISISInterfaceStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSOISISInterfaceState


class NSOISISInstanceStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSOISISInstanceState


class NSOBGPPeerStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSOBGPPeerState


class NSORoutePolicyStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSORoutePolicyState


class NSOOSPFInstanceStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSOOSPFInstanceState


class NSOOSPFInterfaceStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSOOSPFInterfaceState


class NSORedistributionStateAcceptView(RoutingStateAcceptMixin):  # noqa: D101
    model_class = NSORedistributionState


# ── Routing bulk accept views (Track A) ───────────────────────────────────────


class RoutingBulkAcceptMixin(LoginRequiredMixin, View):
    """Bulk 'Keep NetBox' for all DRIFTED routing rows of a device, then push intent."""

    model_class = None

    def _push(self, mgmt):
        """Trigger the appropriate intent push; override in subclasses."""

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
        n_drift = base.filter(status__in=["changed", "conflict", "drifted"]).update(status="accepted")
        count = n_owned + n_drift

        if count and mgmt.adapter_device_id is not None:
            try:
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
