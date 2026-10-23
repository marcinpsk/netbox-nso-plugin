# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Test helpers for the native-write authority scopes."""

import json
from contextlib import contextmanager
from uuid import uuid4

import requests
from core.models import ObjectType
from dcim.models import Device
from django.contrib.auth import get_user_model
from django.test import TestCase
from users.models import ObjectPermission

from netbox_nso_plugin.models import NSODeviceManagement

from .mixins import IntentPushResetMixin


@contextmanager
def concurrent_writer():
    """Run a simulated concurrent writer outside the request's authority scope, as another request would."""
    from netbox_nso_plugin import native_authorization

    token = native_authorization._SCOPE.set(None)
    try:
        yield
    finally:
        native_authorization._SCOPE.reset(token)


User = get_user_model()


def grant(user, actions, model, constraints=None):
    permission = ObjectPermission.objects.create(
        name=f"{'-'.join(actions)}-{model._meta.model_name}-{uuid4().hex[:6]}",
        actions=list(actions),
        constraints=constraints,
    )
    permission.object_types.add(ObjectType.objects.get_for_model(model))
    permission.users.add(user)
    return permission


def operator(*native_grants):
    """A user who may change every management row; *native_grants* are (actions, model, constraints)."""
    user = User.objects.create_user(username=f"native-{uuid4().hex[:8]}")
    grant(user, ("view", "change"), NSODeviceManagement)
    grant(user, ("view",), Device)
    for actions, model, constraints in native_grants:
        grant(user, actions, model, constraints)
    return user


class FakeAdapter(requests.Session):
    """The adapter transport. GET documents come from ``documents``; every call is counted."""

    documents: dict = {}  # noqa: RUF012 (set per test)
    calls: list = []  # noqa: RUF012 (reset per test)

    def request(self, method, url, *args, **kwargs):
        type(self).calls.append((method, url))
        for suffix, body in type(self).documents.items():
            if method == "GET" and url.endswith(suffix):
                response = requests.Response()
                response.status_code = 200
                response._content = json.dumps(body).encode()
                return response
        raise requests.exceptions.ConnectionError("adapter transport faked in tests")


class AdapterCase(IntentPushResetMixin, TestCase):
    def setUp(self):
        super().setUp()
        from unittest.mock import patch

        FakeAdapter.documents = {}
        FakeAdapter.calls = []
        transport = patch("netbox_nso_plugin.adapter_client.requests.Session", FakeAdapter)
        transport.start()
        self.addCleanup(transport.stop)

    @contextmanager
    def assert_refused_whole(self):
        """The request answers 403, schedules no delivery and never reaches the adapter."""
        calls = len(FakeAdapter.calls)
        with self.captureOnCommitCallbacks() as callbacks:
            yield
        self.assertEqual(callbacks, [], "a refused request scheduled a delivery")
        self.assertEqual(len(FakeAdapter.calls), calls, "a refused request reached the adapter")
