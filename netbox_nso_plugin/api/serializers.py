# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..models import NSODeviceManagement, NSOInstance, NSOInterfaceState, NSOPlatformNedMapping


class NSOPlatformNedMappingSerializer(NetBoxModelSerializer):
    """Serializer for NSOPlatformNedMapping (CICD reads/writes the platform→NED map)."""

    class Meta:
        model = NSOPlatformNedMapping
        fields = [
            "id",
            "url",
            "display",
            "platform",
            "ned_id",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        ]


class NSOInstanceSerializer(NetBoxModelSerializer):
    """Serializer for NSOInstance."""

    class Meta:
        model = NSOInstance
        fields = [
            "id",
            "url",
            "display",
            "name",
            "adapter_instance_id",
            "is_default",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        ]


class NSODeviceManagementSerializer(NetBoxModelSerializer):
    """Serializer for NSODeviceManagement (the adapter reads this for scope reconciliation)."""

    managed_attributes = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text="Computed list of attribute names currently in scope (e.g. ['description', 'enabled']).",
    )

    class Meta:
        model = NSODeviceManagement
        fields = [
            "id",
            "url",
            "display",
            "device",
            "nso_instance",
            "nso_device_name",
            "manage_description",
            "manage_enabled",
            "auto_apply",
            "managed_attributes",
            "adapter_device_id",
            "last_sync_at",
            "last_sync_status",
            "state_snapshot",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        ]


class NSOInterfaceStateSerializer(NetBoxModelSerializer):
    """Serializer for NSOInterfaceState.

    The adapter's scope reconciler reads the device-management endpoint.
    This endpoint exposes the per-interface intent status overlay so the
    adapter can mirror it back (decision L).

    ``intent_value`` is a read-only field that returns the *current* value on
    the linked dcim.Interface (description or enabled).  This is the authoritative
    intent for the reconciler — it reflects edits made after the user accepted,
    not just the cached nso_value at accept time.
    """

    intent_value = serializers.SerializerMethodField()

    def get_intent_value(self, obj) -> str | None:
        """Return the current dcim.Interface field value for this attribute."""
        iface = obj.interface
        if iface is None:
            return None
        if obj.attribute == "description":
            return iface.description or ""
        if obj.attribute == "enabled":
            return str(iface.enabled)
        return None

    class Meta:
        model = NSOInterfaceState
        fields = [
            "id",
            "url",
            "display",
            "interface",
            "attribute",
            "status",
            "nso_value",
            "intent_value",
            "last_sync_at",
            "accepted_at",
            "last_apply_at",
            "last_apply_error",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        ]
