# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared 3-way merge helpers for the routing reconcilers.

The reconcilers materialise device config into operator-editable netbox-routing
objects. To auto-mirror device-side changes *without* clobbering operator edits, each
reconciler stores a ``device_base_hash`` (the device content at the last agreed sync)
and, per row, compares three canonical content hashes — the NetBox object, the current
device, and that base — to pick an action. See [[status-state-machine]] (3-way merge).
"""

from __future__ import annotations

import hashlib
import json


def pk(obj):
    """Return an FK target's pk (None-safe), for canonical content serialization."""
    return obj.pk if obj is not None else None


def content_hash(content: dict) -> str:
    """Return a stable hash of a canonical content dict (FKs as pks, sorted keys)."""
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


def three_way(*, created: bool, base: str, obj_hash: str, dev_hash: str) -> str:
    """Pick the merge action from the object/device/base comparison.

    Returns one of:
      ``seed``     — first import (or no base yet): write device → object, set base.
      ``insync``   — object already equals device: advance base, nothing to do.
      ``mirror``   — device moved, object untouched (== base): write device → object.
      ``freeze``   — object edited, device unchanged (== base): keep the edit, flag drift.
      ``conflict`` — both moved since base: keep the edit, flag conflict.
    """
    if created or not base:
        return "seed"
    if obj_hash == dev_hash:
        return "insync"
    if obj_hash == base and dev_hash != base:
        return "mirror"
    if dev_hash == base and obj_hash != base:
        return "freeze"
    return "conflict"
