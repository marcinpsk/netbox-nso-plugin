# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Root conftest: inject netbox stubs so tests run without a full NetBox install."""

import sys
import types


def _inject_netbox_stubs():
    """Inject minimal netbox stubs into sys.modules.

    Called from pytest_configure so the stubs are in place before pytest-django
    attempts to load ``netbox.settings``.
    """
    if "netbox" in sys.modules:
        return

    # --- netbox.settings stub (required by pytest-django) ---
    settings_mod = types.ModuleType("netbox.settings")
    settings_mod.INSTALLED_APPS = ["netbox_nso_plugin"]
    settings_mod.DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
    settings_mod.USE_TZ = True
    settings_mod.SECRET_KEY = "test-secret-key-not-real"
    settings_mod.PLUGINS_CONFIG = {
        "netbox_nso_plugin": {
            "adapter_url": "http://adapter",
            "adapter_token": "test-token",
        }
    }

    # --- netbox.plugins stub ---
    plugins_mod = types.ModuleType("netbox.plugins")

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

    plugins_mod.PluginConfig = PluginConfig

    # --- netbox top-level ---
    netbox_mod = types.ModuleType("netbox")
    netbox_mod.plugins = plugins_mod
    netbox_mod.settings = settings_mod

    sys.modules["netbox"] = netbox_mod
    sys.modules["netbox.plugins"] = plugins_mod
    sys.modules["netbox.settings"] = settings_mod

    # Stub additional netbox sub-packages that plugin modules may import at
    # module level (views, models, tables, forms, filtersets, search, …).
    _stubs = [
        "netbox.views",
        "netbox.views.generic",
        "netbox.models",
        "netbox.filtersets",
        "netbox.tables",
        "netbox.forms",
        "netbox.search",
        "netbox.search.backends",
        "extras",
        "extras.models",
        "extras.tables",
        "extras.forms",
        "extras.filtersets",
        "dcim",
        "dcim.models",
        "dcim.tables",
        "dcim.forms",
        "utilities",
        "utilities.forms",
        "utilities.tables",
        "utilities.querysets",
        "ipam",
        "tenancy",
    ]
    for mod_name in _stubs:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)


def pytest_configure(config):
    """Inject netbox stubs before pytest-django loads Django settings."""
    _inject_netbox_stubs()
