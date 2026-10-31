# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Pytest configuration: mock the netbox package so unit tests run without a full NetBox install."""

import hashlib
import os
import sys
import types
from unittest.mock import patch

import pytest


def _make_netbox_stubs():
    """Inject minimal netbox stubs into sys.modules before any plugin code is imported."""
    if "netbox" in sys.modules:
        return

    netbox = types.ModuleType("netbox")
    plugins = types.ModuleType("netbox.plugins")

    class PluginConfig:
        name = ""
        verbose_name = ""
        description = ""
        version = "0.0.0"
        base_url = ""
        min_version = ""
        required_settings = []
        default_settings = {}
        author = ""
        author_email = ""

        def ready(self):
            pass

    plugins.PluginConfig = PluginConfig
    netbox.plugins = plugins

    # Stub out other netbox sub-packages referenced anywhere in the plugin.
    for mod in [
        "netbox.plugins",
        "netbox.views",
        "netbox.views.generic",
        "netbox.models",
        "netbox.filtersets",
        "netbox.tables",
        "netbox.forms",
        "netbox.search",
        "netbox.search.backends",
    ]:
        if mod not in sys.modules:
            stub = types.ModuleType(mod)
            sys.modules[mod] = stub

    sys.modules["netbox"] = netbox
    sys.modules["netbox.plugins"] = plugins

    # Also stub extras, dcim, utilities commonly imported by plugin models/views.
    for top in ["extras", "dcim", "utilities", "ipam", "tenancy"]:
        if top not in sys.modules:
            sys.modules[top] = types.ModuleType(top)
        for sub in ["models", "tables", "forms", "filtersets", "views"]:
            key = f"{top}.{sub}"
            if key not in sys.modules:
                sys.modules[key] = types.ModuleType(key)


_make_netbox_stubs()


def _isolated_test_db_name(base_name: str, worker_id: str | None) -> str:
    """Return a PostgreSQL-safe private database name for one pytest worker."""
    suffix = f"_{worker_id}" if worker_id else ""
    return f"{base_name[: 63 - len(suffix)]}{suffix}"


def _isolated_redis_namespace(base_name: str, worker_id: str | None, run_uid: str | None) -> str:
    """Return a Redis-safe namespace private to one pytest run and xdist worker."""
    identity = f"{base_name}:{run_uid or f'pid-{os.getpid()}'}:{worker_id or 'main'}"
    digest = hashlib.blake2s(identity.encode(), digest_size=8).hexdigest()
    return f"nso-test-{digest}"


_TEST_DB_BASE_NAME = os.environ.get("TEST_DB_NAME", "test_netbox_nso_plugin")
_TEST_REDIS_NAMESPACE = _isolated_redis_namespace(
    _TEST_DB_BASE_NAME,
    os.environ.get("PYTEST_XDIST_WORKER"),
    os.environ.get("PYTEST_XDIST_TESTRUNUID"),
)
os.environ["NETBOX_NSO_REDIS_KEY_NAMESPACE"] = _TEST_REDIS_NAMESPACE


@pytest.fixture(scope="session")
def django_db_modify_db_settings(django_db_modify_db_settings):
    """Give the plugin suite its OWN test database name.

    The dev Postgres is shared, and Django's test runner derives the test DB name
    from the configured DB (``test_netbox``). Other Django test suites on the same
    box (e.g. the netbox_routing fork's) default to the SAME name, so concurrent
    runs collide ("database already exists / being accessed by other users") and
    every test aborts at DB setup. Pin the plugin's test DB to a distinct name so
    the two never contend.
    """
    from django.conf import settings

    test_cfg = dict(settings.DATABASES["default"].get("TEST") or {})
    test_cfg["NAME"] = _isolated_test_db_name(_TEST_DB_BASE_NAME, os.environ.get("PYTEST_XDIST_WORKER"))
    settings.DATABASES["default"]["TEST"] = test_cfg


@pytest.fixture(autouse=True, scope="session")
def _block_real_adapter_network():
    """Keep the whole suite hermetic: no test ever reaches the live adapter.

    The devcontainer's PLUGINS_CONFIG points ``adapter_url`` at the real adapter
    (``http://nso-adapter:8000``) with a 30s client timeout, so any *unmocked*
    ``adapter_client`` call makes a real HTTP round-trip — and ``setUpTestData``
    creating an ``NSODeviceManagement`` via ``.create()`` fires the
    ``sync_scope_to_adapter`` signal, which onboards a *test* device into the live
    adapter's DB (pollution) and, if the adapter is slow/hung, blocks for the full
    timeout. CI avoids this with ``adapter.mock.invalid`` (a non-resolving TLD).

    This session-scoped patch replaces ``adapter_client``'s ``requests.Session``
    with one whose ``.request`` fails fast with a ``ConnectionError`` — the same
    ``AdapterError`` outcome a real unreachable adapter would produce (which the
    signal handlers already swallow), but instant. Session scope means it is active
    during ``setUpTestData`` too, which a function-scoped fixture cannot cover.

    Tests that exercise the client for real (``test_adapter_client_ext``) or that
    assert specific adapter behaviour all patch ``requests.Session`` (or higher)
    themselves; those patches nest on top of this one, so they are unaffected.
    """
    import requests

    class _BlockedSession(requests.Session):
        """A REAL requests.Session whose every HTTP call fails fast. get/post/put/... all
        delegate to request(), so overriding request() blocks the WHOLE surface — unlike a
        bare MagicMock, which would only block the one method stubbed and fabricate the rest
        (silently un-hermetic the moment the client used a different verb)."""

        def request(self, *args, **kwargs):
            raise requests.exceptions.ConnectionError("adapter network blocked in tests")

    with patch("netbox_nso_plugin.adapter_client.requests.Session", _BlockedSession):
        yield


@pytest.fixture(autouse=True)
def _reset_intent_push_state():
    """Clear the intent-push coalescing + change-detection caches between tests.

    Both are module-level state in signals.py; without a reset, a hash cached by one
    test would make a later identical push a no-op (skipped), and a coalesced push
    left pending by a rolled-back DB test could leak into the next.
    """
    try:
        from netbox_nso_plugin.signals import reset_intent_push_state

        reset_intent_push_state()
    except Exception:
        pass
    yield


_MANAGEMENT_ROW_LOCKED_WITHOUT_DEVICE_LOCK = """
SELECT EXISTS (
    SELECT 1 FROM netbox_nso_plugin_nsodevicemanagement
    WHERE device_id = %(device)s
      AND xmax IN (SELECT transactionid FROM pg_locks WHERE locktype = 'transactionid' AND pid = pg_backend_pid())
) AND NOT EXISTS (
    SELECT 1 FROM pg_locks
    WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted
      AND classid = %(namespace)s AND objid = %(device)s AND objsubid = 2
)
"""


@pytest.fixture(autouse=True)
def _device_lock_before_management_row():
    """Fail a test whose code asks for a device intent lock (L4) while it holds that device's management row (L5).

    Limits: it sees only exercised paths, only row locks this transaction took with FOR UPDATE/SHARE
    (xmax is one of its own transaction ids, so a plain UPDATE or a multixact lock is not seen), and
    only the management row of the device being locked.
    """
    from django.db import connection

    from netbox_nso_plugin import apply_state

    original = apply_state.lock_device_intent_transaction
    violations = []

    def guarded(device_id):
        if connection.in_atomic_block:
            with connection.cursor() as cursor:
                params = {"device": int(device_id), "namespace": apply_state._DEVICE_INTENT_LOCK_NAMESPACE}
                cursor.execute(_MANAGEMENT_ROW_LOCKED_WITHOUT_DEVICE_LOCK, params)
                if cursor.fetchone()[0]:
                    violations.append(f"device {device_id}: L4 requested while its management row is locked")
        try:
            return original(device_id)
        except apply_state.LockOrderViolation as exc:
            violations.append(f"device {device_id}: {exc}")
            raise

    with patch.object(apply_state, "lock_device_intent_transaction", guarded):
        yield
    assert not violations, f"intent lock order violated (L4 must precede L5): {violations}"
