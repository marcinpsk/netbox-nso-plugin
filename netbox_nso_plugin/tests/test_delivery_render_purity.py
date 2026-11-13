# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1627: every delivery renderer is a pure database read."""

import copy
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TransactionTestCase

from ._candidate_render_case import persist_candidates, render_candidates
from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


def _render_inputs(batch):
    """Copy each candidate's field values and planned relations, so an in-place change shows."""
    from netbox_nso_plugin.candidate_render import overlay_identity

    rows = tuple(
        (
            tuple(row.__dict__),
            tuple((field.attname, copy.deepcopy(getattr(row, field.attname))) for field in row._meta.concrete_fields),
        )
        for row in batch.overlays
    )
    relations = tuple(
        (overlay_identity(relation.instance), relation.field_name, tuple(row.pk for row in relation.related))
        for relation in batch.m2m
    )
    return rows, relations


class TestDeliveryRenderPurity(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("render-purity", 1627)
        from netbox_nso_plugin.models import NSOSnmpV3UserState

        with without_commit_drain(), transaction.atomic():
            acquire_overlay(
                NSOSnmpV3UserState,
                management=self.management,
                username="legacy-user",
                has_auth_secret=True,
                vault_ref="network/netbox/snmp/v3/legacy-user",
                status="accepted",
            )
        retained, relations = render_candidates(self.device, self.management, 2)
        persist_candidates(retained, relations)
        self.candidates, self.relations = render_candidates(self.device, self.management, 1)

    def test_every_registered_scope_renders_on_a_read_only_connection(self):
        from netbox_nso_plugin import delivery

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
            for scope in delivery.delivery_keys():
                delivery.render(scope, self.device.pk, self.management.adapter_device_id)

    def test_populated_candidates_and_planned_m2m_render_on_a_read_only_connection(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.candidate_render import CandidateBatch

        batch = CandidateBatch(self.candidates, self.relations)
        before = _render_inputs(batch)
        with patch("netbox_nso_plugin.adapter_client.requests.Session") as transport, transaction.atomic():
            transport.side_effect = AssertionError("rendering must not open an HTTP session")
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
            for scope in delivery.delivery_keys():
                rendered = delivery.render(scope, self.device.pk, self.management.adapter_device_id, candidates=batch)
                repeated = delivery.render(scope, self.device.pk, self.management.adapter_device_id, candidates=batch)
                self.assertEqual(rendered.payload, repeated.payload)
            transport.assert_not_called()
        self.assertTrue(all(row.pk is None for row in batch.overlays))
        self.assertEqual(_render_inputs(batch), before)
