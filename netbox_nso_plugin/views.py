# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import logging

from dcim.models import Device
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from netbox.views import generic
from utilities.views import ViewTab, register_model_view

from .adapter_client import AdapterError
from .filters import NSODeviceManagementFilterSet, NSOInstanceFilterSet, NSOInterfaceStateFilterSet
from .forms import AdapterConnectionForm, NSODeviceManagementForm, NSOInstanceForm
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
    NSORedistributionState,
    NSORoutePolicyState,
    NSOStaticRouteState,
)
from .tables import NSODeviceManagementTable, NSOInstanceTable, NSOInterfaceStateTable

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
        """Fetch NSO management status, interfaces, compliance, and SNMP from the adapter."""
        from .adapter_client import AdapterError
        from .template_content import _STATUS_BADGE, _upsert_interface_states

        device = instance
        try:
            mgmt = device.nso_management
        except Exception:
            mgmt = None

        interfaces = None
        compliance = None
        adapter_error = None
        adapter_error_code = None
        interface_states: dict = {}
        snmp_data: dict = {}
        routing = self._empty_routing_context()

        if mgmt is not None and mgmt.adapter_device_id is not None:
            from . import adapter_client as client

            try:
                adapter_device = client.get_device(mgmt.adapter_device_id)

                from .template_content import _reconcile_snmp_config

                # Only fetch the scopes this device opted into. Each scope is
                # gated by its master (manage_interfaces / manage_routing) and,
                # for routing, its per-protocol leaf flag — the kill-switch
                # model (see NSODeviceManagement.managed_scopes).
                if mgmt.manage_interfaces:
                    interfaces = client.get_interfaces(mgmt.adapter_device_id)
                    compliance = client.get_compliance(mgmt.adapter_device_id)
                    interface_states = _upsert_interface_states(device, interfaces)

                if mgmt.manage_snmp:
                    snmp_payload = client.get_snmp_config(mgmt.adapter_device_id)
                    snmp_data = _reconcile_snmp_config(device, snmp_payload)

                routing = self._reconcile_routing_scopes(device, mgmt, client)

                _refresh_sync_cache(mgmt, adapter_device)
            except AdapterError as exc:
                adapter_error = str(exc)
                adapter_error_code = exc.code
                logger.debug("Adapter unavailable for device %s: %s", device.pk, exc)
                snapshot = mgmt.compliance_snapshot or {}
                interfaces = snapshot.get("interfaces")
                compliance = snapshot.get("compliance")

        return {
            "mgmt": mgmt,
            "interfaces": interfaces,
            "compliance": compliance,
            "adapter_error": adapter_error,
            "adapter_error_code": adapter_error_code,
            "interface_states": interface_states,
            "status_badge": _STATUS_BADGE,
            "snmp_data": snmp_data,
            **routing,
        }

    @staticmethod
    def _empty_routing_context():
        """Default (no-op) routing-scope context used before/without reconcile."""
        return {
            "static_routes": [],
            "isis_interfaces": [],
            "isis_processes": [],
            "route_policy_states": [],
            "ospf_data": {"instances": [], "interfaces": []},
            "redistribution_states": [],
            "bgp_peers": [],
        }

    def _reconcile_routing_scopes(self, device, mgmt, client):
        """Reconcile each opted-in routing protocol for this device.

        Every protocol is gated by its master (manage_routing) AND its leaf
        flag — the kill-switch model (see NSODeviceManagement.managed_scopes).
        """
        from .bgp_reconciler import _reconcile_bgp_config
        from .redistribution_reconciler import reconcile_redistribution
        from .route_policy_reconciler import reconcile_route_policy
        from .template_content import (
            _reconcile_isis_interfaces,
            _reconcile_isis_process,
            _reconcile_ospf,
            _reconcile_static_routes,
        )

        ctx = self._empty_routing_context()
        if not mgmt.manage_routing:
            return ctx
        dev_id = mgmt.adapter_device_id

        if mgmt.manage_static:
            ctx["static_routes"] = _reconcile_static_routes(device, client.get_static_routes(dev_id))
        if mgmt.manage_isis:
            isis_payload = client.get_isis_interfaces(dev_id)
            ctx["isis_interfaces"] = _reconcile_isis_interfaces(device, isis_payload.get("interfaces", []))
            ctx["isis_processes"] = _reconcile_isis_process(device, isis_payload.get("processes", []))
        if mgmt.manage_route_policy:
            ctx["route_policy_states"] = reconcile_route_policy(device, client.get_route_policy(dev_id))
        if mgmt.manage_ospf:
            ctx["ospf_data"] = _reconcile_ospf(device, client.get_ospf(dev_id))
        if mgmt.manage_redistribution:
            ctx["redistribution_states"] = reconcile_redistribution(device, client.get_redistribution(dev_id))
        if mgmt.manage_bgp:
            ctx["bgp_peers"] = _reconcile_bgp_config(device, client.get_bgp_config(dev_id))
        return ctx


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
                compliance = client.get_compliance(instance.adapter_device_id)
            except AdapterError as exc:
                adapter_error = str(exc)
                adapter_error_code = exc.code
                # Fall back to snapshot so the page remains useful
                snapshot = instance.compliance_snapshot or {}
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
    "check-compliance": "Check Compliance",
    "connect": "Test Connection",
    "apply": "Apply Intent",
}


class NSODeviceActionView(LoginRequiredMixin, View):
    """Trigger an adapter action (sync / check-compliance / connect) via POST."""

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
            "check-compliance": client.trigger_check_compliance,
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


class NSORefreshComplianceView(LoginRequiredMixin, View):
    """Fetch live compliance + interface data from the adapter and cache it."""

    def post(self, request, pk):
        """Call the adapter and update compliance_snapshot on the management record."""
        from . import adapter_client as client

        mgmt = get_object_or_404(NSODeviceManagement, pk=pk)

        if mgmt.adapter_device_id is None:
            messages.warning(request, "Device is not yet onboarded to the adapter.")
            return redirect(_device_nso_tab_url(mgmt.device.pk))

        try:
            compliance = client.get_compliance(mgmt.adapter_device_id)
            interfaces = client.get_interfaces(mgmt.adapter_device_id)
            NSODeviceManagement.objects.filter(pk=mgmt.pk).update(
                compliance_snapshot={
                    "compliance": compliance,
                    "interfaces": interfaces,
                    "refreshed_at": timezone.now().isoformat(),
                }
            )
            messages.success(request, "Compliance data refreshed.")
        except AdapterError as exc:
            messages.error(request, f"Could not reach adapter: {exc}")

        return redirect(_device_nso_tab_url(mgmt.device.pk))


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
    """Build full intent snapshot for a device and push it to the adapter."""
    from . import adapter_client as client

    try:
        mgmt = NSODeviceManagement.objects.select_related("nso_instance").get(device_id=device_id)
    except NSODeviceManagement.DoesNotExist:
        logger.warning("No NSODeviceManagement for device %s, skipping intent push", device_id)
        return

    if mgmt.adapter_device_id is None:
        return

    states = NSOInterfaceState.objects.filter(
        interface__device_id=device_id,
        status="accepted",
    ).select_related("interface")

    attributes = []
    for state in states:
        iface = state.interface
        if state.attribute == "description":
            intent_value = iface.description or ""
        elif state.attribute == "enabled":
            intent_value = str(iface.enabled).lower()
        else:
            continue

        attributes.append(
            {
                "interface": iface.name,
                "attribute": state.attribute,
                "intent_value": intent_value,
                "accepted_at": state.accepted_at.isoformat() if state.accepted_at else None,
            }
        )

    try:
        client.put_intent(mgmt.adapter_device_id, attributes)
    except Exception as exc:
        logger.warning("Failed to push intent for device %s: %s", device_id, exc)


class NSOAcceptAttributeView(LoginRequiredMixin, View):
    """Accept a single interface attribute — sets status to 'accepted' and pushes intent."""

    def post(self, request, pk):
        """Accept the interface state and push the updated intent snapshot to the adapter.

        Note: the post_save signal on NSOInterfaceState (push_intent_on_accept) handles
        the adapter PUT /intent call, so we do not call _push_intent_for_device here.
        """
        state = get_object_or_404(NSOInterfaceState, pk=pk)
        state.status = "accepted"
        state.accepted_at = timezone.now()
        state.save(update_fields=["status", "accepted_at"])

        messages.success(request, f"Accepted {state.attribute} on {state.interface}.")
        return redirect(_device_nso_tab_url(state.interface.device_id))


class NSOBulkAcceptView(LoginRequiredMixin, View):
    """Bulk-accept all 'changed' interface states for a device and push a single intent snapshot."""

    def post(self, request, device_pk):
        """Accept all changed states for the given device."""
        now = timezone.now()
        updated = NSOInterfaceState.objects.filter(
            interface__device_id=device_pk,
            status__in=["changed", "imported"],
        ).update(status="accepted", accepted_at=now)

        if updated:
            _push_intent_for_device(device_pk)
            messages.success(request, f"Accepted {updated} interface attribute(s).")
        else:
            messages.info(request, "No changed attributes to accept.")

        device = get_object_or_404(Device, pk=device_pk)
        return redirect(_device_nso_tab_url(device.pk))


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
        state.status = "accepted"
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
    """Bulk-accept all 'imported'/'in_sync' routing state rows for a device and push intent."""

    model_class = None

    def _push(self, mgmt):
        """Trigger the appropriate intent push; override in subclasses."""

    def post(self, request, device_pk):  # noqa: D102
        try:
            mgmt = NSODeviceManagement.objects.get(device_id=device_pk)
        except NSODeviceManagement.DoesNotExist:
            messages.warning(request, "Device is not NSO-managed.")
            return redirect(_device_nso_tab_url(device_pk))

        qs = self.model_class.objects.filter(
            management=mgmt,
            status__in=["imported", "in_sync"],
        )
        count = qs.count()
        qs.update(status="accepted")

        if count and mgmt.adapter_device_id is not None:
            try:
                self._push(mgmt)
            except Exception as exc:
                logger.warning("Bulk accept push failed for device %s: %s", device_pk, exc)

        if count:
            messages.success(request, f"Accepted {count} routing state(s).")
        else:
            messages.info(request, "No states to accept.")
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
