# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Present stable native choices for explicit acquisition."""

from collections import defaultdict
from ipaddress import ip_interface

from django.apps import apps
from django.core import signing

from .ownership_planner import _NATIVE_BINDING_BUILDERS, ROUTE_POLICY_NATIVE_MODEL_LABELS, _native_bindings

SELECTION_SALT = "netbox_nso_plugin.intend_selection"


def intend_choices(management, user, *, identities=True):
    """List native identities independently of Differences filters and pagination."""
    from .models import NSOOwnershipManifest

    bindings = list(_native_bindings(management, set(_NATIVE_BINDING_BUILDERS)))
    for family, label in ROUTE_POLICY_NATIVE_MODEL_LABELS.items():
        bindings.extend(
            (
                "route_policy",
                native,
                "netbox_nso_plugin.nsoroutepolicystate",
                {"family": family, "object_name": native.name},
            )
            for native in apps.get_model(label).objects.restrict(user, "view").order_by("pk")
        )
    grouped = defaultdict(set)
    for _scope, native, _label, _key in bindings:
        grouped[type(native)].add(native.pk)
    visible = {
        model: set(model.objects.restrict(user, "view").filter(pk__in=pks).values_list("pk", flat=True))
        for model, pks in grouped.items()
    }
    # A superset of the acquisition's per-state-key ended rule, so the control is never missing when required.
    ended = set(
        NSOOwnershipManifest.objects.filter(device_id=management.device_id)
        .exclude(ownership_state="owned")
        .values_list("native_model_label", "native_id", "state_model_label")
    )
    choices = []
    for scope, native, label, state_key in bindings:
        if native.pk not in visible[type(native)]:
            continue
        selection = {
            "scope": scope,
            "native_model": native._meta.label_lower,
            "native_id": native.pk,
            "state_key": state_key,
        }
        token = signing.Signer(salt=SELECTION_SALT).sign_object(
            {"management": management.pk, "selection": selection}, compress=True
        )
        choices.append(
            {
                "token": token,
                "selection": selection,
                "scope": scope,
                # Redistribution delivers under its destination scope but compares as its own Differences scope.
                "difference_scope": "redistribution" if label.endswith(".nsoredistributionstate") else scope,
                "label": str(native),
                "native": native,
                "state_key": state_key,
                "bundle": label.endswith(".nsolacpbundlestate"),
                "restart": (native._meta.label_lower, native.pk, label) in ended,
            }
        )
    if identities:
        _difference_identities(management, choices)
    return choices


def _difference_identities(management, choices):
    from dcim.models import Interface

    from .difference_projection import native_entries
    from .template_content import _device_ned_id

    projections = {}
    for scope in {choice["difference_scope"] for choice in choices} - {"interface", "ip"}:
        projections[scope] = native_entries(scope, management, ned_id=_device_ned_id(management.device))
    interfaces = dict(Interface.objects.filter(device_id=management.device_id).values_list("pk", "name"))
    for choice in choices:
        native, scope = choice["native"], choice["difference_scope"]
        if scope == "interface":
            identities = [native.name]
        elif scope == "ip":
            address = ip_interface(str(native.address))
            identities = [
                (interfaces[native.assigned_object_id], str(address.ip), native.vrf.name if native.vrf else None)
            ]
        else:
            identities = [
                entry.identity
                for entry in projections[scope]
                if (root := _native_root(entry)) is not None and type(root) is type(native) and root.pk == native.pk
            ]
        choice["difference_identities"] = identities


def _native_root(entry):
    objects = entry.objects
    # A route-policy root projects as (overlay state, native root).
    if objects and objects[0] is not None and objects[0]._meta.app_label == "netbox_nso_plugin":
        objects = objects[1:]
    return objects[0] if objects else None


def difference_choices(row, choices):
    """Match projected Differences to their explicit native choices."""
    if row.kind not in {"netbox_only", "mismatch"}:
        return []
    identity = row.identity
    if row.scope == "ip" and isinstance(identity, tuple):
        # A prefix mismatch row names the device prefix, so match interface, host and VRF (global as None).
        identity = (identity[0], identity[1], identity[3] or None)
    matched = []
    for choice in choices:
        if row.scope != choice["difference_scope"]:
            continue
        if identity in choice["difference_identities"] and (
            row.scope != "interface" or not row.attribute or choice["state_key"].get("attribute") == row.attribute
        ):
            matched.append(choice)
    return matched


def acquisition_outcomes(management):
    """Report storage only from ordinary acknowledgements for the current owners."""
    from django.db.models import Q

    from . import delivery
    from .adapter_client import AdapterError, public_error_message
    from .durable_capture import mapping_epoch
    from .models import (
        NSOApplyOverlaySnapshot,
        NSOIntentOutboxState,
        NSOIntentRevision,
        NSOOwnershipManifest,
    )
    from .ownership_planner import _state_filters_without_native, converted_scope_rules
    from .refusal_registry import OVERLAYS
    from .selection_manifest import current_content_digest, owner_relations
    from .status_machine import OWNED_STATES

    if management is None:
        return []
    owners = defaultdict(dict)
    rules = converted_scope_rules()
    groups = defaultdict(list)
    for manifest in NSOOwnershipManifest.objects.filter(
        device_id=management.device_id, grant_kind="intend", ownership_state="owned"
    ):
        groups[(manifest.state_model_label, manifest.scope)].append(manifest)
    for (label, scope), manifests in sorted(groups.items()):
        spec = OVERLAYS[label]
        rule = rules["redistribution" if label.endswith(".nsoredistributionstate") else scope]
        query = Q(pk__in=[])
        for manifest in manifests:
            model, filters = _state_filters_without_native(manifest, rule, management)
            query |= Q(**filters)
        for row in model.objects.select_related(*owner_relations(spec)).filter(
            query, status__in=OWNED_STATES, **{spec.device_lookup: management.device_id}
        ):
            owners[scope][(label, row.pk)] = row
    epoch = mapping_epoch(management)
    outcomes = []
    for key, by_owner in sorted(owners.items()):
        rows = by_owner.values()
        revision = NSOIntentRevision.objects.filter(device_id=management.device_id, scope=key).first()
        state = NSOIntentOutboxState.objects.filter(device_id=management.device_id, scope=key).first()
        snapshots = NSOApplyOverlaySnapshot.objects.filter(
            attempt__trigger_kind="push",
            selection__management=management,
            selection__mapping_epoch=epoch,
            selection__stream=delivery.delivery_keys()[key].section,
            selection__publication_state="sent",
            selection__acknowledged_at__isnull=False,
        ).exclude(selection__binding_state="invalidated")
        stored = revision is not None and revision.verified_revision == revision.revision
        if stored:
            acknowledged = set(
                snapshots.filter(selection__plugin_revision=revision.revision).values_list(
                    "model_label", "overlay_pk", "accepted_at", "content_digest"
                )
            )
            stored = all(
                row.accepted_at is not None
                and (row._meta.label_lower, row.pk, row.accepted_at, current_content_digest(row)) in acknowledged
                for row in rows
            )
        error = (management.intent_push_errors or {}).get(key)
        if (
            state
            and state.last_error_at
            and (state.last_success_at is None or state.last_error_at > state.last_success_at)
        ):
            error = state.last_error_code
        if error:
            code = error.get("code", "") if isinstance(error, dict) else error
            text, css = (
                f"acquired; ordinary delivery failed: {public_error_message(AdapterError('', code=code))}",
                "text-bg-danger",
            )
        elif stored:
            text, css = "acquired; stored", "text-bg-success"
        else:
            text, css = "acquired; delivery pending", "text-bg-warning text-dark"
        outcomes.append({"scope": key, "text": text, "css": css})
    return outcomes
