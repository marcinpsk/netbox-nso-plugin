# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Stub for netbox.models."""

import django.db.models as _m


class NetBoxModel(_m.Model):
    """Minimal stub for NetBoxModel."""

    class Meta:
        abstract = True
        app_label = "netbox_nso_plugin"
