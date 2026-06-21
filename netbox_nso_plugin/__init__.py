# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.plugins import PluginConfig

__version__ = "0.1.0"


class NSOPluginConfig(PluginConfig):
    """NetBox plugin configuration for NSO integration."""

    name = "netbox_nso_plugin"
    verbose_name = "NSO Plugin"
    description = "Integrates NetBox with Cisco NSO via the nso-adapter REST API."
    version = __version__
    base_url = "nso"
    min_version = "4.6.0"
    required_settings = []
    default_settings = {
        "adapter_url": "",
        "adapter_token": "",
        "derived_intent": {
            "description_templates": [],  # list of {sentinel, template}; empty = feature off
        },
    }
    author = "Marcin Zieba"
    author_email = "marcinpsk@gmail.com"

    def ready(self):  # pragma: no cover
        """Connect signal handlers after all apps are loaded."""
        super().ready()
        from . import signals  # noqa: F401
        from .signals import _connect_g_activated

        _connect_g_activated()

        # Register the shared-object materialization specs (route-policy families) at startup.
        # They live in route_policy_reconciler (run via its module-level _register_specs()), but
        # that module was previously imported only lazily during a reconcile — so a web worker
        # rendering the versions page before any reconcile had an EMPTY registry, making
        # hash_captured() return "" and every device version falsely read as "matches". Importing
        # it here guarantees the specs exist in every process (web + worker).
        from django.conf import settings

        from . import route_policy_reconciler  # noqa: F401
        from .derived_intent import _register_description_from_cable, load_sentinel_templates

        raw = (
            settings.PLUGINS_CONFIG.get("netbox_nso_plugin", {})
            .get("derived_intent", {})
            .get("description_templates", [])
        )
        # Fail-fast: raises ConfigError on bad config — surfaces in NetBox boot log.
        self._derived_intent_templates = load_sentinel_templates(raw)
        if self._derived_intent_templates:
            _register_description_from_cable()

        cfg = settings.PLUGINS_CONFIG.get("netbox_nso_plugin", {})
        self._interface_ip_auto_create = cfg.get("interface_ip_auto_create", False)
        self._static_route_auto_create = cfg.get("static_route_auto_create", False)


config = NSOPluginConfig
