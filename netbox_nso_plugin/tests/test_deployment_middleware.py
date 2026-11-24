# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The deployment gate middleware under NetBox's BASE_PATH."""

from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory, TransactionTestCase, override_settings

from .mixins import _CascadeFlushMixin


@override_settings(BASE_PATH="netbox/")
class TestTheDeploymentGateUnderABasePath(_CascadeFlushMixin, TransactionTestCase):
    def _post_while_quiesced(self, path):
        from netbox_nso_plugin.deployment import quiesce, resume
        from netbox_nso_plugin.middleware import IntentDeploymentMiddleware

        reached = []

        def view(request):
            reached.append((request.path_info, connection.in_atomic_block))
            return HttpResponse("ran")

        quiesce()
        try:
            response = IntentDeploymentMiddleware(view)(RequestFactory().post(path))
        finally:
            resume()
        return response, reached

    def test_a_plugin_mutation_under_a_base_path_takes_the_deployment_gate(self):
        response, reached = self._post_while_quiesced("/netbox/plugins/nso/devices/1/link-retry/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(reached, [])

    def test_the_sync_complete_callback_under_a_base_path_bypasses_the_gate(self):
        response, reached = self._post_while_quiesced("/netbox/api/plugins/nso/sync-complete/")

        self.assertEqual(response.status_code, 200)
        # The callback bypasses the middleware: no request transaction wraps it.
        self.assertEqual(reached, [("/netbox/api/plugins/nso/sync-complete/", False)])
