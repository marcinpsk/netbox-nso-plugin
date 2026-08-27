# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import status
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..filters import (
    NSODerivedIntentTemplateFilterSet,
    NSODeviceManagementFilterSet,
    NSOInstanceFilterSet,
    NSOInterfaceStateFilterSet,
    NSOLinkRoleAssignmentFilterSet,
    NSOLinkRoleFilterSet,
    NSOPlatformNedMappingFilterSet,
)
from ..models import (
    NSODerivedIntentTemplate,
    NSODeviceManagement,
    NSOInstance,
    NSOInterfaceState,
    NSOLinkRole,
    NSOLinkRoleAssignment,
    NSOPlatformNedMapping,
)
from .serializers import (
    NSODerivedIntentTemplateSerializer,
    NSODeviceManagementSerializer,
    NSOInstanceSerializer,
    NSOInterfaceStateSerializer,
    NSOLinkRoleAssignmentSerializer,
    NSOLinkRoleSerializer,
    NSOPlatformNedMappingSerializer,
)


class NSODerivedIntentTemplateViewSet(NetBoxModelViewSet):
    """REST API for database-managed derived-intent templates."""

    queryset = NSODerivedIntentTemplate.objects.all()
    serializer_class = NSODerivedIntentTemplateSerializer
    filterset_class = NSODerivedIntentTemplateFilterSet


class NSOPlatformNedMappingViewSet(NetBoxModelViewSet):
    """REST API for NSOPlatformNedMapping — CICD-editable platform→NED map."""

    queryset = NSOPlatformNedMapping.objects.select_related("platform")
    serializer_class = NSOPlatformNedMappingSerializer
    filterset_class = NSOPlatformNedMappingFilterSet


class NSOInstanceViewSet(NetBoxModelViewSet):
    """REST API for NSOInstance objects."""

    queryset = NSOInstance.objects.all()
    serializer_class = NSOInstanceSerializer
    filterset_class = NSOInstanceFilterSet


class NSODeviceManagementViewSet(NetBoxModelViewSet):
    """REST API for NSODeviceManagement — the adapter reads this to reconcile scope."""

    queryset = NSODeviceManagement.objects.select_related("device", "nso_instance")
    serializer_class = NSODeviceManagementSerializer
    filterset_class = NSODeviceManagementFilterSet

    def perform_create(self, serializer):
        """Route API creates through the exact management writer."""
        from ..management_lifecycle import management_crud_writes

        with management_crud_writes():
            return super().perform_create(serializer)

    def perform_update(self, serializer):
        """Route API updates through the exact management writer."""
        from ..management_lifecycle import management_crud_writes

        with management_crud_writes():
            return super().perform_update(serializer)

    def perform_destroy(self, instance):
        """Route API deletes through the exact management writer."""
        from ..management_lifecycle import management_crud_writes

        with management_crud_writes():
            return super().perform_destroy(instance)


class NSOInterfaceStateViewSet(NetBoxModelViewSet):
    """REST API for NSOInterfaceState — per-interface intent status overlay.

    The adapter's scope reconciler also reads this to mirror intent (decision L).
    Endpoint: /api/plugins/nso/interface-state/
    """

    queryset = NSOInterfaceState.objects.select_related("interface")
    serializer_class = NSOInterfaceStateSerializer
    filterset_class = NSOInterfaceStateFilterSet

    def perform_update(self, serializer):
        """Apply an API overlay update through one exact renderer plan."""
        import copy

        from django.core.exceptions import FieldDoesNotExist

        from ..renderer_writer import RendererMutationPlan, planned_save, renderer_mirror_writes, renderer_writes

        candidate = copy.copy(serializer.instance)
        if "status" in serializer.validated_data:
            candidate._nso_explicit_status_update = True
        deferred_m2m = []
        for field_name, value in serializer.validated_data.items():
            try:
                field = candidate._meta.get_field(field_name)
            except FieldDoesNotExist:
                setattr(candidate, field_name, value)
                continue
            if field.many_to_many:
                deferred_m2m.append((field_name, value))
                continue
            setattr(candidate, field_name, value)

        plan = RendererMutationPlan.build(saves=(planned_save(candidate),))
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
        with mutation as writer:
            writer.save(candidate)
        for field_name, value in deferred_m2m:
            getattr(candidate, field_name).set(value)
        serializer.instance = candidate


class NSOLinkRoleViewSet(NetBoxModelViewSet):
    """REST API for NSOLinkRole — the configurable provisioning catalog."""

    queryset = NSOLinkRole.objects.select_related("ipv4_pool_prefix", "ipv6_pool_prefix")
    serializer_class = NSOLinkRoleSerializer
    filterset_class = NSOLinkRoleFilterSet


class NSOLinkRoleAssignmentViewSet(NetBoxModelViewSet):
    """REST API for NSOLinkRoleAssignment — role ← cable or interface."""

    queryset = NSOLinkRoleAssignment.objects.select_related("role", "cable", "interface")
    serializer_class = NSOLinkRoleAssignmentSerializer
    filterset_class = NSOLinkRoleAssignmentFilterSet


class OnboardingCandidatesView(APIView):
    """CICD-facing read of onboarding state for an NSO instance.

    ``GET /api/plugins/nso/onboarding-candidates/?instance=<id>``
    returns the same three buckets as the dashboard (onboarded / candidates /
    orphans) as JSON, so a pipeline can discover which staged devices are ready to
    onboard. ``instance`` defaults to the default NSO instance.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Return {instance, error, onboarded, candidates, orphans} for the instance."""
        from ..onboarding import build_onboarding_dashboard

        selected = request.query_params.get("instance")
        instance = None
        if selected:
            instance = NSOInstance.objects.filter(adapter_instance_id=selected).first()
            if instance is None:
                return Response({"detail": f"unknown instance '{selected}'"}, status=status.HTTP_404_NOT_FOUND)
        else:
            instance = NSOInstance.get_default()
        if instance is None:
            return Response({"detail": "no NSO instance configured"}, status=status.HTTP_404_NOT_FOUND)

        data = build_onboarding_dashboard(instance)
        return Response(
            {
                "instance": data["instance"],
                "error": data["error"],
                "onboarded": [
                    {
                        "nso_name": o["nso_name"],
                        "ned_id": o["ned_id"],
                        "admin_state": o["admin_state"],
                        "netbox_device_id": o["netbox_device"].id if o["netbox_device"] else None,
                        "plugin_managed": o["plugin_managed"],
                    }
                    for o in data["onboarded"]
                ],
                "candidates": [
                    {
                        "netbox_device_id": c["device"].id,
                        "name": c["device"].name,
                        "platform": str(c["platform"]) if c["platform"] else None,
                        # primary_ip kept for backward compat (None for an OOB-only candidate);
                        # mgmt_ip is the address onboarding uses (primary or OOB), oob_only flags it.
                        "primary_ip": None if c["oob_only"] else c["mgmt_ip"],
                        "mgmt_ip": c["mgmt_ip"],
                        "oob_only": c["oob_only"],
                        "ned_id": c["ned_id"],
                    }
                    for c in data["candidates"]
                ],
                "orphans": [
                    {"nso_name": r["nso_name"], "ned_id": r["ned_id"], "address": r["address"]} for r in data["orphans"]
                ],
            }
        )


class HasNSOChangePermission(BasePermission):
    """Require the NetBox ``change_nsodevicemanagement`` permission (mirrors the UI gate).

    Onboarding provisions a device into NSO (create node → fetch-host-keys → unlock →
    sync-from), so — like the UI's ``NSOActionPermissionMixin`` — it must not be open to any
    authenticated API token; a read-only monitoring token must not be able to trigger it.
    """

    message = "This action requires the netbox_nso_plugin.change_nsodevicemanagement permission."

    def has_permission(self, request, view):  # noqa: D102
        user = request.user
        return bool(user and user.is_authenticated and user.has_perm("netbox_nso_plugin.change_nsodevicemanagement"))


class OnboardView(APIView):
    """CICD-facing onboard action.

    ``POST /api/plugins/nso/onboard/`` body:
    ``{"netbox_device_id": <int>, "instance": "<adapter_instance_id>"?}``.
    Provisions the device into NSO (create node → fetch-host-keys → unlock →
    sync-from) and creates the management row. Returns the step-by-step result;
    400 on pre-flight failure (no NED mapping / no primary IP / already managed).
    """

    permission_classes = [HasNSOChangePermission]

    def post(self, request):
        """Onboard the posted NetBox device into the selected (or default) NSO instance."""
        from dcim.models import Device

        from ..onboarding import onboard_candidate

        device_id = request.data.get("netbox_device_id")
        if device_id is None:
            return Response({"detail": "netbox_device_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        device = Device.objects.filter(pk=device_id).first()
        if device is None:
            return Response({"detail": f"device {device_id} not found"}, status=status.HTTP_404_NOT_FOUND)

        selected = request.data.get("instance")
        instance = None
        if selected:
            instance = NSOInstance.objects.filter(adapter_instance_id=selected).first()
            if instance is None:
                return Response({"detail": f"unknown instance '{selected}'"}, status=status.HTTP_404_NOT_FOUND)
        else:
            instance = NSOInstance.get_default()
        if instance is None:
            return Response({"detail": "no NSO instance configured"}, status=status.HTTP_404_NOT_FOUND)

        result = onboard_candidate(device, instance)
        code = status.HTTP_200_OK if result["ok"] else status.HTTP_400_BAD_REQUEST
        return Response(result, status=code)


class SyncCompleteView(APIView):
    """Adapter → plugin callback: a device sync finished, refresh its NSO*State cache.

    POSTed by the adapter at the end of each ``sync_device`` so the plugin reconciles
    adapter state into its display tables OFF the request path — instead of doing it
    (slowly, with write-on-read) every time an operator opens the NSO tab. Enqueues a
    deduped background reconcile and returns 202 immediately so the adapter never
    waits on the reconcile.

    Endpoint: ``POST /api/plugins/nso/sync-complete/``
    Body: ``{"netbox_device_id": <int>}`` (or ``{"adapter_device_id": <int>}``).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Resolve the device and enqueue its background reconcile."""
        from ..reconcile import enqueue_device_reconcile

        device_id = request.data.get("netbox_device_id")
        adapter_device_id = request.data.get("adapter_device_id")

        if device_id is None and adapter_device_id is None:
            return Response(
                {"detail": "netbox_device_id or adapter_device_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if device_id is None:
            mgmt = NSODeviceManagement.objects.filter(adapter_device_id=adapter_device_id).first()
            if mgmt is None:
                return Response(
                    {"detail": f"no managed device for adapter_device_id={adapter_device_id}"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            device_id = mgmt.device_id

        enqueue_device_reconcile(int(device_id))
        return Response({"queued": True, "netbox_device_id": int(device_id)}, status=status.HTTP_202_ACCEPTED)


class ProvisionCompleteView(APIView):
    """Adapter → plugin callback: a device-provision job finished, advance its onboarding row.

    POSTed by the adapter when a provision job reaches a terminal state (succeeded / failed /
    timeout). The plugin advances the gated NSODeviceManagement row OFF the request path — flipping
    it to ready (→ the un-gated signal maps/scopes/syncs) or provision_failed — instead of relying
    on the dashboard poll being open when the job completes. Enqueues a background advance and
    returns 202; the device-tab self-heal and hourly sweep remain as backstops for a missed call.

    Endpoint: ``POST /api/plugins/nso/provision-complete/``
    Body: ``{"provision_job_id": <int>}`` — the adapter provision job id (== the row's onboard_job_id).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Resolve the onboarding row by its provision job id and enqueue its advance."""
        from ..reconcile import enqueue_onboard_advance

        job_id = request.data.get("provision_job_id")
        if job_id in (None, ""):
            return Response({"detail": "provision_job_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        mgmt = NSODeviceManagement.objects.filter(onboard_job_id=str(job_id)).first()
        if mgmt is None:
            # Unknown / untracked provision job (row deleted, or a provision not driven by the
            # plugin) — ack so the best-effort adapter callback does not treat it as retryable.
            return Response(
                {"queued": False, "detail": f"no onboarding row for provision_job_id={job_id}"},
                status=status.HTTP_202_ACCEPTED,
            )

        enqueue_onboard_advance(mgmt.id)
        return Response({"queued": True, "mgmt_id": mgmt.id}, status=status.HTTP_202_ACCEPTED)
