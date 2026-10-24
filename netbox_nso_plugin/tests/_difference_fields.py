# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Render the fields of difference rows for value-leak assertions."""

from netbox_nso_plugin.comparison_values import MISSING


def shown_fields(rows):
    """Return the shown row fields as text; repr(rows) holds object addresses that can contain any digits."""
    return repr(
        [
            tuple(
                "missing" if value is MISSING else value
                for value in (
                    row.identity,
                    row.attribute,
                    row.netbox_value,
                    row.device_value,
                    row.reason,
                    row.association_candidate,
                )
            )
            for row in rows
        ]
    )
