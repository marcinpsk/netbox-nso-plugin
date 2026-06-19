# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel


class AdapterConnection(NetBoxModel):
    """Singleton — URL and non-secret connection settings for the nso-adapter.

    The bearer token is intentionally absent; it is always read from
    PLUGINS_CONFIG / env and never stored in the database.
    When this record exists and ``enabled=True`` its values override
    PLUGINS_CONFIG for the URL and non-secret settings.
    """

    # CharField, not URLField: Django's URLValidator rejects single-label hosts
    # (e.g. http://nso-adapter:8000, a Docker service name), which is a valid and
    # preferred way to reach the adapter. Reachability is exercised at request time.
    url = models.CharField(
        max_length=200,
        blank=True,
        help_text=(
            "nso-adapter base URL, e.g. http://nso-adapter:8000 (a Docker service "
            "name is fine). Overrides the env bootstrap when set; leave blank to use "
            "PLUGINS_CONFIG / env."
        ),
    )
    verify_tls = models.BooleanField(
        default=True,
        help_text="Verify TLS certificates when calling the adapter.",
    )
    ca_cert_path = models.CharField(
        max_length=500,
        blank=True,
        help_text="Path to a CA bundle file on the NetBox host. Leave blank to use the system trust store.",
    )
    timeout_seconds = models.PositiveIntegerField(
        default=30,
        help_text="Request timeout in seconds.",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="When disabled the plugin falls back to PLUGINS_CONFIG / env for all settings.",
    )
    static_route_auto_create = models.BooleanField(
        default=False,
        help_text=(
            "Auto-create netbox_routing.StaticRoute objects from NSO during reconcile. "
            "When off, NSO static routes only correlate to manually-created routes."
        ),
    )
    interface_ip_auto_create = models.BooleanField(
        default=False,
        help_text="Auto-create interface IP addresses from NSO during reconcile.",
    )
    vrf_auto_create = models.BooleanField(
        default=False,
        help_text=(
            "Auto-create ipam.VRF objects referenced by NSO routes when missing. "
            "When off, routes in an unknown VRF are skipped (logged)."
        ),
    )
    onboard_authgroup = models.CharField(
        max_length=128,
        default="network",
        help_text="NSO authgroup applied when onboarding a device from NetBox (the default auth group).",
    )

    class Meta:
        verbose_name = "Adapter Connection"
        verbose_name_plural = "Adapter Connection"

    def __str__(self):
        return self.url or "nso-adapter (not configured)"

    def get_absolute_url(self):
        """Return the URL for the singleton edit view."""
        return reverse("plugins:netbox_nso_plugin:adapterconnection")

    def save(self, *args, **kwargs):
        """Enforce singleton: reuse the existing row's PK when creating a second instance."""
        if not self.pk:
            existing = AdapterConnection.objects.first()
            if existing:
                self.pk = existing.pk
        super().save(*args, **kwargs)


class NSOFailoverSettings(NetBoxModel):
    """Singleton — global mgmt-IP failover tuning, pushed to the adapter on save.

    Mirrors the adapter's ``FailoverConfig``. A post_save signal PUTs these to
    ``/api/v1/config/failover``; the adapter's base tick reads them live (next tick), so a
    change applies without restarting either service. Defaults are the perf-spike prod values
    (primary 15 min, OOB 6 h) — the dominant fleet cost is an unreachable connect, so probe
    *concurrency* (not the timers) is the load lever. See the failover-perf-spike writeup.
    """

    enabled = models.BooleanField(
        default=True,
        help_text="Master switch for the adapter's failover loop (live on/off, no restart).",
    )
    primary_probe_interval = models.PositiveIntegerField(
        default=15,
        validators=[MinValueValidator(1)],
        help_text="Minutes between probes of the primary (in-band) management IP.",
    )
    oob_probe_interval = models.PositiveIntegerField(
        default=360,
        validators=[MinValueValidator(1)],
        help_text="Minutes between fallback-health checks of the OOB IP (6–12 h is typical).",
    )
    failure_threshold = models.PositiveIntegerField(
        default=3,
        validators=[MinValueValidator(1)],
        help_text="Consecutive failed primary probes before failing over to OOB.",
    )
    success_threshold = models.PositiveIntegerField(
        default=5,
        validators=[MinValueValidator(1)],
        help_text="Consecutive good primary probes before failing back from OOB (keep > failure to damp flapping).",
    )
    probe_timeout = models.PositiveIntegerField(
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(120)],
        help_text="Seconds before an unreachable connect is given up — short, so a down device can't stall the loop.",
    )
    probe_concurrency = models.PositiveIntegerField(
        default=8,
        validators=[MinValueValidator(1), MaxValueValidator(64)],
        help_text="How many devices the adapter probes at once — the load lever for an unreachable fleet.",
    )
    max_flips_per_tick = models.PositiveIntegerField(
        default=8,
        validators=[MinValueValidator(1), MaxValueValidator(256)],
        help_text="Cap on disruptive address flips per tick (a safety belt against NSO churn).",
    )
    sync_from_after_switch = models.BooleanField(
        default=True,
        help_text="Run sync-from after a primary↔OOB switch so NSO's CDB matches the device.",
    )

    class Meta:
        verbose_name = "Failover Settings"
        verbose_name_plural = "Failover Settings"

    def __str__(self):
        return "NSO Failover Settings"

    def get_absolute_url(self):
        """Return the URL for the singleton edit view."""
        return reverse("plugins:netbox_nso_plugin:nsofailoversettings")

    def save(self, *args, **kwargs):
        """Enforce singleton: reuse the existing row's PK when creating a second instance."""
        if not self.pk:
            existing = NSOFailoverSettings.objects.first()
            if existing:
                self.pk = existing.pk
        super().save(*args, **kwargs)


class _NSODeviceTabURLMixin:
    """Resolve get_absolute_url to the device's NSO tab.

    Overlay rows have no detail view of their own, so NetBox delete-dependency /
    linkify rendering raises NoReverseMatch when a parent object (route, VLAN,
    interface, ...) with an overlay is deleted. Point at the device NSO tab instead.
    """

    def get_absolute_url(self):
        mgmt = getattr(self, "management", None)
        if mgmt is not None:
            return reverse("dcim:device_nso", kwargs={"pk": mgmt.device_id})
        iface = getattr(self, "interface", None)
        if iface is not None:
            return reverse("dcim:device_nso", kwargs={"pk": iface.device_id})
        return reverse("plugins:netbox_nso_plugin:nsodevicemanagement_list")


class NSOInstance(NetBoxModel):
    """Represents a Cisco NSO instance registered in the adapter."""

    name = models.CharField(max_length=100, unique=True)
    adapter_instance_id = models.CharField(
        max_length=100,
        unique=True,
        help_text="The instance ID used by the nso-adapter (matches adapter config).",
    )
    is_default = models.BooleanField(
        default=False,
        help_text=(
            "Pre-selected when onboarding a new device. The first instance created "
            "becomes the default automatically; setting another clears the previous one."
        ),
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "NSO Instance"
        verbose_name_plural = "NSO Instances"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        """Return the detail URL for this instance."""
        return reverse("plugins:netbox_nso_plugin:nsoinstance", args=[self.pk])

    @classmethod
    def get_default(cls):
        """Return the default NSO instance, or None if none exists."""
        return cls.objects.filter(is_default=True).first()

    def save(self, *args, **kwargs):
        """Keep exactly one default instance.

        The first instance created becomes the default automatically; marking
        another as default clears the previous one.
        """
        # If no other default exists (e.g. this is the first instance), force this
        # one to be the default so onboarding always has something to pre-select.
        if not NSOInstance.objects.exclude(pk=self.pk).filter(is_default=True).exists():
            self.is_default = True
        super().save(*args, **kwargs)
        if self.is_default:
            NSOInstance.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)


class NSOPlatformNedMapping(NetBoxModel):
    """Maps a NetBox Platform to an NSO NED ID, for device onboarding.

    The onboarding flow is hybrid: it suggests a NED from the NED's vendor/OS
    metadata, the operator confirms, and the confirmed Platform→ned_id lands here
    and is reused as the default. This is the editable, user-visible source of
    truth for "what NED does a device of this platform onboard with" — it drives
    the onboardable-candidates tile and pre-selects the NED on onboard.
    """

    platform = models.OneToOneField(
        to="dcim.Platform",
        on_delete=models.CASCADE,
        related_name="nso_ned_mapping",
    )
    ned_id = models.CharField(
        max_length=128,
        help_text="NSO NED ID, e.g. cisco-ios-cli-6.114:cisco-ios-cli-6.114 (see NSO instance NEDs).",
    )

    class Meta:
        ordering = ["platform"]
        verbose_name = "NSO Platform-NED Mapping"
        verbose_name_plural = "NSO Platform-NED Mappings"

    def __str__(self):
        return f"{self.platform} → {self.ned_id}"

    def get_absolute_url(self):
        """Return the detail URL for this mapping."""
        return reverse("plugins:netbox_nso_plugin:nsoplatformnedmapping", args=[self.pk])


class NSODeviceManagement(NetBoxModel):
    """Scope record — one per NSO-managed NetBox device."""

    device = models.OneToOneField(
        to="dcim.Device",
        on_delete=models.CASCADE,
        related_name="nso_management",
    )
    nso_instance = models.ForeignKey(
        to=NSOInstance,
        on_delete=models.PROTECT,
        related_name="managed_devices",
    )
    nso_device_name = models.CharField(
        max_length=255,
        help_text="Device name in NSO. Defaults to the NetBox device name.",
    )
    manage_description = models.BooleanField(
        default=False,
        help_text="Sync interface description attribute from NSO.",
    )
    manage_enabled = models.BooleanField(
        default=False,
        help_text="Sync interface enabled/shutdown attribute from NSO.",
    )
    # ── Management scopes (opt-in) ────────────────────────────────────────────
    # Two-level switches that gate which sections of the NSO device tab are
    # active. The group masters (manage_interfaces, manage_routing) are real
    # kill-switches: a section shows only if its master AND its leaf flag are
    # set. The edit form auto-checks a master when any child is checked, and
    # unchecking a master disables the whole group while remembering child
    # selections. Default False so brownfield devices are onboarded one scope at
    # a time; existing rows are backfilled to fully-enabled in migration 0016.
    manage_interfaces = models.BooleanField(
        default=False,
        help_text="Master switch for interface-attribute management (description/enabled).",
    )
    manage_routing = models.BooleanField(
        default=False,
        help_text="Master switch for routing management. Enable, then pick protocols below.",
    )
    manage_static = models.BooleanField(
        default=False,
        help_text="Manage static routes.",
    )
    manage_isis = models.BooleanField(
        default=False,
        help_text="Manage IS-IS interfaces and instances.",
    )
    manage_ospf = models.BooleanField(
        default=False,
        help_text="Manage OSPF instances and interfaces.",
    )
    manage_bgp = models.BooleanField(
        default=False,
        help_text="Manage BGP peers.",
    )
    manage_route_policy = models.BooleanField(
        default=False,
        help_text="Manage route policy objects.",
    )
    manage_redistribution = models.BooleanField(
        default=False,
        help_text="Manage redistribution statements.",
    )
    manage_snmp = models.BooleanField(
        default=False,
        help_text="Manage SNMP configuration for this device.",
    )
    manage_logging = models.BooleanField(
        default=False,
        help_text="Manage logging/syslog configuration for this device.",
    )
    manage_l2 = models.BooleanField(
        default=False,
        help_text=(
            "Manage L2 for this device — the master switch for the whole L2 domain "
            "(L2VPN/VPLS/epipe, EVPN, and VLAN-database/switchport), the L2 analogue of "
            "manage_routing."
        ),
    )
    auto_apply = models.BooleanField(
        default=False,
        help_text=(
            "When True, every accept of a value on this device enqueues an apply job "
            "on the adapter. Disabled by default so brownfield devices are brought into "
            "management one cautious push at a time."
        ),
    )
    sync_before_apply = models.BooleanField(
        default=True,
        help_text=(
            "When True, the adapter sync-froms this device before each apply, clearing the "
            "NSO/device out-of-sync state a timed-out or partial prior commit can leave (which "
            "would otherwise make the next apply fail). Disable for NEDs that already sync on "
            "connect."
        ),
    )
    adapter_device_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="The device ID assigned by the nso-adapter after onboarding.",
    )
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_status = models.CharField(max_length=50, blank=True, default="")
    last_journaled_apply_job = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "Adapter apply job id whose route-policy outcome was last written to object "
            "journals. Idempotency guard so a re-run of the post-apply reconcile does not "
            "re-post the same apply to the netbox-routing object journals."
        ),
    )
    state_snapshot = models.JSONField(
        null=True,
        blank=True,
        help_text="Cached sync-state counts and per-interface statuses from the last sync.",
    )
    onboarded_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this device was provisioned into NSO via the plugin onboarding action.",
    )
    onboard_steps = models.JSONField(
        null=True,
        blank=True,
        help_text="Step-by-step result of the last plugin onboarding (create / fetch-host-keys / unlock / sync-from).",
    )

    class Meta:
        ordering = ["device"]
        verbose_name = "NSO Device Management"
        verbose_name_plural = "NSO Device Management"

    def __str__(self):
        return f"{self.device} → {self.nso_instance.name}/{self.nso_device_name}"

    def get_absolute_url(self):
        """Return the detail URL for this management record."""
        return reverse("plugins:netbox_nso_plugin:nsodevicemanagement", args=[self.pk])

    @property
    def managed_attributes(self):
        """Return list of managed attribute names."""
        attrs = []
        if self.manage_description:
            attrs.append("description")
        if self.manage_enabled:
            attrs.append("enabled")
        return attrs

    @property
    def routing_protocols(self):
        """Return the enabled routing-protocol scope labels, in display order.

        Only meaningful when manage_routing (the master) is set; used for the
        Managed Scopes display and gated everywhere by manage_routing.
        """
        protos = []
        if self.manage_isis:
            protos.append("IS-IS")
        if self.manage_ospf:
            protos.append("OSPF")
        if self.manage_bgp:
            protos.append("BGP")
        if self.manage_static:
            protos.append("Static")
        if self.manage_route_policy:
            protos.append("Route Policy")
        if self.manage_redistribution:
            protos.append("Redistribution")
        return protos

    @property
    def managed_scopes(self):
        """Return the enabled top-level management scopes for display.

        Each group is gated by its master flag, so an orphaned leaf (a protocol
        checked while its master is off) does not surface here — matching the
        kill-switch gating used in the view and templates.
        """
        scopes = []
        if self.manage_interfaces:
            scopes.append("Interfaces")
        if self.manage_routing:
            protos = self.routing_protocols
            scopes.append("Routing ({})".format(", ".join(protos)) if protos else "Routing")
        if self.manage_snmp:
            scopes.append("SNMP")
        if self.manage_logging:
            scopes.append("Logging")
        return scopes


class NSOInterfaceState(NetBoxModel):
    """Per-interface, per-attribute intent status overlay (Phase 2).

    Intent value lives on ``dcim.Interface`` (description/enabled fields).
    This model holds the *status overlay*: what the adapter last reported,
    what has been accepted, and when it was last applied.

    Unique constraint: one row per (interface, attribute).
    """

    ATTRIBUTE_CHOICES = [
        ("description", "Description"),
        ("enabled", "Enabled"),
    ]

    STATUS_CHOICES = [
        ("unknown", "Unknown"),
        ("imported", "Imported"),
        ("changed", "Changed"),
        ("accepted", "Accepted"),
        ("deploying", "Deploying"),
        ("in_sync", "In Sync"),
        ("apply_failed", "Apply Failed"),
        ("error", "Error"),
    ]

    interface = models.ForeignKey(
        to="dcim.Interface",
        on_delete=models.CASCADE,
        related_name="nso_states",
    )
    attribute = models.CharField(max_length=64, choices=ATTRIBUTE_CHOICES)
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default="unknown",
    )
    nso_value = models.TextField(
        blank=True,
        null=True,
        help_text="Last value reported by NSO (cached for display).",
    )
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.JSONField(
        null=True,
        blank=True,
        help_text="Populated when status=apply_failed. Contains code/message/detail.",
    )

    class Meta:
        ordering = ["interface", "attribute"]
        unique_together = [("interface", "attribute")]
        verbose_name = "NSO Interface State"
        verbose_name_plural = "NSO Interface States"

    def __str__(self):
        return f"{self.interface} / {self.attribute} [{self.status}]"

    def get_absolute_url(self):
        """Return the absolute URL for this object."""
        return reverse("plugins:netbox_nso_plugin:nsointerfacestate", args=[self.pk])


class NSOInterfaceIPState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-interface, per-address IP address status overlay (Phase 3).

    Tracks the synchronisation state for each IP address reported by NSO for a
    managed interface.  Intent is driven by NetBox IPAM (``ipam.IPAddress``);
    this model holds the *status overlay*.

    Unique constraint: one row per (interface, address, vrf).
    Empty-string ``vrf`` means the global/default routing table.
    """

    STATUS_CHOICES = [
        ("unknown", "Unknown"),
        ("imported", "Imported"),
        ("changed", "Changed"),
        ("accepted", "Accepted"),
        ("deploying", "Deploying"),
        ("in_sync", "In Sync"),
        ("apply_failed", "Apply Failed"),
        ("error", "Error"),
        ("conflict", "Conflict"),  # address already assigned elsewhere in NetBox
    ]

    interface = models.ForeignKey(
        to="dcim.Interface",
        on_delete=models.CASCADE,
        related_name="nso_ip_states",
    )
    address = models.CharField(
        max_length=64,
        help_text="IP address in 'ip/prefix-length' notation (e.g. 10.0.0.1/24).",
    )
    vrf = models.CharField(
        max_length=256,
        blank=True,
        default="",
        help_text="VRF name; empty string means the global routing table.",
    )
    family = models.CharField(
        max_length=8,
        default="ipv4",
        help_text="Address family: ipv4 or ipv6 (derived, informational).",
    )
    secondary = models.BooleanField(
        default=False,
        help_text="True if this is a secondary IP address on the interface.",
    )
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default="unknown",
    )
    nso_value = models.TextField(
        blank=True,
        null=True,
        help_text="Last address string reported by NSO (cached for display).",
    )
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.JSONField(
        null=True,
        blank=True,
        help_text="Populated when status=apply_failed.",
    )
    # auto-assignment fields
    auto_assigned = models.BooleanField(
        default=False,
        help_text="True when this address was minted by the IP auto-assignment engine.",
    )
    source_pool = models.ForeignKey(
        to="ipam.Prefix",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_ip_states_from_pool",
        help_text="The Prefix pool this address was drawn from (audit trail).",
    )
    peer_state = models.ForeignKey(
        to="self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="peer_back",
        help_text="The other end of a P2P pair (reserve-then-activate).",
    )

    class Meta:
        ordering = ["interface", "address", "vrf"]
        unique_together = [("interface", "address", "vrf")]
        verbose_name = "NSO Interface IP State"
        verbose_name_plural = "NSO Interface IP States"

    def __str__(self):
        vrf_label = f" [{self.vrf}]" if self.vrf else ""
        return f"{self.interface} / {self.address}{vrf_label} [{self.status}]"


# ─── SNMP state overlays ─────────────────────────────────────────────────

_SNMP_STATUS_CHOICES = [
    ("unknown", "Unknown"),
    ("imported", "Imported"),
    ("accepted", "Accepted"),
    ("deploying", "Deploying"),
    ("in_sync", "In Sync"),
    ("apply_failed", "Apply Failed"),
    ("conflict", "Conflict"),
    ("error", "Error"),
]


class NSOSnmpCommunityState(NetBoxModel):
    """Per-device SNMP community status overlay (read path).

    The community string itself is never stored — only its opaque SHA-256 hash
    (``community_hash``) as published by the NSO package.  A Vault reference
    (``vault_ref``) links to the secret needed for the write path (Phase B).
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="snmp_community_states",
    )
    community_hash = models.CharField(
        max_length=64,
        help_text="Opaque SHA-256 prefix of the community string (16 hex chars).",
    )
    access = models.CharField(
        max_length=8,
        default="RO",
        help_text="Access mode: RO or RW.",
    )
    acl = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="ACL name bound to this community entry, if any.",
    )
    has_secret = models.BooleanField(
        default=True,
        help_text="Always True for community entries — community name IS the secret.",
    )
    vault_ref = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="Vault KV path#key for the community string (required for the write path).",
    )
    status = models.CharField(max_length=32, choices=_SNMP_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When an operator accepted this row (NetBox becomes source of truth)."
    )

    class Meta:
        ordering = ["management", "community_hash"]
        unique_together = [("management", "community_hash")]
        verbose_name = "NSO SNMP Community State"
        verbose_name_plural = "NSO SNMP Community States"

    def __str__(self):
        return f"{self.management} / community:{self.community_hash} [{self.status}]"

    def get_absolute_url(self):
        """Return the device NSO tab URL (the overlay's detail; used by edit redirects)."""
        from django.urls import reverse

        return reverse("dcim:device_nso", kwargs={"pk": self.management.device_id})


class NSOSnmpV3UserState(NetBoxModel):
    """Per-device SNMP v3 user status overlay (read path).

    Passwords are never stored — ``has_auth_secret`` / ``has_priv_secret``
    indicate whether the NSO device has secrets set.  ``vault_ref`` carries the
    Vault path for the write path (Phase B).
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="snmp_v3_user_states",
    )
    username = models.CharField(max_length=128)
    has_auth_secret = models.BooleanField(default=False)
    has_priv_secret = models.BooleanField(default=False)
    vault_ref = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="Vault KV path#key for the v3 auth/priv secrets (required for the write path).",
    )
    status = models.CharField(max_length=32, choices=_SNMP_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When an operator accepted this row (NetBox becomes source of truth)."
    )

    class Meta:
        ordering = ["management", "username"]
        unique_together = [("management", "username")]
        verbose_name = "NSO SNMP V3 User State"
        verbose_name_plural = "NSO SNMP V3 User States"

    def __str__(self):
        return f"{self.management} / v3:{self.username} [{self.status}]"

    def get_absolute_url(self):
        """Return the device NSO tab URL (the overlay's detail; used by edit redirects)."""
        from django.urls import reverse

        return reverse("dcim:device_nso", kwargs={"pk": self.management.device_id})


class NSOSnmpHostState(NetBoxModel):
    """Per-device SNMP trap/inform host status overlay (read path)."""

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="snmp_host_states",
    )
    address = models.CharField(max_length=256, help_text="Trap/inform target address.")
    version = models.CharField(max_length=8, default="v2c", help_text="SNMP version: v1, v2c, v3.")
    notify_type = models.CharField(
        max_length=16,
        default="trap",
        help_text="Notification type: trap or inform.",
    )
    port = models.PositiveIntegerField(null=True, blank=True, help_text="UDP port (null = default 162).")
    community_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Hash of the community string used for this host (v1/v2c only).",
    )
    status = models.CharField(max_length=32, choices=_SNMP_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When an operator accepted this row (NetBox becomes source of truth)."
    )

    class Meta:
        ordering = ["management", "address"]
        unique_together = [("management", "address")]
        verbose_name = "NSO SNMP Host State"
        verbose_name_plural = "NSO SNMP Host States"

    def __str__(self):
        return f"{self.management} / host:{self.address} [{self.status}]"

    def get_absolute_url(self):
        """Return the device NSO tab URL (the overlay's detail; used by edit redirects)."""
        from django.urls import reverse

        return reverse("dcim:device_nso", kwargs={"pk": self.management.device_id})


class NSOSnmpSystemInfoState(NetBoxModel):
    """Per-device SNMP system location/contact status overlay (read path).

    At most one row per device management object (enforced by OneToOneField).
    """

    management = models.OneToOneField(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="snmp_system_info_state",
    )
    location = models.CharField(max_length=256, blank=True, default="")
    contact = models.CharField(max_length=256, blank=True, default="")
    status = models.CharField(max_length=32, choices=_SNMP_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When an operator accepted this row (NetBox becomes source of truth)."
    )

    class Meta:
        verbose_name = "NSO SNMP System Info State"
        verbose_name_plural = "NSO SNMP System Info States"

    def __str__(self):
        return f"{self.management} / system-info [{self.status}]"

    def get_absolute_url(self):
        """Return the device NSO tab URL (the overlay's detail; used by edit redirects)."""
        from django.urls import reverse

        return reverse("dcim:device_nso", kwargs={"pk": self.management.device_id})


class NSOLoggingHostState(NetBoxModel):
    """Per-device remote syslog server (logging host) status overlay (read path)."""

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="logging_host_states",
    )
    address = models.CharField(max_length=256, help_text="Remote syslog server address.")
    port = models.PositiveIntegerField(null=True, blank=True, help_text="Destination port (null = NED default 514).")
    severity = models.CharField(max_length=32, blank=True, default="", help_text="Minimum severity sent.")
    facility = models.CharField(max_length=32, blank=True, default="", help_text="Syslog facility, when set.")
    transport = models.CharField(max_length=16, blank=True, default="", help_text="Transport (udp/tcp), when set.")
    vrf = models.CharField(max_length=128, blank=True, default="", help_text="VRF/routing-instance, when set.")
    source = models.CharField(max_length=256, blank=True, default="", help_text="Source interface/address, when set.")
    status = models.CharField(max_length=32, choices=_SNMP_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When an operator accepted this row (NetBox becomes source of truth)."
    )

    class Meta:
        ordering = ["management", "address"]
        unique_together = [("management", "address")]
        verbose_name = "NSO Logging Host State"
        verbose_name_plural = "NSO Logging Host States"

    def __str__(self):
        return f"{self.management} / syslog:{self.address} [{self.status}]"

    def get_absolute_url(self):
        """Return the device NSO tab URL (the overlay's detail; used by edit redirects)."""
        from django.urls import reverse

        return reverse("dcim:device_nso", kwargs={"pk": self.management.device_id})


_STATIC_ROUTE_STATUS_CHOICES = [
    ("unknown", "Unknown"),
    ("imported", "Imported"),
    ("accepted", "Accepted"),
    ("deploying", "Deploying"),
    ("in_sync", "In Sync"),
    ("apply_failed", "Apply Failed"),
    ("conflict", "Conflict"),
    ("changed", "Changed"),
    ("error", "Error"),
]


class NSOStaticRouteState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, static_route) compliance overlay for static routing.

    One row exists per (NSODeviceManagement, StaticRoute) pair.  The StaticRoute
    object itself is shared across devices via M2M — this row tracks the
    compliance status for each individual (device, route) pairing.

    The ``conflict`` status is set when the device reports a route matching an
    existing StaticRoute that is *not* associated with this device — requiring an
    explicit operator decision before the plugin will add the device to the M2M.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="static_route_states",
    )
    static_route = models.ForeignKey(
        to="netbox_routing.StaticRoute",
        on_delete=models.CASCADE,
        related_name="nso_states",
    )
    status = models.CharField(max_length=32, choices=_STATIC_ROUTE_STATUS_CHOICES, default="unknown")
    nso_vrf = models.CharField(max_length=128, blank=True, default="")
    nso_prefix = models.CharField(max_length=64, blank=True, default="")
    nso_next_hop = models.CharField(max_length=64, blank=True, default="")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["management", "static_route"]
        unique_together = [("management", "static_route")]
        verbose_name = "NSO Static Route State"
        verbose_name_plural = "NSO Static Route States"

    def __str__(self):
        return f"{self.management} / {self.nso_prefix} [{self.status}]"


_L2_SAP_STATUS_CHOICES = [
    ("unknown", "Unknown"),
    ("imported", "Imported"),
    ("accepted", "Accepted"),
    ("deploying", "Deploying"),
    ("in_sync", "In Sync"),
    ("apply_failed", "Apply Failed"),
    ("conflict", "Conflict"),
    ("changed", "Changed"),
    ("error", "Error"),
]


class NSOL2SapState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-SAP compliance overlay for Nokia L2 services.

    One row per (device, service, SAP). The service is reconciled into a native
    ``vpn.L2VPN`` (epipe→E-Line, vpls→VPLS) and each SAP into a ``vpn.L2VPNTermination``
    on its port interface; this overlay carries the status/drift, the operator-accept
    marker, and the per-SAP **dot1q encap** (outer/inner) — which has no home on the
    native L2VPNTermination (the tag is interface-local encap, not an ipam.VLAN).
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="l2_sap_states",
    )
    service_name = models.CharField(max_length=64)
    service_type = models.CharField(max_length=16, blank=True, default="")  # epipe | vpls
    service_id = models.PositiveIntegerField(null=True, blank=True)
    sap_id = models.CharField(max_length=64)
    port = models.CharField(max_length=64, blank=True, default="")
    outer_tag = models.PositiveIntegerField(null=True, blank=True)
    inner_tag = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=_L2_SAP_STATUS_CHOICES, default="unknown")
    l2vpn = models.ForeignKey(
        to="vpn.L2VPN",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_l2_sap_states",
    )
    termination = models.ForeignKey(
        to="vpn.L2VPNTermination",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_l2_sap_states",
    )
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["management", "service_name", "sap_id"]
        unique_together = [("management", "service_name", "sap_id")]
        verbose_name = "NSO L2 SAP State"
        verbose_name_plural = "NSO L2 SAP States"

    def __str__(self):
        return f"{self.management} / {self.service_name}:{self.sap_id} [{self.status}]"


_ISIS_STATUS_CHOICES = [
    ("unknown", "Unknown"),
    ("imported", "Imported"),
    ("accepted", "Accepted"),
    ("deploying", "Deploying"),
    ("in_sync", "In Sync"),
    ("apply_failed", "Apply Failed"),
    ("conflict", "Conflict"),
    ("changed", "Changed"),
    ("error", "Error"),
]


class NSOISISInterfaceState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, interface, af) IS-IS enablement compliance overlay.

    Tracks the status of IS-IS interface enablement for each (NSODeviceManagement,
    dcim.Interface, address-family) triple.  The ``status`` lifecycle mirrors the
    other intent models: unknown → imported → accepted → deploying → in_sync /
    apply_failed.

    When netbox-routing ISISInterface objects exist for this device they will
    be linked via ``isis_interface`` (nullable FK added in a later migration).
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="isis_interface_states",
    )
    interface = models.ForeignKey(
        to="dcim.Interface",
        on_delete=models.CASCADE,
        related_name="nso_isis_states",
    )
    af = models.CharField(max_length=8)  # "ipv4" or "ipv6"
    process_tag = models.CharField(max_length=128, blank=True, default="")
    circuit_type = models.CharField(max_length=32, blank=True, default="")
    network_type = models.CharField(max_length=32, blank=True, default="")
    metric = models.PositiveIntegerField(null=True, blank=True)
    passive = models.BooleanField(default=False)
    # Per-interface IIH (hello) authentication, secret-safe (type + present flag;
    # the key is never imported — Junos exports it $9$-encrypted, Nokia hides it).
    hello_auth_type = models.CharField(max_length=32, blank=True, default="")
    hello_auth_present = models.BooleanField(default=False)
    # Linked netbox-routing object (nullable — created/linked by the reconcile)
    isis_interface = models.ForeignKey(
        to="netbox_routing.ISISInterface",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_interface_states",
    )
    status = models.CharField(max_length=32, choices=_ISIS_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")
    device_base_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["management", "interface", "af"]
        unique_together = [("management", "interface", "af")]
        verbose_name = "NSO IS-IS Interface State"
        verbose_name_plural = "NSO IS-IS Interface States"

    def __str__(self):
        return f"{self.management} / {self.interface} ({self.af}) [{self.status}]"


class NSOISISInstanceState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, isis_process_tag) IS-IS process compliance overlay.

    Tracks the status of IS-IS process-level config (net, is-type, metric-style,
    overload-bit, area/domain auth) for each (NSODeviceManagement, process_tag)
    pair.  Status lifecycle mirrors NSOISISInterfaceState.

    ``isis_instance`` links to the netbox-routing ISISInstance once resolved.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="isis_instance_states",
    )
    process_tag = models.CharField(max_length=128)
    # Denormalised read-path fields (what NSO reports)
    net = models.CharField(max_length=100, blank=True, default="")
    is_type = models.CharField(max_length=50, blank=True, default="")
    metric_style = models.CharField(max_length=20, blank=True, default="")
    overload_bit = models.BooleanField(null=True, blank=True)
    area_auth_type = models.CharField(max_length=10, blank=True, default="")
    area_auth_present = models.BooleanField(default=False)
    # Routing-protocol auth keys (NOT device-access credentials). Carried so the
    # write path can push them and the read path can import them; the *present*
    # flags stay authoritative for "is auth configured" when no key is held.
    area_auth_key = models.CharField(max_length=128, blank=True, default="")
    domain_auth_type = models.CharField(max_length=10, blank=True, default="")
    domain_auth_present = models.BooleanField(default=False)
    domain_auth_key = models.CharField(max_length=128, blank=True, default="")
    # Linked netbox-routing object (nullable — may not exist yet)
    isis_instance = models.ForeignKey(
        to="netbox_routing.ISISInstance",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_instance_states",
    )
    status = models.CharField(max_length=32, choices=_ISIS_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")
    device_base_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["management", "process_tag"]
        unique_together = [("management", "process_tag")]
        verbose_name = "NSO IS-IS Instance State"
        verbose_name_plural = "NSO IS-IS Instance States"

    def __str__(self):
        return f"{self.management} / isis {self.process_tag} [{self.status}]"


class NSOISISFlexAlgoState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, process_tag, algo_id) IS-IS Flex-Algorithm compliance overlay.

    Tracks the status of one Flex-Algo definition the operator manages via NSO.
    Status lifecycle mirrors NSOISISInstanceState.  ``isis_flex_algo`` links to
    the netbox-routing ISISFlexAlgo object once resolved.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="isis_flex_algo_states",
    )
    process_tag = models.CharField(max_length=128, blank=True, default="")
    algo_id = models.PositiveSmallIntegerField()
    # Denormalised definition fields (what NSO reports / what we write)
    metric_type = models.CharField(max_length=40, blank=True, default="")
    priority = models.PositiveSmallIntegerField(null=True, blank=True)
    admin_group_exclude = models.CharField(max_length=200, blank=True, default="")
    admin_group_include_any = models.CharField(max_length=200, blank=True, default="")
    admin_group_include_all = models.CharField(max_length=200, blank=True, default="")
    # Linked netbox-routing object (nullable — may not exist yet)
    isis_flex_algo = models.ForeignKey(
        to="netbox_routing.ISISFlexAlgo",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_flex_algo_states",
    )
    status = models.CharField(max_length=32, choices=_ISIS_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")
    device_base_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["management", "process_tag", "algo_id"]
        unique_together = [("management", "process_tag", "algo_id")]
        verbose_name = "NSO IS-IS Flex-Algo State"
        verbose_name_plural = "NSO IS-IS Flex-Algo States"

    def __str__(self):
        return f"{self.management} / flex-algo {self.algo_id} [{self.status}]"


_BGP_STATUS_CHOICES = [
    ("unknown", "Unknown"),
    ("imported", "Imported"),
    ("accepted", "Accepted"),
    ("deploying", "Deploying"),
    ("in_sync", "In Sync"),
    ("apply_failed", "Apply Failed"),
    ("conflict", "Conflict"),
    ("changed", "Changed"),
    ("error", "Error"),
]

_BGP_WRITE_PATH_STATUSES = {"accepted", "deploying", "in_sync", "apply_failed"}


class NSOBGPPeerState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, asn, vrf, peer_address) BGP peer compliance overlay.

    Tracks the reconcile status of each BGP peer discovered from NSO.
    The identity key is (management, asn_str, vrf_name, peer_address_str) — all
    denormalised strings so that unlinked/unresolved peers can still be recorded.

    ``bgp_peer`` links to the netbox-routing BGPPeer object once successfully
    resolved.  It is nullable: a peer that cannot be linked is still tracked
    (status='conflict' or 'error') so the operator can see what NSO reported.

    Status lifecycle: unknown → imported → accepted → deploying → in_sync /
    apply_failed.  ``conflict`` is set when a matching BGPPeer exists but was
    NOT created by this plugin.  ``changed`` is set when NSO no longer reports
    this peer.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="bgp_peer_states",
    )
    asn_str = models.CharField(max_length=10)
    vrf_name = models.CharField(max_length=128, blank=True, default="")
    peer_address_str = models.CharField(max_length=64)
    bgp_peer = models.ForeignKey(
        to="netbox_routing.BGPPeer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_bgp_states",
    )
    remote_as_str = models.CharField(max_length=10, blank=True, default="")
    enabled = models.BooleanField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=_BGP_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")
    # 3-way merge base: hash of the device content at the last agreed sync. Lets the
    # reconciler tell an operator edit (object moved, device == base → freeze/drift)
    # apart from a device-side change (device moved, object == base → auto-mirror).
    device_base_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["management", "asn_str", "vrf_name", "peer_address_str"]
        unique_together = [("management", "asn_str", "vrf_name", "peer_address_str")]
        verbose_name = "NSO BGP Peer State"
        verbose_name_plural = "NSO BGP Peer States"

    def __str__(self):
        vrf_part = f" vrf:{self.vrf_name}" if self.vrf_name else ""
        return f"{self.management} / ASN:{self.asn_str}{vrf_part} peer:{self.peer_address_str} [{self.status}]"


class NSOBGPPeerTemplateState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, peer-group name) BGP peer-group TEMPLATE compliance overlay.

    Tracks the reconcile status of each BGP peer-group template (netbox-routing's
    ``BGPPeerTemplate``) discovered from NSO, with a 3-way merge base so an operator
    edit to the template's per-AF policies is distinguished from a device-side change
    (object moved, device == base → freeze/drift; device moved, object == base →
    auto-mirror; both moved → conflict) — the same clobber-safe contract the BGP peer
    overlay uses, instead of the older seed-once-never-touch behaviour.

    The template is keyed globally by ``name`` in netbox-routing, but each device tracks
    its own ``device_base_hash`` against the policies that device reports for that group.
    ``template`` links to the resolved BGPPeerTemplate; ``template_name`` is the natural
    key so the row survives the FK target being deleted. There is no apply path for
    templates, so the lifecycle rests at imported/changed/conflict (or in_sync once an
    accepted edit re-matches the device).
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="bgp_peer_template_states",
    )
    template_name = models.CharField(max_length=128)
    template = models.ForeignKey(
        to="netbox_routing.BGPPeerTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_bgp_template_states",
    )
    remote_as_str = models.CharField(max_length=10, blank=True, default="")
    status = models.CharField(max_length=32, choices=_BGP_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")
    # 3-way merge base: hash of the device-reported template content at last agreed sync.
    device_base_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["management", "template_name"]
        unique_together = [("management", "template_name")]
        verbose_name = "NSO BGP Peer Template State"
        verbose_name_plural = "NSO BGP Peer Template States"

    def __str__(self):
        return f"{self.management} / peer-group:{self.template_name} [{self.status}]"


_ROUTE_POLICY_STATUS_CHOICES = [
    ("unknown", "Unknown"),
    ("imported", "Imported"),
    ("accepted", "Accepted"),
    ("deploying", "Deploying"),
    ("in_sync", "In Sync"),
    ("apply_failed", "Apply Failed"),
    ("conflict", "Conflict"),
    ("changed", "Changed"),
    ("error", "Error"),
]


class SharedObjectStateMixin(models.Model):
    """Per-device capture + materialized-owner fields for globally-deduped named objects.

    Some config families (route-policy route-maps / community-lists / prefix-lists /
    as-paths today; ACLs later) are deduplicated *by name* into a single NetBox object,
    yet every device has its OWN content for that name.  These two fields let a row keep
    the device's own captured content alongside the shared object, and mark which device's
    version is currently materialized into it:

    - ``captured`` — the raw per-object payload this device reported (its own version),
      refreshed every reconcile.  Display-only; never feeds the shared object unless an
      operator re-materializes from it.  This is what makes "show every device's version"
      possible.
    - ``is_materialized`` — exactly one row per (family, object_name) group holds ``True``:
      the device whose ``captured`` currently populates the shared NetBox object.  The
      first device to import an object owns it; an operator can re-point ownership to a
      different device's version (see ``shared_object_ownership.rematerialize``).

    Abstract so the route-policy overlay and the future ACL overlay share one contract;
    the family-agnostic machinery in ``shared_object_ownership`` operates purely through
    these fields plus ``family`` / ``object_name`` / ``content_hash`` / the GFK target.
    """

    captured = models.JSONField(default=dict, blank=True)
    is_materialized = models.BooleanField(default=False)

    class Meta:
        abstract = True


class NSORoutePolicyState(SharedObjectStateMixin, _NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, policy-object) compliance overlay for route policy.

    A single generic model covers all four object families (prefix-list,
    community-list, as-path, route-map).  ``content_type`` + ``object_id``
    point to the netbox-routing model instance; ``object_name`` is a
    denormalised copy so the row remains readable even if the FK target is
    deleted before cleanup.

    Status lifecycle mirrors the other intent overlays: unknown → imported →
    accepted → deploying → in_sync / apply_failed.  ``conflict`` is set when
    the on-device config diverges from the NetBox object (content_hash differs).
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="route_policy_states",
    )
    content_type = models.ForeignKey(
        to=ContentType,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    assigned_object = GenericForeignKey("content_type", "object_id")
    # Family tag for filtering without joining content_type.
    family = models.CharField(
        max_length=32,
        help_text="One of: prefix_list, community_list, as_path, route_map",
    )
    object_name = models.CharField(max_length=256)
    content_hash = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=32, choices=_ROUTE_POLICY_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["management", "family", "object_name"]
        unique_together = [("management", "family", "object_name")]
        verbose_name = "NSO Route Policy State"
        verbose_name_plural = "NSO Route Policy States"

    def __str__(self):
        return f"{self.management} / {self.family}:{self.object_name} [{self.status}]"


# ──────────────────────────────────────────────────────────────────────────────
# OSPF plugin state models
# ──────────────────────────────────────────────────────────────────────────────

_OSPF_STATUS_CHOICES = [
    ("unknown", "Unknown"),
    ("imported", "Imported"),
    ("accepted", "Accepted"),
    ("deploying", "Deploying"),
    ("in_sync", "In Sync"),
    ("apply_failed", "Apply Failed"),
    ("conflict", "Conflict"),
    ("changed", "Changed"),
    ("error", "Error"),
]

_OSPF_WRITE_PATH_STATUSES = {"accepted", "deploying", "in_sync", "apply_failed"}


class NSOOSPFInstanceState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, process_id) OSPF process compliance overlay.

    Tracks the status of OSPF process-level config (router-id, vrf, areas)
    for each (NSODeviceManagement, process_id) pair.

    ``ospf_instance`` links to the netbox-routing OSPFInstance once resolved.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="ospf_instance_states",
    )
    process_id = models.CharField(max_length=64)
    router_id = models.CharField(max_length=64, blank=True, default="")
    vrf = models.CharField(max_length=64, blank=True, default="")
    # areas stored as JSON: [{area_id, area_type}]
    areas = models.JSONField(default=list, blank=True)
    # OSPF process admin-state (Nokia SR OS 'admin-state enable'); null when the NED
    # has no explicit admin-state (process enabled by config presence).
    enabled = models.BooleanField(null=True, blank=True)
    # Linked netbox-routing object (nullable — may not exist yet)
    ospf_instance = models.ForeignKey(
        to="netbox_routing.OSPFInstance",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_ospf_instance_states",
    )
    status = models.CharField(max_length=32, choices=_OSPF_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")
    device_base_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["management", "process_id"]
        unique_together = [("management", "process_id")]
        verbose_name = "NSO OSPF Instance State"
        verbose_name_plural = "NSO OSPF Instance States"

    def __str__(self):
        return f"{self.management} / ospf {self.process_id} [{self.status}]"


class NSOOSPFInterfaceState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, interface) OSPF interface compliance overlay.

    Tracks the status of OSPF interface config (area, passive, cost, network-type, auth)
    for each (NSODeviceManagement, dcim.Interface) pair.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="ospf_interface_states",
    )
    interface = models.ForeignKey(
        to="dcim.Interface",
        on_delete=models.CASCADE,
        related_name="nso_ospf_states",
    )
    process_id = models.CharField(max_length=64, null=True, blank=True)
    area_id = models.CharField(max_length=64, blank=True, default="")
    passive = models.BooleanField(default=False)
    priority = models.PositiveSmallIntegerField(null=True, blank=True)
    cost = models.PositiveIntegerField(null=True, blank=True)
    network_type = models.CharField(max_length=32, blank=True, default="")
    auth_type = models.CharField(max_length=32, blank=True, default="")
    auth_present = models.BooleanField(default=False)
    status = models.CharField(max_length=32, choices=_OSPF_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")
    device_base_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["management", "interface"]
        unique_together = [("management", "interface")]
        verbose_name = "NSO OSPF Interface State"
        verbose_name_plural = "NSO OSPF Interface States"

    def __str__(self):
        return f"{self.management} / {self.interface} ospf [{self.status}]"


_REDISTRIBUTION_STATUS_CHOICES = [
    ("unknown", "Unknown"),
    ("imported", "Imported"),
    ("accepted", "Accepted"),
    ("deploying", "Deploying"),
    ("in_sync", "In Sync"),
    ("apply_failed", "Apply Failed"),
    ("conflict", "Conflict"),
    ("changed", "Changed"),
    ("error", "Error"),
]

_REDISTRIBUTION_WRITE_PATH_STATUSES = {"accepted", "deploying", "in_sync", "apply_failed"}


class NSORedistributionState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, destination, source) redistribution statement compliance overlay.

    Tracks the observed + intended state of each `redistribute <source>` statement
    under an OSPF/ISIS/BGP destination protocol scope for a managed device.

    ``redistribution`` links to netbox-routing.Redistribution once resolved.
    ``dest_protocol`` + ``dest_ref`` + ``source_protocol`` + ``source_ref`` mirror
    the adapter's DeviceRedistribution key and are used for matching before the
    netbox-routing row exists.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="redistribution_states",
    )
    # Natural key from the adapter (for matching before/without netbox-routing rows)
    dest_protocol = models.CharField(max_length=16, blank=False)
    dest_ref = models.CharField(max_length=128, blank=True, default="")
    source_protocol = models.CharField(max_length=16, blank=False)
    source_ref = models.CharField(max_length=64, blank=True, default="")
    # Optional resolved fields
    route_map = models.CharField(max_length=128, blank=True, default="")
    metric = models.PositiveIntegerField(null=True, blank=True)
    metric_type = models.CharField(max_length=16, blank=True, default="")
    # Linked netbox-routing object (nullable — may not exist yet)
    redistribution = models.ForeignKey(
        to="netbox_routing.Redistribution",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_redistribution_states",
    )
    status = models.CharField(max_length=32, choices=_REDISTRIBUTION_STATUS_CHOICES, default="unknown")
    # Whether the device still reports this redistribution. The reconciler sets it False when
    # the entry drops out of the payload (the entry was removed on the device) — the row + its
    # netbox-routing object are KEPT and flagged ``changed`` (no silent delete), and this flag
    # lets the drift delta render "removed on device" instead of comparing the stale last-synced
    # fields against the object (which would falsely read as "no drift").
    device_present = models.BooleanField(default=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")
    device_base_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["management", "dest_protocol", "dest_ref", "source_protocol"]
        unique_together = [("management", "dest_protocol", "dest_ref", "source_protocol", "source_ref")]
        verbose_name = "NSO Redistribution State"
        verbose_name_plural = "NSO Redistribution States"

    def __str__(self):
        src = self.source_protocol
        if self.source_ref:
            src += f" {self.source_ref}"
        return f"{self.management} / {self.dest_protocol} ← {src} [{self.status}]"


_LACP_STATUS_CHOICES = [
    ("unknown", "Unknown"),
    ("imported", "Imported"),
    ("accepted", "Accepted"),
    ("deploying", "Deploying"),
    ("in_sync", "In Sync"),
    ("apply_failed", "Apply Failed"),
    ("conflict", "Conflict"),
    ("changed", "Changed"),
    ("error", "Error"),
]

_LACP_WRITE_PATH_STATUSES = {"accepted", "deploying", "in_sync"}


class NSOLACPBundleState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, LAG interface) LACP bundle compliance overlay.

    Carries the LACP parameters NetBox has no native column for — min-links,
    system-priority, system-id, timer, admin-key — plus the standard NSO overlay
    status lifecycle (unknown → imported → accepted → deploying → in_sync /
    apply_failed; drift → changed). One row per (management, LAG interface).
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="lacp_bundle_states",
    )
    interface = models.ForeignKey(
        to="dcim.Interface",
        on_delete=models.CASCADE,
        related_name="nso_lacp_bundle_states",
    )
    lag_id = models.PositiveIntegerField(null=True, blank=True)
    min_links = models.PositiveSmallIntegerField(null=True, blank=True)
    system_priority = models.PositiveIntegerField(null=True, blank=True)
    system_id = models.CharField(max_length=17, blank=True, default="")
    timer = models.CharField(max_length=8, blank=True, default="")
    admin_key = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=_LACP_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["management", "interface"]
        unique_together = [("management", "interface")]
        verbose_name = "NSO LACP Bundle State"
        verbose_name_plural = "NSO LACP Bundle States"

    def __str__(self):
        return f"{self.management} / {self.interface} [{self.status}]"


class NSOLACPMemberState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, member interface) LACP member compliance overlay.

    Carries the per-member LACP mode + port-priority and links to the parent LAG
    interface. One row per (management, member interface). Status lifecycle
    mirrors NSOLACPBundleState.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="lacp_member_states",
    )
    interface = models.ForeignKey(
        to="dcim.Interface",
        on_delete=models.CASCADE,
        related_name="nso_lacp_member_states",
    )
    lag_bundle = models.ForeignKey(
        to="dcim.Interface",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_lacp_member_bundles",
    )
    mode = models.CharField(max_length=8, blank=True, default="")
    port_priority = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=_LACP_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["management", "interface"]
        unique_together = [("management", "interface")]
        verbose_name = "NSO LACP Member State"
        verbose_name_plural = "NSO LACP Member States"

    def __str__(self):
        return f"{self.management} / {self.interface} [{self.status}]"


_VLAN_STATUS_CHOICES = [
    ("unknown", "Unknown"),
    ("imported", "Imported"),
    ("accepted", "Accepted"),
    ("deploying", "Deploying"),
    ("in_sync", "In Sync"),
    ("apply_failed", "Apply Failed"),
    ("conflict", "Conflict"),
    ("changed", "Changed"),
    ("error", "Error"),
]


class NSOVLANState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, ipam.VLAN) VLAN-database compliance overlay.

    The VLAN itself is reconciled into a per-device ``ipam.VLANGroup`` (slug
    ``nso-{device.pk}``); this overlay carries the status/drift + accept marker.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="vlan_states",
    )
    vlan = models.ForeignKey(
        to="ipam.VLAN",
        on_delete=models.CASCADE,
        related_name="nso_vlan_states",
    )
    device_name = models.CharField(
        max_length=64, blank=True, default="", help_text="VLAN name observed on the device (for drift display)."
    )
    status = models.CharField(max_length=32, choices=_VLAN_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["management", "vlan"]
        unique_together = [("management", "vlan")]
        verbose_name = "NSO VLAN State"
        verbose_name_plural = "NSO VLAN States"

    def __str__(self):
        return f"{self.management} / VLAN {self.vlan} [{self.status}]"


class NSOSwitchportState(_NSODeviceTabURLMixin, NetBoxModel):
    """Per-(device, interface) L2 switchport compliance overlay.

    Reconciles into the native ``Interface.mode``/``untagged_vlan``/``tagged_vlans``;
    this overlay mirrors the NSO-observed mode/untagged/tagged for drift + accept.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="switchport_states",
    )
    interface = models.ForeignKey(
        to="dcim.Interface",
        on_delete=models.CASCADE,
        related_name="nso_switchport_states",
    )
    mode = models.CharField(max_length=16, blank=True, default="")
    untagged_vlan = models.ForeignKey(
        to="ipam.VLAN",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_switchport_untagged_states",
    )
    tagged_vlans = models.ManyToManyField(
        to="ipam.VLAN",
        blank=True,
        related_name="nso_switchport_tagged_states",
    )
    status = models.CharField(max_length=32, choices=_VLAN_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")
    # 3-way merge base: hash of the device L2 content at the last agreed sync. Lets the
    # reconciler seed a pristine NetBox interface from the device (read mirror) and then
    # tell an operator edit (freeze) apart from a device-side change (auto-mirror).
    device_base_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["management", "interface"]
        unique_together = [("management", "interface")]
        verbose_name = "NSO Switchport State"
        verbose_name_plural = "NSO Switchport States"

    def __str__(self):
        return f"{self.management} / {self.interface} [{self.status}]"


class NSOSVIState(NetBoxModel):
    """Per-SVI/IRB compliance overlay.

    Tracks an L3 VLAN interface (IOS interface VlanN / Junos irb.N) materialised
    into NetBox as a virtual dcim.Interface, linked to its VLAN. IP addresses are
    NOT tracked here — they ride the interface-IP path on the same interface.
    """

    management = models.ForeignKey(to="NSODeviceManagement", on_delete=models.CASCADE, related_name="svi_states")
    interface = models.ForeignKey(to="dcim.Interface", on_delete=models.CASCADE, related_name="nso_svi_states")
    vlan = models.ForeignKey(
        to="ipam.VLAN", null=True, blank=True, on_delete=models.SET_NULL, related_name="nso_svi_states"
    )
    svi_type = models.CharField(max_length=8, default="svi", help_text="svi (IOS) or irb (Junos).")
    vrf = models.CharField(max_length=128, blank=True, default="", help_text="VRF/routing-instance; empty for global.")
    status = models.CharField(max_length=32, choices=_VLAN_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When an operator accepted this SVI (NetBox owns it)."
    )
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["management", "interface"]
        unique_together = [("management", "interface")]
        verbose_name = "NSO SVI State"
        verbose_name_plural = "NSO SVI States"

    def __str__(self):
        return f"{self.management} / svi:{self.interface} [{self.status}]"

    def get_absolute_url(self):
        """Return the device NSO tab URL (the overlay's detail; used by edit redirects)."""
        from django.urls import reverse

        return reverse("dcim:device_nso", kwargs={"pk": self.management.device_id})


class NSOSubinterfaceState(NetBoxModel):
    """Per-subinterface compliance overlay (dot1q L3 subinterfaces).

    Tracks a dot1q subinterface (IOS Gi0/1.100 / Junos ge-0/0/0.100) materialised
    into NetBox as a virtual ``dcim.Interface`` linked to its physical parent via
    ``Interface.parent``. The dot1q encapsulation tag is interface-local and is
    recorded as a plain integer (``dot1q_vlan``) — deliberately NOT an ``ipam.VLAN``
    FK (a routed subinterface tag is not a device VLAN-database entry). IP addresses
    are NOT tracked here — they ride the interface-IP path on the same interface.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement", on_delete=models.CASCADE, related_name="subinterface_states"
    )
    interface = models.ForeignKey(to="dcim.Interface", on_delete=models.CASCADE, related_name="nso_subinterface_states")
    parent_interface = models.ForeignKey(
        to="dcim.Interface",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="nso_child_subinterface_states",
        help_text="Physical parent interface (Interface.parent); null if not yet present in NetBox.",
    )
    dot1q_vlan = models.PositiveIntegerField(
        null=True, blank=True, help_text="Interface-local 802.1q encapsulation tag (NOT a VLAN FK)."
    )
    vrf = models.CharField(max_length=128, blank=True, default="", help_text="VRF/routing-instance; empty for global.")
    status = models.CharField(max_length=32, choices=_VLAN_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When an operator accepted this subinterface (NetBox owns it)."
    )
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["management", "interface"]
        unique_together = [("management", "interface")]
        verbose_name = "NSO Subinterface State"
        verbose_name_plural = "NSO Subinterface States"

    def __str__(self):
        return f"{self.management} / subif:{self.interface} [{self.status}]"

    def get_absolute_url(self):
        """Return the device NSO tab URL (the overlay's detail; used by edit redirects)."""
        from django.urls import reverse

        return reverse("dcim:device_nso", kwargs={"pk": self.management.device_id})


class NSOInterfaceMtuState(NetBoxModel):
    """Per-interface MTU compliance overlay (Phase 2b — read path).

    Mirrors the device's MTU surface for one ``dcim.Interface``: ``l2_mtu`` (the
    native interface MTU; Cisco/Junos ``mtu`` / Nokia ``port ethernet mtu``),
    ``ip_mtu`` and ``mpls_mtu``. Read-only display first — the native ``l2_mtu``
    is NOT yet written to ``dcim.Interface.mtu`` (that is the accept/write slice).
    Only explicitly-configured MTU surfaces (the export reads NO_DEFAULTS), so a
    row exists only for interfaces that actually set an MTU. ``bound_port`` carries
    the Nokia port↔router-interface binding for operator context.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement", on_delete=models.CASCADE, related_name="interface_mtu_states"
    )
    interface = models.ForeignKey(to="dcim.Interface", on_delete=models.CASCADE, related_name="nso_mtu_states")
    l2_mtu = models.PositiveIntegerField(
        null=True, blank=True, help_text="Native L2/physical MTU reported by the device."
    )
    ip_mtu = models.PositiveIntegerField(null=True, blank=True, help_text="IP MTU reported by the device.")
    mpls_mtu = models.PositiveIntegerField(null=True, blank=True, help_text="MPLS MTU reported by the device.")
    bound_port = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Nokia physical/LAG port backing a router interface; empty otherwise.",
    )
    status = models.CharField(max_length=32, choices=_VLAN_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When an operator accepted this MTU (NetBox owns it)."
    )
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["management", "interface"]
        unique_together = [("management", "interface")]
        verbose_name = "NSO Interface MTU State"
        verbose_name_plural = "NSO Interface MTU States"

    def __str__(self):
        return f"{self.management} / mtu:{self.interface} [{self.status}]"

    def get_absolute_url(self):
        """Return the device NSO tab URL (the overlay has no standalone detail view)."""
        from django.urls import reverse

        return reverse("dcim:device_nso", kwargs={"pk": self.management.device_id})


class NSOBFDInterfaceState(NetBoxModel):
    """Per-interface BFD compliance overlay (write path).

    BFD timers themselves are modelled in netbox_routing (BFDInterface/BFDProfile);
    this overlay carries the NSO-observed timers + status/accept marker so an
    operator can adopt and push BFD back to the device (bfd-reconciler). One row
    per (management, interface).
    """

    management = models.ForeignKey(to="NSODeviceManagement", on_delete=models.CASCADE, related_name="bfd_states")
    interface = models.ForeignKey(to="dcim.Interface", on_delete=models.CASCADE, related_name="nso_bfd_states")
    min_tx = models.PositiveIntegerField(null=True, blank=True, help_text="Desired min TX interval (ms).")
    min_rx = models.PositiveIntegerField(null=True, blank=True, help_text="Required min RX interval (ms).")
    multiplier = models.PositiveIntegerField(null=True, blank=True, help_text="Detection multiplier.")
    micro_bfd = models.BooleanField(default=False, help_text="RFC 7130 per-LAG-member BFD.")
    status = models.CharField(max_length=32, choices=_VLAN_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When an operator accepted this BFD config (NetBox owns it)."
    )
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["management", "interface"]
        unique_together = [("management", "interface")]
        verbose_name = "NSO BFD State"
        verbose_name_plural = "NSO BFD States"

    def __str__(self):
        return f"{self.management} / bfd:{self.interface} [{self.status}]"

    def get_absolute_url(self):
        """Return the device NSO tab URL (the overlay's detail; used by edit redirects)."""
        from django.urls import reverse

        return reverse("dcim:device_nso", kwargs={"pk": self.management.device_id})
