# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Guard tests for the overlay status state machine (status_machine.py).

These tests do not exercise runtime behaviour — they assert the *spec* is sound and
that every overlay's status vocabulary tracks the canonical one. The reachability
test deliberately documents the ``apply_failed`` / ``error`` gaps via an xfail that
flips green only once those transitions are wired.
"""

from __future__ import annotations

import unittest

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.test import SimpleTestCase

from netbox_nso_plugin import status_machine as sm

# Core lifecycle states every write-path overlay is expected to declare.
_CORE = {"imported", "accepted", "deploying", "in_sync", "changed"}
# Overlay models that intentionally omit some core states (tracked, to be fixed).
# The SNMP overlays predate `changed` and never grew a drift status (they use no
# drift state at all). The interface/IP overlays use the legacy `drifted` synonym.
_KNOWN_VOCAB_GAPS = {"changed"}
_LEGACY_DRIFT_SYNONYM = {"drifted"}


def _overlay_models():
    """Every plugin model carrying the lifecycle ``status`` field."""
    for model in apps.get_app_config("netbox_nso_plugin").get_models():
        try:
            field = model._meta.get_field("status")
        except FieldDoesNotExist:
            continue
        choices = {c[0] for c in (field.choices or [])}
        if {"imported", "in_sync"} <= choices:
            yield model, choices


class TestStatusVocabulary(SimpleTestCase):
    def test_at_least_one_overlay_discovered(self):
        # Guards against the introspection silently matching nothing.
        self.assertGreaterEqual(len(list(_overlay_models())), 10)

    def test_every_overlay_choice_is_canonical_or_pinned_legacy(self):
        """No overlay may declare a status outside STATES ∪ pinned LEGACY_STATES.

        A brand-new stray state still fails here; only the explicitly tracked
        legacy states (`drifted`, `reserved`) are tolerated.
        """
        allowed = sm.STATES | sm.LEGACY_STATES
        offenders = {
            model.__name__: sorted(choices - allowed) for model, choices in _overlay_models() if not choices <= allowed
        }
        self.assertEqual(offenders, {}, f"overlays declare unknown states: {offenders}")

    def test_legacy_vocab_divergence_is_pinned(self):
        """Pin exactly which overlays use legacy (non-canonical) states.

        Folding `drifted`→`changed` (or migrating `reserved`) must update
        status_machine.LEGACY_VOCAB_BY_MODEL — making the cleanup a visible diff.
        """
        actual = {
            model.__name__: sorted(choices & sm.LEGACY_STATES)
            for model, choices in _overlay_models()
            if choices & sm.LEGACY_STATES
        }
        expected = {name: sorted(states) for name, states in sm.LEGACY_VOCAB_BY_MODEL.items()}
        self.assertEqual(actual, expected)

    def test_core_state_omissions_are_contained(self):
        """The only tolerated core-state omission is `changed`, on a pinned set.

        Overlays using the legacy `drifted` synonym cover drift under a different
        name; the EAV/secret-style mirrors (SNMP, logging) signal divergence via
        `conflict` and have no value-diff drift state. Anything else is a real gap.
        """
        for model, choices in _overlay_models():
            excused = _KNOWN_VOCAB_GAPS if (choices & _LEGACY_DRIFT_SYNONYM) else set()
            missing = (_CORE - choices) - excused
            if not missing:
                continue
            self.assertEqual(
                missing, _KNOWN_VOCAB_GAPS, f"{model.__name__} missing unexpected core states: {sorted(missing)}"
            )
            self.assertIn(
                model.__name__,
                sm.OVERLAYS_WITHOUT_DRIFT_STATE,
                f"unexpected overlay {model.__name__} missing {sorted(missing)}",
            )

    def test_no_drift_overlay_pin_is_accurate(self):
        """The pinned no-`changed` set must match reality (no stale/missing entries)."""
        actual = {model.__name__ for model, choices in _overlay_models() if "changed" not in choices}
        self.assertEqual(actual, set(sm.OVERLAYS_WITHOUT_DRIFT_STATE))


class TestStateMachineSpec(SimpleTestCase):
    def test_transitions_reference_only_declared_states_and_events(self):
        for t in sm.TRANSITIONS:
            self.assertIn(t.src, sm.STATES, f"{t} has unknown src")
            self.assertIn(t.dst, sm.STATES, f"{t} has unknown dst")
            self.assertIn(t.event, sm.EVENTS, f"{t} has unknown event")

    def test_intended_graph_reaches_every_state(self):
        """The intended machine (all edges) must reach every declared state.

        This proves the spec has no orphan/dead state — independent of whether each
        edge is implemented yet.
        """
        self.assertEqual(sm.reachable_states(implemented_only=False), sm.STATES)

    def test_gaps_are_exactly_apply_failed_and_error(self):
        """Pin the *current* implementation gaps so closing one is a visible diff.

        These two states are declared (and rendered) but unreachable because the
        only edges into them are implemented=False. When a gap is wired, this test
        fails and must be updated alongside the transition flag.
        """
        self.assertEqual(sm.unreachable_states(implemented_only=True), {sm.APPLY_FAILED, sm.ERROR})

    # apply_failed/error are not yet wired (deploying→apply_failed needs the adapter
    # to expose per-intent errors; reconcile_error→error needs reconcile exception
    # handling). Under the unittest runner an expectedFailure that *passes* is an
    # unexpected success → the suite goes red, forcing this marker to be removed once
    # the edges are implemented. That is the intended "flips green when fixed" guard.
    @unittest.expectedFailure
    def test_implemented_graph_reaches_every_state(self):
        self.assertEqual(sm.reachable_states(implemented_only=True), sm.STATES)

    def test_allowed_rejects_illegal_transition(self):
        # A reconcile must never pull an owned 'in_sync' row back to 'imported'.
        self.assertNotIn(sm.IMPORTED, sm.allowed(sm.RECONCILE, sm.IN_SYNC))
        # Apply only proceeds from accepted.
        self.assertEqual(sm.allowed(sm.APPLY, sm.ACCEPTED), {sm.DEPLOYING})
        self.assertEqual(sm.allowed(sm.APPLY, sm.IMPORTED), frozenset())
