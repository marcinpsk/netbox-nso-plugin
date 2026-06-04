# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django import forms
from netbox.forms import NetBoxModelForm

from .models import AdapterConnection, NSODeviceManagement, NSOInstance, NSOPlatformNedMapping


class NSOPlatformNedMappingForm(NetBoxModelForm):
    """Form for Platform→NED mappings.

    ``ned_id`` is rendered as a dropdown of the NEDs actually installed on the
    default NSO instance when the adapter is reachable (the hybrid suggestion),
    falling back to a free-text field when it is not — so the mapping stays
    editable even if the adapter is down.
    """

    class Meta:
        model = NSOPlatformNedMapping
        fields = ["platform", "ned_id", "tags"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = self._available_ned_choices()
        if choices:
            current = self.initial.get("ned_id") or getattr(self.instance, "ned_id", "")
            if current and current not in {c[0] for c in choices}:
                choices = [(current, current), *choices]
            self.fields["ned_id"] = forms.ChoiceField(
                choices=[("", "---------"), *choices],
                required=True,
                label="NED ID",
                help_text="NEDs installed on the default NSO instance.",
            )

    @staticmethod
    def _available_ned_choices():
        """Return [(ned_id, label)] from the adapter, or [] if unavailable."""
        try:
            from . import adapter_client as client
            from .models import NSOInstance

            inst = NSOInstance.get_default()
            if inst is None:
                return []
            neds = client.get_neds(inst.adapter_instance_id)
            return [(n["ned_id"], f"{n['ned_id']} ({n.get('vendor') or '?'})") for n in neds if n.get("ned_id")]
        except Exception:
            return []


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
