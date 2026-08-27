# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1627: every delivery renderer is a pure database read."""

from django.db import connection, transaction
from django.test import TransactionTestCase

from ._outbox_case import make_managed, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestDeliveryRenderPurity(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("render-purity", 1627)
        from netbox_nso_plugin.models import NSOSnmpV3UserState

        with without_commit_drain(), transaction.atomic():
            NSOSnmpV3UserState.objects.create(
                management=self.management,
                username="legacy-user",
                has_auth_secret=True,
                vault_ref="network/netbox/snmp/v3/legacy-user",
                status="accepted",
            )

    def test_every_registered_scope_renders_on_a_read_only_connection(self):
        from netbox_nso_plugin import delivery

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
            for scope in delivery.delivery_keys():
                delivery.render(scope, self.device.pk, self.management.adapter_device_id)
