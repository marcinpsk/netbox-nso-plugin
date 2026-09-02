# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Guard tests for the overlay status state machine (status_machine.py).

These tests do not exercise runtime behaviour — they assert the *spec* is sound and
that every overlay's status vocabulary tracks the canonical one. Every declared state
is now reachable through real (``implemented=True``) transitions, so the reachability
guard is fully green (the historical ``apply_failed`` / ``error`` gaps are wired).
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_no_remaining_gaps(self):
        """All states are now reachable through real (implemented) code paths.

        apply_failed was wired in step 4 (on_apply_result + _settle_apply_failures);
        ``error`` is now wired via on_reconcile_error + reconcile._safe_reconcile. A
        new declared-but-unwired state re-introduces an entry here and fails this test.
        """
        self.assertEqual(sm.unreachable_states(implemented_only=True), frozenset())

    def test_implemented_graph_reaches_every_state(self):
        self.assertEqual(sm.reachable_states(implemented_only=True), sm.STATES)

    def test_allowed_rejects_illegal_transition(self):
        # A reconcile must never pull an owned 'in_sync' row back to 'imported'.
        self.assertNotIn(sm.IMPORTED, sm.allowed(sm.RECONCILE, sm.IN_SYNC))
        # Apply proceeds from accepted and retries a failed owned row.
        self.assertEqual(sm.allowed(sm.APPLY, sm.ACCEPTED), {sm.DEPLOYING})
        self.assertEqual(sm.allowed(sm.APPLY, sm.APPLY_FAILED), {sm.DEPLOYING})
        self.assertEqual(sm.allowed(sm.APPLY, sm.IMPORTED), frozenset())


class TestAdvanceEngine(SimpleTestCase):
    """The runtime guard. Pure functions — no DB."""

    def test_operator_edit_repends_every_existing_status(self):
        for current in sm.STATES:
            with self.subTest(current=current):
                self.assertEqual(sm.on_operator_edit(current), sm.ACCEPTED)

    def test_deterministic_edges_infer_target(self):
        self.assertEqual(sm.advance(sm.IMPORTED, sm.ACCEPT), sm.ACCEPTED)
        self.assertEqual(sm.advance(sm.CHANGED, sm.ACCEPT), sm.ACCEPTED)
        self.assertEqual(sm.advance(sm.CONFLICT, sm.ACCEPT), sm.ACCEPTED)
        self.assertEqual(sm.advance(sm.ACCEPTED, sm.APPLY), sm.DEPLOYING)
        self.assertEqual(sm.advance(sm.APPLY_FAILED, sm.APPLY), sm.DEPLOYING)
        self.assertEqual(sm.advance(sm.ACCEPTED, sm.REVERT), sm.IMPORTED)
        self.assertEqual(sm.advance(sm.DEPLOYING, sm.APPLY_OK), sm.IN_SYNC)

    def test_apply_failed_is_retryable_via_accept(self):
        self.assertEqual(sm.advance(sm.APPLY_FAILED, sm.ACCEPT), sm.ACCEPTED)

    def test_guarded_edge_requires_explicit_target(self):
        # reconcile of an owned 'accepted' row is value-aware → caller must choose.
        with self.assertRaises(sm.AmbiguousTransition):
            sm.advance(sm.ACCEPTED, sm.RECONCILE)
        self.assertEqual(sm.advance(sm.ACCEPTED, sm.RECONCILE, to=sm.IN_SYNC), sm.IN_SYNC)
        self.assertEqual(sm.advance(sm.ACCEPTED, sm.RECONCILE, to=sm.ACCEPTED), sm.ACCEPTED)

    def test_no_clobber_of_owned_rows_is_enforced(self):
        # The bug the duplicated `if status not in WRITE_PATH` idiom guards against:
        # a reconcile must not pull an owned row back to 'imported'.
        with self.assertRaises(sm.IllegalTransition):
            sm.advance(sm.ACCEPTED, sm.RECONCILE, to=sm.IMPORTED)
        with self.assertRaises(sm.IllegalTransition):
            sm.advance(sm.IN_SYNC, sm.RECONCILE, to=sm.IMPORTED)

    def test_apply_rejects_unowned_and_settled_states(self):
        with self.assertRaises(sm.IllegalTransition):
            sm.advance(sm.IMPORTED, sm.APPLY)
        with self.assertRaises(sm.IllegalTransition):
            sm.advance(sm.IN_SYNC, sm.APPLY)

    def test_unknown_event_raises_value_error(self):
        with self.assertRaises(ValueError):
            sm.advance(sm.IMPORTED, "frobnicate")

    def test_unknown_state_raises(self):
        with self.assertRaises(sm.IllegalTransition):
            sm.advance("bogus", sm.ACCEPT)

    def test_engine_accepts_unimplemented_gap_edges(self):
        # advance works over the INTENDED machine, so step 4 only has to start
        # *calling* these — no engine change.
        self.assertEqual(sm.advance(sm.DEPLOYING, sm.APPLY_ERR), sm.APPLY_FAILED)
        self.assertEqual(sm.advance(sm.IMPORTED, sm.RECONCILE_ERROR), sm.ERROR)

    def test_can_mirrors_advance_legality(self):
        self.assertTrue(sm.can(sm.APPLY, sm.ACCEPTED))
        self.assertTrue(sm.can(sm.APPLY, sm.ACCEPTED, to=sm.DEPLOYING))
        self.assertTrue(sm.can(sm.APPLY, sm.APPLY_FAILED, to=sm.DEPLOYING))
        self.assertFalse(sm.can(sm.APPLY, sm.IMPORTED))
        self.assertFalse(sm.can(sm.RECONCILE, sm.ACCEPTED, to=sm.IMPORTED))

    def test_advance_only_ever_returns_declared_states(self):
        # Property: every legal (event, src[, to]) result is a canonical state.
        for t in sm.TRANSITIONS:
            result = sm.advance(t.src, t.event, to=t.dst)
            self.assertIn(result, sm.STATES)
            self.assertEqual(result, t.dst)


class TestOnReconcile(SimpleTestCase):
    """The single reconcile rule shared by every overlay."""

    def test_unowned_value_overlay(self):
        # imported when the device matches NetBox, changed when it diverges.
        self.assertEqual(sm.on_reconcile(sm.IMPORTED, matches=True), sm.IMPORTED)
        self.assertEqual(sm.on_reconcile(sm.IMPORTED, matches=False), sm.CHANGED)
        self.assertEqual(sm.on_reconcile(sm.UNKNOWN, matches=True), sm.IMPORTED)
        self.assertEqual(sm.on_reconcile(sm.CHANGED, matches=True), sm.IMPORTED)

    def test_unowned_mirror_overlay_rests_at_imported(self):
        # No editable value (matches=None): a present, unowned row is 'imported',
        # never 'in_sync'. This is the correction that unifies the read overlays.
        self.assertEqual(sm.on_reconcile(sm.IMPORTED, matches=None), sm.IMPORTED)
        self.assertEqual(sm.on_reconcile(sm.UNKNOWN, matches=None), sm.IMPORTED)
        self.assertEqual(sm.on_reconcile(sm.CHANGED, matches=None), sm.IMPORTED)

    def test_unowned_conflict(self):
        self.assertEqual(sm.on_reconcile(sm.IMPORTED, matches=None, conflict=True), sm.CONFLICT)

    def test_owned_settles_and_repends_by_value(self):
        self.assertEqual(sm.on_reconcile(sm.ACCEPTED, matches=True), sm.IN_SYNC)
        self.assertEqual(sm.on_reconcile(sm.ACCEPTED, matches=False), sm.ACCEPTED)
        self.assertEqual(sm.on_reconcile(sm.IN_SYNC, matches=False), sm.ACCEPTED)
        self.assertEqual(sm.on_reconcile(sm.DEPLOYING, matches=True), sm.IN_SYNC)
        self.assertEqual(sm.on_reconcile(sm.DEPLOYING, matches=None), sm.IN_SYNC)
        self.assertEqual(sm.on_reconcile(sm.DEPLOYING, matches=False), sm.DEPLOYING)

    def test_owned_mirror_is_preserved(self):
        # Owned, no value to compare: accepted/in_sync stay put (deploying settles).
        self.assertEqual(sm.on_reconcile(sm.ACCEPTED, matches=None), sm.ACCEPTED)
        self.assertEqual(sm.on_reconcile(sm.IN_SYNC, matches=None), sm.IN_SYNC)

    def test_absent_is_drift_for_confirmed_and_unowned(self):
        self.assertEqual(sm.on_reconcile(sm.IMPORTED, present=False), sm.CHANGED)
        self.assertEqual(sm.on_reconcile(sm.IN_SYNC, present=False), sm.CHANGED)
        self.assertEqual(sm.on_reconcile(sm.CHANGED, present=False), sm.CHANGED)

    def test_absent_preserves_pending_intent(self):
        # accepted/deploying = operator intent not yet confirmed on device; the
        # device legitimately not reporting it is expected, so it is NOT drift.
        self.assertEqual(sm.on_reconcile(sm.ACCEPTED, present=False), sm.ACCEPTED)
        self.assertEqual(sm.on_reconcile(sm.DEPLOYING, present=False), sm.DEPLOYING)

    def test_on_reconcile_never_clobbers_owned_to_imported(self):
        # The whole point: no owned status can land on 'imported' via reconcile.
        for owned in sm.OWNED_STATES:
            for matches in (True, False, None):
                self.assertNotEqual(sm.on_reconcile(owned, matches=matches), sm.IMPORTED)

    def test_settles_owned_false_does_not_settle_by_materialization(self):
        # FK/content overlays: 'matches'=materialized-at-import, NOT device confirmation.
        # An owned row must NOT settle to in_sync via reconcile — only Apply (deploying)
        # may settle it. Unowned rows still rest at imported/changed by 'matches'.
        self.assertEqual(sm.on_reconcile(sm.ACCEPTED, matches=True, settles_owned=False), sm.ACCEPTED)
        self.assertEqual(sm.on_reconcile(sm.IN_SYNC, matches=True, settles_owned=False), sm.IN_SYNC)
        self.assertEqual(sm.on_reconcile(sm.DEPLOYING, matches=True, settles_owned=False), sm.IN_SYNC)
        self.assertEqual(sm.on_reconcile(sm.IMPORTED, matches=True, settles_owned=False), sm.IMPORTED)
        self.assertEqual(sm.on_reconcile(sm.IMPORTED, matches=False, settles_owned=False), sm.CHANGED)

    def test_is_owned(self):
        self.assertTrue(all(sm.is_owned(s) for s in sm.OWNED_STATES))
        self.assertFalse(sm.is_owned(sm.IMPORTED))
        self.assertFalse(sm.is_owned(sm.CHANGED))
        self.assertFalse(sm.is_owned(sm.CONFLICT))
        self.assertTrue(sm.is_owned(sm.APPLY_FAILED))  # owned row whose apply errored


class TestOnApplyResult(SimpleTestCase):
    """Step 4: the apply outcome settles a deploying row."""

    def test_apply_ok_settles_in_sync(self):
        self.assertEqual(sm.on_apply_result(sm.DEPLOYING, ok=True), sm.IN_SYNC)

    def test_apply_fail_marks_apply_failed(self):
        self.assertEqual(sm.on_apply_result(sm.DEPLOYING, ok=False), sm.APPLY_FAILED)

    def test_only_acts_on_deploying(self):
        # The apply outcome only concerns in-flight rows; others are untouched.
        for s in (sm.ACCEPTED, sm.IN_SYNC, sm.IMPORTED, sm.CHANGED):
            self.assertEqual(sm.on_apply_result(s, ok=False), s)

    def test_apply_failed_recovers_on_reconcile(self):
        # Not stuck: the device catching up → in_sync; still differing → re-pend accepted.
        self.assertEqual(sm.on_reconcile(sm.APPLY_FAILED, matches=True), sm.IN_SYNC)
        self.assertEqual(sm.on_reconcile(sm.APPLY_FAILED, matches=False), sm.ACCEPTED)
        # Absent from payload (pending retry) → preserved, not drifted.
        self.assertEqual(sm.on_reconcile(sm.APPLY_FAILED, present=False), sm.APPLY_FAILED)

    def test_apply_failed_retryable_via_accept(self):
        self.assertEqual(sm.advance(sm.APPLY_FAILED, sm.ACCEPT), sm.ACCEPTED)


class TestOnReconcileError(SimpleTestCase):
    """A reconcile that raised: unowned rows go to error, owned rows are preserved."""

    def test_unowned_states_move_to_error(self):
        for s in (sm.UNKNOWN, sm.IMPORTED, sm.CHANGED, sm.CONFLICT):
            self.assertEqual(sm.on_reconcile_error(s), sm.ERROR)

    def test_owned_rows_are_preserved(self):
        # A crash in the read path must never silently drop operator ownership.
        for s in (sm.ACCEPTED, sm.DEPLOYING, sm.IN_SYNC, sm.APPLY_FAILED):
            self.assertEqual(sm.on_reconcile_error(s), s)

    def test_errored_row_recovers_on_next_reconcile(self):
        # 'error' is transient: the next good read re-classifies the row by what it
        # observes — matches → imported, diverges → changed, diverges+native object
        # → conflict, absent → drift. Regression: the diverged+conflict path tried
        # error->conflict, which had no edge and raised IllegalTransition, aborting the
        # WHOLE reconcile and re-marking every row 'error' (a self-perpetuating wedge).
        self.assertEqual(sm.on_reconcile(sm.ERROR, matches=True, settles_owned=False), sm.IMPORTED)
        self.assertEqual(sm.on_reconcile(sm.ERROR, matches=False, settles_owned=False), sm.CHANGED)
        self.assertEqual(sm.on_reconcile(sm.ERROR, matches=False, conflict=True, settles_owned=False), sm.CONFLICT)
        self.assertEqual(sm.on_reconcile(sm.ERROR, present=False), sm.CHANGED)

    def test_error_is_idempotent(self):
        self.assertEqual(sm.on_reconcile_error(sm.ERROR), sm.ERROR)

    def test_error_recovers_on_next_good_reconcile(self):
        # The next successful read pulls an errored row back to imported/changed.
        self.assertEqual(sm.on_reconcile(sm.ERROR, matches=True), sm.IMPORTED)
        self.assertEqual(sm.on_reconcile(sm.ERROR, matches=False), sm.CHANGED)
        self.assertEqual(sm.on_reconcile(sm.ERROR, matches=None), sm.IMPORTED)
        # Vanished while errored → drift.
        self.assertEqual(sm.on_reconcile(sm.ERROR, present=False), sm.CHANGED)


class _FakeRow:
    """Minimal stand-in for a stale overlay row (no DB) to exercise finalise_stale_overlay."""

    def __init__(self, status):
        self.status = status
        self.last_sync_at = None
        self.deleted = False
        self.saved_fields = None

    def delete(self):
        self.deleted = True

    def save(self, update_fields=None):
        self.saved_fields = list(update_fields) if update_fields else None


class _ContendedRow(_FakeRow):
    """A persisted-shaped row whose status changes before each attempted row lock."""

    _meta = SimpleNamespace(label_lower="netbox_nso_plugin.teststate")
    pk = 37

    def __init__(self):
        super().__init__(sm.IMPORTED)
        self._statuses = iter((sm.ACCEPTED, sm.DEPLOYING, sm.IN_SYNC, sm.APPLY_FAILED))

    def refresh_from_db(self):
        self.status = next(self._statuses)


class TestFinaliseStaleOverlay(SimpleTestCase):
    """Shared stale-loop tail: prune vestigial unowned ghosts, mark genuine removals changed."""

    def test_vestigial_unowned_is_pruned(self):
        for s in (sm.UNKNOWN, sm.IMPORTED, sm.CHANGED, sm.CONFLICT, sm.ERROR):
            row = _FakeRow(s)
            sm.finalise_stale_overlay(row, vestigial=True)
            self.assertTrue(row.deleted)
            self.assertIsNone(row.saved_fields)

    def test_owned_is_never_pruned(self):
        # Owned rows are preserved even when 'vestigial' — present=False keeps accepted/deploying.
        for s in (sm.ACCEPTED, sm.DEPLOYING, sm.IN_SYNC, sm.APPLY_FAILED):
            row = _FakeRow(s)
            sm.finalise_stale_overlay(row, vestigial=True)
            self.assertFalse(row.deleted)

    def test_non_vestigial_unowned_marked_changed(self):
        row = _FakeRow(sm.IMPORTED)
        sm.finalise_stale_overlay(row, vestigial=False)
        self.assertFalse(row.deleted)
        self.assertEqual(row.status, sm.CHANGED)
        self.assertEqual(row.saved_fields, ["status"])

    def test_owned_confirmed_marked_changed_not_pruned(self):
        # in_sync was device-confirmed → its disappearance is real drift, kept as changed.
        row = _FakeRow(sm.IN_SYNC)
        sm.finalise_stale_overlay(row, vestigial=False)
        self.assertFalse(row.deleted)
        self.assertEqual(row.status, sm.CHANGED)

    def test_now_bumps_last_sync_at(self):
        row = _FakeRow(sm.IMPORTED)
        sm.finalise_stale_overlay(row, vestigial=False, now="T")
        self.assertEqual(row.last_sync_at, "T")
        self.assertEqual(row.saved_fields, ["status", "last_sync_at"])

    def test_no_change_no_save(self):
        # already changed + still absent → no transition → no save.
        row = _FakeRow(sm.CHANGED)
        sm.finalise_stale_overlay(row, vestigial=False)
        self.assertIsNone(row.saved_fields)

    def test_repeated_status_contention_is_left_for_the_next_reconcile(self):
        row = _ContendedRow()
        with (
            patch("netbox_nso_plugin.intent_state.footprint_for_instance", return_value=object()),
            patch("netbox_nso_plugin.intent_state.reconcile_transaction", side_effect=lambda _plan: nullcontext()),
            self.assertLogs("netbox_nso_plugin.status_machine", level="WARNING") as logs,
        ):
            sm.finalise_stale_overlay(row, vestigial=False)

        self.assertEqual(row.status, sm.APPLY_FAILED)
        self.assertFalse(row.deleted)
        self.assertIsNone(row.saved_fields)
        self.assertIn("next reconcile will retry", logs.output[0])
