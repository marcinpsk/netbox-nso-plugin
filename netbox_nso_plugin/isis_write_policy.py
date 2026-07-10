# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""IS-IS write-path capability: which mirrored fields the intent push carries (#78).

The reconciler mirrors far more of a device's IS-IS config into netbox-routing than
the write path can push: the intent payload (``signals._push_isis_intent_for_device``)
serialises only the overlay fields, so editing any other mirrored field and clicking
Accept silently re-pushes the same snapshot — the change never reaches the device.

This module is the single source of truth for that boundary, surfaced two ways:

* a warning panel on the netbox-routing IS-IS object pages (where the edit happens),
* an integrity test that captures a REAL push payload and asserts both directions
  (every read-only field absent, every writable field present) — so this registry
  cannot silently drift from the code.

When a writer lands for one of these fields (e.g. the #83 TI-LFA/FRR pipeline),
move it from the read-only tuple to the pushed tuple and the test keeps you honest.
"""

from __future__ import annotations

# Mirrored into netbox-routing by the reconciler but NOT carried by the intent push.
ISIS_READ_ONLY_FIELDS: dict[str, tuple[str, ...]] = {
    "isis_instance": (
        "spf_initial_wait",
        "spf_max_wait",
        "lsp_initial_wait",
        "lsp_max_wait",
        "lsp_lifetime",
        "lsp_refresh_interval",
        "lsp_mtu",
        "overload_on_startup",
        "overload_timeout",
        "te_enabled",
        "suppress_attached_bit",
        "ignore_attached_bit",
        "distance",
        "maximum_paths",
        "reference_bandwidth",
    ),
    "isis_interface": (
        "hello_auth_type",
        "csnp_interval",
        "retransmit_interval",
        "lsp_interval",
        "mesh_group",
    ),
    "isis_level": ("default_metric", "preference", "auth_type"),
}

# Carried by the push — editing these + Accept genuinely reaches the device.
# fast_reroute/microloop_avoidance + frr_enabled/frr_protection moved here when
# the #83 writers landed (per-NED expressibility gaps WARN in the reconciler).
ISIS_PUSHED_FIELDS: dict[str, tuple[str, ...]] = {
    "isis_instance": (
        "net",
        "is_type",
        "metric_style",
        "overload_bit",
        "area_auth_type",
        "area_auth_key",
        "domain_auth_type",
        "domain_auth_key",
        "fast_reroute",
        "microloop_avoidance",
    ),
    "isis_interface": (
        "circuit_type",
        "network_type",
        "metric",
        "passive",
        "bfd_enabled",
        "frr_enabled",
        "frr_protection",
    ),
    "isis_level": ("wide_metrics_only", "labeled_preference", "disabled"),
}

# Whole child models with NO write path at all (mirror-only) — listed per page.
ISIS_CHILD_NOTES: dict[str, tuple[str, ...]] = {
    "isis_instance": (
        "Level rows: default_metric, preference and auth_type are read-only "
        "(wide_metrics_only, labeled_preference and disabled are pushed).",
        "Interface-level rows, the IS-IS settings bag, segment-routing (SRGB/SRLB/MSD) "
        "and SRv6 locators are fully read-only mirrors.",
    ),
    "isis_interface": ("Interface-level rows, prefix-SIDs and the IS-IS settings bag are fully read-only mirrors.",),
}
