# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import logging

from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import status
from rest_framework.exceptions import NotFound, ParseError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.routers import APIRootView
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
    NSOBGPRouterState,
    NSODerivedIntentTemplate,
    NSODeviceManagement,
    NSOInstance,
    NSOInterfaceState,
    NSOLinkRole,
    NSOLinkRoleAssignment,
    NSOPlatformNedMapping,
)
from ..native_authorization import permissions_violation
from ..request_authorization import (
    ApiOnboarding,
    AuthorizedAPIView,
    Collection,
    EndpointPolicy,
    IntegrationProvisionAttempt,
    IntegrationSync,
    OwnerRow,
    generic_policy,
    is_valid_instance_id,
    selected_instance,
)
from .serializers import (
    NSOBGPRouterStateSerializer,
    NSODerivedIntentTemplateSerializer,
    NSODeviceManagementSerializer,
    NSOInstanceSerializer,
    NSOInterfaceStateSerializer,
    NSOLinkRoleAssignmentSerializer,
    NSOLinkRoleSerializer,
    NSOPlatformNedMappingSerializer,
)

logger = logging.getLogger(__name__)

_SYNC_DEFERRED = "Device reconcile is deferred while the deployment gate is active"


class _ProjectedPageMixin:
    """Load the refusal evidence of one list page in batched queries."""

    def paginate_queryset(self, queryset):
        from ..refusal_display import project_rows

        page = super().paginate_queryset(queryset)
        return None if page is None else project_rows(page)


class NSOAPIRootView(AuthorizedAPIView, APIRootView):
    """List the plugin API endpoints. The index names routes only."""

    policy = EndpointPolicy(Collection(), "read")


class NSOBGPRouterStateViewSet(AuthorizedAPIView, _ProjectedPageMixin, NetBoxModelViewSet):
    """Expose router observations and lifecycle through the overlay API."""

    policy = generic_policy()

    queryset = NSOBGPRouterState.objects.select_related("management", "bgp_router")
    serializer_class = NSOBGPRouterStateSerializer
    http_method_names = ("get", "head", "options")

    def get_queryset(self):
        from ..views import _visible_bgp_router_states

        return _visible_bgp_router_states(self.request.user)


class OwnerStateView(AuthorizedAPIView, APIView):
    """Read a registered owner's projected state through its model serializer."""

    permission_classes = [IsAuthenticated]
    policy = EndpointPolicy(OwnerRow(), "read")

    def get(self, request, model, pk):
        from . import serializers

        owner = self.target.primary
        serializer = getattr(serializers, type(owner).__name__ + "Serializer")
        return Response(serializer(owner, context={"request": request}).data)


class NSODerivedIntentTemplateViewSet(AuthorizedAPIView, NetBoxModelViewSet):
    """REST API for database-managed derived-intent templates."""

    policy = generic_policy()

    queryset = NSODerivedIntentTemplate.objects.all()
    serializer_class = NSODerivedIntentTemplateSerializer
    filterset_class = NSODerivedIntentTemplateFilterSet


class NSOPlatformNedMappingViewSet(AuthorizedAPIView, NetBoxModelViewSet):
    """REST API for NSOPlatformNedMapping — CICD-editable platform→NED map."""

    policy = generic_policy()

    queryset = NSOPlatformNedMapping.objects.select_related("platform")
    serializer_class = NSOPlatformNedMappingSerializer
    filterset_class = NSOPlatformNedMappingFilterSet


class NSOInstanceViewSet(AuthorizedAPIView, NetBoxModelViewSet):
    """REST API for NSOInstance objects."""

    policy = generic_policy()

    queryset = NSOInstance.objects.all()
    serializer_class = NSOInstanceSerializer
    filterset_class = NSOInstanceFilterSet


class NSODeviceManagementViewSet(AuthorizedAPIView, NetBoxModelViewSet):
    """REST API for NSODeviceManagement — the adapter reads this to reconcile scope."""

    policy = generic_policy()

    queryset = NSODeviceManagement.objects.select_related("device", "nso_instance")
    serializer_class = NSODeviceManagementSerializer
    filterset_class = NSODeviceManagementFilterSet


class NSOInterfaceStateViewSet(AuthorizedAPIView, _ProjectedPageMixin, NetBoxModelViewSet):
    """REST API for NSOInterfaceState — per-interface intent status overlay.

    The adapter's scope reconciler also reads this to mirror intent (decision L).
    Endpoint: /api/plugins/nso/interface-state/
    """

    policy = generic_policy()

    queryset = NSOInterfaceState.objects.select_related("interface")
    serializer_class = NSOInterfaceStateSerializer
    filterset_class = NSOInterfaceStateFilterSet

    def perform_update(self, serializer):
        """Apply an API overlay update through one exact renderer plan."""
        import copy

        from django.core.exceptions import FieldDoesNotExist

        from ..intent_state import IntentMutationProtocolError, normalize_overlay_lifecycle
        from ..renderer_writer import RendererMutationPlan, planned_save, renderer_mirror_writes, renderer_writes

        candidate = copy.copy(serializer.instance)
        for field_name, value in serializer.validated_data.items():
            try:
                field = candidate._meta.get_field(field_name)
            except FieldDoesNotExist:
                if field_name in {"add_tags", "remove_tags"}:
                    continue
                if field_name == "changelog_message":
                    continue
                raise IntentMutationProtocolError(f"unsupported serializer-only field {field_name!r}")
            if field.many_to_many:
                if field_name != "tags":
                    raise IntentMutationProtocolError(f"unsupported deferred M2M field {field_name!r}")
                continue
            setattr(candidate, field_name, value)

        if "status" not in serializer.validated_data:
            normalize_overlay_lifecycle(candidate, serializer.validated_data)
        plan = RendererMutationPlan.build(saves=(planned_save(candidate),))
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
        serializer.instance = candidate
        with mutation as writer:
            writer.save_via(candidate, serializer.save)


class NSOLinkRoleViewSet(AuthorizedAPIView, NetBoxModelViewSet):
    """REST API for NSOLinkRole — the configurable provisioning catalog."""

    policy = generic_policy()

    queryset = NSOLinkRole.objects.select_related("ipv4_pool_prefix", "ipv6_pool_prefix")
    serializer_class = NSOLinkRoleSerializer
    filterset_class = NSOLinkRoleFilterSet


class NSOLinkRoleAssignmentViewSet(AuthorizedAPIView, NetBoxModelViewSet):
    """REST API for NSOLinkRoleAssignment — role ← cable or interface."""

    policy = generic_policy()

    queryset = NSOLinkRoleAssignment.objects.select_related("role", "cable", "interface")
    serializer_class = NSOLinkRoleAssignmentSerializer
    filterset_class = NSOLinkRoleAssignmentFilterSet


class OnboardingCandidatesView(AuthorizedAPIView, APIView):
    """CICD-facing read of onboarding state for an NSO instance.

    ``GET /api/plugins/nso/onboarding-candidates/?instance=<id>``
    returns the same three buckets as the dashboard (onboarded / candidates /
    orphans) as JSON, so a pipeline can discover which staged devices are ready to
    onboard. ``instance`` defaults to the default NSO instance.
    """

    permission_classes = [IsAuthenticated]
    policy = EndpointPolicy(Collection(("netbox_nso_plugin.view_nsoinstance",)), "read")

    def get(self, request):
        """Return {instance, error, onboarded, candidates, orphans} for the instance."""
        from ..onboarding import build_onboarding_dashboard

        selected = request.query_params.get("instance")
        if not is_valid_instance_id(selected):
            raise ParseError("Invalid NSO instance identity")
        instance = selected_instance(request.user, selected)
        if instance is None:
            raise NotFound(f"unknown instance '{selected}'" if selected else "no NSO instance configured")

        data = build_onboarding_dashboard(instance, request.user)
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


class OnboardView(AuthorizedAPIView, APIView):
    """CICD-facing onboard action.

    ``POST /api/plugins/nso/onboard/`` body:
    ``{"netbox_device_id": <int>, "instance": "<adapter_instance_id>"?}``.
    Provisions the device into NSO (create node → fetch-host-keys → unlock →
    sync-from) and creates the management row. Returns the step-by-step result;
    400 on pre-flight failure (no NED mapping / no primary IP / already managed).
    """

    permission_classes = [IsAuthenticated]
    # Provisioning calls the adapter after it commits the durable attempt: no operator transaction.
    policy = EndpointPolicy(ApiOnboarding(), "adapter_action")

    def post(self, request):
        """Onboard the posted NetBox device into the selected (or default) NSO instance."""
        from ..onboarding import onboard_candidate

        result = onboard_candidate(self.target.device, self.target.related["instance"])
        if result.get("permission_denied"):
            raise permissions_violation(result["permission_denied"])
        code = result.pop(
            "_http_status",
            status.HTTP_200_OK if result["ok"] else status.HTTP_400_BAD_REQUEST,
        )
        return Response(result, status=code)


class SyncCompleteView(AuthorizedAPIView, APIView):
    """Adapter → plugin callback: a device sync finished, refresh its NSO*State cache.

    POSTed by the adapter at the end of each ``sync_device`` so the plugin reconciles
    adapter state into its display tables OFF the request path — instead of doing it
    (slowly, with write-on-read) every time an operator opens the NSO tab. Enqueues a
    deduped background reconcile and returns 202 immediately so the adapter never
    waits on the reconcile.

    The caller needs the ``sync`` action on the reported NSO device management row.

    Endpoint: ``POST /api/plugins/nso/sync-complete/``
    Body: ``{"netbox_device_id": <int>}`` (or ``{"adapter_device_id": <int>}``).
    """

    permission_classes = [IsAuthenticated]
    policy = EndpointPolicy(IntegrationSync(), "adapter_action")

    def post(self, request):
        """Enqueue the background reconcile of the authorized management's device."""
        from ..deployment import DeploymentQuiesced, is_quiesced, operation
        from ..deployment_probe import ProbeBlocked, enqueue_probe
        from ..models import NSOApplyAttempt
        from ..reconcile import enqueue_device_reconcile

        device_id = self.target.management.device_id
        try:
            if is_quiesced():
                enqueue_probe(device_id)
            else:
                with operation("sync-complete callback"):
                    enqueue_device_reconcile(device_id)
        except (ProbeBlocked, DeploymentQuiesced, NSOApplyAttempt.DoesNotExist) as exc:
            logger.info("Deferred the sync-complete callback for device %s: %r", device_id, exc)
            return Response({"detail": _SYNC_DEFERRED}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"queued": True, "netbox_device_id": device_id}, status=status.HTTP_202_ACCEPTED)


class ProvisionCompleteView(AuthorizedAPIView, APIView):
    """Adapter callback: record terminal provision evidence for the fenced plugin sweep.

    The adapter posts this callback when a provision attempt reaches a terminal state. The
    callback only records the first terminal evidence with compare-and-set. It then enqueues
    the tombstone sweep, which is the sole owner of management completion and orphan offboarding.

    Endpoint: ``POST /api/plugins/nso/provision-complete/``
    Body includes ``provision_attempt_id`` and the adapter's terminal evidence.
    """

    permission_classes = [IsAuthenticated]
    policy = EndpointPolicy(IntegrationProvisionAttempt(), "adapter_action")

    def post(self, request):
        """CAS-mark terminal evidence and delegate every completion action to the sweep."""
        from ..adapter_client import AdapterError
        from ..provision_lifecycle import mark_provision_terminal, validate_provision_evidence
        from ..reconcile import enqueue_provision_tombstone_sweep

        attempt_id = self.target.related["attempt_id"]
        try:
            evidence = validate_provision_evidence(dict(self.target.related["body"]), terminal_required=True)
        except (TypeError, ValueError):
            return Response({"detail": "invalid provision completion evidence"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            known = mark_provision_terminal(attempt_id, evidence)
        except AdapterError:
            return Response({"detail": "invalid provision completion evidence"}, status=status.HTTP_400_BAD_REQUEST)
        if not known:
            return Response({"queued": False}, status=status.HTTP_202_ACCEPTED)
        enqueue_provision_tombstone_sweep(attempt_id)
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)
