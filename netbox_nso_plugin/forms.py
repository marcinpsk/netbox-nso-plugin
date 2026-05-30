# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.forms import NetBoxModelForm

from .models import AdapterConnection, NSODeviceManagement, NSOInstance


class AdapterConnectionForm(NetBoxModelForm):
    """Form for the AdapterConnection singleton."""

    class Meta:
        model = AdapterConnection
        fields = ["url", "verify_tls", "ca_cert_path", "timeout_seconds", "enabled", "tags"]


class NSOInstanceForm(NetBoxModelForm):
    """Form for creating/editing NSOInstance records."""

    class Meta:
        model = NSOInstance
        fields = ["name", "adapter_instance_id", "tags"]


class NSODeviceManagementForm(NetBoxModelForm):
    """Form for creating/editing NSODeviceManagement records."""

    class Meta:
        model = NSODeviceManagement
        fields = [
            "device",
            "nso_instance",
            "nso_device_name",
            "manage_description",
            "manage_enabled",
            "auto_apply",
            "tags",
        ]

    def __init__(self, *args, **kwargs):
        """Pre-populate nso_device_name from the device name when adding via device page."""
        super().__init__(*args, **kwargs)
        # Only auto-fill on new records where no value is set yet
        if not self.instance.pk and not self.initial.get("nso_device_name"):
            device_pk = self.initial.get("device") or self.data.get("device")
            if device_pk:
                try:
                    from dcim.models import Device

                    device = Device.objects.get(pk=device_pk)
                    self.initial["nso_device_name"] = device.name
                except Exception:
                    pass
