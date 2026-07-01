# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..models import (
    NSODeviceManagement,
    NSOInstance,
    NSOInterfaceState,
    NSOLinkRole,
    NSOLinkRoleAssignment,
    NSOPlatformNedMapping,
)


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
    primary_ip = serializers.SerializerMethodField(
        help_text="Host string of the device's primary management IP (no prefix), or null. Drives failover.",
    )
    oob_ip = serializers.SerializerMethodField(
        help_text="Host string of the device's out-of-band IP (no prefix), or null — the failover fallback.",
    )

    def get_primary_ip(self, obj) -> str | None:
        """Return the device's primary management IP as a bare host string (no prefix)."""
        from ..onboarding import _ip_host

        return _ip_host(getattr(obj.device, "primary_ip", None))

    def get_oob_ip(self, obj) -> str | None:
        """Return the device's out-of-band IP as a bare host string (no prefix)."""
        from ..onboarding import _ip_host

        return _ip_host(getattr(obj.device, "oob_ip", None))

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
            "primary_ip",
            "oob_ip",
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


class NSOLinkRoleSerializer(NetBoxModelSerializer):
    """Serializer for NSOLinkRole (the configurable provisioning catalog)."""

    class Meta:
        model = NSOLinkRole
        fields = [
            "id",
            "url",
            "display",
            "name",
            "slug",
            "description",
            "enabled",
            "link_type",
            "assign_ipv4",
            "ipv4_pool_prefix",
            "ipv4_pool_role",
            "ipv4_mask",
            "assign_ipv6",
            "ipv6_pool_prefix",
            "ipv6_pool_role",
            "ipv6_mask",
            "description_template",
            "igp",
            "isis_circuit_type",
            "isis_passive",
            "isis_metric",
            "isis_process_tag",
            "ospf_area",
            "ospf_network_type",
            "ospf_passive",
            "ospf_cost",
            "ospf_process_id",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        ]


class NSOLinkRoleAssignmentSerializer(NetBoxModelSerializer):
    """Serializer for NSOLinkRoleAssignment (role ← cable or interface)."""

    class Meta:
        model = NSOLinkRoleAssignment
        fields = [
            "id",
            "url",
            "display",
            "role",
            "cable",
            "interface",
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
#
# Secret-exposure convention: ``fields = "__all__"`` is acceptable here ONLY because the
# overlay models hold no plaintext credentials — secrets live behind ``vault_ref`` +
# ``has_*_secret`` booleans (SNMP) or presence flags (``*_auth_present``), never the raw
# value. The one exception is NSOISISInstanceState, whose ``area_auth_key`` /
# ``domain_auth_key`` ARE plaintext (routing-auth keys, plaintext-at-rest by policy); those
# are excluded below so they never reach a changelog/event payload. Any future overlay field
# that carries a secret MUST be excluded the same way — ``test_serializers`` introspects every
# serializer and fails if a plaintext-secret-looking field is ever serialized.
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
        # area_auth_key / domain_auth_key are plaintext IS-IS auth keys — never serialize
        # them (they would land in ObjectChange/webhook payloads). Mirrors the netbox-routing
        # GraphQL exclusion; the area/domain auth_type fields still report that auth is set.
        exclude = ["area_auth_key", "domain_auth_key"]


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
