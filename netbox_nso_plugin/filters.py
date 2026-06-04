# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django_filters
from netbox.filtersets import NetBoxModelFilterSet

from .models import NSODeviceManagement, NSOInstance, NSOInterfaceState, NSOPlatformNedMapping


class NSOPlatformNedMappingFilterSet(NetBoxModelFilterSet):
    """FilterSet for NSOPlatformNedMapping."""

    class Meta:
        model = NSOPlatformNedMapping
        fields = ["id", "platform", "ned_id"]


class NSOInstanceFilterSet(NetBoxModelFilterSet):
    """FilterSet for NSOInstance."""

    class Meta:
        model = NSOInstance
        fields = ["id", "name", "adapter_instance_id"]


class NSODeviceManagementFilterSet(NetBoxModelFilterSet):
    """FilterSet for NSODeviceManagement."""

    nso_instance_id = django_filters.ModelMultipleChoiceFilter(
        queryset=NSOInstance.objects.all(),
    )
    last_sync_status = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        model = NSODeviceManagement
        fields = [
            "id",
            "device",
            "nso_instance_id",
            "nso_device_name",
            "manage_description",
            "manage_enabled",
            "auto_apply",
            "last_sync_status",
        ]


class NSOInterfaceStateFilterSet(NetBoxModelFilterSet):
    """FilterSet for NSOInterfaceState."""

    status = django_filters.MultipleChoiceFilter(
        choices=NSOInterfaceState.STATUS_CHOICES,
    )
    attribute = django_filters.MultipleChoiceFilter(
        choices=NSOInterfaceState.ATTRIBUTE_CHOICES,
    )

    class Meta:
        model = NSOInterfaceState
        fields = ["id", "interface", "attribute", "status"]
