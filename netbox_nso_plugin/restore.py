# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Identity-namespace recovery for a plugin-only database restore."""

from __future__ import annotations

_ADVANCE_BATCH = 10_000


def advance_static_route_pk(watermark: int) -> int:
    """Burn route ids through *watermark* without ever moving the sequence backwards."""
    from django.core.management.base import CommandError
    from django.db import connection
    from netbox_routing.models import StaticRoute

    from .drain import MAX_RESTORE_GAP

    watermark = int(watermark)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [StaticRoute._meta.db_table])
        sequence = cursor.fetchone()[0]
        if not sequence:
            raise RuntimeError("StaticRoute.id has no PostgreSQL sequence")
        cursor.execute("SELECT nextval(%s)", [sequence])
        issued = int(cursor.fetchone()[0])
        gap = watermark - issued
        if gap > MAX_RESTORE_GAP:
            raise CommandError(
                f"The adapter's route-id watermark {watermark} is {gap} values ahead of the local sequence"
            )
        while issued < watermark:
            previous = issued
            cursor.execute(
                "SELECT max(nextval(%s)) FROM generate_series(1, %s)",
                [sequence, min(watermark - issued, _ADVANCE_BATCH)],
            )
            issued = int(cursor.fetchone()[0])
            if issued <= previous:
                raise CommandError(
                    f"The route-id advance stalled at {issued}, below the adapter's watermark {watermark}"
                )
    return issued
