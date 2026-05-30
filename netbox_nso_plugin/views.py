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
from .models import AdapterConnection, NSODeviceManagement, NSOInstance, NSOInterfaceState
from .tables import NSODeviceManagementTable, NSOInstanceTable, NSOInterfaceStateTable

logger = logging.getLogger(__name__)


def _device_nso_tab_url(device_pk):
    """Return the URL for the NSO tab on a device detail page."""
    return reverse("dcim:device_nso", kwargs={"pk": device_pk})


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
        interface_states: dict = {}
        snmp_data: dict = {}
        static_routes: list = []
        isis_interfaces: list = []
        isis_processes: list = []
        route_policy_states: list = []
        ospf_data: dict = {"instances": [], "interfaces": []}
        redistribution_states: list = []

        if mgmt is not None and mgmt.adapter_device_id is not None:
            from . import adapter_client as client

            try:
                adapter_device = client.get_device(mgmt.adapter_device_id)
                interfaces = client.get_interfaces(mgmt.adapter_device_id)
                compliance = client.get_compliance(mgmt.adapter_device_id)
                interface_states = _upsert_interface_states(device, interfaces)

                from .route_policy_reconciler import reconcile_route_policy
                from .template_content import (
                    _reconcile_isis_interfaces,
                    _reconcile_isis_process,
                    _reconcile_ospf,
                    _reconcile_redistribution,
                    _reconcile_snmp_config,
                    _reconcile_static_routes,
                )

                snmp_payload = client.get_snmp_config(mgmt.adapter_device_id)
                snmp_data = _reconcile_snmp_config(device, snmp_payload)

                sr_payload = client.get_static_routes(mgmt.adapter_device_id)
                static_routes = _reconcile_static_routes(device, sr_payload)

                isis_payload = client.get_isis_interfaces(mgmt.adapter_device_id)
                isis_interfaces = _reconcile_isis_interfaces(device, isis_payload.get("interfaces", []))
                isis_processes = _reconcile_isis_process(device, isis_payload.get("processes", []))

                rp_payload = client.get_route_policy(mgmt.adapter_device_id)
                route_policy_states = reconcile_route_policy(device, rp_payload)

                ospf_payload = client.get_ospf(mgmt.adapter_device_id)
                ospf_data = _reconcile_ospf(device, ospf_payload)

                redistribution_payload = client.get_redistribution(mgmt.adapter_device_id)
                redistribution_states = _reconcile_redistribution(device, redistribution_payload)

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
                    NSODeviceManagement.objects.filter(pk=mgmt.pk).update(
                        **{f: getattr(mgmt, f) for f in update_fields}
                    )
            except AdapterError as exc:
                adapter_error = str(exc)
                logger.debug("Adapter unavailable for device %s: %s", device.pk, exc)
                snapshot = mgmt.compliance_snapshot or {}
                interfaces = snapshot.get("interfaces")
                compliance = snapshot.get("compliance")

        return {
            "mgmt": mgmt,
            "interfaces": interfaces,
            "compliance": compliance,
            "adapter_error": adapter_error,
            "interface_states": interface_states,
            "status_badge": _STATUS_BADGE,
            "snmp_data": snmp_data,
            "static_routes": static_routes,
            "isis_interfaces": isis_interfaces,
            "isis_processes": isis_processes,
            "route_policy_states": route_policy_states,
            "ospf_data": ospf_data,
            "redistribution_states": redistribution_states,
        }


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
    """List view for managed NSO devices."""

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

        if instance.adapter_device_id is not None:
            try:
                interfaces = client.get_interfaces(instance.adapter_device_id)
                compliance = client.get_compliance(instance.adapter_device_id)
            except AdapterError as exc:
                adapter_error = str(exc)
                # Fall back to snapshot so the page remains useful
                snapshot = instance.compliance_snapshot or {}
                interfaces = snapshot.get("interfaces")
                compliance = snapshot.get("compliance")

        return {
            "interfaces": interfaces,
            "compliance": compliance,
            "adapter_error": adapter_error,
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

        if action not in _ACTION_LABELS:
            messages.error(request, f"Unknown action: {action}")
            return redirect(mgmt.device.get_absolute_url())

        if mgmt.adapter_device_id is None:
            messages.warning(request, "Device is not yet onboarded to the adapter.")
            return redirect(_device_nso_tab_url(mgmt.device.pk))

        action_fn = {
            "sync": client.trigger_sync,
            "check-compliance": client.trigger_check_compliance,
            "connect": client.trigger_connect,
            "apply": client.trigger_apply,
        }[action]

        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

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
