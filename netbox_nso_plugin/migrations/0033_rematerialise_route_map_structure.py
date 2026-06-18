# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""M17 P1: backfill the structured route-map fields for already-imported objects.

Route-maps imported before P1 stored everything in the opaque match/set JSON blobs; the
reconciler only (re)fills entries when an object is new or empty, so existing materialised
route-maps would never gain the new structured fields on a normal poll. This one-shot
re-materialise replays each owner's stored ``captured`` blob through the reconciler fill
logic — which now lifts match_afi / set_communities / call_policy / vendor_ext and
RouteMap.default_action. Lossless and idempotent (full-replace per owner); anything not yet
first-class lands in vendor_ext exactly as a fresh import would.
"""

from django.db import migrations


def _rematerialise_route_maps(apps, schema_editor):
    try:
        from netbox_routing.models import RouteMap
    except ImportError:  # netbox_routing not installed → nothing to materialise
        return

    from netbox_nso_plugin import route_policy_reconciler as rpr
    from netbox_nso_plugin.signals import suppress_intent_push

    State = apps.get_model("netbox_nso_plugin", "NSORoutePolicyState")
    owners = State.objects.filter(family="route_map", is_materialized=True).iterator()

    # Suppress the operator-edit push signals: this is an import-time re-fill, not an edit.
    with suppress_intent_push():
        for state in owners:
            captured = state.captured or {}
            if not captured.get("entries"):
                continue
            rm = RouteMap.objects.filter(name__iexact=state.object_name).first()
            if rm is None:
                continue
            rpr._rm_fill(rm, captured)


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0032_shared_object_capture_materialized"),
        # The structured columns these fills write must already exist.
        ("netbox_routing", "0034_integration"),
    ]

    operations = [
        migrations.RunPython(_rematerialise_route_maps, migrations.RunPython.noop),
    ]
