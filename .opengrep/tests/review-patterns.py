# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Positive and negative examples for the custom review checks."""

from unittest.mock import patch
from threading import BrokenBarrierError
from ipam.models import VLANGroup
from netbox_nso_plugin.signals import suppress_intent_push, _schedule_intent_push
from netbox_nso_plugin import signals


def suppressed_push(device):
    with suppress_intent_push():
        # ruleid: nso-push-inside-suppression
        _schedule_intent_push(device, "ip")
    with suppress_intent_push(), patch("netbox_nso_plugin.signals.logger"):
        # ruleid: nso-push-inside-suppression
        _schedule_intent_push(device, "ip")
    with signals.suppress_intent_push():
        # ruleid: nso-push-inside-suppression
        signals._schedule_intent_push(device, "ip")
    with signals.suppress_intent_push(), patch("netbox_nso_plugin.signals.logger"):
        # ruleid: nso-push-inside-suppression
        _schedule_intent_push(device, "ip")
    # ok: nso-push-inside-suppression
    signals._schedule_intent_push(device, "ip")
    # ok: nso-push-inside-suppression
    _schedule_intent_push(device, "ip")


def tautological_assertions(self, value, read):
    # ruleid: nso-tautological-assertion
    self.assertEqual(value, value)
    # ruleid: nso-tautological-assertion
    assert value == value
    # ok: nso-tautological-assertion
    self.assertEqual(value, "accepted")
    # ok: nso-tautological-assertion
    assert value == "accepted"
    # ok: nso-tautological-assertion
    self.assertEqual(read(), read())


def patch_clock():
    # ruleid: nso-global-monotonic-patch
    patch("time.monotonic", return_value=0)
    # ruleid: nso-global-monotonic-patch
    patch("netbox_nso_plugin.renderer_audit.time.monotonic", return_value=0)
    # ok: nso-global-monotonic-patch
    patch("netbox_nso_plugin.renderer_audit._monotonic", return_value=0)


def swallowed_barrier(barrier):
    # ruleid: nso-swallowed-barrier-failure
    try:
        barrier.wait(timeout=2)
    except BrokenBarrierError:
        pass
    # ruleid: nso-swallowed-barrier-failure
    try:
        barrier.wait(timeout=2)
    except BrokenBarrierError:
        return


def visible_barrier_failure(barrier):
    # ok: nso-swallowed-barrier-failure
    try:
        barrier.wait(timeout=2)
    except BrokenBarrierError:
        raise AssertionError("The concurrent writer did not reach the barrier")


def vlan_group_identity():
    # ruleid: nso-vlan-group-mutable-lookup
    VLANGroup.objects.get_or_create(slug="nso-group", name="display name")
    # ok: nso-vlan-group-mutable-lookup
    VLANGroup.objects.get_or_create(slug="nso-group", defaults={"name": "display name"})


def write_set_cardinality(self, plan, expected):
    # ruleid: nso-write-set-cardinality-assertion
    self.assertEqual({write.model_label for write in plan.write_set}, expected)
    # ruleid: nso-write-set-cardinality-assertion
    self.assertEqual(
        {write.pk for write in plan.write_set if write.operation == "save"},
        expected,
    )
    # ok: nso-write-set-cardinality-assertion
    self.assertCountEqual([write.model_label for write in plan.write_set], expected)
    # ok: nso-write-set-cardinality-assertion
    self.assertTrue(expected <= {write.model_label for write in plan.write_set})
    # ok: nso-write-set-cardinality-assertion
    self.assertEqual({item.model_label for item in expected}, {"model"})


def text_file_encodings(path, data, archive):
    # ruleid: nso-implicit-text-encoding
    path.read_text()
    # ruleid: nso-implicit-text-encoding
    path.write_text(data)
    # ruleid: nso-implicit-text-encoding
    path.open()
    # ruleid: nso-implicit-text-encoding
    path.open("w")
    # ruleid: nso-implicit-text-encoding
    open(path)
    # ruleid: nso-implicit-text-encoding
    open(path, "w")
    # ok: nso-implicit-text-encoding
    path.read_text(encoding="utf-8")
    # ok: nso-implicit-text-encoding
    path.write_text(data, encoding="utf-8")
    # ok: nso-implicit-text-encoding
    path.open("w", encoding="utf-8")
    # ok: nso-implicit-text-encoding
    open(path, "w", encoding="utf-8")
    # ok: nso-implicit-text-encoding
    path.open("wb")
    # ok: nso-implicit-text-encoding
    path.open(mode="rb")
    # ok: nso-implicit-text-encoding
    open(path, "rb")
    # ok: nso-implicit-text-encoding
    open(path, mode="rb")
    # A module-level open takes the file first and the mode second.
    # ok: nso-implicit-text-encoding
    archive.open(path, "rb")


def silent_adapter_duplicates(payload, normalized, entries):
    seen = set()
    for entry in payload.get("entries") or []:
        # ruleid: nso-silent-duplicate-adapter-entry
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])

    seen = set()
    for entry in normalized:
        # ruleid: nso-silent-duplicate-adapter-entry
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])

    for entry in entries:
        # ruleid: nso-silent-duplicate-adapter-entry
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])


def visible_adapter_duplicates(payload, normalized, entries, states):
    seen = set()
    for entry in payload.get("entries") or []:
        # ok: nso-silent-duplicate-adapter-entry
        if entry["id"] in seen:
            raise ValueError("duplicate entry")
        seen.add(entry["id"])

    for entry in normalized:
        # ok: nso-silent-duplicate-adapter-entry
        if entry["id"] in seen:
            raise ValueError("duplicate entry")
        seen.add(entry["id"])

    for entry in entries:
        # ok: nso-silent-duplicate-adapter-entry
        if entry["id"] in seen:
            raise ValueError("duplicate entry")
        seen.add(entry["id"])

    for key, state in states.items():
        # ok: nso-silent-duplicate-adapter-entry
        if key in seen:
            continue
        state.mark_stale()


def wire_signals(handler, sender):
    # ruleid: nso-signal-connect-without-dispatch-uid
    pre_save.connect(handler, sender=sender)
    # ok: nso-signal-connect-without-dispatch-uid
    post_save.connect(handler, sender=sender, dispatch_uid="nso_plugin_example")
