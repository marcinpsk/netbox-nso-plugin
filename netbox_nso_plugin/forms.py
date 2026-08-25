# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from dcim.models import Cable, Interface
from django import forms
from ipam.models import ASN, VRF, IPAddress, Prefix
from netbox.forms import NetBoxModelForm
from utilities.forms.fields import DynamicModelChoiceField, SlugField
from utilities.forms.rendering import FieldSet

from .models import (
    AdapterConnection,
    NSODerivedIntentTemplate,
    NSODeviceManagement,
    NSOFailoverSettings,
    NSOInstance,
    NSOInterfaceMtuState,
    NSOLinkRole,
    NSOLinkRoleAssignment,
    NSOLoggingHostState,
    NSOLoggingLevelState,
    NSOPlatformNedMapping,
    NSOSnmpCommunityState,
    NSOSnmpHostState,
    NSOSnmpSystemInfoState,
    NSOSnmpV3UserState,
    NSOVaultSettings,
)
from .vault_refs import VaultRefError, parse_vault_ref, qualify_snmp_ref, secret_fingerprint


def _vault_settings_layout():
    """Return (kv_mount, base_path) from the enabled NSOVaultSettings singleton, else (None, None)."""
    settings_obj = NSOVaultSettings.objects.first()
    if settings_obj is None or not settings_obj.enabled:
        return None, None
    return settings_obj.kv_mount, settings_obj.base_path


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


class NSODerivedIntentTemplateForm(NetBoxModelForm):
    """Form for database-managed interface-description templates."""

    class Meta:
        model = NSODerivedIntentTemplate
        fields = ["sentinel", "template", "enabled", "tags"]


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
            "active_probe_timeout",
            "probe_concurrency",
            "max_flips_per_tick",
            "sync_from_after_switch",
            "tags",
        ]
        # Django title-cases field names ("oob" → "Oob"); spell the acronym/units properly.
        labels = {
            "primary_probe_interval": "Primary probe interval (min)",
            "oob_probe_interval": "OOB probe interval (min)",
            "probe_timeout": "Inactive-address probe timeout (sec)",
            "active_probe_timeout": "Active-address probe timeout (sec)",
            "sync_from_after_switch": "Sync-from after switch",
        }


class NSOVaultSettingsForm(NetBoxModelForm):
    """Form for the NSOVaultSettings singleton (Vault KV layout for generated refs)."""

    class Meta:
        model = NSOVaultSettings
        fields = ["kv_mount", "base_path", "enabled", "tags"]
        labels = {"kv_mount": "KV mount", "base_path": "Base path"}


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
    """Edit an SNMP community overlay — access/ACL, Vault ref, or set a new secret value.

    ``secret_value`` is write-only: it transits one adapter call (which writes
    Vault and returns ref + fingerprint) and is never bound to a model field.
    Setting it REKEYS the row (``community_hash`` = fingerprint of the new
    value), marks it accepted, and re-points sibling trap hosts at the new hash.
    The Vault write happens during validation — an adapter/Vault failure surfaces
    as a form error and nothing is saved.
    """

    secret_value = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        label="Set secret value",
        help_text=(
            "New community string. Written to Vault via the adapter (never stored in NetBox); "
            "leave blank to keep the current secret. Rekeys this row to the new value's fingerprint."
        ),
    )

    class Meta:
        model = NSOSnmpCommunityState
        fields = ["access", "acl", "vault_ref", "tags"]

    def clean(self):
        cleaned = super().clean() or self.cleaned_data
        secret = cleaned.get("secret_value") or ""
        ref = (cleaned.get("vault_ref") or "").strip()
        kv_mount, base_path = _vault_settings_layout()
        self._old_hash = self.instance.community_hash
        self._secret_result = None

        new_hash = secret_fingerprint(secret) if secret else None
        if new_hash:
            collision = (
                NSOSnmpCommunityState.objects.filter(management=self.instance.management, community_hash=new_hash)
                .exclude(pk=self.instance.pk)
                .exists()
            )
            if collision:
                self.add_error("secret_value", "This value matches another community on the same device.")
            if not ref:
                if kv_mount and base_path:
                    ref = f"{kv_mount}/{base_path}/community/{new_hash}#community"
                else:
                    self.add_error(
                        "secret_value",
                        "No Vault ref on this row and no enabled Vault settings to derive one — "
                        "configure Settings → Vault or paste a ref first.",
                    )

        if ref:
            try:
                ref = qualify_snmp_ref(ref, kind="community", kv_mount=kv_mount, base_path=base_path)
                parse_vault_ref(ref, require_key=True)
            except VaultRefError as exc:
                self.add_error("vault_ref", str(exc))
            else:
                cleaned["vault_ref"] = ref

        if secret and not self.errors:
            from . import adapter_client

            key = parse_vault_ref(ref, require_key=True).key
            try:
                result = adapter_client.set_secret(ref, {key: secret})
            except adapter_client.AdapterError as exc:
                self.add_error("secret_value", f"Vault write failed: {adapter_client.public_error_message(exc)}")
            else:
                self._secret_result = {"hash": new_hash, "version": result.get("version")}
        return cleaned

    def save(self, *args, **kwargs):
        if self._secret_result:
            from django.utils import timezone

            self.instance.community_hash = self._secret_result["hash"]
            self.instance.vault_secret_hash = self._secret_result["hash"]
            self.instance.vault_secret_version = self._secret_result["version"]
            self.instance.status = "accepted"
            self.instance.accepted_at = timezone.now()
        obj = super().save(*args, **kwargs)
        if self._secret_result and self._old_hash and self._old_hash != obj.community_hash:
            # Trap hosts reference the community by hash-as-label; re-point them
            # so the push doesn't reference the rotated-away hash (which the
            # reconciler now rejects instead of configuring it as a community).
            NSOSnmpHostState.objects.filter(management=obj.management, community_hash=self._old_hash).update(
                community_hash=obj.community_hash
            )
        return obj


class NSOSnmpV3UserStateForm(NetBoxModelForm):
    """Edit an SNMP v3 user overlay — group/protocols, Vault ref, or set secret values.

    The secret fields are write-only (one adapter call writes Vault; nothing is
    stored in NetBox). ``vault_ref`` is a PATH ref ('mount/path'); the auth and
    priv passwords live at its ``auth``/``priv`` fields — writes MERGE, so
    setting only one leg preserves the other.
    """

    auth_secret_value = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        label="Set auth password",
        help_text="Written to Vault field 'auth' at the ref. Requires an auth protocol.",
    )
    priv_secret_value = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        label="Set priv password",
        help_text="Written to Vault field 'priv' at the ref. Requires a priv protocol.",
    )

    class Meta:
        model = NSOSnmpV3UserState
        fields = ["group_name", "auth_protocol", "priv_protocol", "vault_ref", "tags"]

    def clean(self):
        cleaned = super().clean() or self.cleaned_data
        auth_secret = cleaned.get("auth_secret_value") or ""
        priv_secret = cleaned.get("priv_secret_value") or ""
        ref = (cleaned.get("vault_ref") or "").strip()
        kv_mount, base_path = _vault_settings_layout()
        self._secret_result = None

        if auth_secret and not cleaned.get("auth_protocol"):
            self.add_error("auth_protocol", "Setting an auth password requires an auth protocol.")
        if priv_secret and not cleaned.get("priv_protocol"):
            self.add_error("priv_protocol", "Setting a priv password requires a priv protocol.")
        if cleaned.get("priv_protocol") and not cleaned.get("auth_protocol"):
            self.add_error("priv_protocol", "SNMPv3 privacy requires authentication (authPriv).")

        if (auth_secret or priv_secret) and not ref:
            if kv_mount and base_path:
                ref = f"{kv_mount}/{base_path}/v3/{self.instance.username}"
            else:
                self.add_error(
                    "auth_secret_value" if auth_secret else "priv_secret_value",
                    "No Vault ref on this row and no enabled Vault settings to derive one — "
                    "configure Settings → Vault or paste a ref first.",
                )

        if ref:
            try:
                ref = qualify_snmp_ref(ref, kind="v3", kv_mount=kv_mount, base_path=base_path)
                parse_vault_ref(ref, require_key=False)
            except VaultRefError as exc:
                self.add_error("vault_ref", str(exc))
            else:
                cleaned["vault_ref"] = ref

        if (auth_secret or priv_secret) and not self.errors:
            from . import adapter_client

            values = {}
            if auth_secret:
                values["auth"] = auth_secret
            if priv_secret:
                values["priv"] = priv_secret
            try:
                result = adapter_client.set_secret(ref, values)
            except adapter_client.AdapterError as exc:
                self.add_error(None, f"Vault write failed: {adapter_client.public_error_message(exc)}")
            else:
                self._secret_result = {"fields": set(values), "version": result.get("version")}
        return cleaned

    def save(self, *args, **kwargs):
        if self._secret_result:
            from django.utils import timezone

            if "auth" in self._secret_result["fields"]:
                self.instance.vault_has_auth = True
            if "priv" in self._secret_result["fields"]:
                self.instance.vault_has_priv = True
            self.instance.status = "accepted"
            self.instance.accepted_at = timezone.now()
        return super().save(*args, **kwargs)


class NSOSnmpHostStateForm(NetBoxModelForm):
    """Edit an SNMP trap/inform host overlay.

    ``username`` is the SNMPv3 security user name. It is editable because a v3 host cannot be
    pushed without one — both NSO writers key the receiver on it — and an operator provisioning a
    greenfield v3 host has no device to import it from (CR-P16).
    """

    class Meta:
        model = NSOSnmpHostState
        fields = ["address", "version", "notify_type", "port", "username", "tags"]


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


class NSOLoggingLevelStateForm(NetBoxModelForm):
    """Edit the per-device local logging severity levels overlay (console/monitor/module)."""

    class Meta:
        model = NSOLoggingLevelState
        fields = ["console_severity", "monitor_severity", "module_severity", "tags"]


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


# Fallback address-family choices used only if netbox_routing is somehow unimportable at
# form-build time; the real BGPAddressFamilyChoices supersede these in __init__.
_BGP_AF_FALLBACK = (
    ("ipv4-unicast", "IPv4 Unicast"),
    ("ipv6-unicast", "IPv6 Unicast"),
    ("vpnv4-unicast", "VPNv4 Unicast"),
    ("vpnv6-unicast", "VPNv6 Unicast"),
)


class NSOBgpPeerGreenfieldForm(forms.Form):
    """Create a greenfield BGP peer scoped to one managed device (in-tab "Add BGP peer").

    Device-scoped: the operator only supplies the local ASN (pre-filled from an existing
    BGPRouter when the device already has one), an optional VRF, the neighbor address, and
    the per-peer attributes. The view assembles the netbox-routing object graph
    (BGPRouter → BGPScope → BGPPeer + address-families) the reconciler would build for a
    brownfield peer, so the greenfield signal owns it as an accepted overlay and a later
    reconcile of the same peer reuses that identity (no duplicate).
    """

    local_asn = DynamicModelChoiceField(
        queryset=ASN.objects.all(),
        label="Local ASN",
        help_text="This device's local BGP ASN (its BGPRouter). Pre-filled if already known.",
    )
    vrf = DynamicModelChoiceField(
        queryset=VRF.objects.all(),
        required=False,
        label="VRF",
        help_text="Leave blank for the global routing table.",
    )
    peer = DynamicModelChoiceField(
        queryset=IPAddress.objects.all(),
        label="Peer address",
        help_text="The neighbor's IP address.",
    )
    remote_as = DynamicModelChoiceField(
        queryset=ASN.objects.all(),
        required=False,
        label="Remote AS",
    )
    peer_local_as = DynamicModelChoiceField(
        queryset=ASN.objects.all(),
        required=False,
        label="Peer local-as",
        help_text="Optional per-peer local-as override (BGP local-as).",
    )
    ttl = forms.IntegerField(
        required=False,
        min_value=0,
        max_value=255,
        label="TTL",
        help_text="eBGP multihop / GTSM TTL (0–255).",
    )
    source = DynamicModelChoiceField(
        queryset=IPAddress.objects.all(),
        required=False,
        label="Source address",
        help_text="Junos / Nokia local-address (an IP).",
    )
    update_source = DynamicModelChoiceField(
        queryset=Interface.objects.all(),
        required=False,
        label="Update source",
        help_text="IOS / IOS-XR update-source (an interface on this device).",
    )
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=True),
        label="Password",
    )
    enabled = forms.BooleanField(required=False, initial=True, label="Enabled")
    address_families = forms.MultipleChoiceField(
        required=False,
        choices=_BGP_AF_FALLBACK,
        initial=["ipv4-unicast"],
        label="Address families",
        help_text="Activate the neighbor under these AFs (default IPv4 Unicast).",
    )

    def __init__(self, *args, device=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.device = device
        # Real AF vocabulary from netbox_routing when available.
        try:
            from netbox_routing.choices import BGPAddressFamilyChoices

            self.fields["address_families"].choices = BGPAddressFamilyChoices
        except ImportError:
            pass
        # Optional peer-group field only when netbox_routing exposes the template model.
        try:
            from netbox_routing.models import BGPPeerTemplate

            self.fields["peer_group"] = DynamicModelChoiceField(
                queryset=BGPPeerTemplate.objects.all(),
                required=False,
                label="Peer group",
            )
        except ImportError:
            pass
        if device is not None:
            # Scope the update-source picker to this device's interfaces.
            self.fields["update_source"].queryset = Interface.objects.filter(device=device)
            self.fields["update_source"].widget.add_query_param("device_id", device.pk)
            # Pre-fill the local ASN from an existing BGPRouter on this device.
            if not self.is_bound:
                self.fields["local_asn"].initial = self._existing_local_asn(device)

    @staticmethod
    def _existing_local_asn(device):
        """Return the ASN pk of a BGPRouter already assigned to *device*, or None."""
        try:
            from dcim.models import Device
            from django.contrib.contenttypes.models import ContentType
            from netbox_routing.models import BGPRouter
        except ImportError:
            return None
        ct = ContentType.objects.get_for_model(Device)
        router = BGPRouter.objects.filter(assigned_object_type=ct, assigned_object_id=device.pk).first()
        return router.asn_id if router else None
