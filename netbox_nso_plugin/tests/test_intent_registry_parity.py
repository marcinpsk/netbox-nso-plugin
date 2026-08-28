# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1627 P4 pin P2 — the demotion ranks and the renderer-input registry must agree.

``OVERLAY_MODEL_RANKS`` orders the overlays a repair may demote; the renderer-input
registry says which models a scope actually resolves. A label in one and not the other is
silent: a ranked-but-unregistered overlay is never planned for demotion, and a registered
overlay with no rank has no place in the lock order.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from netbox_nso_plugin.intent_state import OVERLAY_MODEL_RANKS, renderer_input_specs

#: Ranked for demotion but registered nowhere, so no repair plan and no ownership sweep can
#: reach it (#1627 P4-M5a). A production fix is owed; until it lands this ledger names the
#: whole of the gap, so the pin fails both when the gap grows and when it is closed.
_UNREGISTERED_RANKS = frozenset({"netbox_nso_plugin.nsobgppeertemplatestate"})


def _plugin_overlays_with_status() -> set:
    """Registered plugin models carrying a lifecycle ``status``, which is what a rank orders."""
    return {
        label
        for label, spec in renderer_input_specs().items()
        if label.startswith("netbox_nso_plugin.")
        and any(field.name == "status" for field in spec.model._meta.concrete_fields)
    }


class TestOverlayRankRegistryParity(SimpleTestCase):
    def test_every_ranked_overlay_is_a_registered_renderer_input(self):
        unregistered = set(OVERLAY_MODEL_RANKS) - set(renderer_input_specs())

        self.assertEqual(sorted(unregistered), sorted(_UNREGISTERED_RANKS))

    def test_every_registered_overlay_with_a_status_carries_a_rank(self):
        unranked = _plugin_overlays_with_status() - set(OVERLAY_MODEL_RANKS)

        self.assertEqual(sorted(unranked), [])
