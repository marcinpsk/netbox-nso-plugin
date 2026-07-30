# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Cross-repository contract checks against a disposable live nso-adapter.

This module intentionally lives outside the normal test tree: the fast suite's
session guard must never permit network access. CI invokes this module alone
after starting an isolated adapter and PostgreSQL store.
"""

import os

import pytest
from django.conf import settings

pytestmark = pytest.mark.skipif(
    os.environ.get("NSO_LIVE_ADAPTER_TEST") != "1",
    reason="requires the dedicated disposable live-adapter test environment",
)


def test_live_adapter_read_and_auth_contract():
    """Exercise the real plugin HTTP client, adapter auth, ORM reads, and response schemas."""
    adapter_url = os.environ["NSO_LIVE_ADAPTER_URL"]
    adapter_token = os.environ["NSO_LIVE_ADAPTER_TOKEN"]
    plugin_config = {
        "netbox_nso_plugin": {
            "adapter_url": adapter_url,
            "adapter_token": adapter_token,
        }
    }
    if settings.configured:
        settings.PLUGINS_CONFIG = plugin_config
    else:
        settings.configure(PLUGINS_CONFIG=plugin_config)

    import netbox_nso_plugin.adapter_client as client

    client._cfg_cache.clear()
    client.reset_session()
    try:
        devices = client.list_devices()
        assert isinstance(devices, list)

        failover = client.get_failover_config()
        assert {
            "enabled",
            "deployment_enabled",
            "primary_probe_interval",
            "oob_probe_interval",
            "failure_threshold",
            "success_threshold",
            "probe_timeout",
            "active_probe_timeout",
            "probe_concurrency",
            "max_flips_per_tick",
            "sync_from_after_switch",
        } == set(failover)

        settings.PLUGINS_CONFIG["netbox_nso_plugin"]["adapter_token"] = "intentionally-wrong-test-token"
        client._cfg_cache.clear()
        with pytest.raises(client.AdapterError) as exc_info:
            client.list_devices()
        assert exc_info.value.code == "unauthorized"
    finally:
        client._cfg_cache.clear()
        client.reset_session()
