# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Derived-intent engine for the NSO plugin.

Implements — Description-from-Cable-Topology. See docs/m8-derived-intent-plan.md
for the full design spec.  The public surface is:

  * ``load_sentinel_templates(raw)`` — validate and parse the config-time sentinel list.
  * ``is_managed_description(value, templates)`` — check if a description is managed.
  * ``compute_description(interface, sentinel)`` — pure compute returning the target value.
  * ``register() / registered_fields() / fields_for_attribute()`` — generic registry for
    future derived fields (LLDP, MTU, …).

Registration of ``description_from_cable`` happens in ``PluginConfig.ready()`` via
``_register_description_from_cable()`` so tests can isolate without side effects.
"""

import logging
import string
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentinel config types
# ---------------------------------------------------------------------------

KNOWN_PLACEHOLDERS: frozenset[str] = frozenset(
    {"peer_host", "peer_iface", "peer_site", "peer_role", "self_host", "self_iface"}
)


@dataclass(frozen=True)
class SentinelTemplate:
    """Parsed, validated sentinel → template mapping from PLUGINS_CONFIG."""

    sentinel: str
    template: str


class ConfigError(ValueError):
    """Raised when the ``derived_intent`` config block is malformed."""


def load_sentinel_templates(raw: list) -> list[SentinelTemplate]:
    """Validate and parse ``description_templates`` from PLUGINS_CONFIG.

    Raises ``ConfigError`` on any malformed entry so NetBox surfaces the
    problem at boot time rather than silently at runtime.
    """
    if not isinstance(raw, list):
        raise ConfigError(f"derived_intent.description_templates must be a list, got {type(raw).__name__}")

    result: list[SentinelTemplate] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"description_templates[{i}] must be a dict, got {type(item).__name__}")
        unknown_keys = set(item) - {"sentinel", "template"}
        if unknown_keys:
            raise ConfigError(
                f"description_templates[{i}] has unexpected keys: {sorted(unknown_keys)} — "
                "did you mean 'template' (singular)?"
            )
        if "sentinel" not in item:
            raise ConfigError(f"description_templates[{i}] is missing key 'sentinel'")
        if "template" not in item:
            raise ConfigError(f"description_templates[{i}] is missing key 'template'")

        sentinel = item["sentinel"]
        template = item["template"]

        if not isinstance(sentinel, str) or not sentinel:
            raise ConfigError(f"description_templates[{i}].sentinel must be a non-empty string")
        if not isinstance(template, str):
            raise ConfigError(f"description_templates[{i}].template must be a string")
        if not template.startswith(sentinel):
            raise ConfigError(
                f"description_templates[{i}].template must start with its sentinel {sentinel!r}, got {template!r}"
            )

        # Validate placeholders via stdlib Formatter — no regex
        field_names = {
            field_name for _, field_name, _, _ in string.Formatter().parse(template) if field_name is not None
        }
        unknown = field_names - KNOWN_PLACEHOLDERS
        if unknown:
            raise ConfigError(
                f"description_templates[{i}].template references unknown "
                f"placeholder(s): {sorted(unknown)}.  Known: {sorted(KNOWN_PLACEHOLDERS)}"
            )

        result.append(SentinelTemplate(sentinel=sentinel, template=template))

    # Reject prefix-overlapping sentinels: sort by length ascending (shorter first),
    # then check each against the next — a shorter sentinel is always a prefix candidate.
    sorted_sentinels = sorted((t.sentinel for t in result), key=lambda s: (len(s), s))
    for idx in range(len(sorted_sentinels) - 1):
        curr = sorted_sentinels[idx]
        nxt = sorted_sentinels[idx + 1]
        if nxt.startswith(curr):
            raise ConfigError(
                f"Sentinel {curr!r} is a prefix of {nxt!r} — ambiguous match order. Use non-overlapping sentinels."
            )

    return result


# ---------------------------------------------------------------------------
# Generic derived-field registry (Phase 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedField:
    """Descriptor for one derived field registered with this engine."""

    name: str
    target_attribute: str
    compute: Callable
    is_managed: Callable


_REGISTRY: dict[str, DerivedField] = {}


def register(field: DerivedField) -> None:
    """Register a DerivedField. Raises ConfigError on duplicate name."""
    if field.name in _REGISTRY:
        raise ConfigError(f"Derived field {field.name!r} already registered")
    _REGISTRY[field.name] = field


def registered_fields() -> list[DerivedField]:
    """Return all registered DerivedField entries."""
    return list(_REGISTRY.values())


def fields_for_attribute(attr: str) -> list[DerivedField]:
    """Return registered fields whose target_attribute matches ``attr``."""
    return [f for f in _REGISTRY.values() if f.target_attribute == attr]


# ---------------------------------------------------------------------------
# Description-from-cable compute (Phase 4)
# ---------------------------------------------------------------------------

SKIP_LAG_MEMBER = "lag_member"
SKIP_BREAKOUT_CHILD = "breakout_child"
SKIP_MULTI_TERMINATION = "multi_termination"
SKIP_NON_INTERFACE_PEER = "non_interface_peer"


@dataclass
class SkipReason:
    """Structured skip-log payload; serializable for logger.info calls."""

    reason: str
    detail: dict


def detect_skip(interface) -> SkipReason | None:
    """Return a SkipReason if v1 scope excludes this interface, else None.

    v1 excludes: LAG members, breakout children.  Multi-termination and
    non-Interface peers are caught later in find_peer.
    """
    if getattr(interface, "lag_id", None) is not None:
        return SkipReason(
            reason=SKIP_LAG_MEMBER,
            detail={"interface_id": interface.pk, "lag_id": interface.lag_id},
        )
    if getattr(interface, "parent_id", None) is not None:
        return SkipReason(
            reason=SKIP_BREAKOUT_CHILD,
            detail={"interface_id": interface.pk, "parent_id": interface.parent_id},
        )
    return None


def find_peer(interface):
    """Return the single peer Interface or None.

    Uses ``interface.link_peers`` — confirmed as the canonical single-hop
    helper in Phase 1 spike (test_spike_cable_api.py).

    Returns None if:
    - no cable attached
    - more than one termination on either side (multi-point — v1 skips)
    - peer is not an Interface (e.g. circuit termination)
    """
    from dcim.models import Interface as _Interface

    peers = interface.link_peers
    if not peers:
        return None
    if len(peers) > 1:
        logger.info(
            "derived_intent.skipped field=description_from_cable interface_id=%s reason=%s detail=%s",
            interface.pk,
            SKIP_MULTI_TERMINATION,
            {"peer_count": len(peers)},
        )
        return None
    peer = peers[0]
    if not isinstance(peer, _Interface):
        logger.info(
            "derived_intent.skipped field=description_from_cable interface_id=%s reason=%s detail=%s",
            interface.pk,
            SKIP_NON_INTERFACE_PEER,
            {"peer_type": type(peer).__name__},
        )
        return None
    return peer


def render_template(template: str, *, self_iface, peer_iface) -> str:
    """Render a sentinel template with the interface objects.

    *peer_iface* may be ``None`` (single-ended link roles): the ``peer_*``
    placeholders then render empty. Shared by the M8 cable-derived descriptions
    and the link-role description consumer.
    """
    peer_dev = peer_iface.device if peer_iface is not None else None
    return template.format(
        self_host=self_iface.device.name,
        self_iface=self_iface.name,
        peer_host=peer_dev.name if peer_dev is not None else "",
        peer_iface=peer_iface.name if peer_iface is not None else "",
        peer_site=peer_dev.site.name if (peer_dev is not None and peer_dev.site_id) else "",
        peer_role=(peer_dev.role.name if (peer_dev is not None and getattr(peer_dev, "role_id", None)) else ""),
    )


def compute_description(interface, sentinel: SentinelTemplate) -> str | None:
    """Pure function: return the target description value for *interface*.

    Returns None if v1 scope excludes this interface (caller leaves it alone).
    Returns the bare sentinel if no cable is attached or if either endpoint
    has no assigned device (inventory-mode interfaces).
    Returns the rendered template otherwise.
    """
    if getattr(interface, "device", None) is None:
        return None  # no device — skip; nothing meaningful to render
    skip = detect_skip(interface)
    if skip:
        logger.info(
            "derived_intent.skipped field=description_from_cable interface_id=%s reason=%s detail=%s",
            interface.pk,
            skip.reason,
            skip.detail,
        )
        return None
    peer = find_peer(interface)
    if peer is None:
        return sentinel.sentinel
    if getattr(peer, "device", None) is None:
        return sentinel.sentinel  # peer has no device — fall back to bare sentinel
    return render_template(sentinel.template, self_iface=interface, peer_iface=peer)


def is_managed_description(value: str, templates: list[SentinelTemplate]) -> SentinelTemplate | None:
    """Return the matching SentinelTemplate if *value* is a managed description, else None.

    Uses longest-prefix match (prefix-overlap is rejected at config load so
    this is defensive, not strictly necessary).
    """
    if not value:
        return None
    for t in sorted(templates, key=lambda x: -len(x.sentinel)):
        if value.startswith(t.sentinel):
            return t
    return None


# ---------------------------------------------------------------------------
# Registration shim (called from PluginConfig.ready())
# ---------------------------------------------------------------------------


def _register_description_from_cable() -> None:
    """Register the description_from_cable DerivedField.

    Called once from PluginConfig.ready() after load_sentinel_templates
    returns successfully. Tests that don't need a full AppConfig can skip
    this and call register() directly.
    """
    register(
        DerivedField(
            name="description_from_cable",
            target_attribute="description",
            compute=compute_description,
            is_managed=is_managed_description,
        )
    )
