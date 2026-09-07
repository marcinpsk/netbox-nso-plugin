# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Template acceptance survives concurrent BGP reconciliation."""

import threading
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import connection, connections
from django.test import RequestFactory, TransactionTestCase

from ._adapter_http import make_session
from ._outbox_case import CFG, make_managed, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestTemplateAcceptConcurrency(IntentPushResetMixin, _CascadeFlushMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerTemplateState

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=make_session(json_data={"id": 77})),
        ):
            self.device, self.management = make_managed("template-race", 77)
        self.user = get_user_model().objects.create_superuser(username="template-operator", password="test")
        self.payload = {
            "routers": [
                {
                    "asn": "65100",
                    "scopes": [
                        {
                            "vrf": "",
                            "address_families": ["ipv4-unicast"],
                            "peers": [],
                            "peer_groups": [{"name": "RR", "remote_as": "65100", "address_families": []}],
                        }
                    ],
                }
            ],
        }
        with without_commit_drain():
            _reconcile_bgp_config(self.device, self.payload)
        self.state = NSOBGPPeerTemplateState.objects.get(management=self.management)
        self.assertEqual(self.state.status, "imported")

    def _race_accept(self, payload):
        from netbox_nso_plugin import status_machine
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.views import NSOBGPPeerTemplateStateAcceptView

        read_done = threading.Event()
        release = threading.Event()
        accept_started = threading.Event()
        accept_done = threading.Event()
        errors = []
        accept_pids = []
        responses = []
        on_reconcile = status_machine.on_reconcile

        def pause_after_read(current, **kwargs):
            read_done.set()
            if not release.wait(30):
                raise AssertionError("the reconciler was not released")
            return on_reconcile(current, **kwargs)

        def reconcile():
            try:
                _reconcile_bgp_config(self.device, payload)
            except Exception as exc:  # noqa: BLE001 (re-raised on the test thread)
                errors.append(exc)
            finally:
                connections.close_all()
                read_done.set()

        def accept():
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    accept_pids.append(cursor.fetchone()[0])
                request = RequestFactory().post("/")
                request.user = self.user
                request.session = {}
                request._messages = FallbackStorage(request)
                accept_started.set()
                responses.append(NSOBGPPeerTemplateStateAcceptView.as_view()(request, pk=self.state.pk))
            except Exception as exc:  # noqa: BLE001 (re-raised on the test thread)
                errors.append(exc)
            finally:
                connections.close_all()
                accept_done.set()

        blocked = False
        reader = threading.Thread(target=reconcile)
        writer = threading.Thread(target=accept)
        with patch("netbox_nso_plugin.status_machine.on_reconcile", pause_after_read), without_commit_drain():
            reader.start()
            try:
                self.assertTrue(read_done.wait(15), "reconciliation did not read the template")
                if errors:
                    raise errors[0]
                writer.start()
                self.assertTrue(accept_started.wait(15), "Accept did not start")
                deadline = time.monotonic() + 10
                while not accept_done.is_set() and time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [accept_pids[0]])
                        blocked = cursor.fetchone()[0]
                    if blocked:
                        break
                    time.sleep(0.01)
            finally:
                release.set()
                reader.join(15)
                if writer.ident is not None:
                    writer.join(15)
        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(responses[0].status_code, 302)
        self.state.refresh_from_db()
        self.assertEqual(self.state.status, "in_sync")
        self.assertIsNotNone(self.state.accepted_at)
        self.assertTrue(blocked, "Accept did not wait for reconciliation to release the template")

    def test_accept_survives_reported_template_reconciliation(self):
        self._race_accept(self.payload)

    def test_accept_survives_stale_template_reconciliation(self):
        self._race_accept({"routers": []})
