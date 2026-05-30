# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Pytest configuration: mock the netbox package so unit tests run without a full NetBox install."""

import sys
import types


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
