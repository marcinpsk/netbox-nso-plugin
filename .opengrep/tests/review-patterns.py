# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Positive and negative examples for the custom review checks."""

from unittest.mock import patch
from threading import BrokenBarrierError
from django.db.models import signals as model_signals
from django.db.models.signals import post_delete as post_delete_signal
from django.db.models.signals import post_save, pre_save
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


def wire_signals(handler, sender, custom_signal):
    # ruleid: nso-signal-connect-without-dispatch-uid
    pre_save.connect(handler, sender=sender)
    # ruleid: nso-signal-connect-without-dispatch-uid
    model_signals.post_save.connect(handler, sender=sender)
    # ruleid: nso-signal-connect-without-dispatch-uid
    post_delete_signal.connect(handler, sender=sender)
    # ok: nso-signal-connect-without-dispatch-uid
    post_save.connect(handler, sender=sender, dispatch_uid="nso_plugin_example")
    # ok: nso-signal-connect-without-dispatch-uid
    model_signals.post_save.connect(handler, sender=sender, dispatch_uid="nso_plugin_example")
    # ok: nso-signal-connect-without-dispatch-uid
    custom_signal.connect(handler, sender=sender)
