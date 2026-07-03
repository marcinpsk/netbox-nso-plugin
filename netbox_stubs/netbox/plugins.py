# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Stub for netbox.plugins — used when running tests outside the devcontainer."""


class PluginConfig:
    """Minimal stub for PluginConfig."""

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
        """No-op ready hook."""
