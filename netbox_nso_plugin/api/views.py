# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.api.viewsets import NetBoxModelViewSet

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
