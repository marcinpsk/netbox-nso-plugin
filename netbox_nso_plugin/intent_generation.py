# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The plugin-global allocator behind ``NSOStaticRouteState.intent_generation``.

A generation names the intent a push carried, and an apply result settles an overlay only
when it still names that generation. The allocator is therefore a **database sequence**,
not a counter column:

* a counter on ``NSODeviceManagement`` resets when that row is deleted and recreated —
  which offboard/re-onboard does routinely — while the adapter keeps the same ``Device``,
  incarnation and job history, so a fresh overlay would reissue a generation an
  unconsumed result still carries and false-green the new lifecycle;
* a singleton counter row would serialize every unrelated static-route edit until commit.

A sequence burns values on rollback. That is correct here: the requirement is
monotonicity, not contiguity. ``0`` is the unallocated sentinel — every row migrated in
keeps it, it is never allocated, never put on the wire and never correlates.
"""

from __future__ import annotations

from django.db import connection

SEQUENCE_NAME = "nso_intent_generation_seq"

UNALLOCATED = 0


def allocate_intent_generation() -> int:
    """Return the next plugin-global intent generation (strictly increasing, never reused)."""
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT nextval('{SEQUENCE_NAME}')")  # noqa: S608 — a module constant, not input
        return int(cursor.fetchone()[0])
