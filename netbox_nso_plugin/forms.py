# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from dcim.models import Cable, Interface
from django import forms
from ipam.models import Prefix
from netbox.forms import NetBoxModelForm
from utilities.forms.fields import DynamicModelChoiceField, SlugField
from utilities.forms.rendering import FieldSet

from .models import (
    AdapterConnection,
    NSODeviceManagement,
    NSOFailoverSettings,
    NSOInstance,
    NSOInterfaceMtuState,
    NSOLinkRole,
    NSOLinkRoleAssignment,
    NSOLoggingHostState,
    NSOPlatformNedMapping,
    NSOSnmpCommunityState,
    NSOSnmpHostState,
    NSOSnmpSystemInfoState,
    NSOSnmpV3UserState,
)


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
            "onboard_authgroup",
            "tags",
        ]


class NSOFailoverSettingsForm(NetBoxModelForm):
    """Form for the NSOFailoverSettings singleton (pushed to the adapter on save)."""

    class Meta:
        model = NSOFailoverSettings
        fields = [
            "enabled",
            "primary_probe_interval",
            "oob_probe_interval",
            "failure_threshold",
            "success_threshold",
            "probe_timeout",
            "probe_concurrency",
            "max_flips_per_tick",
            "sync_from_after_switch",
            "tags",
        ]
        # Django title-cases field names ("oob" → "Oob"); spell the acronym/units properly.
        labels = {
            "primary_probe_interval": "Primary probe interval (min)",
            "oob_probe_interval": "OOB probe interval (min)",
            "probe_timeout": "Probe timeout (sec)",
            "sync_from_after_switch": "Sync-from after switch",
        }


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
        "manage_logging",
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
            # SNMP / Logging
            "manage_snmp",
            "manage_logging",
            "auto_apply",
            "sync_before_apply",
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


# ── SNMP / Logging overlay edit forms (operator modify → accept → push) ────────
# These mirror what NSO observed; the operator may edit the non-secret fields and
# (for SNMP secrets) set a vault_ref. Identity/status/sync fields are not editable.


class NSOSnmpCommunityStateForm(NetBoxModelForm):
    """Edit an SNMP community overlay — access/ACL + the Vault ref for the secret."""

    class Meta:
        model = NSOSnmpCommunityState
        fields = ["access", "acl", "vault_ref", "tags"]


class NSOSnmpV3UserStateForm(NetBoxModelForm):
    """Edit an SNMP v3 user overlay — the Vault ref for the auth/priv secrets."""

    class Meta:
        model = NSOSnmpV3UserState
        fields = ["vault_ref", "tags"]


class NSOSnmpHostStateForm(NetBoxModelForm):
    """Edit an SNMP trap/inform host overlay."""

    class Meta:
        model = NSOSnmpHostState
        fields = ["address", "version", "notify_type", "port", "tags"]


class NSOSnmpSystemInfoStateForm(NetBoxModelForm):
    """Edit the SNMP system location/contact overlay."""

    class Meta:
        model = NSOSnmpSystemInfoState
        fields = ["location", "contact", "tags"]


class NSOLoggingHostStateForm(NetBoxModelForm):
    """Edit a remote syslog server overlay."""

    class Meta:
        model = NSOLoggingHostState
        fields = ["address", "port", "severity", "facility", "transport", "vrf", "source", "tags"]


class NSOInterfaceMtuStateForm(NetBoxModelForm):
    """Operator-edit the per-interface MTU overlay (Phase 2b write path).

    Changing a value declares intent that diverges from the device; an unowned row
    is flagged ``changed`` so it surfaces for Accept (which marks it owned, writes
    the native L2 MTU onto the interface, and pushes). An already-owned row keeps
    its ownership and re-pushes the new value via the save signal.
    """

    class Meta:
        model = NSOInterfaceMtuState
        fields = ["l2_mtu", "ip_mtu", "mpls_mtu", "tags"]

    def save(self, commit=True):
        """Flag an edited unowned row as ``changed`` (diverged → needs Accept)."""
        from . import status_machine as sm

        obj = super().save(commit=False)
        if not sm.is_owned(obj.status):
            obj.status = "changed"
        if commit:
            obj.save()
            self.save_m2m()
        return obj


# ── Link-role provisioning ─────────────────────────────────────────────────────


class NSOLinkRoleForm(NetBoxModelForm):
    """Create/edit a configurable link role — the intent bundle (IP + description + IGP)."""

    slug = SlugField(slug_source="name")
    ipv4_pool_prefix = DynamicModelChoiceField(queryset=Prefix.objects.all(), required=False, label="IPv4 pool prefix")
    ipv6_pool_prefix = DynamicModelChoiceField(queryset=Prefix.objects.all(), required=False, label="IPv6 pool prefix")

    fieldsets = (
        FieldSet("name", "slug", "description", "enabled", "link_type", name="Role"),
        FieldSet(
            "assign_ipv4",
            "ipv4_pool_prefix",
            "ipv4_pool_role",
            "ipv4_mask",
            "assign_ipv6",
            "ipv6_pool_prefix",
            "ipv6_pool_role",
            "ipv6_mask",
            name="IP assignment",
        ),
        FieldSet("description_template", name="Description"),
        FieldSet(
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
            name="IGP",
        ),
    )

    class Meta:
        model = NSOLinkRole
        fields = [
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
        ]


class NSOLinkRoleAssignmentForm(NetBoxModelForm):
    """Assign a link role to a cable (p2p) or a single interface (loopback/access)."""

    role = DynamicModelChoiceField(queryset=NSOLinkRole.objects.all())
    cable = DynamicModelChoiceField(queryset=Cable.objects.all(), required=False)
    interface = DynamicModelChoiceField(queryset=Interface.objects.all(), required=False)

    class Meta:
        model = NSOLinkRoleAssignment
        fields = ["role", "cable", "interface", "tags"]
