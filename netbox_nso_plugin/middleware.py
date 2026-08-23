# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""HTTP enforcement for an intent-protocol deployment quiescence window."""

import logging

from django.http import HttpResponse

from .deployment import DeploymentQuiesced, operation

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_PLUGIN_PATH_PREFIXES = ("/plugins/nso/", "/api/plugins/nso/")
logger = logging.getLogger(__name__)


class IntentDeploymentMiddleware:
    """Refuse mutations while the deployment command holds the fleet switch."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in _SAFE_METHODS:
            return self.get_response(request)
        try:
            if request.path_info.startswith(_PLUGIN_PATH_PREFIXES):
                with operation("HTTP mutations"):
                    return self.get_response(request)
            return self.get_response(request)
        except DeploymentQuiesced:
            logger.info("Refused a mutation during intent deployment: %s", request.path_info)
            return HttpResponse("Intent deployment is quiesced.", status=503, content_type="text/plain")
