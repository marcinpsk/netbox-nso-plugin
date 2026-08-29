# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Real HTTP test doubles for adapter_client tests.

The adapter transport (``requests.Session``) is a genuine external network
boundary, so it is the one place a mock is warranted — but it is bound with
``spec=requests.Session`` and used only to record the outgoing request and hand
back a canned response (see :func:`make_session`).

The *response*, by contrast, is a real :class:`requests.Response`. Production
reads ``resp.ok`` / ``resp.json()`` / ``resp.text`` / ``resp.content`` /
``resp.status_code``, all of which a real ``Response`` derives from the status
code and body. Hand-rolling those on a ``MagicMock`` duplicates production logic
(``response.ok = status < 400``) and lets a test assert impossible states — e.g.
empty ``.content`` together with a non-empty ``.json()``, or a 503 body whose
``.json()`` is canned to succeed. A real ``Response`` forbids those: a non-JSON
body makes ``.json()`` raise a real ``JSONDecodeError``, exactly as in prod.
"""

import json as _json
from unittest.mock import MagicMock, patch

import requests

# Bound at import time, BEFORE any test patches ``requests.Session``. The adapter
# tests do ``@patch("netbox_nso_plugin.adapter_client.requests.Session")`` which
# swaps ``Session`` on the shared ``requests`` module, so spec'ing against a live
# ``requests.Session`` lookup during a test would try to spec the patch mock
# itself (InvalidSpecError). Capturing the real class here keeps the spec real.
_REAL_SESSION = requests.Session


def make_response(status_code=200, json_data=None, content=None):
    """Build a real ``requests.Response`` with a real body.

    ``content`` (raw bytes) wins if given; otherwise ``json_data`` is serialized;
    otherwise the body is empty. ``.ok`` / ``.json()`` / ``.text`` are computed by
    the real ``Response``, not stubbed.
    """
    resp = requests.Response()
    resp.status_code = status_code
    resp.encoding = "utf-8"
    if content is not None:
        resp._content = content
    elif json_data is not None:
        resp._content = _json.dumps(json_data).encode()
    else:
        resp._content = b""
    return resp


def make_session(status_code=200, json_data=None, content=None, response=None):
    """A ``spec=requests.Session`` stand-in returning a real canned response.

    ``spec`` bounds the mock to the real client interface (an attribute typo or an
    API drift surfaces as ``AttributeError``); only the network ``send`` is
    stubbed. ``session.request`` still records ``call_args`` so callers can assert
    on the outgoing request, and a caller may override ``session.request.side_effect``
    to simulate a transport exception.
    """
    session = MagicMock(spec=_REAL_SESSION)
    if response is None:
        response = make_response(status_code, json_data, content)
    session.request.return_value = response
    return session


def patch_matching_control_state(test_case):
    """Patch the adapter boundary with an empty control state that matches default management."""
    patchers = (
        patch("netbox_nso_plugin.adapter_client.get_device", return_value={"failover": None}),
        patch(
            "netbox_nso_plugin.adapter_client.get_scope",
            return_value={"attributes": [], "auto_apply": False, "sync_before_apply": True},
        ),
        patch("netbox_nso_plugin.adapter_client.set_scope", return_value={}),
    )
    for patcher in patchers:
        patcher.start()
        test_case.addCleanup(patcher.stop)
