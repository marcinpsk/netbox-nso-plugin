# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django_filters
from netbox.filtersets import NetBoxModelFilterSet

from .models import (
    NSODerivedIntentTemplate,
    NSODeviceManagement,
    NSOInstance,
    NSOInterfaceState,
    NSOLinkRole,
    NSOLinkRoleAssignment,
    NSOPlatformNedMapping,
)


class NSODerivedIntentTemplateFilterSet(NetBoxModelFilterSet):
    """FilterSet for database-managed derived-intent templates."""

    class Meta:
        model = NSODerivedIntentTemplate
        fields = ["id", "sentinel", "template", "enabled"]


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
    state = django_filters.MultipleChoiceFilter(
        choices=[
            ("unknown", "Unknown"),
            ("drift", "Drift"),
            ("pending", "Pending apply"),
            ("deploying", "Deploying"),
            ("in_sync", "In sync"),
            ("apply_failed", "Apply failed"),
            ("unavailable", "Unavailable"),
        ],
        method="filter_state",
    )
    attribute = django_filters.MultipleChoiceFilter(
        choices=NSOInterfaceState.ATTRIBUTE_CHOICES,
    )

    def filter_state(self, queryset, name, values):
        """Filter displayed state after current execution and value projection."""
        from django.db.models import Q

        from .refusal_display import project_rows, row_projection, settled_interface_owners

        settled = settled_interface_owners(queryset).values("pk")
        candidates = queryset.exclude(pk__in=settled).select_related("interface")
        matched = Q(pk__in=[row.pk for row in project_rows(candidates) if row_projection(row)["state"] in values])
        return queryset.filter(matched | Q(pk__in=settled) if "in_sync" in values else matched)

    class Meta:
        model = NSOInterfaceState
        fields = ["id", "interface", "attribute", "status"]


class NSOLinkRoleFilterSet(NetBoxModelFilterSet):
    """FilterSet for NSOLinkRole."""

    class Meta:
        model = NSOLinkRole
        fields = ["id", "name", "slug", "link_type", "igp", "enabled"]


class NSOLinkRoleAssignmentFilterSet(NetBoxModelFilterSet):
    """FilterSet for NSOLinkRoleAssignment."""

    class Meta:
        model = NSOLinkRoleAssignment
        fields = ["id", "role", "cable", "interface"]
