# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Opt-in observations for the real explicit renderer writer."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from unittest.mock import patch


@dataclass(frozen=True)
class StrictWriterRecord:
    """One frozen plan and the operation indexes consumed by its real writer."""

    write_set: tuple
    consumed_indexes: frozenset[int]


@contextlib.contextmanager
def strict_writer_harness():
    """Record explicit writer completion inside one requesting test only."""
    from netbox_nso_plugin.renderer_writer import RendererWriter

    records = []
    real_assert_complete = RendererWriter.assert_complete

    def record_and_assert(writer):
        records.append(StrictWriterRecord(writer.plan.write_set, frozenset(writer._consumed)))
        return real_assert_complete(writer)

    with patch.object(RendererWriter, "assert_complete", record_and_assert):
        yield records
