# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import uuid

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models.functions import Now
from django.urls import reverse
from netbox.models import NetBoxModel

from .outbox import CONTRIBUTION_KIND_CHOICES, CONTRIBUTION_KIND_ORDINARY


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


class NSODerivedIntentTemplate(NetBoxModel):
    """Database-managed sentinel and interface-description template mapping."""

    sentinel = models.CharField(
        max_length=64,
        unique=True,
        help_text="Prefix marking an interface description as derived intent, for example '[auto]'.",
    )
    template = models.CharField(
        max_length=500,
        help_text=(
            "Description pattern. It must begin with the sentinel and may use: "
            "{peer_host}, {peer_iface}, {peer_site}, {peer_role}, {self_host}, {self_iface}."
        ),
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Use this template for automatic interface description management.",
    )

    class Meta:
        ordering = ["sentinel"]
        verbose_name = "NSO Derived Intent Template"
        verbose_name_plural = "NSO Derived Intent Templates"

    def __str__(self):
        return self.sentinel

    def get_absolute_url(self):
        """Return the detail URL for this template."""
        return reverse("plugins:netbox_nso_plugin:nsoderivedintenttemplate", args=[self.pk])

    def clean(self):
        """Validate this pattern and reject ambiguous enabled sentinel prefixes."""
        super().clean()
        from .derived_intent import ConfigError, load_sentinel_templates

        raw = [{"sentinel": self.sentinel, "template": self.template}]
        try:
            load_sentinel_templates(raw)
            if self.enabled:
                existing = list(
                    type(self).objects.filter(enabled=True).exclude(pk=self.pk).values("sentinel", "template")
                )
                load_sentinel_templates([*existing, *raw])
        except ConfigError as exc:
            raise ValidationError({"template": str(exc)}) from exc


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
        help_text="Seconds allowed for an inactive-address flip probe — short, so a down path can't stall the loop.",
    )
    active_probe_timeout = models.PositiveIntegerField(
        default=45,
        validators=[MinValueValidator(1), MaxValueValidator(120)],
        help_text="Seconds allowed for the active address to establish a cold NSO session.",
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


class NSOVaultSettings(NetBoxModel):
    """Singleton — Vault KV layout used to derive refs for UI-managed SNMP secrets.

    No Vault address or credentials live here: the adapter owns the Vault
    connection (its AppRole must hold create/update/read on ``base_path``), and
    the NSO snmp-reconciler resolves refs via its own vault-cred-manager config.
    These settings only shape the ``<kv_mount>/<base_path>/...`` refs the plugin
    generates when an operator sets a secret value; pasted refs that already
    contain a ``/`` are stored verbatim.
    """

    kv_mount = models.CharField(
        max_length=128,
        default="network",
        help_text="Vault KV v2 mount for generated secret refs (e.g. 'network').",
    )
    base_path = models.CharField(
        max_length=256,
        default="netbox/snmp",
        help_text=(
            "Path prefix (within the mount) for generated SNMP secret refs: communities land at "
            "BASE/community/HASH#community, v3 users at BASE/v3/USERNAME (fields auth/priv)."
        ),
    )
    enabled = models.BooleanField(
        default=True,
        help_text="When disabled, secret values cannot be set from the UI (pasted refs still work).",
    )

    class Meta:
        verbose_name = "Vault Settings"
        verbose_name_plural = "Vault Settings"

    def __str__(self):
        return f"Vault refs: {self.kv_mount}/{self.base_path}"

    def get_absolute_url(self):
        """Return the URL for the singleton edit view."""
        return reverse("plugins:netbox_nso_plugin:nsovaultsettings")

    def save(self, *args, **kwargs):
        """Enforce singleton: reuse the existing row's PK when creating a second instance."""
        if not self.pk:
            existing = NSOVaultSettings.objects.first()
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
        from django.db import transaction

        from .intent_state import MutationFootprint, SourceRow, intent_transaction

        with transaction.atomic():
            default_ids = frozenset(
                NSOInstance.objects.filter(is_default=True).exclude(pk=self.pk).values_list("pk", flat=True)
            )
            footprint = MutationFootprint.for_keys(
                (),
                shared_keys=(("nso-instance-default", "singleton"),),
                source_rows=(
                    SourceRow(self._meta.label_lower, self.pk),
                    *(SourceRow(self._meta.label_lower, pk) for pk in default_ids),
                ),
            )
            with intent_transaction(footprint):
                current_default_ids = frozenset(
                    NSOInstance.objects.filter(is_default=True).exclude(pk=self.pk).values_list("pk", flat=True)
                )
                if current_default_ids != default_ids:
                    from .intent_state import IntentMutationProtocolError

                    raise IntentMutationProtocolError("the NSO instance default set changed during acquisition")
                # The shared key serializes the default check. The source rows lock the
                # existing default instances before the write.
                other_defaults = NSOInstance.objects.filter(is_default=True).exclude(pk=self.pk)
                # If no other default exists (e.g. this is the first instance), force this one to be
                # the default so onboarding always has something to pre-select.
                if not other_defaults.exists():
                    self.is_default = True
                super().save(*args, **kwargs)
                if self.is_default:
                    NSOInstance.objects.filter(is_default=True).exclude(pk=self.pk).update(is_default=False)


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
            "When True, every accepted value on this device starts Apply automatically. "
            "Disabled by default so brownfield devices are brought into "
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
    adapter_link_error = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Set when the last attempt to link/sync this managed device to the nso-adapter "
            "(onboard → scope → sync-notify) failed, so the row would otherwise look managed "
            "while silently unlinked. Cleared once linking succeeds. Surfaced on the NSO tab."
        ),
    )
    # When the link repair last TRIED this row, whether or not the repair worked. The
    # repair is capped per run, so an unordered traversal lets a persistently failing head
    # hold the cap on every run and starve a repairable tail row; ordering the broken list
    # least-recently-attempted first bounds every broken row at ceil(B / C) ticks.
    adapter_link_attempted_at = models.DateTimeField(null=True, blank=True)
    # ── READSEM S4 (D5): adopted store incarnation + durable reset markers ──────
    # The adapter store mints an (incarnation UUID, born) pair on every DB rebuild;
    # the gated reconcile ADOPTS the newest born (resetting per-family read state),
    # while tab observations may only RECORD a pending rebuild here — greatest born
    # wins (monotonic), and reset_conflict_born pins the highest born at which two
    # DIFFERENT incarnation UUIDs collided (equal-born ambiguity fails closed until
    # a strictly-later born adopts). Born values are compared only against other
    # adapter-born values — never against this host's clock.
    adapter_incarnation = models.CharField(max_length=64, blank=True, default="")
    adapter_incarnation_born = models.DateTimeField(null=True, blank=True)
    adapter_source_epoch = models.BigIntegerField(null=True, blank=True)
    source_epoch_aware = models.BooleanField(default=False)
    source_rekey_pending = models.BooleanField(default=False)
    reset_pending_source_epoch = models.BigIntegerField(null=True, blank=True)
    reset_pending_incarnation = models.CharField(max_length=64, blank=True, default="")
    reset_pending_born = models.DateTimeField(null=True, blank=True)
    reset_conflict_born = models.DateTimeField(null=True, blank=True)
    # ── #1396 R3 / Appendix S: settlement cursor + intent-push rejection record ──
    # The cursor names how far the consumer has walked this device's ordered settlement
    # feed, under the epoch ``(store incarnation, adapter device id)``. Neither half may
    # be read from a cache: ``adapter_incarnation`` is written only when a read-state
    # publication is adopted, so the incarnation is taken from the feed response's
    # ``X-Store-Incarnation`` header, and the device half from ``adapter_device_id`` on
    # the row the consumer locks. A rebuilt store and a device remap both restart the
    # adapter's per-device counter at 1, so either mismatch resets the cursor AND the
    # stall triple. Replaying an old result after a reset is harmless because intent
    # generations are allocated from a database-global sequence.
    settle_cursor_seq = models.BigIntegerField(null=True, blank=True)
    settle_cursor_incarnation = models.CharField(max_length=64, blank=True, default="")
    settle_cursor_device_id = models.BigIntegerField(null=True, blank=True)
    # One unresolvable settlement may not block the device forever, and the bound has to
    # survive a worker restart — so the triple lives on the row, not in a module global.
    # It describes exactly one sequence: any advance clears it, as does an epoch change.
    settle_stall_seq = models.BigIntegerField(null=True, blank=True)
    settle_stall_attempts = models.PositiveIntegerField(default=0)
    settle_stall_first_seen_at = models.DateTimeField(null=True, blank=True)
    # Per-scope record of what an intent push was rejected with. ``intent_push_attempts``
    # is the never-cleared per-scope high-water mark: clearing the error entry on success
    # would take the attempt token with it, and a delayed failure from an earlier attempt
    # would then resurrect over the newer success.
    intent_push_errors = models.JSONField(default=dict, blank=True)
    intent_push_attempts = models.JSONField(default=dict, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_status = models.CharField(max_length=50, blank=True, default="")
    degraded_surfaces = models.JSONField(
        null=True,
        blank=True,
        help_text=(
            "When last_sync_status is 'partial', the routing surfaces (e.g. ['bgp', 'ospf']) "
            "whose read from NSO failed on the last sync — their mirrored data may be stale. "
            "NULL when nothing is degraded. Cached from the adapter."
        ),
    )
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
    # ── Async onboarding lifecycle ────────────────────────────────────────────
    # Provisioning a device into NSO (create node → fetch-host-keys → unlock → sync-from)
    # runs as a background adapter job — it can take minutes when the primary mgmt IP is
    # unreachable and the adapter must bootstrap over OOB before a full sync-from. The row
    # is created immediately in ``provisioning`` and the dashboard polls the job; only when
    # it succeeds does the row go ready ("") and fire the adapter map/scope/sync signal. The
    # empty default keeps every legacy / externally-managed row at the steady "ready" state.
    ONBOARD_STATUS_CHOICES = [
        ("", "Ready"),
        ("provisioning", "Provisioning"),
        ("provision_failed", "Provisioning failed"),
    ]
    onboard_status = models.CharField(
        max_length=20,
        blank=True,
        default="",
        choices=ONBOARD_STATUS_CHOICES,
        help_text="Async onboarding lifecycle: '' (ready/managed), provisioning, or provision_failed.",
    )
    onboard_job_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Adapter job id of the in-flight (or last) provision job for this device.",
    )
    onboard_error = models.TextField(
        blank=True,
        default="",
        help_text="Populated when onboard_status=provision_failed — the blocking step / error summary.",
    )

    class Meta:
        ordering = ["device"]
        verbose_name = "NSO Device Management"
        verbose_name_plural = "NSO Device Management"

    # Columns a save that does not name them must never carry — see save().
    _PUSH_RECORD_FIELDS = ("intent_push_errors", "intent_push_attempts")
    # The settlement cursor, its epoch, the stall triple and the link-repair clock. All are
    # written by a sweep through ``.update()`` while it holds this row's lock, and all lose
    # their meaning when rewound: a cursor that moves backwards re-consumes settled results,
    # and a stall count that resets never reaches its bound, so one unresolvable result
    # blocks the device forever — the exact failure the durable counter exists to prevent.
    _SETTLEMENT_RECORD_FIELDS = (
        "settle_cursor_seq",
        "settle_cursor_incarnation",
        "settle_cursor_device_id",
        "settle_stall_seq",
        "settle_stall_attempts",
        "settle_stall_first_seen_at",
        "adapter_link_attempted_at",
    )
    # One authority for the refresh, so a column protected in one group cannot be missed.
    _STALE_SAVE_PROTECTED_FIELDS = _PUSH_RECORD_FIELDS + _SETTLEMENT_RECORD_FIELDS

    def __str__(self):
        return f"{self.device} → {self.nso_instance.name}/{self.nso_device_name}"

    def save(self, *args, **kwargs):
        """Keep a full save from rewinding the row's durable records.

        A full ``save()`` rewrites every column from whatever this instance held when it
        was loaded, and several sweeps load a row, make an adapter call, and only then
        save it (``sync_cache.reconcile_device_links``). Anything recorded in that gap
        would be erased — and for these columns erasure is not the worst of it, because
        each one is a high-water mark whose whole job is to move in one direction.
        Rewinding ``intent_push_attempts`` lets a late failure resurrect over a newer
        success; rewinding the settlement cursor re-consumes results that already
        settled; resetting ``settle_stall_attempts`` means the bound is never reached and
        one unresolvable result blocks the device forever.

        Re-reading the columns is not enough on its own — the read and the UPDATE are
        separate statements, so a writer committing between them still wins. The read is
        therefore taken under ``select_for_update`` in a transaction that stays open
        across ``super().save()``, which is what makes the concurrent writer wait.

        A save that NAMES its fields is untouched: every writer of these columns names
        them deliberately, holding this same row lock.
        """
        if kwargs.get("update_fields") is not None or not self.pk:
            with transaction.atomic():
                return super().save(*args, **kwargs)
        protected = self._STALE_SAVE_PROTECTED_FIELDS
        with transaction.atomic():
            current = type(self).objects.select_for_update().filter(pk=self.pk).values(*protected).first()
            if current is not None:
                for field in protected:
                    setattr(self, field, current[field])
            return super().save(*args, **kwargs)

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


class NSOFamilyReadState(NetBoxModel):
    """Declared read-outcome bookkeeping for one (management, family) — READSEM S4 D5.

    ``observed_*`` is the newest declared outcome seen from the adapter (via the tab's
    aggregate observation or a reconcile gate decision); ``applied_*`` is the identity
    of the last payload actually ADMITTED to a reconciler body. The reconcile fence
    compares against APPLIED only — a tab observation must never strand overlay rows
    by fencing out a slightly-older worker payload. Ordering is incarnation adoption
    (by born) then ``attempt_id`` within the adopted incarnation; ``observed_read_at``
    is display-only (never an ordering key). A blank ``observed_outcome`` means
    unknown/legacy — the UI ignores the row.
    """

    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="family_read_states",
    )
    family = models.CharField(max_length=64)
    observed_outcome = models.CharField(max_length=32, blank=True, default="")
    observed_reason = models.CharField(max_length=32, blank=True, default="")
    observed_freshness = models.CharField(max_length=16, blank=True, default="")
    observed_result = models.CharField(max_length=16, blank=True, default="")
    observed_succeeded = models.BooleanField(null=True, blank=True)
    observed_read_at = models.DateTimeField(null=True, blank=True)
    observed_attempt_id = models.BigIntegerField(null=True, blank=True)
    observed_incarnation = models.CharField(max_length=64, blank=True, default="")
    observed_incarnation_born = models.DateTimeField(null=True, blank=True)
    observed_epoch = models.IntegerField(
        null=True,
        blank=True,
        help_text="The adapter_device_id this observation was fetched from (replay guard).",
    )
    observed_source_epoch = models.BigIntegerField(null=True, blank=True)
    observed_payload_revision = models.BigIntegerField(null=True, blank=True)
    admitted_attempt_id = models.BigIntegerField(null=True, blank=True)
    admitted_incarnation = models.CharField(max_length=64, blank=True, default="")
    admitted_source_epoch = models.BigIntegerField(null=True, blank=True)
    admitted_payload_revision = models.BigIntegerField(null=True, blank=True)
    applied_attempt_id = models.BigIntegerField(null=True, blank=True)
    applied_incarnation = models.CharField(max_length=64, blank=True, default="")
    applied_source_epoch = models.BigIntegerField(null=True, blank=True)
    applied_payload_revision = models.BigIntegerField(null=True, blank=True)
    publication_sequence = models.BigIntegerField(default=0)
    applied_publication_sequence = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["management", "family"]
        unique_together = [("management", "family")]
        verbose_name = "NSO Family Read State"
        verbose_name_plural = "NSO Family Read States"

    def __str__(self):
        return f"{self.management} / {self.family} [{self.observed_outcome or 'unknown'}]"


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
    ALLOCATION_KIND_MANUAL = "manual"
    ALLOCATION_KIND_SINGLE = "single"
    ALLOCATION_KIND_P2P = "p2p"
    ALLOCATION_KIND_CHOICES = [
        (ALLOCATION_KIND_MANUAL, "Manual"),
        (ALLOCATION_KIND_SINGLE, "Auto-assigned single address"),
        (ALLOCATION_KIND_P2P, "Auto-assigned P2P pair"),
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
    allocation_kind = models.CharField(
        max_length=16,
        choices=ALLOCATION_KIND_CHOICES,
        default=ALLOCATION_KIND_MANUAL,
        help_text="The allocation shape that owns cleanup of the source pool.",
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

# snmp-reconciler YANG enum spellings (sent verbatim to the adapter/NSO)
_SNMP_AUTH_PROTOCOL_CHOICES = [
    ("md5", "MD5"),
    ("sha", "SHA"),
    ("sha-256", "SHA-256"),
    ("sha-384", "SHA-384"),
    ("sha-512", "SHA-512"),
]

_SNMP_PRIV_PROTOCOL_CHOICES = [
    ("des", "DES"),
    ("3des", "3DES"),
    ("aes-128", "AES-128"),
    ("aes-192", "AES-192"),
    ("aes-256", "AES-256"),
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
        help_text=(
            "Fully-qualified Vault ref 'mount/path#key' for the community string (required for the write path)."
        ),
    )
    vault_secret_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "SHA-256[:16] fingerprint of the Vault-held plaintext, set whenever the adapter "
            "touches the secret (set/verify/harvest). Equal to community_hash ⇒ Vault matches device."
        ),
    )
    vault_secret_version = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Vault KV v2 version observed when the secret was last set/verified (audit only).",
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
    group_name = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="SNMPv3 group for the write path (optional).",
    )
    auth_protocol = models.CharField(
        max_length=16,
        blank=True,
        default="",
        choices=_SNMP_AUTH_PROTOCOL_CHOICES,
        help_text="Authentication protocol for the write path (blank = no authentication).",
    )
    priv_protocol = models.CharField(
        max_length=16,
        blank=True,
        default="",
        choices=_SNMP_PRIV_PROTOCOL_CHOICES,
        help_text="Privacy protocol for the write path (blank = no privacy).",
    )
    vault_ref = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text=(
            "Fully-qualified Vault PATH ref 'mount/path' (no #key) — fields 'auth'/'priv' "
            "by convention. Required for the write path."
        ),
    )
    vault_has_auth = models.BooleanField(
        default=False,
        help_text="Vault holds an 'auth' field at the ref (set when the adapter touches the secret).",
    )
    vault_has_priv = models.BooleanField(
        default=False,
        help_text="Vault holds a 'priv' field at the ref (set when the adapter touches the secret).",
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
    username = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=(
            "SNMPv3 security user name for this receiver (v3 only). Not a secret — the same "
            "identity as the v3-user rows. Both NSO host writers KEY the receiver on this field, "
            "so a v3 host without it cannot be pushed."
        ),
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


#: The closed OC severity vocabulary — the logging-reconciler's ``log-severity-oc``
#: YANG enum verbatim (NX-P4). Sent to the adapter unmapped.
_OC_SEVERITY_CHOICES = [
    ("EMERGENCY", "Emergency"),
    ("ALERT", "Alert"),
    ("CRITICAL", "Critical"),
    ("ERROR", "Error"),
    ("WARNING", "Warning"),
    ("NOTICE", "Notice"),
    ("INFORMATIONAL", "Informational"),
    ("DEBUG", "Debug"),
]


class NSOLoggingLevelState(NetBoxModel):
    """Per-device local logging severity levels overlay (console/monitor/module).

    At most one row per device management object (OneToOneField) — the
    NSOSnmpSystemInfoState singleton shape. A blank severity means that
    destination is unmanaged. Un-accepting an owned row RETRACTS the managed
    leaves from the reconciler service; on NX that renders ``no logging <dest>``,
    which DISABLES the destination rather than reverting it to a previous level —
    the un-accept flow surfaces this before acting.
    """

    SEVERITY_FIELDS = ("console_severity", "monitor_severity", "module_severity")

    management = models.OneToOneField(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="logging_level_state",
    )
    console_severity = models.CharField(max_length=16, blank=True, default="", choices=_OC_SEVERITY_CHOICES)
    monitor_severity = models.CharField(max_length=16, blank=True, default="", choices=_OC_SEVERITY_CHOICES)
    module_severity = models.CharField(max_length=16, blank=True, default="", choices=_OC_SEVERITY_CHOICES)
    status = models.CharField(max_length=32, choices=_SNMP_STATUS_CHOICES, default="unknown")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When an operator accepted this row (NetBox becomes source of truth)."
    )
    last_apply_at = models.DateTimeField(null=True, blank=True)
    last_apply_error = models.TextField(blank=True, default="")
    apply_attempt_id = models.UUIDField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["management", "status", "apply_attempt_id"],
                name="nso_loglvl_apply_lookup",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="deploying") | models.Q(apply_attempt_id__isnull=False),
                name="nso_loglvl_deploy_attempt",
            )
        ]
        verbose_name = "NSO Logging Level State"
        verbose_name_plural = "NSO Logging Level States"

    def __str__(self):
        return f"{self.management} / local-levels [{self.status}]"

    def set_severities(self) -> dict:
        """Return the managed (non-blank) severities as ``{field_name: OC value}``."""
        return {f: getattr(self, f) for f in self.SEVERITY_FIELDS if getattr(self, f)}

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
    apply_attempt_id = models.UUIDField(null=True, blank=True)
    # ── #1396 R3: intent generation + the settlement expectation ────────────────
    # ``intent_generation`` is allocated from a database-global sequence
    # (:func:`~netbox_nso_plugin.intent_generation.allocate_intent_generation`) on every
    # content change, so no result can outlive the generation it names — not even across a
    # delete/recreate of the management row, which the adapter's job history survives. ``0``
    # is the unallocated sentinel: it is never put on the wire and never correlates.
    # ``expected_*`` is what the intent PUT echoed back for the generation actually pushed;
    # an apply result settles this row only when it matches both.
    intent_generation = models.BigIntegerField(default=0)
    generation_started_at = models.DateTimeField(null=True, blank=True)
    expected_generation = models.BigIntegerField(null=True, blank=True)
    expected_fingerprint = models.CharField(max_length=128, blank=True, default="")
    # Why a result did not settle green (an ``unproven`` verdict's reason) — a statement
    # about evidence, not about ownership, so it qualifies the status instead of being one.
    last_result_advisory = models.TextField(blank=True, default="")
    # ── #1503 Appendix O: the triple the adapter last ACKNOWLEDGED for this route ──
    # ``{vrf, prefix, next_hop}``, written only when a push carrying this route is
    # acknowledged, so a deletion's lineage names a triple the adapter can actually match.
    # NULL is never fabricated: it IS the wire's ``unverified`` flag. Stamping a live mirror
    # here would label content the adapter never accepted as acknowledged, and the first
    # deletion after that would match nothing and silently detach the route.
    last_acked_triple = models.JSONField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["management", "static_route"]
        unique_together = [("management", "static_route")]
        indexes = [
            models.Index(
                fields=["management", "status", "apply_attempt_id"],
                name="nso_static_apply_lookup",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="deploying") | models.Q(apply_attempt_id__isnull=False),
                name="nso_static_deploy_attempt",
            )
        ]
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
    apply_attempt_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["management", "service_name", "sap_id"]
        unique_together = [("management", "service_name", "sap_id")]
        indexes = [
            models.Index(
                fields=["management", "status", "apply_attempt_id"],
                name="nso_l2sap_apply_lookup",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="deploying") | models.Q(apply_attempt_id__isnull=False),
                name="nso_l2sap_deploy_attempt",
            )
        ]
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
    # IS-IS BFD enablement, tri-state (null = no opinion / NED default → reconcile
    # never touches brownfield BFD; True = enable, False = explicitly disable). The
    # write intent carries it so an operator can drive IS-IS BFD from the plugin UI.
    bfd_enabled = models.BooleanField(null=True, blank=True)
    # FRR/TI-LFA (#83), same tri-state contract; frr_protection: link | node (Junos).
    frr_enabled = models.BooleanField(null=True, blank=True)
    frr_protection = models.CharField(max_length=8, blank=True, default="")
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
    # FRR/TI-LFA (#83): the instance flavor (lfa | remote-lfa | ti-lfa) +
    # tri-state microloop-avoidance, pushed since the #83 writers landed.
    fast_reroute = models.CharField(max_length=16, blank=True, default="")
    microloop_avoidance = models.BooleanField(null=True, blank=True)
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
    - ``device_present`` — whether the device still reports this object.  The reconciler sets
      it False when the object drops out of the device's payload (it was removed on the
      device) — the row + shared object are KEPT and flagged ``changed`` (no silent delete),
      and this lets the drift delta render "removed on device" instead of comparing the stale
      ``captured`` against the object (which would falsely read as "no drift").

    Abstract so the route-policy overlay and the future ACL overlay share one contract;
    the family-agnostic machinery in ``shared_object_ownership`` operates purely through
    these fields plus ``family`` / ``object_name`` / ``content_hash`` / the GFK target.
    """

    captured = models.JSONField(default=dict, blank=True)
    is_materialized = models.BooleanField(default=False)
    device_present = models.BooleanField(default=True)

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
    apply_attempt_id = models.UUIDField(null=True, blank=True)
    # community-list members this device's NED cannot hold (e.g. a wildcard color on
    # Nokia). The adapter reports them on intent push; they are surfaced as "unsupported
    # on <ned>" so an operator understands why an owned object may sit at "pending apply"
    # (the codec silently skips them on the device) instead of it looking like an
    # unexplained/suspicious phantom. Deterministic per (member, ned); [] when all apply.
    unsupported_members = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["management", "family", "object_name"]
        unique_together = [("management", "family", "object_name")]
        indexes = [
            models.Index(
                fields=["management", "status", "apply_attempt_id"],
                name="nso_routepol_apply_lookup",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="deploying") | models.Q(apply_attempt_id__isnull=False),
                name="nso_routepol_deploy_attempt",
            )
        ]
        verbose_name = "NSO Route Policy State"
        verbose_name_plural = "NSO Route Policy States"

    def __str__(self):
        return f"{self.management} / {self.family}:{self.object_name} [{self.status}]"

    @property
    def classification_mode(self) -> str:
        """MASTER (shared, the default) or LOCAL (per-device) classification of this group."""
        from .route_policy_reconciler import _group_mode

        return _group_mode(self.family, self.object_name)


_ROUTE_POLICY_OBJECT_MODE_CHOICES = [
    ("master", "Master (shared)"),
    ("local", "Per-device (local)"),
]
_ROUTE_POLICY_OBJECT_CLASS_SOURCE_CHOICES = [
    ("operator", "Operator"),
    ("heuristic", "Heuristic"),
    ("default", "Default"),
]


class NSORoutePolicyObjectClass(NetBoxModel):
    """Classification for a shared route-policy object group ``(family, object_name)``.

    MASTER (the default — implied by the ABSENCE of a row): deduped to one materialized
    netbox-routing object; a device whose capture diverges from the owner is real drift.
    LOCAL: the object legitimately differs per device (VRRP, per-region prefix lists), so it
    is NOT materialized — each device keeps its own ``captured`` version (NSO tab only) and
    cross-device divergence is NOT flagged as drift. A row exists once an object is marked
    LOCAL, or the operator explicitly confirms MASTER (silencing the heuristic suggestion).
    The heuristic "this MASTER group is diverging → suggest LOCAL" verdict is computed live
    (not persisted), so it always reflects current reality.
    """

    family = models.CharField(max_length=32, help_text="One of: prefix_list, community_list, as_path, route_map")
    object_name = models.CharField(max_length=256)
    mode = models.CharField(max_length=16, choices=_ROUTE_POLICY_OBJECT_MODE_CHOICES, default="master")
    source = models.CharField(max_length=16, choices=_ROUTE_POLICY_OBJECT_CLASS_SOURCE_CHOICES, default="operator")

    class Meta:
        ordering = ["family", "object_name"]
        unique_together = [("family", "object_name")]
        verbose_name = "NSO Route Policy Object Class"
        verbose_name_plural = "NSO Route Policy Object Classes"

    def __str__(self):
        return f"{self.family}:{self.object_name} [{self.mode}]"


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
    # NX-P2 vPC preserve/REFUSE: True when the bundle carries a per-bundle vPC discriminator
    # (vpc <id>/peer-link/orphan-port). Such a bundle is REPORTED for visibility but is NOT
    # onboardable — the lag-reconciler refuses it zero-write (a retract of an adopted vPC
    # peer-link would delete it → dual-active split-brain), so Accept is gated on this flag and
    # a vpc-sensitive bundle is never included in the pushed write intent.
    vpc_sensitive = models.BooleanField(default=False)
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
    apply_attempt_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["management", "vlan"]
        unique_together = [("management", "vlan")]
        indexes = [
            models.Index(
                fields=["management", "status", "apply_attempt_id"],
                name="nso_vlan_apply_lookup",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="deploying") | models.Q(apply_attempt_id__isnull=False),
                name="nso_vlan_deploy_attempt",
            )
        ]
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
    apply_attempt_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["management", "interface"]
        unique_together = [("management", "interface")]
        indexes = [
            models.Index(
                fields=["management", "status", "apply_attempt_id"],
                name="nso_svi_apply_lookup",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="deploying") | models.Q(apply_attempt_id__isnull=False),
                name="nso_svi_deploy_attempt",
            )
        ]
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
    apply_attempt_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["management", "interface"]
        unique_together = [("management", "interface")]
        indexes = [
            models.Index(
                fields=["management", "status", "apply_attempt_id"],
                name="nso_subif_apply_lookup",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="deploying") | models.Q(apply_attempt_id__isnull=False),
                name="nso_subif_deploy_attempt",
            )
        ]
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
    apply_attempt_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["management", "interface"]
        unique_together = [("management", "interface")]
        indexes = [
            models.Index(
                fields=["management", "status", "apply_attempt_id"],
                name="nso_mtu_apply_lookup",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="deploying") | models.Q(apply_attempt_id__isnull=False),
                name="nso_mtu_deploy_attempt",
            )
        ]
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
    apply_attempt_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["management", "interface"]
        unique_together = [("management", "interface")]
        indexes = [
            models.Index(
                fields=["management", "status", "apply_attempt_id"],
                name="nso_bfd_apply_lookup",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="deploying") | models.Q(apply_attempt_id__isnull=False),
                name="nso_bfd_deploy_attempt",
            )
        ]
        verbose_name = "NSO BFD State"
        verbose_name_plural = "NSO BFD States"

    def __str__(self):
        return f"{self.management} / bfd:{self.interface} [{self.status}]"

    def get_absolute_url(self):
        """Return the device NSO tab URL (the overlay's detail; used by edit redirects)."""
        from django.urls import reverse

        return reverse("dcim:device_nso", kwargs={"pk": self.management.device_id})


# ──────────────────────────────────────────────────────────────────────────────
# Link-role provisioning — configurable role catalog + assignments
# ──────────────────────────────────────────────────────────────────────────────

_LINK_ROLE_TYPE_CHOICES = [
    ("p2p", "Point-to-point (cable, both ends)"),
    ("single", "Single-ended (interface)"),
]

# EIGRP is intentionally absent and must never be added (operator directive).
_LINK_ROLE_IGP_CHOICES = [
    ("none", "None"),
    ("isis", "IS-IS"),
    ("ospf", "OSPF"),
]


class NSOLinkRole(NetBoxModel):
    """Operator-defined, configurable interface/link role — a reusable intent bundle.

    A single role is the source of truth for three derived outputs on the
    interface(s) it is assigned to (see ``NSOLinkRoleAssignment``): the interface
    **description** (rendered from an M8 template), an **IP assignment** (pool +
    mask, reusing the shipped M13 auto-assign engine), and **IGP interface
    enablement** (IS-IS or OSPF). This replaces M8's and M13's separate hardcoded
    heuristics with one editable catalog — classify a link once and the whole link
    comes up on both ends.

    ``link_type`` decides how the role attaches and how addresses are drawn:
    ``p2p`` binds to a ``dcim.Cable`` and carves a child prefix for both ends;
    ``single`` binds to one ``dcim.Interface`` (loopback/access) and draws a host.
    """

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.CharField(max_length=200, blank=True, default="")
    enabled = models.BooleanField(
        default=True,
        help_text="Disabled roles stay in the catalog but are skipped by the provisioner.",
    )
    link_type = models.CharField(
        max_length=16,
        choices=_LINK_ROLE_TYPE_CHOICES,
        default="p2p",
        help_text=(
            "p2p → attaches to a cable and carves a child prefix for both ends; "
            "single → attaches to one interface (loopback/access) and draws a host."
        ),
    )

    # ── IP assignment (both pool references supported; explicit Prefix FK wins) ──
    assign_ipv4 = models.BooleanField(default=True, help_text="Assign an IPv4 address for this role.")
    assign_ipv6 = models.BooleanField(default=False, help_text="Assign an IPv6 address for this role.")
    ipv4_pool_prefix = models.ForeignKey(
        to="ipam.Prefix",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_link_roles_v4",
        help_text="Explicit IPv4 pool prefix to allocate from (wins over the pool role slug).",
    )
    ipv4_pool_role = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Fallback: an ipam Prefix role slug used to find the IPv4 pool when no explicit prefix is set.",
    )
    ipv6_pool_prefix = models.ForeignKey(
        to="ipam.Prefix",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nso_link_roles_v6",
        help_text="Explicit IPv6 pool prefix to allocate from (wins over the pool role slug).",
    )
    ipv6_pool_role = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Fallback: an ipam Prefix role slug used to find the IPv6 pool when no explicit prefix is set.",
    )
    ipv4_mask = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(32)],
        help_text="p2p child prefix length for IPv4 (e.g. 31). Blank → reuse the M13 per-pool/default.",
    )
    ipv6_mask = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(128)],
        help_text="p2p child prefix length for IPv6 (e.g. 127). Blank → reuse the M13 per-pool/default.",
    )

    # ── Description (M8 template rendered per end) ──
    description_template = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text=(
            "M8 description template rendered on each end (placeholders such as "
            "{peer_host}, {peer_iface}). Blank → do not manage the description."
        ),
    )

    # ── IGP interface enablement ──
    igp = models.CharField(max_length=16, choices=_LINK_ROLE_IGP_CHOICES, default="none")
    isis_circuit_type = models.CharField(
        max_length=32, blank=True, default="", help_text="IS-IS circuit type, e.g. point-to-point."
    )
    isis_passive = models.BooleanField(default=False)
    isis_metric = models.PositiveIntegerField(null=True, blank=True)
    isis_process_tag = models.CharField(max_length=128, blank=True, default="")
    ospf_area = models.CharField(max_length=64, blank=True, default="")
    ospf_network_type = models.CharField(max_length=32, blank=True, default="")
    ospf_passive = models.BooleanField(default=False)
    ospf_cost = models.PositiveIntegerField(null=True, blank=True)
    ospf_process_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["name"]
        verbose_name = "NSO Link Role"
        verbose_name_plural = "NSO Link Roles"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        """Return the detail URL for this link role."""
        return reverse("plugins:netbox_nso_plugin:nsolinkrole", args=[self.pk])

    def _has_ipv4_pool(self):
        return bool(self.ipv4_pool_prefix_id or self.ipv4_pool_role)

    def _has_ipv6_pool(self):
        return bool(self.ipv6_pool_prefix_id or self.ipv6_pool_role)

    def clean(self):
        """Validate the intent bundle is internally consistent and drives an output."""
        super().clean()
        errors = {}
        # 1. A pool reference is required for each opted-in family.
        if self.assign_ipv4 and not self._has_ipv4_pool():
            errors["ipv4_pool_role"] = "Set an IPv4 pool prefix or pool role when 'Assign IPv4' is on."
        if self.assign_ipv6 and not self._has_ipv6_pool():
            errors["ipv6_pool_role"] = "Set an IPv6 pool prefix or pool role when 'Assign IPv6' is on."
        # 2. Child masks apply only to p2p roles, and must leave room for a host pair.
        if self.link_type != "p2p":
            if self.ipv4_mask is not None:
                errors["ipv4_mask"] = "A child mask only applies to p2p roles."
            if self.ipv6_mask is not None:
                errors["ipv6_mask"] = "A child mask only applies to p2p roles."
        else:
            if self.ipv4_mask is not None and self.ipv4_mask > 31:
                errors["ipv4_mask"] = "IPv4 p2p child mask must be /31 or shorter to fit two hosts."
            if self.ipv6_mask is not None and self.ipv6_mask > 127:
                errors["ipv6_mask"] = "IPv6 p2p child mask must be /127 or shorter to fit two hosts."
        # 3. IGP parameters must match the chosen IGP (no stray other-protocol params).
        if self.igp != "isis" and (
            self.isis_circuit_type or self.isis_passive or self.isis_metric is not None or self.isis_process_tag
        ):
            errors["igp"] = "IS-IS parameters are set but the IGP is not 'isis'."
        if self.igp != "ospf" and (
            self.ospf_area
            or self.ospf_network_type
            or self.ospf_passive
            or self.ospf_cost is not None
            or self.ospf_process_id
        ):
            errors["igp"] = "OSPF parameters are set but the IGP is not 'ospf'."
        # 4. A role must drive at least one output (not a pure no-op).
        drives_ip = (self.assign_ipv4 and self._has_ipv4_pool()) or (self.assign_ipv6 and self._has_ipv6_pool())
        if not (drives_ip or self.description_template or self.igp != "none"):
            errors[NON_FIELD_ERRORS] = [
                "A link role must drive at least one output: an IP family, a description template, or an IGP."
            ]
        # 5. The description template may only use the M8 known placeholders.
        if self.description_template:
            import string

            from .derived_intent import KNOWN_PLACEHOLDERS

            used = {fn for _, fn, _, _ in string.Formatter().parse(self.description_template) if fn is not None}
            unknown = used - KNOWN_PLACEHOLDERS
            if unknown:
                errors["description_template"] = (
                    f"Unknown placeholder(s): {sorted(unknown)}. Known: {sorted(KNOWN_PLACEHOLDERS)}"
                )
        if errors:
            raise ValidationError(errors)


class NSOLinkRoleAssignment(NetBoxModel):
    """Binds an ``NSOLinkRole`` to exactly one network object.

    The nullable ``cable``/``interface`` pair is XOR-constrained (a CheckConstraint
    enforces exactly one set): ``p2p`` roles attach to a ``dcim.Cable`` (both
    terminated ends provisioned together), ``single`` roles attach to a single
    ``dcim.Interface`` (loopback/access). Each cable and each interface may carry at
    most one assignment.
    """

    role = models.ForeignKey(to="NSOLinkRole", on_delete=models.PROTECT, related_name="assignments")
    cable = models.ForeignKey(
        to="dcim.Cable",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="nso_link_role_assignments",
    )
    interface = models.ForeignKey(
        to="dcim.Interface",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="nso_link_role_assignments",
    )

    class Meta:
        ordering = ["role", "pk"]
        verbose_name = "NSO Link Role Assignment"
        verbose_name_plural = "NSO Link Role Assignments"
        constraints = [
            models.UniqueConstraint(
                fields=["cable"],
                condition=models.Q(cable__isnull=False),
                name="nso_linkrole_unique_cable",
            ),
            models.UniqueConstraint(
                fields=["interface"],
                condition=models.Q(interface__isnull=False),
                name="nso_linkrole_unique_interface",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(cable__isnull=False, interface__isnull=True)
                    | models.Q(cable__isnull=True, interface__isnull=False)
                ),
                name="nso_linkrole_cable_xor_interface",
            ),
        ]

    def __str__(self):
        target = self.cable if self.cable_id else self.interface
        return f"{self.role} → {target}"

    def get_absolute_url(self):
        """Return the detail URL for this assignment."""
        return reverse("plugins:netbox_nso_plugin:nsolinkroleassignment", args=[self.pk])

    def clean(self):
        """Enforce the cable-XOR-interface rule and keep the target consistent with the role type."""
        super().clean()
        has_cable = self.cable_id is not None
        has_iface = self.interface_id is not None
        if has_cable == has_iface:
            raise ValidationError("Set exactly one of cable or interface.")
        if self.role_id:
            if self.role.link_type == "p2p" and not has_cable:
                raise ValidationError({"cable": "A point-to-point role must be assigned to a cable."})
            if self.role.link_type == "single" and not has_iface:
                raise ValidationError({"interface": "A single-ended role must be assigned to an interface."})


# ── #1503 Appendix O: the durable push outbox ─────────────────────────────────
#
# Plain Django models, deliberately not NetBoxModel: these are machinery rows written by
# every operator transaction that touches intent, so change logging, journaling and custom
# fields would multiply the write cost of a bulk edit for records no operator browses.


class NSOOwnershipManifest(models.Model):
    """Durable ownership evidence for one native object in one delivery scope."""

    device_id = models.PositiveBigIntegerField(db_column="device_id", db_index=True)
    scope = models.CharField(max_length=32)
    native_model_label = models.CharField(max_length=200)
    native_key = models.JSONField()
    ownership_state = models.CharField(max_length=32, default="owned")
    deletion_authority = models.BooleanField(default=False)
    acknowledged_lineage = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["device_id", "scope", "native_model_label", "native_key"],
                name="nso_owner_manifest_identity",
            )
        ]
        ordering = ["device_id", "scope", "native_model_label", "id"]
        verbose_name = "NSO Ownership Manifest Entry"
        verbose_name_plural = "NSO Ownership Manifest Entries"

    def __str__(self):
        return f"{self.device_id}/{self.scope} {self.native_model_label} {self.native_key}"


class NSOIntentRevision(models.Model):
    """The durable content revision of one device delivery scope."""

    device = models.ForeignKey(to="dcim.Device", on_delete=models.CASCADE, related_name="nso_intent_revisions")
    scope = models.CharField(max_length=32)
    revision = models.BigIntegerField(default=0)
    verified_revision = models.BigIntegerField(null=True, blank=True)
    verified_fingerprint = models.CharField(max_length=64, null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["device", "scope"],
                name="nso_intent_revision_key",
            )
        ]
        ordering = ["device", "scope"]
        verbose_name = "NSO Intent Revision"
        verbose_name_plural = "NSO Intent Revisions"

    def __str__(self):
        return f"{self.device_id}/{self.scope} r{self.revision}"


class NSOApplyAttempt(models.Model):
    """One manual Apply identity and the exact request available for replay."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    management = models.ForeignKey(
        to="NSODeviceManagement",
        on_delete=models.CASCADE,
        related_name="apply_attempts",
    )
    created_at = models.DateTimeField(db_default=Now())
    adapter_device_id = models.BigIntegerField(null=True, blank=True)
    scope_revisions = models.JSONField(default=dict, blank=True)
    selected = models.JSONField(default=dict, blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["management", "-created_at"], name="nso_apply_attempt_device")]
        ordering = ["-created_at"]
        verbose_name = "NSO Apply Attempt"
        verbose_name_plural = "NSO Apply Attempts"

    def __str__(self):
        return str(self.pk)


class NSOIntentOutboxEntry(models.Model):
    """One operator transaction's contribution to a delivery key.

    Appended inside the operator's own transaction, so a rollback — whole or savepoint —
    takes the contribution with it, which the in-memory coalescer cannot do. Rewritten by
    compaction and deleted when the claim that consumed it is acknowledged.
    """

    device = models.ForeignKey(to="dcim.Device", on_delete=models.CASCADE, related_name="nso_intent_outbox_entries")
    scope = models.CharField(max_length=32)
    kind = models.CharField(
        max_length=16,
        choices=CONTRIBUTION_KIND_CHOICES,
        default=CONTRIBUTION_KIND_ORDINARY,
    )
    # ``txid_current()`` — provenance only: the fold is transaction-blind (OQ-O-9), so no
    # rule reads this. It groups the rows one transaction wrote, for an operator reading them.
    batch_id = models.BigIntegerField()
    # Ordered, in the order the operator's transaction applied them: two histories that
    # differ only in order must not reduce to the same authority.
    transitions = models.JSONField(default=list, blank=True)
    # The AND and the OR of this row's contributors' legacy delete-origin marks. There is no
    # per-entry ``delete_origin``: a compacted row would inherit it as true and send marked.
    mark_and = models.BooleanField(default=False)
    mark_any = models.BooleanField(default=False)
    consumed_by_push_seq = models.BigIntegerField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        ordering = ["id"]
        # No unique constraint, by design: two transactions appending to one key must never
        # collide, and compaction is what collapses them.
        indexes = [
            models.Index(
                fields=["device", "scope"],
                condition=models.Q(consumed_by_push_seq__isnull=True),
                name="nso_outbox_unconsumed",
            ),
        ]
        verbose_name = "NSO Intent Outbox Entry"
        verbose_name_plural = "NSO Intent Outbox Entries"

    def __str__(self):
        return f"{self.device_id}/{self.scope} #{self.pk}"


class NSOIntentOutboxState(models.Model):
    """The drain's state for one ``(device, scope)``, and the lock every drain-side operation takes.

    Created on demand by the first drain-side operation, never by an enqueue: an operator
    transaction that took this row would serialize with every other writer of the key.
    """

    device = models.ForeignKey(to="dcim.Device", on_delete=models.CASCADE, related_name="nso_intent_outbox_states")
    scope = models.CharField(max_length=32)
    # Authority folded out of consumed entries, before a claim exists. Deletions are whole
    # triple-bearing records; a revocation needs no triple, so those stay bare ids.
    queued_deletions = models.JSONField(default=list, blank=True)
    revoked_ids = models.JSONField(default=list, blank=True)
    # The logical operation id, replayed on takeover and never reallocated.
    push_seq = models.BigIntegerField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claim_payload = models.JSONField(null=True, blank=True)
    claim_deletions = models.JSONField(default=list, blank=True)
    claim_flags = models.JSONField(default=dict, blank=True)
    claim_revision = models.BigIntegerField(null=True, blank=True)
    # Not the receipt's digest: that one is over the wire body, and is derived from
    # ``claim_payload`` when a restore needs it. This also carries the mode, the authority
    # and the legacy flag, which no body carries, and is what an unchanged claim drops against.
    claim_identity = models.CharField(max_length=64, blank=True, default="")
    # The legacy flag actually sent (``query_flag`` scopes), the AND of the folded
    # contributors' ``mark_and`` values. Not a run identifier: there are no runs.
    claim_mark = models.BooleanField(null=True, blank=True)
    # ``{route_id: last_acked_triple}`` — one triple per pk, carried across a revoked
    # deletion so a re-ownership of that pk inherits the only history that can matter.
    lineage_carry = models.JSONField(default=dict, blank=True)
    last_success_identity = models.CharField(max_length=64, blank=True, default="")
    last_success_at = models.DateTimeField(null=True, blank=True)
    attempts = models.IntegerField(default=0)
    # Stamped on EVERY drain attempt, success or failure: the fair-selection cursor.
    last_drain_attempted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    last_error_at = models.DateTimeField(null=True, blank=True)
    # Never cleared by a push outcome — only by an explicit operator acknowledgement.
    degraded_deletions = models.JSONField(default=list, blank=True)
    fence_withheld_since = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["device", "scope"]
        unique_together = [("device", "scope")]
        verbose_name = "NSO Intent Outbox State"
        verbose_name_plural = "NSO Intent Outbox States"

    def __str__(self):
        return f"{self.device_id}/{self.scope}"


class NSOIntentDeploymentControl(models.Model):
    """The fleet-wide deployment quiescence switch, stored where every process sees it."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    quiesced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "NSO Intent Deployment Control"
        verbose_name_plural = "NSO Intent Deployment Control"
