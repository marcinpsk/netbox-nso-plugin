# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Report captured match references missing from old native policy graphs."""

import json

from django.core.management.base import BaseCommand

from netbox_nso_plugin.models import NSORoutePolicyState

_MATCH_REFERENCES = (
    ("match_prefix_lists", "match_prefix_list"),
    ("match_community_lists", "match_community_list"),
    ("match_as_paths", "match_aspath"),
)


class Command(BaseCommand):
    help = "List old captured match references with no native edge or unmapped marker. This report is read-only."

    def handle(self, *args, **options):
        records = []
        states = NSORoutePolicyState.objects.filter(family="route_map").select_related("management", "content_type")
        for state in states.order_by("management_id", "object_name", "pk"):
            native = state.assigned_object
            if native is None or native._meta.label_lower != "netbox_routing.routemap":
                continue
            entries = tuple(
                native.route_map_entries.order_by("sequence").prefetch_related(
                    "match_prefix_list", "match_community_list", "match_aspath"
                )
            )
            for position, captured in enumerate((state.captured or {}).get("entries") or []):
                if position >= len(entries):
                    continue
                entry = entries[position]
                markers = (entry.vendor_ext or {}).get("unmapped", {})
                missing = {}
                for capture_key, field in _MATCH_REFERENCES:
                    resolved = {obj.name for obj in getattr(entry, field).all()}
                    marked = set(markers.get(field) or [])
                    names = [name for name in captured.get(capture_key) or [] if name not in resolved | marked]
                    if names:
                        missing[field] = names
                if missing:
                    records.append(
                        {
                            "device_id": state.management.device_id,
                            "state_id": state.pk,
                            "object_name": state.object_name,
                            "native_name": native.name,
                            "status": state.status,
                            "is_materialized": state.is_materialized,
                            "entry_id": entry.pk,
                            "sequence": entry.sequence,
                            "missing": missing,
                        }
                    )
        self.stdout.write(json.dumps(records, sort_keys=True, indent=2))
