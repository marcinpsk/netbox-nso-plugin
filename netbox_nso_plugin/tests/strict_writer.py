# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Opt-in observations for the real explicit renderer writer."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from unittest.mock import patch


class _ConsumedInOrder(set):
    """The writer's consumed-index set, keeping the order — and the repeats — it was given.

    ``_consumed`` is a set on purpose: the writer asks it only whether an operation is still
    available. That makes it blind to an operation consumed TWICE, which is exactly the
    failure a strict observation exists to catch, so the harness records every event.
    """

    def __init__(self):
        super().__init__()
        self.order: list[int] = []

    def add(self, index):
        self.order.append(index)
        super().add(index)

    def update(self, indexes):
        indexes = list(indexes)
        self.order.extend(indexes)
        super().update(indexes)


@dataclass(frozen=True)
class StrictWriterRecord:
    """One frozen plan and the operation indexes consumed by its real writer."""

    write_set: tuple
    consumed_order: tuple

    @property
    def consumed_indexes(self) -> frozenset[int]:
        return frozenset(self.consumed_order)

    @property
    def duplicates(self) -> tuple:
        """Every index the writer consumed more than once, in the order it repeated them."""
        seen: set[int] = set()
        repeated = []
        for index in self.consumed_order:
            if index in seen:
                repeated.append(index)
            seen.add(index)
        return tuple(repeated)

    @property
    def unconsumed(self) -> tuple:
        """Planned non-cascade operations the writer never executed."""
        return tuple(
            index
            for index, write in enumerate(self.write_set)
            if index not in self.consumed_indexes and not write.cascade
        )


def assert_each_operation_consumed_once(records) -> None:
    """Every observed plan executed each planned operation exactly once."""
    for record in records:
        assert not record.duplicates, f"the writer consumed {record.duplicates} twice: {record.write_set}"
        assert not record.unconsumed, f"the writer left {record.unconsumed} unexecuted: {record.write_set}"


@contextlib.contextmanager
def strict_writer_harness():
    """Record explicit writer completion inside one requesting test only."""
    from netbox_nso_plugin.renderer_writer import RendererWriter

    records = []
    real_init = RendererWriter.__init__
    real_assert_complete = RendererWriter.assert_complete

    def init_and_record_order(writer, *args, **kwargs):
        real_init(writer, *args, **kwargs)
        writer._consumed = _ConsumedInOrder()

    def record_and_assert(writer):
        records.append(StrictWriterRecord(writer.plan.write_set, tuple(writer._consumed.order)))
        return real_assert_complete(writer)

    with (
        patch.object(RendererWriter, "__init__", init_and_record_order),
        patch.object(RendererWriter, "assert_complete", record_and_assert),
    ):
        yield records
