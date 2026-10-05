# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Frozen renderer writes retain JSON shape through real database execution."""

import copy

from dcim.models import Interface
from django.test import TestCase

from netbox_nso_plugin.intent_state import IntentMutationProtocolError
from netbox_nso_plugin.lacp_topology import execute_frozen_operations
from netbox_nso_plugin.models import NSOInterfaceState
from netbox_nso_plugin.renderer_writer import (
    RendererMutationPlan,
    planned_save,
    planned_set_update,
    renderer_mirror_writes,
)

from ._outbox_case import make_managed, without_commit_drain
from .mixins import IntentPushResetMixin


class FrozenJSONWriteTests(IntentPushResetMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("frozen-json", 16271)
        self.interface = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")

    def test_nullable_json_survives_frozen_execution_without_a_default(self):
        state = NSOInterfaceState.objects.create(
            interface=self.interface, attribute="description", status="imported", last_apply_error={"code": "old"}
        )
        candidate = copy.copy(state)
        candidate.last_apply_error = None
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("last_apply_error",)),))
        with without_commit_drain(), renderer_mirror_writes(plan) as writer:
            execute_frozen_operations(writer, {state._meta.label_lower})
        state.refresh_from_db()
        self.assertIsNone(state.last_apply_error)

    def _nested_value(self):
        return {
            "metadata": {
                "empty_object": {},
                "empty_array": [],
                "pairs": [["name", "value"]],
                "items": [{"enabled": True, "number": 2, "value": None}],
            }
        }

    def _native_plan(self, instance):
        candidate = copy.copy(instance)
        candidate.custom_field_data = self._nested_value()
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("custom_field_data",)),))
        candidate.custom_field_data["metadata"]["items"].clear()
        return plan

    def test_lacp_execution_preserves_nested_json_and_freezes_mutable_inputs(self):
        from netbox_nso_plugin.lacp_reconciler import reconcile_lag_config

        with without_commit_drain(), renderer_mirror_writes(self._native_plan(self.interface)):
            reconcile_lag_config(self.device, {"bundles": []})
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.custom_field_data, self._nested_value())

    def test_subinterface_execution_preserves_nested_json(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        with without_commit_drain(), renderer_mirror_writes(self._native_plan(self.interface)):
            reconcile_subinterface(self.device, {"interfaces": []})
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.custom_field_data, self._nested_value())

    def test_redistribution_execution_preserves_nested_json(self):
        from netbox_routing.models import ISISInstance, Redistribution

        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        destination = ISISInstance.objects.create(device=self.device, process_tag="")
        native = Redistribution.objects.create(destination=destination, source_protocol="static")
        with without_commit_drain(), renderer_mirror_writes(self._native_plan(native)):
            reconcile_redistribution(self.device, {"entries": []})
        native.refresh_from_db()
        self.assertEqual(native.custom_field_data, self._nested_value())

    def test_exact_save_rejects_an_array_in_place_of_a_nested_object(self):
        candidate = copy.copy(self.interface)
        candidate.custom_field_data = {"metadata": {}}
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("custom_field_data",)),))
        candidate.custom_field_data = {"metadata": []}
        with without_commit_drain(), self.assertRaises(IntentMutationProtocolError):
            with renderer_mirror_writes(plan) as writer:
                writer.save(candidate, update_fields=("custom_field_data",))
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.custom_field_data, {})

    def test_json_set_update_preserves_nested_values(self):
        expected = self._nested_value()
        plan = RendererMutationPlan.build(
            set_updates=(
                planned_set_update(Interface.objects.filter(pk=self.interface.pk), custom_field_data=expected),
            )
        )
        with without_commit_drain(), renderer_mirror_writes(plan) as writer:
            writer.set_update(Interface, plan.write_set[0], custom_field_data=expected)
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.custom_field_data, expected)

    def test_exact_set_update_rejects_an_array_in_place_of_a_nested_object(self):
        plan = RendererMutationPlan.build(
            set_updates=(
                planned_set_update(Interface.objects.filter(pk=self.interface.pk), custom_field_data={"metadata": {}}),
            )
        )
        with without_commit_drain(), self.assertRaises(IntentMutationProtocolError):
            with renderer_mirror_writes(plan) as writer:
                writer.set_update(Interface, plan.write_set[0], custom_field_data={"metadata": []})
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.custom_field_data, {})

    def test_creation_lookup_preserves_json_natural_key_values(self):
        candidate = Interface(
            device=self.device,
            name="Ethernet2",
            type="1000base-t",
            custom_field_data=self._nested_value(),
        )
        candidate._site = self.device.site
        candidate._location = self.device.location
        candidate._rack = self.device.rack
        plan = RendererMutationPlan.build(
            saves=(planned_save(candidate, force_insert=True, natural_key=("device", "name", "custom_field_data")),)
        )
        with without_commit_drain(), renderer_mirror_writes(plan) as writer:
            writer.save(candidate, force_insert=True)
        stored = Interface.objects.get(device=self.device, name="Ethernet2")
        self.assertEqual(stored.custom_field_data, self._nested_value())
