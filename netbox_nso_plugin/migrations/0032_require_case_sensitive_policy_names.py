# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Require the netbox-routing schema with case-sensitive policy names."""

from django.db import migrations


class Migration(migrations.Migration):
    # Exact route-policy identity needs case-distinct RouteMap, PrefixList and ASPath names.
    dependencies = [
        ("netbox_nso_plugin", "0031_lacp_native_topology"),
        ("netbox_routing", "0040_case_sensitive_policy_names"),
    ]

    operations = []
