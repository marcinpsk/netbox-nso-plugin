# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Preview and confirm explicit native intent acquisition."""

from contextlib import contextmanager
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class IntendResult:
    """Return the acquisition outcome and signed confirmation evidence."""

    status: str
    message: str
    confirmation: str | None = None
    preflight: dict | None = None
    keys: tuple[str, ...] = ()


class _Refused(Exception):
    def __init__(self, message, *, ordinary_delivery=False):
        super().__init__(message)
        self.ordinary_delivery = ordinary_delivery


def _selection(selection):
    from copy import deepcopy

    from .ownership_planner import _NATIVE_BINDING_BUILDERS

    if not isinstance(selection, (tuple, list)) or not selection:
        raise _Refused("Select at least one native row.")
    result = []
    for item in selection:
        if not isinstance(item, dict) or set(item) - {"scope", "native_model", "native_id", "state_key", "options"}:
            raise _Refused("Invalid native selection.")
        scope = item.get("scope")
        if scope not in {*_NATIVE_BINDING_BUILDERS, "redistribution", "route_policy"}:
            raise _Refused(f"Intend does not support scope {scope!r}.")
        if (
            not isinstance(item.get("native_model"), str)
            or type(item.get("native_id")) is not int
            or item["native_id"] <= 0
        ):
            raise _Refused("Select a typed native identity.")
        options = item.get("options", {})
        state_key = item.get("state_key", {})
        if not isinstance(options, dict) or set(options) - {"lag_id", "restart"} or not isinstance(state_key, dict):
            raise _Refused("Invalid acquisition options.")
        if "restart" in options and type(options["restart"]) is not bool:
            raise _Refused("Restart must be explicit.")
        if "lag_id" in options and (type(options["lag_id"]) is not int or options["lag_id"] <= 0):
            raise _Refused("LACP requires a positive explicit lag_id.")
        result.append(deepcopy({**item, "state_key": state_key, "options": options}))
    return result


def _permission(model, user, action, row=None):
    from .native_authorization import _permission as permission_name
    from .native_authorization import _restricted

    if not user.is_authenticated or not user.has_perm(permission_name(model, action)):
        raise _Refused(
            f"Permission denied: {model._meta.label_lower} {action}. Check your permissions and preview again."
        )
    if row is not None and not _restricted(model, user, action).filter(pk=row.pk).exists():
        raise _Refused(f"Row is not visible: {model._meta.label_lower} {row.pk}.")


def _values(row):
    return {field.attname: getattr(row, field.attname) for field in row._meta.concrete_fields}


def _digest_values(row):
    """Leave out management delivery and settlement records: they change without an acquisition input."""
    values = _values(row)
    if row._meta.label_lower == "netbox_nso_plugin.nsodevicemanagement":
        for name in row._STALE_SAVE_PROTECTED_FIELDS:
            del values[name]
    return values


def _existing(model, pk):
    from django.core.exceptions import ObjectDoesNotExist

    try:
        return model.objects.get(pk=pk)
    except ObjectDoesNotExist as exc:
        raise _Refused(f"{model._meta.label_lower} row {pk} no longer exists. Preview again.") from exc


def _binding(management, item):
    from django.apps import apps

    from .ownership_planner import ROUTE_POLICY_NATIVE_MODEL_LABELS, _native_bindings

    scope = item["scope"]
    if scope == "redistribution" and item["native_model"] != "netbox_routing.redistribution":
        raise _Refused("Redistribution selection requires a redistribution row.")
    if scope == "route_policy":
        families = {label: family for family, label in ROUTE_POLICY_NATIVE_MODEL_LABELS.items()}
        family = families.get(item["native_model"])
        if family is None:
            raise _Refused("Unsupported policy root.")
        native = apps.get_model(item["native_model"]).objects.filter(pk=item["native_id"]).first()
        if native is None:
            raise _Refused("The selected policy no longer exists.")
        return scope, native, "netbox_nso_plugin.nsoroutepolicystate", {"family": family, "object_name": native.name}
    requested = {"bgp", "isis", "ospf"} if scope == "redistribution" else {scope}
    matches = [
        binding
        for binding in _native_bindings(management, requested)
        if binding[1]._meta.label_lower == item["native_model"]
        and binding[1].pk == item["native_id"]
        and all(binding[3].get(key) == value for key, value in item["state_key"].items())
    ]
    if len(matches) != 1:
        raise _Refused(f"Native binding is missing or ambiguous: {item['native_model']} {item['native_id']}.")
    return matches[0]


def _candidate(management, user, item, timestamp, correlations):
    import copy
    from types import SimpleNamespace

    from django.core.exceptions import ValidationError

    from . import status_machine as sm
    from .models import NSOOwnershipManifest
    from .ownership_planner import _state_filters, converted_scope_rules

    scope, native, label, state_key = _binding(management, item)
    rule = converted_scope_rules()["redistribution" if label.endswith(".nsoredistributionstate") else scope]
    manifest_stub = SimpleNamespace(state_model_label=label, state_key=state_key, acknowledged_lineage=[])
    model, filters = _state_filters(manifest_stub, rule, native, management)
    draft = model(**filters)
    lookup = {name: getattr(draft, model._meta.get_field(name).attname) for name in model._meta.unique_together[0]}
    current = model.objects.filter(**lookup).first()
    if label.endswith(".nsoredistributionstate"):
        observations = list(model.objects.filter(management=management, redistribution=native))
        if len(observations) > 1:
            raise _Refused("Redistribution has ambiguous observation bindings.")
        if observations:
            current = observations[0]
            state_key = {**state_key, "source_protocol": current.source_protocol, "source_ref": current.source_ref}

    _permission(type(native), user, "view", native)
    _permission(model, user, "change" if current else "add", current)
    episodes = list(
        NSOOwnershipManifest.objects.filter(
            device_id=management.device_id,
            scope=scope,
            native_model_label=native._meta.label_lower,
            native_id=native.pk,
            state_model_label=label,
            state_key=state_key,
        )
    )
    ended = [] if any(episode.ownership_state == "owned" for episode in episodes) else episodes
    restart = item["options"].get("restart", False)
    if ended and not restart:
        raise _Refused(f"Explicit restart is required for {label} {native.pk}.")
    if current is not None and sm.is_owned(current.status) and not (ended and restart):
        raise _Refused(f"{label} row {current.pk} is already owned. Preview again after an ordinary edit.")
    candidate = copy.copy(current) if current is not None else draft
    _project_candidate(candidate, current, native, dict(rule.overlay_native_fields)[label], manifest_stub)
    candidate.status = sm.advance(
        current.status if current is not None and not sm.is_owned(current.status) else sm.UNKNOWN, sm.ACQUIRE
    )
    candidate.accepted_at = timestamp
    if hasattr(candidate, "apply_attempt_id"):
        candidate.apply_attempt_id = None
    if model._meta.model_name == "nsolacpbundlestate":
        if "lag_id" in item["options"]:
            candidate.lag_id = item["options"]["lag_id"]
        if candidate.lag_id is None or candidate.vpc_sensitive:
            raise _Refused(f"LACP bundle {native.name} requires an explicit lag_id and must not be vPC-protected.")
    elif "lag_id" in item["options"]:
        raise _Refused("lag_id is only valid for an LACP bundle.")
    if model._meta.model_name == "nsoredistributionstate" and current is not None:
        candidate.source_protocol = current.source_protocol
    if model._meta.model_name == "nsostaticroutestate":
        _reserve_route(management, native, current, candidate, episodes, timestamp, correlations)
    try:
        candidate.full_clean(validate_unique=False, validate_constraints=False)
    except ValidationError as exc:
        raise _Refused(str(exc)) from exc
    return scope, native, current, candidate


def _project_candidate(candidate, current, native, native_field, manifest_stub):
    from .ownership_planner import project_native_state

    if not native_field.startswith("__") and native_field != "assigned_object":
        existing_anchor = getattr(candidate, f"{native_field}_id")
        if existing_anchor is not None and existing_anchor != native.pk:
            raise _Refused("The observation is linked to a different native row.")
        setattr(candidate, native_field, native)
    elif native_field == "assigned_object":
        existing_anchor = candidate.assigned_object
        if existing_anchor is not None and (
            type(existing_anchor) is not type(native) or existing_anchor.pk != native.pk
        ):
            raise _Refused("The observation is linked to a different native policy.")
        candidate.assigned_object = native
    observed = {
        name: getattr(candidate, name)
        for name in ("nso_value", "device_name", "nso_vrf", "nso_prefix", "nso_next_hop")
        if hasattr(candidate, name)
    }
    project_native_state(candidate, native, native_field, manifest_stub)
    for name, value in observed.items():
        setattr(candidate, name, value)
    if current is not None and candidate._meta.model_name == "nsoospfinstancestate":
        candidate.enabled = current.enabled


def _reserve_route(management, native, current, candidate, episodes, timestamp, correlations):
    from .intent_generation import allocate_intent_generation

    correlation_key = str(native.pk)
    if correlation_key not in correlations:
        correlations[correlation_key] = allocate_intent_generation()
    candidate.intent_generation = correlations[correlation_key]
    candidate.generation_started_at = timestamp
    candidate.last_apply_error = ""
    candidate.last_result_advisory = ""
    candidate.expected_generation = None
    candidate.expected_fingerprint = ""
    lineage = [episode.acknowledged_lineage[-1] for episode in episodes if episode.acknowledged_lineage]
    from .models import NSOIntentOutboxState
    from .outbox import carried_triple

    state = NSOIntentOutboxState.objects.filter(device_id=management.device_id, scope="static_route").first()
    carried = carried_triple(native.pk, lineage_carry=state.lineage_carry) if state else None
    candidate.last_acked_triple = (current.last_acked_triple if current else None) or (
        lineage[-1] if lineage else carried
    )
    _route_pending(management, native.pk)


def _route_pending(management, route_id):
    from .models import NSOIntentOutboxEntry, NSOIntentOutboxState

    entries = NSOIntentOutboxEntry.objects.filter(device_id=management.device_id, scope="static_route")
    for transitions in entries.values_list("transitions", flat=True):
        if any(transition.get("route_id") == route_id for transition in transitions):
            raise _Refused(
                f"Static route {route_id} has pending deletion or revocation authority.", ordinary_delivery=True
            )
    state = NSOIntentOutboxState.objects.filter(device_id=management.device_id, scope="static_route").first()
    if state is not None and (
        route_id in state.revoked_ids
        or any(record.get("route_id") == route_id for record in [*state.queued_deletions, *state.claim_deletions])
    ):
        raise _Refused(f"Static route {route_id} has pending deletion or revocation authority.", ordinary_delivery=True)


def _retained(management, keys, replaced):
    from django.apps import apps

    from .candidate_render import overlay_identity
    from .refusal_registry import OVERLAYS
    from .status_machine import OWNED_STATES

    rows = []
    for label, spec in OVERLAYS.items():
        if not set(keys).intersection(spec.delivery_keys):
            continue
        query = apps.get_model(label).objects.filter(
            **{spec.device_lookup: management.device_id}, status__in=OWNED_STATES
        )
        if label.endswith(".nsoredistributionstate"):
            query = query.filter(dest_protocol__in=keys)
        rows.extend(query.order_by("pk"))
    for row in rows:
        if overlay_identity(row) not in replaced and row.accepted_at is None:
            raise _Refused(f"Owned {row._meta.label_lower} row {row.pk} ({row}) has no acceptance timestamp.")
    return rows


def _render(management, keys, batch):
    import json

    from . import delivery, switching_preparation
    from .models import NSOIntentRevision

    payloads, bodies = {}, {}
    for key in keys:
        rendered = delivery.render(key, management.device_id, management.adapter_device_id, candidates=batch)
        payloads[key] = rendered.payload
        revision = NSOIntentRevision.objects.filter(device_id=management.device_id, scope=key).first()
        deleted_roots = ()
        if key in delivery.direct_keys():
            _events, versions = switching_preparation.root_deletions(management, key, rendered.payload)
            deleted_roots = tuple(sorted(versions))
        source_revision = (revision.revision if revision else 0) + (batch is not None)
        with switching_preparation._bound(deleted_roots, source_revision):
            bodies[delivery.delivery_keys()[key].section] = json.loads(delivery.wire_body(rendered, rendered.payload))
    return payloads, bodies


def _policy_references(native):
    from .signals import _route_map_contributors

    if native._meta.model_name == "routemap":
        yield from (obj for _family, obj in _route_map_contributors((native,)))
        for entry in native.route_map_entries.all():
            for field in ("call_policy", "apply_policy"):
                if (policy := getattr(entry, field)) is not None:
                    yield policy
    elif native._meta.model_name == "bgppeer":
        for family in native.address_families.all():
            for field in ("routemap_in", "routemap_out", "prefixlist_in", "prefixlist_out"):
                if (policy := getattr(family, field)) is not None:
                    yield policy
    elif getattr(native, "route_map_id", None):
        yield native.route_map


def _dependencies(management, rows, natives):
    from django.contrib.contenttypes.models import ContentType

    from .models import NSORoutePolicyState
    from .ownership_planner import OwnershipNotQualified, require_qualifying_ownership
    from .renderer_audit import RendererAuditRepairFailed
    from .signals import route_policy_intent_item
    from .status_machine import OWNED_STATES

    policy_roots = {
        (row.content_type_id, row.object_id) for row in rows if row._meta.model_name == "nsoroutepolicystate"
    }
    pending = [policy for native in natives for policy in _policy_references(native)]
    seen, owners = set(), []
    while pending:
        policy = pending.pop()
        ct = ContentType.objects.get_for_model(policy)
        identity = (ct.pk, policy.pk)
        if identity in seen:
            continue
        seen.add(identity)
        pending.extend(_policy_references(policy))
        if identity in policy_roots:
            continue
        owner = NSORoutePolicyState.objects.filter(
            management=management, content_type=ct, object_id=policy.pk, status__in=OWNED_STATES
        ).first()
        if owner is None:
            raise _Refused(f"Select the required policy dependency {policy._meta.label_lower} {policy.pk} ({policy}).")
        if owner.accepted_at is None:
            raise _Refused(f"Owned policy dependency {policy} row {owner.pk} has no acceptance timestamp.")
        try:
            require_qualifying_ownership(owner)
            if route_policy_intent_item(owner) is None:
                raise _Refused(f"Owned policy dependency {policy} is not renderable.")
        except (OwnershipNotQualified, RendererAuditRepairFailed) as exc:
            raise _Refused(f"Owned policy dependency {policy} is not renderable: {exc}") from exc
        owners.append(owner)
    return tuple(owners)


_RENDER_COLLECTIONS = {
    "dcim.interface": ("ip_addresses",),
    "netbox_routing.bgprouter": ("scopes",),
    "netbox_routing.bgpscope": ("address_families",),
    "netbox_routing.bgppeer": ("address_families",),
    "netbox_routing.isisinstance": ("levels",),
    "netbox_routing.ospfinstance": ("interfaces",),
    "netbox_routing.prefixlist": ("prefix_list_entries",),
    "netbox_routing.communitylist": ("communitylistentries",),
    "netbox_routing.aspath": ("aspath_entries",),
    "netbox_routing.routemap": ("route_map_entries",),
    "netbox_routing.routemapentry": ("set_communities",),
}


def _render_dependencies(natives, retained, candidates, relations):
    """Freeze native anchors, rendered collections and their relation edges."""
    from django.contrib.contenttypes.fields import GenericForeignKey

    from .intent_state import OVERLAY_MODEL_RANKS, SOURCE_MODEL_RANKS

    ranked = {*SOURCE_MODEL_RANKS, *OVERLAY_MODEL_RANKS}
    pending = [*natives, *retained, *candidates, *(row for rel in relations for row in rel.related)]
    seen, stored = set(), {}
    while pending:
        row = pending.pop()
        identity = (row._meta.label_lower, row.pk)
        marker = (*identity, id(row) if row._state.adding else None)
        if marker in seen or identity[0] not in ranked:
            continue
        seen.add(marker)
        if row.pk is not None and not row._state.adding:
            stored[identity] = row
        for field in row._meta.concrete_fields:
            if field.is_relation and field.related_model._meta.label_lower in ranked:
                related = getattr(row, field.name)
                if related is not None:
                    pending.append(related)
        for field in row._meta.private_fields:
            if isinstance(field, GenericForeignKey):
                related = getattr(row, field.name)
                if related is not None:
                    pending.append(related)
        if row.pk is None or row._state.adding:
            continue
        for name in _RENDER_COLLECTIONS.get(identity[0], ()):
            pending.extend(getattr(row, name).all())
        for field in row._meta.many_to_many:
            through = field.remote_field.through
            if through._meta.label_lower in ranked:
                pending.extend(through.objects.filter(**{field.m2m_field_name(): row.pk}))
    return tuple(stored[key] for key in sorted(stored))


def _plan(management, user, selection, timestamp, correlations):
    import copy

    from . import delivery
    from .candidate_render import CandidateBatch, overlay_identity
    from .intent_state import MutationFootprint
    from .models import NSODeviceManagement, NSOIntentRevision, NSOOwnershipManifest, NSOSwitchingRootDeletion
    from .ownership_grants import OwnershipGrant
    from .renderer_writer import RendererMutationPlan, planned_m2m_set, planned_save

    management = _existing(NSODeviceManagement, management.pk)
    _permission(NSODeviceManagement, user, "change", management)
    _permission(type(management.device), user, "view", management.device)
    if management.auto_apply or management.adapter_device_id is None:
        raise _Refused("Intend requires a linked device with auto_apply disabled.")
    resolved = [_candidate(management, user, item, timestamp, correlations) for item in selection]
    keys = tuple(sorted({scope for scope, _native, _current, _candidate_row in resolved}))
    rows = tuple(row for _scope, _native, _current, row in resolved)
    identities = [overlay_identity(row) for row in rows]
    if len(set(identities)) != len(identities):
        raise _Refused("Duplicate candidate identity in the selection.")
    natives = tuple(native for _scope, native, _current, _row in resolved)
    relations = tuple(
        planned_m2m_set(row, "tagged_vlans", native.tagged_vlans.all())
        for _scope, native, _current, row in resolved
        if row._meta.model_name == "nsoswitchportstate"
    )
    retained = _retained(management, keys, set(identities))
    dependencies = _render_dependencies(natives, retained, rows, relations)
    policy_owners = _dependencies(management, rows, dependencies)
    dependencies = _render_dependencies(dependencies, policy_owners, (), ())
    batch = CandidateBatch(rows, relations)
    payloads, bodies = _render(management, keys, batch)
    for index, row in enumerate(rows):
        masked = copy.copy(row)
        masked.status = "imported"
        alternate = CandidateBatch((*rows[:index], masked, *rows[index + 1 :]), relations)
        # The shared renderer must observe each selected contribution.
        masked_payloads = {
            key: delivery.render(key, management.device_id, management.adapter_device_id, candidates=alternate).payload
            for key in keys
        }
        if delivery.canonical_fingerprint(masked_payloads) == delivery.canonical_fingerprint(payloads):
            raise _Refused(
                f"{row._meta.label_lower} ({row}) requires a renderable dependency, bundle or destination. "
                "Select the missing dependency explicitly."
            )
    revisions = {
        row.scope: row for row in NSOIntentRevision.objects.filter(device_id=management.device_id, scope__in=keys)
    }
    baseline = [
        {
            "scope": key,
            "revision": revisions[key].revision if key in revisions else 0,
            "verified_revision": revisions[key].verified_revision if key in revisions else None,
            "verified_fingerprint": revisions[key].verified_fingerprint if key in revisions else None,
        }
        for key in keys
    ]
    manifests = list(
        NSOOwnershipManifest.objects.filter(device_id=management.device_id, scope__in=keys).order_by("pk").values()
    )
    persisted, _ = _render(management, keys, None)
    digest = delivery.canonical_fingerprint(
        {
            "management": _digest_values(management),
            "native": [_values(row) for row in natives],
            "dependencies": [(row._meta.label_lower, _digest_values(row)) for row in dependencies],
            "before": [
                _values(current) if current is not None else None for _scope, _native, current, _row in resolved
            ],
            "candidate": [_values(row) for row in rows],
            "bodies": bodies,
            "switching_deletions": list(
                NSOSwitchingRootDeletion.objects.filter(management=management, scope__in=keys)
                .order_by("scope", "root_name")
                .values()
            ),
            "persisted": persisted,
            "relations": [
                (overlay_identity(rel.instance), rel.field_name, [row.pk for row in rel.related]) for rel in relations
            ],
            "baseline": baseline,
            "manifests": manifests,
        }
    )
    saves = tuple(
        planned_save(row, force_insert=row.pk is None, natural_key=row._meta.unique_together[0]) for row in rows
    )
    mutation = RendererMutationPlan.build(
        saves=saves,
        m2m_writes=relations,
        read_dependencies=dependencies,
        additional_footprints=(
            MutationFootprint(
                device_ids=(management.device_id,), revision_keys=tuple((management.device_id, key) for key in keys)
            ),
        ),
        planned_at=timestamp,
        grant=OwnershipGrant("intend"),
        settles_deploying=False,
    )
    # A new ownership episode needs delivery even when its wire content is unchanged.
    mutation = replace(
        mutation,
        content_keys=tuple(sorted(set(mutation.content_keys) | {(management.device_id, key) for key in keys})),
    )
    return management, batch, keys, payloads, bodies, digest, mutation


def _refusal(message, preflight=None, *, ordinary_delivery=False):
    if ordinary_delivery:
        recovery = (
            "No acquisition occurred; let ordinary delivery for the key complete or repair its reported failure, "
            "or make the change as a separate ordinary edit, then retry Intend."
        )
    else:
        recovery = "No acquisition occurred. Fix the selection or reported problem and preview again."
    return IntendResult("refused", f"{message} {recovery}", preflight=preflight)


CONFIRMATION_MAX_AGE = 300
_SIGNING_SALT = "netbox_nso_plugin.intent_acquisition"


@contextmanager
def _snapshot():
    from django.db import connection, transaction

    nested = connection.in_atomic_block
    with transaction.atomic():
        if not nested:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        yield


def preview_intend(management, user, selection):
    """Preview native identities with expiring signed confirmation evidence.

    Each selection names scope, native_model, native_id, state_key and options.
    LACP bundles require lag_id when absent. Ended episodes require restart=True.
    """
    from django.contrib.auth import get_user_model
    from django.core import signing
    from django.utils import timezone

    from . import adapter_client
    from .apply_preflight import admits_intend, candidate_refusal_message
    from .renderer_audit import RendererAuditRepairFailed
    from .renderer_writer import IntentPlanStaleError

    try:
        user = _existing(get_user_model(), user.pk)
        selection = _selection(selection)
        timestamp, correlations = timezone.now(), {}
        with _snapshot():
            management, _batch, keys, _payloads, bodies, digest, _mutation = _plan(
                management, user, selection, timestamp, correlations
            )
        preflight = adapter_client.apply_candidate_preflight(management.adapter_device_id, bodies)
        if not admits_intend(preflight):
            return _refusal(candidate_refusal_message(preflight), preflight, ordinary_delivery=True)
        evidence = {
            "operator": user.pk,
            "management": management.pk,
            "selection": selection,
            "timestamp": timestamp.isoformat(),
            "correlations": correlations,
            "digest": digest,
            "candidate_identity": preflight["candidate_identity"],
            "plan_digest": preflight["plan_digest"],
        }
        return IntendResult(
            "ready",
            "Candidate ready for acquisition.",
            signing.dumps(evidence, salt=_SIGNING_SALT, compress=True),
            preflight,
            keys,
        )
    except RendererAuditRepairFailed as exc:
        return _refusal(f"The candidate cannot be rendered: {exc}.")
    except (_Refused, IntentPlanStaleError, adapter_client.AdapterError) as exc:
        return _refusal(str(exc), ordinary_delivery=isinstance(exc, _Refused) and exc.ordinary_delivery)


def _confirmation(management, user, confirmation):
    from datetime import datetime

    from django.core import signing
    from django.utils import timezone

    try:
        evidence = signing.loads(confirmation, salt=_SIGNING_SALT, max_age=CONFIRMATION_MAX_AGE)
    except (signing.BadSignature, TypeError):
        raise _Refused("Confirmation is invalid or expired. Preview again.") from None
    if evidence["operator"] != user.pk or evidence["management"] != management.pk:
        raise _Refused("Confirmation belongs to another operator or management.")
    selection = _selection(evidence["selection"])
    timestamp = datetime.fromisoformat(evidence["timestamp"])
    if timezone.is_naive(timestamp):
        raise _Refused("Confirmation timestamp is invalid.")
    return evidence, selection, timestamp


def confirm_intend(management, user, confirmation):
    """Acquire the reconstructed candidate and schedule all ordinary delivery atomically."""
    from django.contrib.auth import get_user_model

    from . import adapter_client, outbox, signals
    from .apply_preflight import admits_intend, candidate_refusal_message
    from .intent_state import RendererTargetsChanged, mirror_transaction
    from .models import NSOIntentOutboxEntry
    from .renderer_audit import RendererAuditRepairFailed
    from .renderer_writer import IntentPlanStaleError, renderer_writes

    try:
        user = _existing(get_user_model(), user.pk)
        evidence, selection, timestamp = _confirmation(management, user, confirmation)
        correlations = dict(evidence["correlations"])
        with _snapshot():
            management, batch, keys, _payloads, bodies, digest, mutation = _plan(
                management, user, selection, timestamp, correlations
            )
        if digest != evidence["digest"]:
            raise _Refused("Acquisition inputs changed. Preview again.")
        preflight = adapter_client.apply_candidate_preflight(management.adapter_device_id, bodies)
        if not admits_intend(preflight):
            return _refusal(candidate_refusal_message(preflight), preflight, ordinary_delivery=True)
        if any(preflight[name] != evidence[name] for name in ("candidate_identity", "plan_digest")):
            raise _Refused("Adapter candidate or plan changed. Preview again.")
        with mirror_transaction(mutation.lock_footprint, detect_content_changes=True, settles_deploying=False):
            user = _existing(get_user_model(), user.pk)
            locked = _plan(management, user, selection, timestamp, correlations)
            if locked[5] != digest or locked[4] != bodies or locked[6].lock_footprint != mutation.lock_footprint:
                raise _Refused("Acquisition inputs changed under the writer locks. Preview again.")
            if signals._intent_push_is_silenced():
                raise _Refused("Ordinary delivery scheduling is suppressed.")
            created_identities = {id(row) for row in batch.overlays if row.pk is None}
            with renderer_writes(mutation) as writer:
                for row in batch.overlays:
                    created = row.pk is None
                    writer.save(row, force_insert=created)
                for relation in batch.m2m:
                    writer.m2m_set(relation.instance, relation.field_name, relation.related)
                for row in batch.overlays:
                    _permission(type(row), user, "add" if id(row) in created_identities else "change", row)
                _confirmed_payloads, confirmed_bodies = _render(management, keys, None)
                if confirmed_bodies != bodies:
                    raise _Refused("Confirmed rendering differs from the frozen candidate.")
                for key in keys:
                    signals._schedule_intent_push((management.device_id, key))
                    if not NSOIntentOutboxEntry.objects.filter(
                        device_id=management.device_id,
                        scope=key,
                        batch_id=outbox.current_txid(),
                        kind=outbox.CONTRIBUTION_KIND_ORDINARY,
                        consumed_by_push_seq__isnull=True,
                    ).exists():
                        raise _Refused(f"Ordinary delivery for {key} was not scheduled.")
        return IntendResult("acquired", "acquired; delivery pending", preflight=preflight, keys=keys)
    except RendererTargetsChanged as exc:
        return _refusal(f"Acquisition inputs changed: {exc}. Preview again.")
    except RendererAuditRepairFailed as exc:
        return _refusal(f"The candidate cannot be rendered: {exc}.")
    except (_Refused, IntentPlanStaleError, adapter_client.AdapterError) as exc:
        return _refusal(str(exc), ordinary_delivery=isinstance(exc, _Refused) and exc.ordinary_delivery)
