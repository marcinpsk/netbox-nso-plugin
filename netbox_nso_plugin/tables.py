# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django_tables2 as tables
from netbox.tables import NetBoxTable, columns

from .models import (
    NSODeviceManagement,
    NSOInstance,
    NSOInterfaceState,
    NSOLinkRole,
    NSOLinkRoleAssignment,
    NSOPlatformNedMapping,
)


class NSOPlatformNedMappingTable(NetBoxTable):
    """Table for listing Platform→NED mappings."""

    platform = tables.Column(linkify=True)
    ned_id = tables.Column(verbose_name="NED ID")

    class Meta(NetBoxTable.Meta):
        model = NSOPlatformNedMapping
        fields = ("pk", "id", "platform", "ned_id", "actions")
        default_columns = ("platform", "ned_id")


class NSOInstanceTable(NetBoxTable):
    """Table for listing NSOInstance records."""

    name = tables.Column(linkify=True)
    adapter_instance_id = tables.Column()
    is_default = columns.BooleanColumn(verbose_name="Default")

    class Meta(NetBoxTable.Meta):
        model = NSOInstance
        fields = ("pk", "id", "name", "adapter_instance_id", "is_default", "actions")
        default_columns = ("name", "adapter_instance_id", "is_default")


class NSODeviceManagementTable(NetBoxTable):
    """Table for listing NSODeviceManagement records."""

    device = tables.Column(linkify=True)
    nso_instance = tables.Column(linkify=True)
    nso_device_name = tables.Column()
    last_sync_at = columns.DateTimeColumn()
    last_sync_status = tables.Column()

    class Meta(NetBoxTable.Meta):
        model = NSODeviceManagement
        fields = (
            "pk",
            "id",
            "device",
            "nso_instance",
            "nso_device_name",
            "manage_description",
            "manage_enabled",
            "auto_apply",
            "last_sync_at",
            "last_sync_status",
            "actions",
        )
        default_columns = (
            "device",
            "nso_instance",
            "nso_device_name",
            "last_sync_at",
            "last_sync_status",
        )


class NSOInterfaceStateTable(NetBoxTable):
    """Table for listing NSOInterfaceState records.

    NSOInterfaceState is sync-managed (no edit view); actions limited to delete + changelog.
    """

    device = tables.Column(accessor="interface__device", linkify=True, verbose_name="Device")
    interface = tables.Column(linkify=True)
    attribute = tables.Column()
    status = tables.Column()
    nso_value = tables.Column()
    last_sync_at = columns.DateTimeColumn()
    accepted_at = columns.DateTimeColumn()
    last_apply_at = columns.DateTimeColumn()
    actions = columns.ActionsColumn(actions=("delete", "changelog"))

    class Meta(NetBoxTable.Meta):
        model = NSOInterfaceState
        fields = (
            "pk",
            "id",
            "device",
            "interface",
            "attribute",
            "status",
            "nso_value",
            "last_sync_at",
            "accepted_at",
            "last_apply_at",
            "actions",
        )
        default_columns = (
            "device",
            "interface",
            "attribute",
            "status",
            "nso_value",
            "last_sync_at",
        )


class NSOLinkRoleTable(NetBoxTable):
    """Table for listing configurable link roles."""

    name = tables.Column(linkify=True)
    link_type = tables.Column()
    igp = tables.Column(verbose_name="IGP")
    assign_ipv4 = columns.BooleanColumn(verbose_name="IPv4")
    assign_ipv6 = columns.BooleanColumn(verbose_name="IPv6")
    enabled = columns.BooleanColumn()

    class Meta(NetBoxTable.Meta):
        model = NSOLinkRole
        fields = (
            "pk",
            "id",
            "name",
            "slug",
            "link_type",
            "assign_ipv4",
            "assign_ipv6",
            "igp",
            "enabled",
            "description",
            "actions",
        )
        default_columns = ("name", "link_type", "assign_ipv4", "assign_ipv6", "igp", "enabled")


class NSOLinkRoleAssignmentTable(NetBoxTable):
    """Table for listing link-role assignments (role ← cable or interface)."""

    role = tables.Column(linkify=True)
    cable = tables.Column(linkify=True)
    interface = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = NSOLinkRoleAssignment
        fields = ("pk", "id", "role", "cable", "interface", "actions")
        default_columns = ("role", "cable", "interface")
