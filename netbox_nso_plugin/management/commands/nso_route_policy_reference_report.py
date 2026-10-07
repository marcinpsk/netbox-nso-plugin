# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Report captured match references missing from old native policy graphs."""

import json

from django.core.management.base import BaseCommand, CommandError

from netbox_nso_plugin.models import NSORoutePolicyState
from netbox_nso_plugin.renderer_audit import RendererAuditRepairFailed
from netbox_nso_plugin.signals import route_map_entry_unmapped

_MATCH_REFERENCES = (
    ("match_prefix_lists", "match_prefix_list"),
    ("match_community_lists", "match_community_list"),
    ("match_as_paths", "match_aspath"),
)


class Command(BaseCommand):
    help = "List old captured match references with no native edge or unmapped marker. This report is read-only."

    def handle(self, *args, **options):
        records = []
        # Only the materialized capture built the native entries (sequence = position + 1).
        states = NSORoutePolicyState.objects.filter(family="route_map", is_materialized=True).select_related(
            "management", "content_type"
        )
        for state in states.order_by("management_id", "object_name", "pk"):
            native = state.assigned_object
            if native is None or native._meta.label_lower != "netbox_routing.routemap":
                continue
            entries = {
                entry.sequence: entry
                for entry in native.route_map_entries.prefetch_related(
                    "match_prefix_list", "match_community_list", "match_aspath"
                )
            }
            for sequence, captured in enumerate((state.captured or {}).get("entries") or [], start=1):
                entry = entries.get(sequence)
                missing = self._missing(entry, native.name, captured)
                if missing or entry is None:
                    records.append(
                        {
                            "device_id": state.management.device_id,
                            "state_id": state.pk,
                            "object_name": state.object_name,
                            "native_name": native.name,
                            "status": state.status,
                            "is_materialized": state.is_materialized,
                            "entry_id": entry.pk if entry else None,
                            "sequence": sequence,
                            "unmatched": entry is None,
                            "missing": missing,
                        }
                    )
        self.stdout.write(json.dumps(records, sort_keys=True, indent=2))

    @staticmethod
    def _missing(entry, native_name, captured):
        """Return captured references with no native edge or unmapped marker (all of them without an entry)."""
        try:
            markers = route_map_entry_unmapped(entry, native_name) if entry else {}
        except RendererAuditRepairFailed as exc:
            raise CommandError(str(exc)) from None
        missing = {}
        for capture_key, field in _MATCH_REFERENCES:
            resolved = {obj.name for obj in getattr(entry, field).all()} if entry else set()
            marked = set(markers.get(field) or [])
            names = [name for name in captured.get(capture_key) or [] if name not in resolved | marked]
            if names:
                missing[field] = names
        return missing
