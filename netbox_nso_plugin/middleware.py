# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""HTTP enforcement for an intent-protocol deployment quiescence window."""

from django.http import HttpResponse, JsonResponse

from .deployment import DeploymentQuiesced, operation

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_PLUGIN_PATH_PREFIXES = ("/plugins/nso/", "/api/plugins/nso/")
_REFUSAL = "Intent deployment gate is active. Nothing was changed."


class IntentDeploymentMiddleware:
    """Refuse mutations while the deployment command holds the fleet switch."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in _SAFE_METHODS or not request.path_info.startswith(_PLUGIN_PATH_PREFIXES):
            return self.get_response(request)
        try:
            with operation("HTTP mutations"):
                return self.get_response(request)
        except DeploymentQuiesced:
            # The gate answers before the view, so this IS the Apply response the tab's
            # AJAX caller parses — it reads JSON, and a text/plain body reaches the
            # operator as a generic parse failure instead of the deliberate refusal.
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"status": "error", "message": _REFUSAL}, status=503)
            return HttpResponse(_REFUSAL, status=503, content_type="text/plain")
