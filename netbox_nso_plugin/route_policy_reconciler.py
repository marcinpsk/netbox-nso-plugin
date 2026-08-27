# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Route-policy reconciler for A4.

Reads the adapter's GET /api/v1/devices/{id}/route-policy response and
reconciles it into netbox-routing policy objects (PrefixList, CommunityList,
ASPath, RouteMap) — including their ENTRIES — plus NSORoutePolicyState overlay
rows.

Decision: global dedup by name — same-named object across N devices = ONE
NetBox object. On-device divergence sets status=conflict, never silently
overwrites existing content. Entries are filled on first import / for empty
shells; once an object has entries, a later content divergence is flagged
``conflict`` and the entries are left untouched (no silent clobber).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from contextlib import nullcontext
from contextvars import ContextVar

from . import shared_object_ownership as ownership
from .route_policy_structure import canonical_route_map, prefix_list_entry_unit

logger = logging.getLogger(__name__)

# One route-policy family → the document key that carries it.
_FAMILY_PAYLOAD_KEYS = {
    "prefix_list": "prefix_lists",
    "community_list": "community_lists",
    "as_path": "as_paths",
    "route_map": "route_maps",
}

# name → tuple of prefix-list units, memoized for one reconcile pass (cleared at its start).
# Lets canonical_route_map expand prefix-list refs to content without re-querying per call.
_PL_UNIT_CACHE: ContextVar[dict[str, tuple] | None] = ContextVar("route_policy_prefix_units", default=None)


class _Operations:
    """Collect proposed route-policy writes and deterministic replay data."""

    def __init__(self):
        self.saves = []
        self.deletes = []
        self.m2m_writes = []
        self.operations = []

    def save(self, instance, *, update_fields=None, force_insert=False, natural_key=(), references=()):
        from .renderer_writer import planned_save

        references = tuple(references)
        self.saves.append(
            planned_save(
                instance,
                update_fields=update_fields,
                force_insert=force_insert,
                natural_key=natural_key,
                references=references,
            )
        )
        self.operations.append(("save", instance, update_fields, force_insert, references, None, ()))

    def delete(self, instance):
        from .renderer_writer import planned_delete

        self.deletes.append(planned_delete(instance))
        self.operations.append(("delete", instance, None, False, (), None, ()))

    def m2m_add(self, instance, field_name, related):
        from .renderer_writer import planned_m2m_add

        related = tuple(related)
        if not related:
            return
        self.m2m_writes.append(planned_m2m_add(instance, field_name, related))
        self.operations.append(("m2m_add", instance, None, False, (), field_name, related))

    def display_m2m_add(self, instance, field_name, related):
        """Record an unregistered display-only M2M projection for replay."""
        related = tuple(related)
        if related:
            self.operations.append(("display_m2m_add", instance, None, False, (), field_name, related))


def route_policy_reconcile_plan(device, payload):
    """Freeze every root, entry, registered M2M, through-row, and overlay write."""
    from django.utils import timezone

    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    try:
        operations = _route_policy_reconcile_operations(device, payload, planned_at)
    except ImportError:
        return RendererMutationPlan.build(planned_at=planned_at)
    return RendererMutationPlan.build(
        saves=operations.saves,
        deletes=operations.deletes,
        m2m_writes=operations.m2m_writes,
        planned_at=planned_at,
    )


def _resolve_prefix_list_units(name: str) -> tuple:
    """Resolve a prefix-list NAME to its content as ``(action, prefix, ge, le)`` units.

    Reads the GLOBAL materialized version (one NetBox object per name; falls back to any
    device's capture) so both devices' route-maps expand a shared list to the SAME units —
    the comparison stays about route-map content, not which box reported the list. A name with
    no captured prefix-list yet resolves to empty (the term simply has nothing to expand).
    """
    key = name.lower()
    cache = _PL_UNIT_CACHE.get()
    if cache is None:
        cache = {}
        _PL_UNIT_CACHE.set(cache)
    if key in cache:
        return cache[key]
    from .models import NSORoutePolicyState

    row = (
        NSORoutePolicyState.objects.filter(family="prefix_list", object_name__iexact=name, is_materialized=True).first()
        or NSORoutePolicyState.objects.filter(family="prefix_list", object_name__iexact=name).first()
    )
    units = tuple(prefix_list_entry_unit(e) for e in ((row.captured or {}).get("entries") or [])) if row else ()
    cache[key] = units
    return units


def _hash(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _norm_action(action: str | None) -> str:
    """Map an adapter action to netbox_routing ActionChoices (permit/deny)."""
    a = (action or "").strip().lower()
    if a in ("permit", "accept"):
        return "permit"
    if a in ("deny", "reject"):
        return "deny"
    return "permit"


def _load_json(value) -> dict:
    """Parse a match/set JSON string into a dict (the adapter sends them as strings)."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        out = json.loads(value)
        return out if isinstance(out, dict) else {}
    except (ValueError, TypeError):
        return {}


class _RoutePolicyGraphPlanner:  # noqa: PLR0904
    """Build the prospective writes for one route-policy document."""

    FAMILY_PAYLOAD_KEYS = _FAMILY_PAYLOAD_KEYS

    def __init__(self, device, payload, planned_at):  # noqa: PLR0915
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import (
            ASPath,
            ASPathEntry,
            Community,
            CommunityList,
            CommunityListEntry,
            CustomPrefix,
            PrefixList,
            PrefixListEntry,
            RouteMap,
            RouteMapEntry,
            RouteMapEntrySetCommunity,
        )

        from .models import NSODeviceManagement, NSORoutePolicyState

        self.device = device
        self.payload = payload
        self.planned_at = planned_at
        self.management = NSODeviceManagement.objects.filter(device=device).first()
        self.ContentType = ContentType
        self.NSORoutePolicyState = NSORoutePolicyState
        self.models = {
            "prefix_list": PrefixList,
            "community_list": CommunityList,
            "as_path": ASPath,
            "route_map": RouteMap,
        }
        self.PrefixListEntry = PrefixListEntry
        self.CustomPrefix = CustomPrefix
        self.CommunityListEntry = CommunityListEntry
        self.Community = Community
        self.ASPathEntry = ASPathEntry
        self.RouteMapEntry = RouteMapEntry
        self.RouteMapEntrySetCommunity = RouteMapEntrySetCommunity
        self.operations = _Operations()
        self.roots = {
            family: {row.name.casefold(): row for row in model.objects.all()} for family, model in self.models.items()
        }
        self.states = (
            {
                (row.family, row.object_name.casefold()): row
                for row in NSORoutePolicyState.objects.filter(management=self.management).select_related(
                    "management", "content_type"
                )
            }
            if self.management is not None
            else {}
        )
        self.prefixes = {str(row.prefix): row for row in CustomPrefix.objects.all()}
        self.communities = {str(row.community): row for row in Community.objects.all()}
        self.name_maps = {family: {} for family in self.models}
        self.community_members = {}
        self.seen = set()
        self.modified_state_pks = set()
        self.prospective_owner_hashes = {}

    def build(self):
        if self.management is None:
            return self.operations
        self._seed_prefix_units()
        for family in ("prefix_list", "community_list", "as_path", "route_map"):
            rows = sorted(
                self.payload.get(self.FAMILY_PAYLOAD_KEYS[family]) or [],
                key=lambda row: (row.get("name") or "").casefold(),
            )
            for captured in rows:
                self.plan_object(family, captured)
        self.plan_stale_states()
        self.plan_resettle_conflicts()
        return self.operations

    def _seed_prefix_units(self):
        cache = {}
        for captured in self.payload.get("prefix_lists") or []:
            name = captured.get("name") or ""
            if not name:
                continue
            owner = self.NSORoutePolicyState.objects.filter(
                family="prefix_list",
                object_name__iexact=name,
                is_materialized=True,
            ).first()
            if owner is not None and owner.management_id != self.management.pk:
                entries = (owner.captured or {}).get("entries") or []
            else:
                entries = captured.get("entries") or []
            cache[name.casefold()] = tuple(prefix_list_entry_unit(entry) for entry in entries)
        _PL_UNIT_CACHE.set(cache)

    def _hash_captured(self, family, captured):
        return ownership.hash_captured(family, captured)

    def _root_has_entries(self, family, root):
        if root.pk is None:
            return False
        lookups = {
            "prefix_list": (self.PrefixListEntry, {"prefix_list": root}),
            "community_list": (self.CommunityListEntry, {"community_list": root}),
            "as_path": (self.ASPathEntry, {"aspath": root}),
            "route_map": (self.RouteMapEntry, {"route_map": root}),
        }
        model, filters = lookups[family]
        return model.objects.filter(**filters).exists()

    def _existing_entries(self, family, root):
        if root.pk is None:
            return ()
        if family == "prefix_list":
            rows = self.PrefixListEntry.objects.filter(prefix_list=root)
        elif family == "community_list":
            rows = self.CommunityListEntry.objects.filter(community_list=root)
        elif family == "as_path":
            rows = self.ASPathEntry.objects.filter(aspath=root)
        else:
            rows = self.RouteMapEntry.objects.filter(route_map=root)
        return tuple(rows.order_by("pk"))

    def _group_mode(self, family, name):
        from .models import NSORoutePolicyObjectClass

        row = NSORoutePolicyObjectClass.objects.filter(family=family, object_name__iexact=name).first()
        return row.mode if row is not None else "master"

    def _canonical_hash(self, family, name):
        return ownership.canonical_hash(self.NSORoutePolicyState, family, name)

    def _state_candidate(self, family, name, root, captured):  # noqa: PLR0915
        from . import status_machine as sm

        key = (family, name.casefold())
        current = self.states.get(key)
        entries_hash = self._hash_captured(family, captured)
        canonical_hash = self._canonical_hash(family, name)
        if current is None:
            conflict = canonical_hash is not None and canonical_hash != entries_hash
            candidate = self.NSORoutePolicyState(
                management=self.management,
                family=family,
                object_name=name,
                content_type=self.ContentType.objects.get_for_model(type(root)),
                object_id=root.pk,
                content_hash=entries_hash,
                captured=captured,
                status=sm.CONFLICT if conflict else sm.IMPORTED,
                last_sync_at=self.planned_at,
                device_present=True,
            )
            return candidate, True, not conflict, False

        candidate = copy.copy(current)
        if sm.is_owned(candidate.status) and ownership.device_caught_up(
            family,
            captured,
            root,
            exclude_members=(list(candidate.unsupported_members or []) or None) if family == "community_list" else None,
        ):
            candidate.status = sm.on_reconcile(candidate.status, matches=True, settles_owned=True)
        if candidate.is_materialized:
            diverged = bool(candidate.content_hash) and candidate.content_hash != entries_hash
        else:
            diverged = (
                bool(candidate.content_hash) and candidate.content_hash != entries_hash
                if canonical_hash is None
                else canonical_hash != entries_hash
            )
        refresh_owner = diverged and candidate.is_materialized and not sm.is_owned(candidate.status)
        if refresh_owner:
            candidate.status = sm.IMPORTED
            candidate.content_hash = entries_hash
        else:
            candidate.status = sm.on_reconcile(
                candidate.status,
                matches=not diverged,
                conflict=diverged,
                settles_owned=False,
            )
            if candidate.status != sm.CONFLICT:
                candidate.content_hash = entries_hash
        candidate.captured = captured
        candidate.last_sync_at = self.planned_at
        candidate.content_type = self.ContentType.objects.get_for_model(type(root))
        candidate.object_id = root.pk
        candidate.device_present = True
        return candidate, False, candidate.status != sm.CONFLICT, refresh_owner

    def _plan_state(self, candidate, created, root):
        references = (("object_id", root),) if root is not None and root.pk is None else ()
        if created:
            self.operations.save(
                candidate,
                force_insert=True,
                natural_key=("management", "family", "object_name"),
                references=references,
            )
            self.modified_state_pks.add((candidate.management_id, candidate.family, candidate.object_name.casefold()))
            return
        fields = (
            "status",
            "content_hash",
            "captured",
            "last_sync_at",
            "content_type",
            "object_id",
            "device_present",
            "is_materialized",
        )
        self.operations.save(candidate, update_fields=fields, references=references)
        self.modified_state_pks.add((candidate.management_id, candidate.family, candidate.object_name.casefold()))

    def _plan_root_save(self, family, root, created, changed_fields=()):
        if created:
            self.operations.save(root, force_insert=True, natural_key=("name",))
        elif changed_fields:
            self.operations.save(root, update_fields=tuple(changed_fields))

    def plan_object(self, family, captured):  # noqa: C901, PLR0912, PLR0915
        name = captured.get("name") or ""
        if not name:
            return
        key = (family, name.casefold())
        self.seen.add(key)
        if self._group_mode(family, name) == "local":
            self.plan_local_state(family, name, captured)
            return
        root = self.roots[family].get(name.casefold())
        created_root = root is None
        if root is None:
            kwargs = {"name": name}
            if family == "community_list":
                kwargs["invert_match"] = bool(captured.get("invert_match", False))
            root = self.models[family](**kwargs)
            self.roots[family][name.casefold()] = root
        self.name_maps[family][name] = root
        has_materialized_owner = self.NSORoutePolicyState.objects.filter(
            family=family,
            object_name__iexact=name,
            is_materialized=True,
        ).exists()
        state, created_state, should_fill, refresh_owner = self._state_candidate(family, name, root, captured)
        fill = refresh_owner or (
            should_fill and (not has_materialized_owner or created_root or not self._root_has_entries(family, root))
        )
        changed_fields = []
        if family == "prefix_list" and should_fill and captured.get("family") in (4, 6):
            if root.family != captured["family"]:
                root = copy.copy(root)
                root.family = captured["family"]
                self.roots[family][name.casefold()] = root
                self.name_maps[family][name] = root
                changed_fields.append("family")
        if family == "community_list" and should_fill:
            invert_match = bool(captured.get("invert_match", False))
            if root.invert_match != invert_match:
                root = copy.copy(root)
                root.invert_match = invert_match
                self.roots[family][name.casefold()] = root
                self.name_maps[family][name] = root
                changed_fields.append("invert_match")
        if fill and not created_root:
            for entry in self._existing_entries(family, root):
                self.operations.delete(entry)
        if fill and family == "route_map":
            default_action = self._route_map_default_action(captured.get("entries") or [])
            if root.default_action != default_action:
                root = copy.copy(root)
                root.default_action = default_action
                self.roots[family][name.casefold()] = root
                self.name_maps[family][name] = root
                changed_fields.append("default_action")
        self._plan_root_save(family, root, created_root, changed_fields)
        if fill:
            if family == "prefix_list":
                self.plan_prefix_entries(root, captured.get("entries") or [])
            elif family == "community_list":
                self.plan_community_entries(root, captured.get("entries") or [])
            elif family == "as_path":
                self.plan_as_path_entries(root, captured.get("entries") or [])
            else:
                self.plan_route_map_entries(root, captured.get("entries") or [])
            if created_state or refresh_owner or not has_materialized_owner:
                state.is_materialized = True
        if state.is_materialized:
            self.prospective_owner_hashes[key] = state.content_hash
        state.object_id = root.pk
        self.states[key] = state
        self._plan_state(state, created_state, root)

    def plan_local_state(self, family, name, captured):
        from . import status_machine as sm

        key = (family, name.casefold())
        current = self.states.get(key)
        entries_hash = self._hash_captured(family, captured)
        if current is None:
            state = self.NSORoutePolicyState(
                management=self.management,
                family=family,
                object_name=name,
                content_hash=entries_hash,
                captured=captured,
                status=sm.IMPORTED,
                last_sync_at=self.planned_at,
                is_materialized=False,
                device_present=True,
            )
            self.states[key] = state
            self._plan_state(state, True, None)
            return
        state = copy.copy(current)
        changed = state.content_hash != entries_hash
        state.status = sm.on_reconcile(state.status, matches=not changed, conflict=False, settles_owned=False)
        state.content_hash = entries_hash
        state.captured = captured
        state.last_sync_at = self.planned_at
        state.is_materialized = False
        state.content_type = None
        state.object_id = None
        state.device_present = True
        self.states[key] = state
        self._plan_state(state, False, None)

    def _flag_removed(self, state):
        from . import status_machine as sm

        candidate = copy.copy(state)
        candidate.status = sm.on_reconcile(candidate.status, present=False)
        candidate.device_present = False
        self.states[(candidate.family, candidate.object_name.casefold())] = candidate
        self._plan_state(candidate, False, candidate.assigned_object)

    def _group_rows(self, state):
        return tuple(
            self.NSORoutePolicyState.objects.filter(
                family=state.family,
                object_name__iexact=state.object_name,
            )
            .select_related("management", "content_type")
            .order_by("pk")
        )

    def plan_stale_states(self):  # noqa: C901, PLR0912
        from . import status_machine as sm

        for key, state in tuple(self.states.items()):
            if key in self.seen:
                continue
            group = self._group_rows(state)
            if sm.is_owned(state.status) or any(sm.is_owned(row.status) for row in group):
                self._flag_removed(state)
                continue
            live = [row for row in group if row.pk != state.pk and row.device_present and row.captured]
            root = state.assigned_object
            if live:
                self.operations.delete(state)
                self.modified_state_pks.add((state.management_id, state.family, state.object_name.casefold()))
                if state.is_materialized:
                    self.plan_rematerialize(live[0], root, group, state.pk)
                continue
            if root is not None and _object_referenced(root, state.family):
                for row in group:
                    identity = (row.management_id, row.family, row.object_name.casefold())
                    if identity not in self.modified_state_pks:
                        self._flag_removed(row)
                continue
            for row in group:
                identity = (row.management_id, row.family, row.object_name.casefold())
                if identity in self.modified_state_pks:
                    continue
                self.operations.delete(row)
                self.modified_state_pks.add(identity)
            if root is not None:
                self.operations.delete(root)

    def _resolve_route_map_name_maps(self, entries):
        for entry in entries:
            for family, key in (
                ("prefix_list", "match_prefix_lists"),
                ("community_list", "match_community_lists"),
                ("as_path", "match_as_paths"),
            ):
                for name in entry.get(key) or []:
                    root = self.roots[family].get(name.casefold())
                    if root is not None:
                        self.name_maps[family][name] = root
                        if family == "community_list" and name not in self.community_members:
                            self.community_members[name] = tuple(
                                row.community
                                for row in self.CommunityListEntry.objects.filter(community_list=root).select_related(
                                    "community"
                                )
                                if row.community_id
                            )

    def plan_replace_root(self, family, root, captured):
        for entry in self._existing_entries(family, root):
            self.operations.delete(entry)
        changed_fields = []
        candidate = copy.copy(root)
        if family == "prefix_list" and captured.get("family") in (4, 6):
            if candidate.family != captured["family"]:
                candidate.family = captured["family"]
                changed_fields.append("family")
        elif family == "community_list":
            invert_match = bool(captured.get("invert_match", False))
            if candidate.invert_match != invert_match:
                candidate.invert_match = invert_match
                changed_fields.append("invert_match")
        elif family == "route_map":
            default_action = self._route_map_default_action(captured.get("entries") or [])
            if candidate.default_action != default_action:
                candidate.default_action = default_action
                changed_fields.append("default_action")
            self._resolve_route_map_name_maps(captured.get("entries") or [])
        if changed_fields:
            self.operations.save(candidate, update_fields=changed_fields)
            root = candidate
        entries = captured.get("entries") or []
        if family == "prefix_list":
            self.plan_prefix_entries(root, entries)
        elif family == "community_list":
            self.plan_community_entries(root, entries)
        elif family == "as_path":
            self.plan_as_path_entries(root, entries)
        else:
            self.plan_route_map_entries(root, entries)
        return root

    def plan_rematerialize(self, owner, root, group, removed_pk):
        from . import status_machine as sm

        if root is None:
            return
        captured = owner.captured or {}
        root = self.plan_replace_root(owner.family, root, captured)
        owner_hash = self._hash_captured(owner.family, captured)
        key = (owner.family, owner.object_name.casefold())
        self.prospective_owner_hashes[key] = owner_hash
        for row in group:
            if row.pk == owner.pk:
                candidate = copy.copy(row)
                candidate.is_materialized = True
                candidate.content_hash = owner_hash
                candidate.content_type = self.ContentType.objects.get_for_model(type(root))
                candidate.object_id = root.pk
                if not sm.is_owned(candidate.status):
                    candidate.status = sm.IMPORTED
            elif row.pk == removed_pk:
                continue
            else:
                candidate = copy.copy(row)
                candidate.is_materialized = False
                if not sm.is_owned(candidate.status) and candidate.captured:
                    diverged = self._hash_captured(candidate.family, candidate.captured) != owner_hash
                    candidate.status = sm.CONFLICT if diverged else sm.IMPORTED
                candidate.last_sync_at = self.planned_at
            identity = (candidate.management_id, candidate.family, candidate.object_name.casefold())
            if identity in self.modified_state_pks:
                continue
            self._plan_state(candidate, False, root)

    def plan_resettle_conflicts(self):
        from . import status_machine as sm

        for key in self.seen:
            owner_hash = self.prospective_owner_hashes.get(key) or self._canonical_hash(*key)
            if not owner_hash:
                continue
            family, name = key
            rows = self.NSORoutePolicyState.objects.filter(
                family=family,
                object_name__iexact=name,
                status=sm.CONFLICT,
                is_materialized=False,
            )
            for row in rows:
                identity = (row.management_id, row.family, row.object_name.casefold())
                if identity in self.modified_state_pks or row.content_hash != owner_hash:
                    continue
                candidate = copy.copy(row)
                candidate.status = sm.on_reconcile(
                    candidate.status,
                    matches=True,
                    conflict=False,
                    settles_owned=False,
                )
                self._plan_state(candidate, False, candidate.assigned_object)

    @staticmethod
    def _route_map_default_action(entries):
        from .route_policy_structure import structure_entry

        for entry in entries:
            if structure_entry(_load_json(entry.get("match")), _load_json(entry.get("set"))).is_default_action:
                return _norm_action(entry.get("action"))
        return None

    def plan_prefix_entries(self, root, entries):
        content_type = self.ContentType.objects.get_for_model(self.CustomPrefix)
        sequence = 0
        for entry in entries:
            prefix = (entry.get("prefix") or "").strip()
            if not prefix:
                continue
            custom_prefix = self.prefixes.get(prefix)
            if custom_prefix is None:
                custom_prefix = self.CustomPrefix(prefix=prefix)
                try:
                    custom_prefix.full_clean(validate_unique=False, validate_constraints=False)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("route-policy: bad prefix %r in %s: %s", prefix, root.name, exc)
                    continue
                self.prefixes[prefix] = custom_prefix
                self.operations.save(custom_prefix, force_insert=True, natural_key=("prefix",))
            sequence += 1
            row = self.PrefixListEntry(
                prefix_list=root,
                assigned_prefix_type=content_type,
                assigned_prefix_id=custom_prefix.pk,
                sequence=sequence,
                action=_norm_action(entry.get("action")),
                ge=entry.get("ge"),
                le=entry.get("le"),
            )
            self.operations.save(
                row,
                force_insert=True,
                natural_key=("prefix_list", "sequence"),
                references=(("assigned_prefix_id", custom_prefix),),
            )

    def plan_community_entries(self, root, entries):
        members = []
        for entry in entries:
            value = (entry.get("community") or "").strip()
            if not value:
                logger.info("route-policy: skipping empty community member in %s", root.name)
                continue
            community = self.communities.get(value)
            if community is None:
                community = self.Community(community=value)
                self.communities[value] = community
                self.operations.save(community, force_insert=True, natural_key=("community",))
            members.append(community)
            row = self.CommunityListEntry(
                community_list=root,
                action=_norm_action(entry.get("action")),
                community=community,
            )
            self.operations.save(
                row,
                force_insert=True,
                natural_key=("community_list", "action", "community"),
            )
        self.community_members[root.name] = tuple(members)

    def plan_as_path_entries(self, root, entries):
        for sequence, entry in enumerate(entries, start=1):
            row = self.ASPathEntry(
                aspath=root,
                sequence=sequence,
                action=_norm_action(entry.get("action")),
                pattern=(entry.get("pattern") or "")[:1000],
            )
            self.operations.save(
                row,
                force_insert=True,
                natural_key=("aspath", "sequence"),
            )

    def plan_route_map_entries(self, root, entries):  # noqa: PLR0915
        from .route_policy_structure import structure_entry

        for sequence, entry in enumerate(entries, start=1):
            match_blob = _load_json(entry.get("match"))
            set_blob = _load_json(entry.get("set"))
            structured = structure_entry(match_blob, set_blob)
            set_data = dict(set_blob)
            flow_control = set_data.pop("flow_control", None)
            vendor_ext = dict(structured.vendor_ext)
            row = self.RouteMapEntry(
                route_map=root,
                sequence=sequence,
                action=_norm_action(entry.get("action")),
                flow_control=flow_control,
                match=match_blob,
                set=set_data,
                match_afi=structured.match_afi or None,
                call_policy=self.models["route_map"].objects.filter(name__iexact=structured.call_policy).first()
                if structured.call_policy
                else None,
                vendor_ext=vendor_ext or None,
            )
            self.operations.save(row, force_insert=True, natural_key=("route_map", "sequence"))
            self._plan_route_map_matches(row, entry)
            unresolved = self.plan_set_communities(row, structured)
            if unresolved:
                vendor_ext.setdefault("unmapped", {})["set_community"] = unresolved
                row.vendor_ext = vendor_ext

    def _plan_route_map_matches(self, row, entry):
        mappings = (
            ("match_prefix_list", "prefix_list", entry.get("match_prefix_lists") or []),
            ("match_community_list", "community_list", entry.get("match_community_lists") or []),
            ("match_aspath", "as_path", entry.get("match_as_paths") or []),
        )
        for field_name, family, names in mappings:
            related = tuple(self.name_maps[family][name] for name in names if name in self.name_maps[family])
            self.operations.m2m_add(row, field_name, related)
            if family == "community_list":
                members = tuple(member for name in names for member in self.community_members.get(name, ()))
                self.operations.display_m2m_add(row, "match_community", members)

    def plan_set_communities(self, row, structured):
        unresolved = []
        for action in structured.set_communities:
            community_list = self.name_maps["community_list"].get(action.name)
            if community_list is not None:
                set_row = self.RouteMapEntrySetCommunity(
                    route_map_entry=row,
                    operation=action.operation,
                    community_list=community_list,
                )
                self.operations.save(
                    set_row,
                    force_insert=True,
                    natural_key=("route_map_entry", "operation", "community_list"),
                )
                continue
            if not _looks_like_community_literal(action.name):
                unresolved.append({"operation": action.operation, "name": action.name})
                continue
            set_row = self.RouteMapEntrySetCommunity(route_map_entry=row, operation=action.operation)
            self.operations.save(
                set_row,
                force_insert=True,
                natural_key=("route_map_entry", "operation", "community_list"),
            )
            community = self.communities.get(action.name)
            if community is None:
                community = self.Community(community=action.name)
                self.communities[action.name] = community
                self.operations.save(community, force_insert=True, natural_key=("community",))
            self.operations.m2m_add(set_row, "communities", (community,))
        return unresolved


def _route_policy_reconcile_operations(device, payload, planned_at):
    return _RoutePolicyGraphPlanner(device, payload, planned_at).build()


def _mutation_plan(operations, planned_at):
    from .renderer_writer import RendererMutationPlan

    return RendererMutationPlan.build(
        saves=operations.saves,
        deletes=operations.deletes,
        m2m_writes=operations.m2m_writes,
        planned_at=planned_at,
    )


def _replay_operations(writer, operations):
    for operation, instance, update_fields, force_insert, references, field_name, related in operations.operations:
        if operation == "delete":
            writer.delete(instance)
            continue
        if operation == "m2m_add":
            writer.m2m_add(instance, field_name, related)
            continue
        if operation == "display_m2m_add":
            getattr(instance, field_name).add(*related)
            continue
        for reference_field, referenced in references:
            setattr(instance, reference_field, referenced.pk)
        writer.save(instance, update_fields=update_fields, force_insert=force_insert)


def _execute_operations(operations, planned_at, *, suppress_push=True):
    from .renderer_writer import renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    plan = _mutation_plan(operations, planned_at)
    mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    suppression = suppress_intent_push() if suppress_push else nullcontext()
    with mutation as writer, suppression:
        _replay_operations(writer, operations)
    return plan


def _rematerialize_operations(state, planned_at):
    """Build a prospective graph for one selected device version."""
    if state.assigned_object is None:
        raise ValueError("overlay row is not linked to a NetBox object")
    if not state.captured:
        raise ValueError("no captured content to materialize for this device")

    planner = _RoutePolicyGraphPlanner(state.management.device, {}, planned_at)
    group = planner._group_rows(state)
    planner.plan_rematerialize(state, state.assigned_object, group, removed_pk=-1)
    return planner.operations


def route_policy_rematerialize_plan(state):
    """Freeze one selected device version without changing the database."""
    from django.utils import timezone

    planned_at = timezone.now()
    return _mutation_plan(_rematerialize_operations(state, planned_at), planned_at)


def rematerialize_route_policy(state):
    """Make one captured device version the materialized root through an exact plan."""
    from django.utils import timezone

    planned_at = timezone.now()
    _execute_operations(_rematerialize_operations(state, planned_at), planned_at)


# ---------------------------------------------------------------------------
# Shared-object fill helpers rebuild one parent's entries with a full replace.
# ---------------------------------------------------------------------------


def _set_prefix_list_family(pl_obj, captured) -> None:
    """Mirror the owner capture's address family onto the materialized PrefixList.

    family is derived by the device reader (any v6 prefix → family 6); without this the
    netbox_routing.PrefixList kept the model default (4), so a v6 list (MARTIANS_V6,
    LGI_PREFIXES_V6, ...) displayed as IPv4 even after the reader was fixed. The dedup hash
    is entries-only, so a family-only change is a pure display correction (never drift).
    """
    family = captured.get("family")
    if family in (4, 6) and pl_obj.family != family:
        pl_obj.family = family
        pl_obj.save(update_fields=["family"])


def _fill_prefix_list(pl_obj, captured) -> None:
    """Materialize a PrefixList from a device capture: address family + entries."""
    _set_prefix_list_family(pl_obj, captured)
    _fill_prefix_list_entries(pl_obj, _entries(captured))


def _fill_prefix_list_entries(pl_obj, entries: list) -> None:
    from django.contrib.contenttypes.models import ContentType
    from netbox_routing.models import CustomPrefix, PrefixListEntry

    ct = ContentType.objects.get_for_model(CustomPrefix)
    PrefixListEntry.objects.filter(prefix_list=pl_obj).delete()
    # Resolve every CustomPrefix once (prefixes repeat across lists).
    cp_by_prefix: dict[str, object] = {}
    rows = []
    # Sequence is assigned positionally: the model field is a (signed) smallint and the
    # adapter's step-10 sequence overflows it on large lists (and Junos prefix-lists have
    # no real sequence). Positional 1-based numbering preserves order and always fits.
    seq = 0
    for e in entries:
        prefix = (e.get("prefix") or "").strip()
        if not prefix:
            continue
        cp = cp_by_prefix.get(prefix)
        if cp is None:
            try:
                cp, _ = CustomPrefix.objects.get_or_create(prefix=prefix)
            except Exception as exc:
                logger.warning("route-policy: bad prefix %r in %s: %s", prefix, pl_obj.name, exc)
                continue
            cp_by_prefix[prefix] = cp
        seq += 1
        rows.append(
            PrefixListEntry(
                prefix_list=pl_obj,
                assigned_prefix_type=ct,
                assigned_prefix_id=cp.pk,
                sequence=seq,
                action=_norm_action(e.get("action")),
                ge=e.get("ge"),
                le=e.get("le"),
            )
        )
    if rows:
        PrefixListEntry.objects.bulk_create(rows)


def _fill_as_path_entries(ap_obj, entries: list) -> None:
    from netbox_routing.models import ASPathEntry

    ASPathEntry.objects.filter(aspath=ap_obj).delete()
    # Positional sequence (smallint-safe; see _fill_prefix_list_entries).
    rows = [
        ASPathEntry(
            aspath=ap_obj,
            sequence=i,
            action=_norm_action(e.get("action")),
            pattern=(e.get("pattern") or "")[:1000],
        )
        for i, e in enumerate(entries, start=1)
    ]
    if rows:
        ASPathEntry.objects.bulk_create(rows)


def _fill_community_list_entries(cl_obj, entries: list) -> None:
    """Fill a CommunityList's members.

    The universal Community model stores every member string VERBATIM, exactly as the
    device reports it — numeric (1111:100), well-known keywords (no-export), typed extended
    (target:1111:100, color:0:128), RFC 8092 large (large:GA:L1:L2), and match-only
    regex/wildcards (1111:*, 1111:1113.). The kind is derived by parsing the text on the
    netbox_routing side; the plugin no longer routes members to parallel typed lists.
    Empty members are skipped.
    """
    from netbox_routing.models import Community, CommunityListEntry

    CommunityListEntry.objects.filter(community_list=cl_obj).delete()
    rows = []
    for e in entries:
        value = (e.get("community") or "").strip()
        if not value:
            logger.info("route-policy: skipping empty community member in %s", cl_obj.name)
            continue
        action = _norm_action(e.get("action"))
        comm, _ = Community.objects.get_or_create(community=value)
        rows.append(CommunityListEntry(community_list=cl_obj, action=action, community=comm))
    if rows:
        CommunityListEntry.objects.bulk_create(rows)


# Community literals (vs community-LIST names) when resolving a set-community by-ref:
# anything with a ':' or a well-known keyword is an inline literal, the rest is a list name.
_WELLKNOWN_COMMUNITIES = frozenset(
    {
        "no-export",
        "no-advertise",
        "no-export-subconfed",
        "local-as",
        "internet",
        "gshut",
        "accept-own",
        "none",
    }
)


def _looks_like_community_literal(name: str) -> bool:
    n = name.strip().lower()
    return ":" in n or n in _WELLKNOWN_COMMUNITIES


def _resolve_call_policy(RouteMap, name: str | None):
    """Resolve a Junos from-policy / IOS-XR apply policy name to an existing RouteMap (by-ref)."""
    if not name:
        return None
    return RouteMap.objects.filter(name__iexact=name).first()


def _materialise_set_communities(rme, structured, cl_by_name) -> list:
    """Create RouteMapEntrySetCommunity rows from the structured set-actions (R3).

    Each action targets a community-LIST by reference (resolved against the materialised
    CommunityList objects), or an inline community literal (IOS-style). Anything that
    resolves to neither — a dangling by-ref to a list the device never defined — is returned
    so the caller can preserve it in vendor_ext rather than drop it (no silent loss).
    """
    from netbox_routing.models import Community, CommunityList, RouteMapEntrySetCommunity

    unresolved: list = []
    for sc in structured.set_communities:
        cl = cl_by_name.get(sc.name) or CommunityList.objects.filter(name__iexact=sc.name).first()
        if cl is not None:
            RouteMapEntrySetCommunity.objects.create(route_map_entry=rme, operation=sc.operation, community_list=cl)
        elif _looks_like_community_literal(sc.name):
            row = RouteMapEntrySetCommunity.objects.create(route_map_entry=rme, operation=sc.operation)
            comm, _ = Community.objects.get_or_create(community=sc.name)
            row.communities.add(comm)
        else:
            unresolved.append({"operation": sc.operation, "name": sc.name})
    return unresolved


def _fill_route_map_entries(rm_obj, entries: list, pl_by_name, cl_by_name, ap_by_name) -> None:
    from netbox_routing.models import RouteMap, RouteMapEntry

    from .route_policy_structure import structure_entry

    RouteMapEntry.objects.filter(route_map=rm_obj).delete()
    # Positional sequence — unique per route-map and smallint-safe (the device sequence
    # can exceed the field's range; see _fill_prefix_list_entries).
    default_action = None
    for i, e in enumerate(entries, start=1):
        match_blob = _load_json(e.get("match"))
        set_blob = _load_json(e.get("set"))
        # Derive the structured projection: match_afi, set-community ops, call-policy,
        # vendor_ext. The full match/set blobs are kept AS-IS (authoritative for the write-side
        # round-trip until the reader/contract speak structured in P3) — the structured fields
        # are an additive, queryable/display view, not a replacement. See route_policy_structure.
        structured = structure_entry(match_blob, set_blob)
        # flow_control (IOS route-map `continue`) rides inside set-json (no dedicated adapter
        # leaf) — lift it into the model field; the push re-adds it so the round-trip is symmetric.
        set_data = dict(set_blob)
        flow_control = set_data.pop("flow_control", None)

        # default-action projection (R5): mirror the device's flagged default-action entry onto
        # RouteMap.default_action. The synthetic entry itself is KEPT so the write-side blob
        # round-trip stays byte-symmetric (the reader still synthesises it pre-contract-v2);
        # P2 hides it in favour of the field, P3 retires the synthesis.
        if structured.is_default_action:
            default_action = _norm_action(e.get("action"))

        vendor_ext = dict(structured.vendor_ext)
        rme = RouteMapEntry.objects.create(
            route_map=rm_obj,
            sequence=i,
            action=_norm_action(e.get("action")),
            flow_control=flow_control,
            match=match_blob,
            set=set_data,
            match_afi=structured.match_afi or None,
            call_policy=_resolve_call_policy(RouteMap, structured.call_policy),
        )
        for nm in e.get("match_prefix_lists") or []:
            obj = pl_by_name.get(nm)
            if obj:
                rme.match_prefix_list.add(obj)
        for nm in e.get("match_community_lists") or []:
            obj = cl_by_name.get(nm)
            if obj:
                rme.match_community_list.add(obj)
                # Devices match community-LISTS, never individual communities, so also
                # link the list's member Communities into match_community — surfacing
                # which concrete communities the route-map matches (user request).
                member_ids = obj.communitylistentries.values_list("community_id", flat=True)
                if member_ids:
                    rme.match_community.add(*[cid for cid in member_ids if cid])
            else:
                logger.debug("route-policy: route-map %s refs community-list %r not resolvable", rm_obj.name, nm)
        for nm in e.get("match_as_paths") or []:
            obj = ap_by_name.get(nm)
            if obj:
                rme.match_aspath.add(obj)
        unresolved = _materialise_set_communities(rme, structured, cl_by_name)
        if unresolved:
            vendor_ext.setdefault("unmapped", {})["set_community"] = unresolved
        # Persist vendor_ext last (it may have grown an "unmapped" note above); null when empty.
        rme.vendor_ext = vendor_ext or None
        rme.save(update_fields=["vendor_ext"])

    # default_action is device-sourced config on the shared RouteMap (full-replace each fill).
    if rm_obj.default_action != default_action:
        rm_obj.default_action = default_action
        rm_obj.save(update_fields=["default_action"])


# ---------------------------------------------------------------------------
# Family materialization specs — register route-policy into the universal
# shared_object_ownership core (route-maps/community-lists/prefix-lists/as-paths).
# Each spec knows how to rebuild its NetBox object from a device capture and how
# to hash a capture; ACL plugs in the same way later.
# ---------------------------------------------------------------------------


def _entries(captured: dict) -> list:
    return captured.get("entries") or []


def _cl_hash(captured: dict) -> str:
    """Hash a community-list capture (invert_match-aware; see _reconcile_community_lists)."""
    entries = _entries(captured)
    if bool(captured.get("invert_match", False)):
        return _hash({"invert_match": True, "entries": entries})
    return _hash(entries)


def _cl_fill(obj, captured: dict) -> None:
    invert = bool(captured.get("invert_match", False))
    if obj.invert_match != invert:
        obj.invert_match = invert
        obj.save(update_fields=["invert_match"])
    _fill_community_list_entries(obj, _entries(captured))


def _resolve_name_maps(entries: list):
    """Resolve a route-map capture's referenced object names to existing NetBox objects.

    Used when re-materializing a route-map from a device capture: the referenced
    prefix-lists / community-lists / as-paths already exist (global dedup), so look them
    up case-insensitively rather than relying on the reconcile-time in-memory maps.
    """
    from netbox_routing.models import ASPath, CommunityList, PrefixList

    pl_names: set[str] = set()
    cl_names: set[str] = set()
    ap_names: set[str] = set()
    for e in entries:
        pl_names.update(e.get("match_prefix_lists") or [])
        cl_names.update(e.get("match_community_lists") or [])
        ap_names.update(e.get("match_as_paths") or [])

    def _lookup(model, names):
        out = {}
        for nm in names:
            obj = model.objects.filter(name__iexact=nm).first()
            if obj is not None:
                out[nm] = obj
        return out

    return _lookup(PrefixList, pl_names), _lookup(CommunityList, cl_names), _lookup(ASPath, ap_names)


def _rm_fill(obj, captured: dict) -> None:
    entries = _entries(captured)
    pl_map, cl_map, ap_map = _resolve_name_maps(entries)
    _fill_route_map_entries(obj, entries, pl_map, cl_map, ap_map)


def _extract_prefix_list(pl_obj) -> dict:
    """Reverse of _fill_prefix_list: CURRENT object content in device-capture shape (#93).

    Key-compatible with the capture entries the fill consumes (prefix/action/ge/le);
    sequences are positional artifacts and are renumbered by the comparator.
    """
    entries = []
    for e in pl_obj.prefix_list_entries.all().order_by("sequence"):
        cp = e.assigned_prefix
        if cp is None:
            continue
        entry = {"sequence": e.sequence, "action": (e.action or "permit").lower(), "prefix": str(cp.prefix)}
        if getattr(e, "ge", None) is not None:
            entry["ge"] = e.ge
        if getattr(e, "le", None) is not None:
            entry["le"] = e.le
        entries.append(entry)
    return {"entries": entries}


def _extract_community_list(cl_obj) -> dict:
    """Reverse of _cl_fill: members verbatim + invert_match, capture-shaped (#93)."""
    entries = []
    seq = 0
    for e in cl_obj.communitylistentries.all():
        if not e.community_id:
            continue
        seq += 1
        entries.append(
            {"sequence": seq, "action": (e.action or "permit").lower(), "community": str(e.community.community)}
        )
    return {"entries": entries, "invert_match": bool(cl_obj.invert_match)}


def _extract_as_path(ap_obj) -> dict:
    """Reverse of _fill_as_path_entries (#93). The fill's key is ``pattern``."""
    return {
        "entries": [
            {"sequence": e.sequence, "action": (e.action or "permit").lower(), "pattern": e.pattern or ""}
            for e in ap_obj.aspath_entries.all().order_by("sequence")
        ]
    }


def _extract_route_map(rm_obj) -> dict:
    """Reverse of _rm_fill, capture-shaped (#100).

    match/set blobs are stored VERBATIM by the fill, so returning them verbatim yields an
    identical canonical_route_map projection (which drops sequences and sorts the name
    refs — M2M order is irrelevant); flow_control is re-lifted into set-json (the fill
    popped it into the model field); the synthetic default-action entry was kept by the
    fill and rides along like any entry.
    """
    entries = []
    for e in rm_obj.route_map_entries.all().order_by("sequence"):
        set_data = dict(e.set or {})
        if e.flow_control is not None and "flow_control" not in set_data:
            set_data["flow_control"] = e.flow_control
        entries.append(
            {
                "seq": e.sequence,
                "action": (e.action or "permit").lower(),
                "match": dict(e.match or {}),
                "set": set_data,
                "match_prefix_lists": sorted(e.match_prefix_list.values_list("name", flat=True)),
                "match_community_lists": sorted(e.match_community_list.values_list("name", flat=True)),
                "match_as_paths": sorted(e.match_aspath.values_list("name", flat=True)),
            }
        )
    return {"entries": entries}


def _register_specs() -> None:
    Spec = ownership.SharedObjectSpec
    ownership.register(
        "prefix_list",
        Spec(
            fill=_fill_prefix_list,
            hash_captured=lambda c: _hash(_entries(c)),
            extract=_extract_prefix_list,
            renderer_models=(
                "netbox_routing.prefixlist",
                "netbox_routing.prefixlistentry",
                "netbox_routing.customprefix",
            ),
        ),
    )
    ownership.register(
        "community_list",
        Spec(
            fill=_cl_fill,
            hash_captured=_cl_hash,
            extract=_extract_community_list,
            renderer_models=(
                "netbox_routing.communitylist",
                "netbox_routing.communitylistentry",
                "netbox_routing.community",
            ),
        ),
    )
    ownership.register(
        "as_path",
        Spec(
            fill=lambda o, c: _fill_as_path_entries(o, _entries(c)),
            hash_captured=lambda c: _hash(_entries(c)),
            extract=_extract_as_path,
            renderer_models=("netbox_routing.aspath", "netbox_routing.aspathentry"),
        ),
    )
    # Route-maps dedup on a VENDOR-NEUTRAL SEMANTIC digest (not the raw entries): the same
    # logical policy spelled in Junos vs Nokia encoding (term/terminal labels, family
    # spelling, scalar-vs-leaf-list, fall-through verb, as-path-group placement) converges
    # instead of showing false cross-vendor conflict. Prefix matches expand to their CONTENT
    # (via _resolve_prefix_list_units) so a Junos inline route-filter set converges with the
    # equivalent named-list refs. Genuine differences keep a distinct digest. See
    # route_policy_structure.canonical_route_map.
    ownership.register(
        "route_map",
        Spec(
            fill=_rm_fill,
            hash_captured=lambda c: _hash(canonical_route_map(c, _resolve_prefix_list_units)),
            extract=_extract_route_map,
            renderer_models=(
                "netbox_routing.routemap",
                "netbox_routing.routemapentry",
                "netbox_routing.routemapentrysetcommunity",
            ),
        ),
    )


_register_specs()


# ---------------------------------------------------------------------------
# MASTER vs LOCAL classification (see docs/master-vs-local-route-policy.md)
# ---------------------------------------------------------------------------


def _group_mode(family: str, object_name: str) -> str:
    """Classification for a route-policy object group: 'master' (default) or 'local'.

    Absence of a NSORoutePolicyObjectClass row == implicit MASTER (auto-dedup, the default).
    """
    from .models import NSORoutePolicyObjectClass

    # Case-insensitive to match the object dedup (name__iexact): otherwise a peer device
    # reporting a different case (ACCEPT-ALL vs accept-all — the same shared object) misses the
    # operator's stored classification and silently reverts to implicit MASTER.
    row = NSORoutePolicyObjectClass.objects.filter(family=family, object_name__iexact=object_name).first()
    return row.mode if row else "master"


def _classification_operations(family, object_name, mode, planned_at):  # noqa: PLR0915
    """Build one prospective classification and materialization graph."""
    from . import status_machine as sm
    from .models import NSORoutePolicyObjectClass, NSORoutePolicyState

    operations = _Operations()
    current_class = NSORoutePolicyObjectClass.objects.filter(
        family=family,
        object_name__iexact=object_name,
    ).first()
    if current_class is None:
        policy_class = NSORoutePolicyObjectClass(
            family=family,
            object_name=object_name,
            mode=mode,
            source="operator",
        )
        operations.save(
            policy_class,
            force_insert=True,
            natural_key=("family", "object_name"),
        )
    else:
        policy_class = copy.copy(current_class)
        policy_class.mode = mode
        policy_class.source = "operator"
        operations.save(policy_class, update_fields=("mode", "source"))

    rows = tuple(
        row
        for row in NSORoutePolicyState.objects.filter(
            family=family,
            object_name__iexact=object_name,
        )
        .select_related("management", "content_type")
        .order_by("pk")
        if row.captured
    )
    if not rows:
        return operations, policy_class

    planner = _RoutePolicyGraphPlanner(rows[0].management.device, {}, planned_at)
    planner.operations = operations
    if mode == "local":
        for row in rows:
            candidate = copy.copy(row)
            content_hash = ownership.hash_captured(family, candidate.captured or {})
            changed = candidate.content_hash != content_hash
            candidate.status = sm.on_reconcile(
                candidate.status,
                matches=not changed,
                conflict=False,
                settles_owned=False,
            )
            candidate.content_hash = content_hash
            candidate.last_sync_at = planned_at
            candidate.is_materialized = False
            candidate.content_type = None
            candidate.object_id = None
            candidate.device_present = True
            planner._plan_state(candidate, False, None)
        return operations, policy_class

    owner = max(rows, key=lambda row: len((row.captured or {}).get("entries") or []))
    root = planner.roots[family].get(object_name.casefold())
    if root is None:
        kwargs = {"name": object_name}
        if family == "prefix_list" and owner.captured.get("family") in (4, 6):
            kwargs["family"] = owner.captured["family"]
        elif family == "community_list":
            kwargs["invert_match"] = bool(owner.captured.get("invert_match", False))
        elif family == "route_map":
            kwargs["default_action"] = planner._route_map_default_action(owner.captured.get("entries") or [])
        root = planner.models[family](**kwargs)
        planner.roots[family][object_name.casefold()] = root
        operations.save(root, force_insert=True, natural_key=("name",))
    root = planner.plan_replace_root(family, root, owner.captured)
    owner_hash = ownership.hash_captured(family, owner.captured)
    for row in rows:
        candidate = copy.copy(row)
        candidate.content_type = planner.ContentType.objects.get_for_model(type(root))
        candidate.object_id = root.pk
        candidate.content_hash = ownership.hash_captured(family, candidate.captured)
        if candidate.pk == owner.pk:
            candidate.is_materialized = True
            if not sm.is_owned(candidate.status):
                candidate.status = sm.IMPORTED
        else:
            candidate.is_materialized = False
            if not sm.is_owned(candidate.status):
                candidate.status = sm.CONFLICT if candidate.content_hash != owner_hash else sm.IMPORTED
        planner._plan_state(candidate, False, root)
    return operations, policy_class


def route_policy_classification_plan(family: str, object_name: str, mode: str):
    """Freeze a classification graph without changing the database."""
    from django.utils import timezone

    if mode not in ("master", "local"):
        raise ValueError(f"invalid mode {mode!r}")
    planned_at = timezone.now()
    operations, _ = _classification_operations(family, object_name, mode, planned_at)
    return _mutation_plan(operations, planned_at)


def set_classification(family: str, object_name: str, mode: str):
    """Operator action: classify a route-policy object group MASTER or LOCAL (re-processed now).

    Re-processes the existing per-device captures so the change takes effect immediately (no
    device read). LOCAL → de-materialize every device row + clear cross-device conflicts
    (captured-only). MASTER → re-materialize an owner from the group's captures + re-compare.
    """
    from django.utils import timezone

    if mode not in ("master", "local"):
        raise ValueError(f"invalid mode {mode!r}")
    planned_at = timezone.now()
    operations, policy_class = _classification_operations(family, object_name, mode, planned_at)
    _execute_operations(operations, planned_at)
    return policy_class


def _resettle_operations(groups):
    """Build prospective saves for conflicts that now match their owner."""
    from . import status_machine as sm
    from .models import NSORoutePolicyState

    if groups is not None:
        if not groups:
            return _Operations(), 0
    else:
        groups = set(
            NSORoutePolicyState.objects.filter(status=sm.CONFLICT, is_materialized=False).values_list(
                "family", "object_name"
            )
        )
    operations = _Operations()
    cleared = 0
    for family, object_name in sorted(groups):
        canonical_hash = ownership.canonical_hash(NSORoutePolicyState, family, object_name)
        if canonical_hash is None:
            continue
        candidates = NSORoutePolicyState.objects.filter(
            family=family,
            object_name__iexact=object_name,
            status=sm.CONFLICT,
            is_materialized=False,
        )
        for state in candidates:
            if state.content_hash != canonical_hash:
                continue
            candidate = copy.copy(state)
            candidate.status = sm.on_reconcile(
                candidate.status,
                matches=True,
                conflict=False,
                settles_owned=False,
            )
            operations.save(candidate, update_fields=("status",))
            cleared += 1
    return operations, cleared


def route_policy_resettle_plan(groups: set[tuple[str, str]] | None = None):
    """Freeze stale-conflict status saves without changing the database."""
    from django.utils import timezone

    planned_at = timezone.now()
    operations, _ = _resettle_operations(groups)
    return _mutation_plan(operations, planned_at)


def resettle_false_conflicts(groups: set[tuple[str, str]] | None = None) -> int:
    """Clear stale conflict rows whose hash now equals their materialized owner's."""
    from django.utils import timezone

    planned_at = timezone.now()
    operations, cleared = _resettle_operations(groups)
    if cleared:
        _execute_operations(operations, planned_at)
    return cleared


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def reconcile_route_policy(device, payload: dict) -> list:
    """Reconcile route-policy data (objects + entries) from the adapter into NetBox.

    Runs under ``suppress_intent_push()``: this reconcile MATERIALIZES netbox-routing fork
    objects (CommunityList/RouteMap/... + their entries) from device state, and those saves
    would otherwise fire the operator-edit push handlers (own + push). Suppression keeps the
    import side-effect-free; it is reentrant, so this is safe whether or not the caller
    (reconcile_device) already suppresses. Returns NSORoutePolicyState instances for the device.
    """
    from .models import NSODeviceManagement, NSORoutePolicyState
    from .renderer_writer import active_renderer_writer, renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return []
    active = active_renderer_writer()
    plan = active.plan if active is not None else route_policy_reconcile_plan(device, payload)
    mutation = nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        operations = _route_policy_reconcile_operations(device, payload, plan.planned_at)
        _replay_operations(writer, operations)
    return list(NSORoutePolicyState.objects.filter(management=management).order_by("family", "object_name"))


def _object_referenced(obj, family) -> bool:
    """Return whether another netbox-routing object still references *obj*.

    Deleting a referenced object would break that reference, so removal keeps it instead.
    Conservative — an unrecognised family is treated as referenced (kept).
    """
    if family in ("prefix_list", "as_path"):
        return obj.route_map_entries.exists()
    if family == "community_list":
        return obj.route_map_entries.exists() or obj.set_by_route_map_entries.exists()
    if family == "route_map":
        return obj.called_by_entries.exists() or obj.applied_by_entries.exists() or obj.redistribution_entries.exists()
    return True
