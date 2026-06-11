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


# ── Internal NSO*State overlay serializers ────────────────────────────────────────
# These overlays are internal status rows (surfaced via the device NSO tab), NOT a
# REST/relational API surface — so they have no viewsets/routes. They exist solely so
# get_serializer_for_model() resolves a class for NetBox's event serialization when a
# parent object with a cascading overlay is deleted (otherwise the delete 500s). Plain
# ModelSerializers (FKs/M2Ms as PKs, no hyperlink url field) so no API route is needed.
from rest_framework.serializers import ModelSerializer  # noqa: E402

from ..models import (  # noqa: E402
    NSOBFDInterfaceState,
    NSOBGPPeerState,
    NSOBGPPeerTemplateState,
    NSOInterfaceIPState,
    NSOISISFlexAlgoState,
    NSOISISInstanceState,
    NSOISISInterfaceState,
    NSOL2SapState,
    NSOLACPBundleState,
    NSOLACPMemberState,
    NSOLoggingHostState,
    NSOOSPFInstanceState,
    NSOOSPFInterfaceState,
    NSORedistributionState,
    NSORoutePolicyState,
    NSOSnmpCommunityState,
    NSOSnmpHostState,
    NSOSnmpSystemInfoState,
    NSOSnmpV3UserState,
    NSOStaticRouteState,
    NSOSubinterfaceState,
    NSOSVIState,
    NSOSwitchportState,
    NSOVLANState,
)


class NSOInterfaceIPStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOInterfaceIPState
        fields = "__all__"


class NSOSnmpCommunityStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOSnmpCommunityState
        fields = "__all__"


class NSOSnmpV3UserStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOSnmpV3UserState
        fields = "__all__"


class NSOSnmpHostStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOSnmpHostState
        fields = "__all__"


class NSOSnmpSystemInfoStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOSnmpSystemInfoState
        fields = "__all__"


class NSOLoggingHostStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOLoggingHostState
        fields = "__all__"


class NSOStaticRouteStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOStaticRouteState
        fields = "__all__"


class NSOL2SapStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOL2SapState
        fields = "__all__"


class NSOISISInterfaceStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOISISInterfaceState
        fields = "__all__"


class NSOISISInstanceStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOISISInstanceState
        fields = "__all__"


class NSOISISFlexAlgoStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOISISFlexAlgoState
        fields = "__all__"


class NSOBGPPeerStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOBGPPeerState
        fields = "__all__"


class NSOBGPPeerTemplateStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOBGPPeerTemplateState
        fields = "__all__"


class NSORoutePolicyStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSORoutePolicyState
        fields = "__all__"


class NSOOSPFInstanceStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOOSPFInstanceState
        fields = "__all__"


class NSOOSPFInterfaceStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOOSPFInterfaceState
        fields = "__all__"


class NSORedistributionStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSORedistributionState
        fields = "__all__"


class NSOLACPBundleStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOLACPBundleState
        fields = "__all__"


class NSOLACPMemberStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOLACPMemberState
        fields = "__all__"


class NSOVLANStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOVLANState
        fields = "__all__"


class NSOSwitchportStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOSwitchportState
        fields = "__all__"


class NSOSVIStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOSVIState
        fields = "__all__"


class NSOSubinterfaceStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOSubinterfaceState
        fields = "__all__"


class NSOBFDInterfaceStateSerializer(ModelSerializer):  # noqa: D101
    class Meta:
        model = NSOBFDInterfaceState
        fields = "__all__"
