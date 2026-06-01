# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..filters import NSODeviceManagementFilterSet, NSOInstanceFilterSet
from ..models import NSODeviceManagement, NSOInstance, NSOInterfaceState
from .serializers import NSODeviceManagementSerializer, NSOInstanceSerializer, NSOInterfaceStateSerializer


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


class NSOInterfaceStateViewSet(NetBoxModelViewSet):
    """REST API for NSOInterfaceState — per-interface intent status overlay.

    The adapter's scope reconciler also reads this to mirror intent (decision L).
    Endpoint: /api/plugins/netbox-nso-plugin/interface-state/
    """

    queryset = NSOInterfaceState.objects.select_related("interface")
    serializer_class = NSOInterfaceStateSerializer


class SyncCompleteView(APIView):
    """Adapter → plugin callback: a device sync finished, refresh its NSO*State cache.

    POSTed by the adapter at the end of each ``sync_device`` so the plugin reconciles
    adapter state into its display tables OFF the request path — instead of doing it
    (slowly, with write-on-read) every time an operator opens the NSO tab. Enqueues a
    deduped background reconcile and returns 202 immediately so the adapter never
    waits on the reconcile.

    Endpoint: ``POST /api/plugins/netbox-nso-plugin/sync-complete/``
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
