# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django_tables2 as tables
from netbox.tables import NetBoxTable, columns

from .models import NSODeviceManagement, NSOInstance, NSOInterfaceState


class NSOInstanceTable(NetBoxTable):
    """Table for listing NSOInstance records."""

    name = tables.Column(linkify=True)
    adapter_instance_id = tables.Column()

    class Meta(NetBoxTable.Meta):
        model = NSOInstance
        fields = ("pk", "id", "name", "adapter_instance_id", "actions")
        default_columns = ("name", "adapter_instance_id")


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
            "interface",
            "attribute",
            "status",
            "nso_value",
            "last_sync_at",
        )
