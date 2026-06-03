# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django import forms
from netbox.forms import NetBoxModelForm

from .models import AdapterConnection, NSODeviceManagement, NSOInstance


class AdapterConnectionForm(NetBoxModelForm):
    """Form for the AdapterConnection singleton."""

    class Meta:
        model = AdapterConnection
        fields = [
            "url",
            "verify_tls",
            "ca_cert_path",
            "timeout_seconds",
            "enabled",
            "static_route_auto_create",
            "interface_ip_auto_create",
            "vrf_auto_create",
            "tags",
        ]


class NSOInstanceForm(NetBoxModelForm):
    """Form for creating/editing NSOInstance records."""

    class Meta:
        model = NSOInstance
        fields = ["name", "adapter_instance_id", "is_default", "tags"]


class NSODeviceManagementForm(NetBoxModelForm):
    """Form for creating/editing NSODeviceManagement records."""

    # Transient convenience toggle (not a model field): when checked, every
    # management scope below is enabled on save. auto_apply is deliberately
    # excluded — it stays an independent, explicit opt-in.
    manage_all = forms.BooleanField(
        required=False,
        label="Manage everything",
        help_text="Enable every management scope below in one click. Auto-apply stays separate.",
    )

    # Every persisted scope flag the "Manage everything" toggle controls.
    SCOPE_FIELDS = (
        "manage_interfaces",
        "manage_description",
        "manage_enabled",
        "manage_routing",
        "manage_static",
        "manage_isis",
        "manage_ospf",
        "manage_bgp",
        "manage_route_policy",
        "manage_redistribution",
        "manage_snmp",
    )

    class Meta:
        model = NSODeviceManagement
        fields = [
            "device",
            "nso_instance",
            "nso_device_name",
            # Interfaces
            "manage_interfaces",
            "manage_description",
            "manage_enabled",
            # Routing
            "manage_routing",
            "manage_static",
            "manage_isis",
            "manage_ospf",
            "manage_bgp",
            "manage_route_policy",
            "manage_redistribution",
            # SNMP
            "manage_snmp",
            "auto_apply",
            "tags",
        ]

    def __init__(self, *args, **kwargs):
        """Pre-populate nso_device_name and default NSO instance when adding."""
        super().__init__(*args, **kwargs)
        # On an existing, fully-managed record, reflect that in the toggle.
        if self.instance.pk:
            self.fields["manage_all"].initial = all(getattr(self.instance, name) for name in self.SCOPE_FIELDS)
        # Only auto-fill on new records where no value is set yet
        if not self.instance.pk:
            # Auto-select the default NSO instance when none is chosen yet.
            if not self.initial.get("nso_instance") and not self.data.get("nso_instance"):
                default_instance = NSOInstance.get_default()
                if default_instance:
                    self.initial["nso_instance"] = default_instance.pk

            if not self.initial.get("nso_device_name"):
                device_pk = self.initial.get("device") or self.data.get("device")
                if device_pk:
                    try:
                        from dcim.models import Device

                        device = Device.objects.get(pk=device_pk)
                        self.initial["nso_device_name"] = device.name
                    except Exception:
                        pass

    def clean(self):
        """If 'Manage everything' is ticked, enable every scope flag (auto_apply stays separate)."""
        super().clean()
        if self.cleaned_data.get("manage_all"):
            for name in self.SCOPE_FIELDS:
                self.cleaned_data[name] = True
        return self.cleaned_data
