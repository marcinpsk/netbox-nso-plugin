# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1), pin O1.19: the commit callback drains the outbox, and nothing else.

The in-memory coalescer is gone. ``_pending_pushes`` kept a scheduled push across a rollback
and dropped it on a failure, and ``_last_pushed_hashes`` authorized deleting routes a stale
worker never knew about (§8.3), so neither symbol survives the swap.

What replaces them is the outbox itself: the operator's transaction appends a row, the commit
callback drains the key through the claim protocol, and change detection becomes the durable
``last_success_identity`` on the state row. O1.2's send half and O1.6's O(1) tail are asserted
HERE against the production trigger, not against a test that calls the drain itself.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import requests
from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase

from netbox_nso_plugin.adapter_client import AdapterError

from ._outbox_case import ReceiptAdapter, entries, make_managed, own_vlan, state_of
from .mixins import IntentPushResetMixin, _CascadeFlushMixin, _deliver_scheduled_keys


class _Abort(Exception):
    """Roll the operator's transaction back the way a failing form save does."""


def _production_modules():
    """Every module a request runs, which is where an unclaimed send would hide."""
    from pathlib import Path

    plugin = Path(__file__).resolve().parent.parent
    # Relative to the plugin: an ancestor directory named tests/ or migrations/ would otherwise
    # yield nothing, and every guard below asserts an EMPTY set, so the scan must be non-empty.
    paths = [p for p in sorted(plugin.rglob("*.py")) if not {"tests", "migrations"} & set(p.relative_to(plugin).parts)]
    assert any(p.name == "signals.py" for p in paths), f"the scan reached {len(paths)} module(s), so it proves nothing"
    return paths


class TestEveryProductionSendGoesThroughTheOutbox(SimpleTestCase):
    """Codex O1 F5: the push builders are the delivery registry's to call, and nobody else's."""

    def test_only_the_delivery_registry_names_a_push_builder(self):
        """A direct call sends with no claim and no ``X-Push-Seq``; the compiler can see it."""
        import ast
        import re

        pattern = re.compile(r"_push_[a-z0-9_]+_intent_for_device")
        named = set()
        for path in _production_modules():
            if path.name == "delivery.py":
                continue  # the registry, which is the one place that may hold them
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                # A call, an attribute access and an import all name it, and all three count.
                if isinstance(node, ast.Name):
                    named_here = node.id
                elif isinstance(node, ast.Attribute):
                    named_here = node.attr
                elif isinstance(node, ast.alias):
                    named_here = node.name
                else:
                    continue
                if pattern.fullmatch(named_here):
                    named.add(f"{path.name}:{node.lineno}")
        assert named == set(), named

    def test_a_push_outside_a_render_refuses_rather_than_sending(self):
        """The fallback is gone with its callers: nothing may deliver intent around the claim."""
        from netbox_nso_plugin import signals

        with self.assertRaises(RuntimeError):
            signals._push_changed((1, "vlan"), [], lambda body: None)


class TestTheCoalescerSymbolsAreGone(SimpleTestCase):
    """O1.19: neither in-memory carrier may still be defined."""

    def test_neither_pending_pushes_nor_last_pushed_hashes_is_defined(self):
        from netbox_nso_plugin import signals

        for name in ("_pending_pushes", "_last_pushed_hashes"):
            with self.subTest(symbol=name):
                assert not hasattr(signals, name)

    def test_no_module_still_reads_them(self):
        import ast

        gone = {"_pending_pushes", "_last_pushed_hashes"}
        read = set()
        for path in _production_modules():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Name):
                    named_here = node.id
                elif isinstance(node, ast.Attribute):
                    named_here = node.attr
                elif isinstance(node, ast.alias):
                    named_here = node.name
                else:
                    continue
                if named_here in gone:
                    read.add(f"{path.name}:{node.lineno}")
        assert read == set(), read


class TestFixtureCommitDrainSuppression(SimpleTestCase):
    def test_overlapping_thread_contexts_restore_the_production_callback(self):
        import threading

        from netbox_nso_plugin import signals

        from ._outbox_case import without_commit_drain

        original = signals._drain_intent_pushes
        first_entered = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()
        release_second = threading.Event()
        first_exited = threading.Event()
        errors = []

        def first_context():
            try:
                with without_commit_drain():
                    first_entered.set()
                    if not release_first.wait(5):
                        raise AssertionError("the second suppression context did not enter")
                first_exited.set()
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)

        def second_context():
            try:
                if not first_entered.wait(5):
                    raise AssertionError("the first suppression context did not enter")
                with without_commit_drain():
                    second_entered.set()
                    if not release_second.wait(5):
                        raise AssertionError("the first suppression context did not exit")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)

        first = threading.Thread(target=first_context)
        second = threading.Thread(target=second_context)
        first.start()
        second.start()
        # Always release non-daemon workers because an earlier assertion failure would block pytest.
        try:
            self.assertTrue(second_entered.wait(5), "the suppression contexts did not overlap")
            release_first.set()
            self.assertTrue(first_exited.wait(5), "the first suppression context did not exit first")
            release_second.set()
            first.join(5)
            second.join(5)

            leaked = signals._drain_intent_pushes is not original
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            if errors:
                raise errors[0]
            self.assertFalse(leaked, "the later context restored the earlier thread's mock")
        finally:
            release_first.set()
            release_second.set()
            first.join(5)
            second.join(5)
            signals._drain_intent_pushes = original


class TestTheReceiptAdapterInjectionSeam(SimpleTestCase):
    """``fail_with`` may only carry what the adapter client can actually raise out of a send."""

    def test_a_builtin_injection_is_refused(self):
        adapter = ReceiptAdapter()
        adapter.fail_with = ConnectionError("builtin, not requests")

        with self.assertRaisesRegex(AssertionError, "requests.RequestException or an AdapterError"):
            adapter._handle("PUT", "http://adapter/devices/1/vlan")

    def test_a_transport_injection_is_raised_unchanged(self):
        adapter = ReceiptAdapter()
        adapter.fail_with = requests.exceptions.ConnectionError("adapter down")

        with self.assertRaises(requests.exceptions.ConnectionError):
            adapter._handle("PUT", "http://adapter/devices/1/vlan")

    def test_an_adapter_error_injection_is_raised_unchanged(self):
        adapter = ReceiptAdapter()
        adapter.fail_with = AdapterError("fence shut", code="conflict")

        with self.assertRaises(AdapterError):
            adapter._handle("PUT", "http://adapter/devices/1/vlan")


class TestTheTestCaseDeliveryDouble(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """The TestCase delivery double follows the production drain's empty-key behavior."""

    def setUp(self):
        super().setUp()
        self.device, self.mgmt = make_managed("double", 7608)

    def test_a_stale_key_with_no_entry_does_not_send(self):
        from netbox_nso_plugin import delivery, signals

        signals._pending_intent_keys().add((self.device.pk, "vlan"))
        with patch.object(delivery, "render") as render, patch.object(delivery, "send") as send:
            _deliver_scheduled_keys()

        render.assert_not_called()
        send.assert_not_called()

    def test_a_delivery_failure_is_logged_and_does_not_escape(self):
        from netbox_nso_plugin import delivery, outbox, signals

        with transaction.atomic():
            outbox.enqueue(self.device.pk, "vlan")
        signals._pending_intent_keys().add((self.device.pk, "vlan"))
        with (
            patch.object(delivery, "send", side_effect=RuntimeError("send failed")) as send,
            self.assertLogs("netbox_nso_plugin.tests.mixins", level="ERROR") as logs,
        ):
            _deliver_scheduled_keys()

        send.assert_called_once()
        assert any("test delivery failed" in line for line in logs.output)
        assert [entry.consumed_by_push_seq for entry in entries(self.device, "vlan")] == [None]

    def test_an_adapter_error_is_logged_and_leaves_the_row_unconsumed(self):
        from netbox_nso_plugin import delivery, outbox, signals

        with transaction.atomic():
            outbox.enqueue(self.device.pk, "vlan")
        signals._pending_intent_keys().add((self.device.pk, "vlan"))
        with (
            patch.object(delivery, "send", side_effect=AdapterError("adapter client failed")) as send,
            self.assertLogs("netbox_nso_plugin.tests.mixins", level="ERROR") as logs,
        ):
            _deliver_scheduled_keys()

        send.assert_called_once()
        assert any("test delivery failed" in line for line in logs.output)
        assert [entry.consumed_by_push_seq for entry in entries(self.device, "vlan")] == [None]

    def test_a_success_retires_rows_before_the_next_delivery(self):
        from netbox_nso_plugin import delivery, outbox, signals
        from netbox_nso_plugin.models import NSOIntentOutboxState

        marks = []
        key = (self.device.pk, "vlan")
        NSOIntentOutboxState.objects.create(device=self.device, scope="vlan")
        assert NSOIntentOutboxState.objects.filter(device=self.device, scope="vlan").exists()
        for delete_origin in (False, True):
            with transaction.atomic():
                outbox.enqueue(*key, delete_origin=delete_origin)
            signals._pending_intent_keys().add(key)
            with patch.object(delivery, "send", side_effect=lambda *args, mark, **kwargs: marks.append(mark)):
                _deliver_scheduled_keys()

        assert marks == [False, True]
        assert all(entry.consumed_by_push_seq is not None for entry in entries(self.device, "vlan"))
        assert not NSOIntentOutboxState.objects.filter(device=self.device, scope="vlan").exists()

    def test_a_deletion_already_held_by_a_claim_is_not_sent_again(self):
        from netbox_nso_plugin import delivery, outbox, signals
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentOutboxState

        deletion = outbox.delete_transition(7, last_acked=None, current=None)
        NSOIntentOutboxState.objects.create(
            device=self.device,
            scope="static_route",
            claim_deletions=[deletion],
        )
        NSOIntentOutboxEntry.objects.create(
            device=self.device,
            scope="static_route",
            batch_id=1,
            transitions=[deletion],
        )
        signals._pending_intent_keys().add((self.device.pk, "static_route"))
        rendered = delivery.Rendered((self.device.pk, "static_route"), {}, lambda _body: None)

        with (
            patch.object(delivery, "render", return_value=rendered),
            patch.object(delivery, "send") as send,
        ):
            _deliver_scheduled_keys()

        send.assert_called_once()
        assert send.call_args.kwargs["deletions"] == []


class _SwapCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """A managed device and a recorded far side, driven through the PRODUCTION trigger."""

    tag = "swap"
    adapter_device_id = 7600

    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        self.device, self.mgmt = make_managed(self.tag, self.adapter_device_id)

    def live(self):
        """The far side, patched in: an edit committed inside this block drains for real."""
        config, session = self.adapter.patches()
        return config, session

    def mine(self):
        return [r for r in self.adapter.requests if f"/devices/{self.adapter_device_id}/" in r["url"]]


class TestTheCommitCallbackDrainsTheOutbox(_SwapCase):
    """O1.2, O1.19: the operator's commit is what sends, through the claim protocol."""

    tag = "trig"
    adapter_device_id = 7601

    def test_a_committed_edit_sends_once_and_retires_its_row(self):
        from netbox_nso_plugin import drain

        state = own_vlan(self.mgmt, 880, self.tag)
        config, session = self.live()
        with config, session, transaction.atomic():
            state.vlan.name = "cl-trig-renamed"
            state.vlan.save()
            state.save()

        assert len(self.mine()) == 1, self.adapter.requests
        assert self.mine()[0]["push_seq"] is not None, "the production send carries the sequence"
        assert entries(self.device, "vlan") == []
        assert state_of(self.device, "vlan").last_success_identity != ""
        assert drain.gate_blockers(self.device.pk) == []

    def test_a_rolled_back_transaction_leaves_no_entry_and_the_next_edit_drains(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        state = own_vlan(self.mgmt, 881, self.tag)
        NSOIntentOutboxEntry.objects.all().delete()  # the fixture's own row, not the pin's
        config, session = self.live()

        with config, session:
            with contextlib.suppress(_Abort), transaction.atomic():
                state.vlan.name = "cl-trig-rolled-back"
                state.vlan.save()
                state.save()
                raise _Abort
            assert entries(self.device, "vlan", unconsumed=True) == []
            assert self.mine() == [], "a rolled-back transaction sends nothing"

            with transaction.atomic():
                state.vlan.name = "cl-trig-kept"
                state.vlan.save()
                state.save()

        assert len(self.mine()) == 1
        body = self.mine()[0]["body"]
        assert "cl-trig-kept" in str(body), body


class TestTheCommitTailIsConstant(_SwapCase):
    """O1.6: N saves of one key cost one claim, one render and one send."""

    tag = "tail"
    adapter_device_id = 7602

    def test_n_saves_register_n_callbacks_but_only_the_first_does_work(self):
        from netbox_nso_plugin import delivery, signals

        states = [own_vlan(self.mgmt, 890 + index, self.tag) for index in range(4)]
        config, session = self.live()

        with (
            patch("django.db.transaction.on_commit", wraps=transaction.on_commit) as registered,
            patch("netbox_nso_plugin.delivery.render", wraps=delivery.render) as render,
            config,
            session,
        ):
            with transaction.atomic():
                for state in states:
                    state.save()

        drains = [c for c in registered.call_args_list if getattr(c.args[0], "__name__", "") == "_drain_intent_pushes"]
        assert len(drains) == len(states), "registration is unconditional, so a rollback cannot lose the drain"
        assert render.call_count == 1, "callbacks 2..N find the cell empty and do nothing"
        assert len(self.mine()) == 1
        assert signals._pending_intent_keys() == set(), "the first callback clears the cell"


class TestALoneSaveDrainsOnItsOwnCommit(_SwapCase):
    """O1.2: one write with nothing to coalesce still sends once, on its own commit."""

    tag = "lone"
    adapter_device_id = 7603

    def test_a_save_in_its_own_transaction_sends_on_the_commit(self):
        state = own_vlan(self.mgmt, 895, self.tag)
        config, session = self.live()
        with config, session:
            assert self.mine() == [], "nothing goes out while the transaction is open"
            with transaction.atomic():
                state.vlan.name = "cl-lone-renamed"
                state.vlan.save()
                state.save()
                assert self.mine() == []

        assert len(self.mine()) == 1
        assert entries(self.device, "vlan") == []
