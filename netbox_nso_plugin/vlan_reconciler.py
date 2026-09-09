# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""reconcile the adapter VLAN-database + switchport payloads into NetBox.

VLANs are *seeded* into a per-device ``ipam.VLANGroup`` (slug ``nso-{device.pk}``)
on first import so imported vids are scoped per device (NetBox enforces
UNIQUE(group, vid) — two switches can both have VLAN 10 without colliding). After
that the device↔VLAN link is anchored on the ``NSOVLANState`` row, NOT on the
group: an operator may move a VLAN into a broader/shared group (site-wide,
shared across switches) and it stays synced with the device — reconcile follows
the overlay FK instead of recreating a duplicate in the per-device group. The
per-device group is only the default landing spot for never-before-seen vids.

Switchports compare the NSO-observed mode/untagged/tagged against the LIVE NetBox
interface to compute drift; the overlay carries status, native L2 fields stay the
source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class VLANRescopeConflict(Exception):
    """The VLAN membership changed while a rescope request waited for admission."""


# NSO emits access/trunk/trunk-all; NetBox interface modes are access/tagged/tagged-all.
_NSO_TO_NETBOX_MODE = {"access": "access", "trunk": "tagged", "trunk-all": "tagged-all"}


def _device_vlan_group(device, *, create=True):
    """Per-device VLAN group — the default landing spot for newly-imported vids."""
    from ipam.models import VLANGroup

    slug = f"nso-{device.pk}"
    if not create:
        return VLANGroup.objects.filter(slug=slug).first()
    group, _ = VLANGroup.objects.get_or_create(slug=slug, defaults={"name": f"NSO {device.name}"})
    return group


def placeholder_vlan_name(vid) -> str:
    """Return the name seeded for a device VLAN that has none.

    NetBox's (group, name) unique constraint rejects a second name='' VLAN in the same
    per-device group (live: the arcos ``vlans 5``/``6`` database, both nameless), so the
    import has to invent something. It is a NetBox-side display placeholder, NOT operator
    intent — :func:`netbox_nso_plugin.signals._push_vlan_intent_for_device` must not ship it
    to the device as a name the VLAN never had.
    """
    return f"VLAN {vid}"


def is_placeholder_vlan_name(row) -> bool:
    """Whether *row*'s NetBox VLAN name is still the import-seeded placeholder.

    True only when the DEVICE reported no name (``device_name == ""``) AND the NetBox name
    is untouched — so an operator rename is always honoured. An operator who deliberately
    types the exact placeholder string is indistinguishable from one who never renamed it;
    that ambiguity resolves to "leave the device's VLAN name alone", the direction that
    cannot write config the device never had.
    """
    return not row.device_name and row.vlan is not None and row.vlan.name == placeholder_vlan_name(row.vlan.vid)


def vlan_name_matches(row) -> bool:
    """Return whether the NetBox name matches this row's device observation."""
    return row.vlan.name == row.device_name if row.device_name else is_placeholder_vlan_name(row)


def rendered_vlan_name(row) -> str:
    """Return the VLAN name emitted by the owned-intent snapshot."""
    return "" if is_placeholder_vlan_name(row) else (row.vlan.name or "")


def _resolve_synced_vlan(management, group, vid, *, name=None, create=True):
    """Return the ipam.VLAN this device's *vid* is synced to.

    Anchor on an existing ``NSOVLANState`` for (management, vid) so a VLAN the
    operator moved to a broader/shared group stays synced and is never duplicated
    in the per-device group. Only fall back to the per-device *group* when this vid
    has never been imported for this device.

    ``create=True`` creates the per-device VLAN on that fallback (seed/write path);
    ``create=False`` returns an existing per-device VLAN or ``None`` (read mirror).
    """
    from ipam.models import VLAN

    from .models import NSOVLANState

    state = NSOVLANState.objects.filter(management=management, vlan__vid=vid).select_related("vlan").first()
    if state is not None:
        return state.vlan
    if create:
        # A nameless device VLAN seeds a unique placeholder (see placeholder_vlan_name).
        # The drift logic treats a nameless device VLAN as always-matching, so the
        # placeholder never reads back as drift.
        return VLAN.objects.get_or_create(group=group, vid=vid, defaults={"name": name or placeholder_vlan_name(vid)})[
            0
        ]
    return VLAN.objects.filter(group=group, vid=vid).first()


def vlan_reconcile_footprint(device, payload: dict):
    """Declare native VLAN rows read or written by one VLAN reconciliation."""
    from ipam.models import VLAN

    from .apply_state import vlan_ids_for_dependency_lock
    from .intent_state import MutationFootprint, SourceRow
    from .models import NSODeviceManagement, NSOVLANState

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return MutationFootprint()
    items = payload.get("vlans", []) or [] if isinstance(payload, dict) else []
    vids = vlan_ids_for_dependency_lock(items)
    group = _device_vlan_group(device, create=False)

    states = list(NSOVLANState.objects.filter(management=management))
    vlan_ids = {state.vlan_id for state in states}
    vlan_ids.update(VLAN.objects.filter(group__slug=f"nso-{device.pk}", vid__in=vids).values_list("pk", flat=True))
    slot_keys = () if group is None else (("vlan-slot", f"{group.pk}:{vid}") for vid in vids)
    return MutationFootprint.for_keys(
        {(device.pk, "vlan")},
        shared_keys=(
            *(("vlan", str(vlan_id)) for vlan_id in vlan_ids),
            *slot_keys,
        ),
        source_rows=(
            *(SourceRow("ipam.vlan", vlan_id) for vlan_id in vlan_ids),
            SourceRow("ipam.vlan", None),
        ),
        overlay_rows=(
            *(SourceRow(state._meta.label_lower, state.pk) for state in states),
            SourceRow("netbox_nso_plugin.nsovlanstate", None),
        ),
    )


def vlan_reconcile_plan(device, payload: dict):
    """Classify a VLAN refresh from its predicted canonical overlay fragments."""
    import copy

    from . import status_machine as sm
    from .intent_state import ReconcileMutationPlan, canonical_fragment
    from .models import NSODeviceManagement, NSOVLANState

    footprint = vlan_reconcile_footprint(device, payload)
    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return ReconcileMutationPlan(footprint)
    reported = {
        int(item["vlan_id"]): item.get("name") or ""
        for item in payload.get("vlans", []) or []
        if isinstance(item, dict) and item.get("vlan_id") is not None
    }
    changes_content = False
    for state in NSOVLANState.objects.filter(management=management).select_related("vlan"):
        candidate = copy.copy(state)
        if state.vlan.vid in reported:
            name = reported[state.vlan.vid]
            candidate.device_name = name
            candidate.status = sm.on_reconcile(
                state.status,
                matches=vlan_name_matches(candidate),
                settles_deploying=False,
            )
        else:
            candidate.status = sm.on_reconcile(state.status, present=False)
        if canonical_fragment(state) != canonical_fragment(candidate):
            changes_content = True
            break
    return ReconcileMutationPlan(footprint, changes_content=changes_content)


def _switchport_items(payload) -> list:
    """Return the payload's interface entries as a list of dicts."""
    items = payload.get("interfaces") or [] if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _switchport_interface_pks(device, payload: dict) -> dict:
    """Resolve the payload's interface names to pks, ONCE, before any lock is taken."""
    from dcim.models import Interface

    names = {item.get("interface_name") for item in _switchport_items(payload)}
    names = {name for name in names if isinstance(name, str)}
    return dict(Interface.objects.filter(device=device, name__in=names).values_list("name", "pk"))


@dataclass(frozen=True)
class SwitchportReconcileAttempt:
    """One switchport read: its plan, and the exact interface pks its body may write.

    The names are resolved before acquisition, so an interface that appears between the
    plan and the body is deferred to the next read instead of escaping the footprint.
    Only pks are frozen: the body reloads the rows under the lock, so a field the
    operator committed after the plan is never compared or saved from a stale copy.
    """

    plan: object
    interface_pks: dict


def prepare_switchport_reconcile(device, payload: dict) -> SwitchportReconcileAttempt:
    """Freeze the payload's interface resolutions, then plan against exactly those."""
    interface_pks = _switchport_interface_pks(device, payload)
    return SwitchportReconcileAttempt(switchport_reconcile_plan(device, payload, interface_pks), interface_pks)


def switchport_reconcile_footprint(device, payload: dict, interface_pks: dict | None = None):
    """Declare native VLAN rows read or written by one switchport reconciliation."""
    from dcim.models import Interface
    from ipam.models import VLAN

    from .apply_state import vlan_ids_for_dependency_lock
    from .intent_state import MutationFootprint, SourceRow
    from .models import NSODeviceManagement, NSOSwitchportState, NSOVLANState

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return MutationFootprint()
    items = _switchport_items(payload)
    vids = vlan_ids_for_dependency_lock(items, "untagged_vlan", "tagged_vlans")
    if interface_pks is None:
        interface_pks = _switchport_interface_pks(device, payload)
    group = _device_vlan_group(device, create=False)

    states = list(NSOSwitchportState.objects.filter(management=management).prefetch_related("tagged_vlans"))
    vlan_ids = {state.untagged_vlan_id for state in states if state.untagged_vlan_id is not None}
    vlan_ids.update(vlan.pk for state in states for vlan in state.tagged_vlans.all())
    vlan_ids.update(
        NSOVLANState.objects.filter(management=management, vlan__vid__in=vids).values_list("vlan_id", flat=True)
    )
    vlan_ids.update(VLAN.objects.filter(group__slug=f"nso-{device.pk}", vid__in=vids).values_list("pk", flat=True))
    slot_keys = () if group is None else (("vlan-slot", f"{group.pk}:{vid}") for vid in vids)
    return MutationFootprint.for_keys(
        {(device.pk, "switchport")},
        shared_keys=(
            *(("vlan", str(vlan_id)) for vlan_id in vlan_ids),
            *slot_keys,
        ),
        source_rows=(
            *(SourceRow("ipam.vlan", vlan_id) for vlan_id in vlan_ids),
            SourceRow("ipam.vlan", None),
            *(SourceRow(Interface._meta.label_lower, interface_pk) for interface_pk in interface_pks.values()),
            SourceRow(Interface._meta.get_field("tagged_vlans").remote_field.through._meta.label_lower, None),
            SourceRow(
                NSOSwitchportState._meta.get_field("tagged_vlans").remote_field.through._meta.label_lower,
                None,
            ),
        ),
        overlay_rows=(
            *(SourceRow(state._meta.label_lower, state.pk) for state in states),
            SourceRow(NSOSwitchportState._meta.label_lower, None),
        ),
    )


def switchport_reconcile_plan(device, payload: dict, interface_pks: dict | None = None):
    """Classify a switchport refresh from its predicted rendered membership."""
    import copy

    from ipam.models import VLAN

    from .intent_state import ReconcileMutationPlan, canonical_fragment
    from .models import NSODeviceManagement, NSOSwitchportState, NSOVLANState
    from .status_machine import is_owned

    footprint = switchport_reconcile_footprint(device, payload, interface_pks)
    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return ReconcileMutationPlan(footprint)
    reported = {item.get("interface_name"): item for item in _switchport_items(payload) if item.get("interface_name")}
    states = tuple(
        NSOSwitchportState.objects.filter(management=management)
        .select_related("interface", "untagged_vlan")
        .prefetch_related("tagged_vlans")
    )
    changes_content = any(state.status == "in_sync" and state.interface.name not in reported for state in states)
    group = _device_vlan_group(device, create=False)
    synced_vlans = {
        state.vlan.vid: state.vlan
        for state in NSOVLANState.objects.filter(management=management).select_related("vlan").order_by("pk")
    }
    group_vlans = (
        {vlan.vid: vlan for vlan in VLAN.objects.filter(group=group).order_by("pk")} if group is not None else {}
    )
    for state in states:
        item = reported.get(state.interface.name)
        if item is None or not is_owned(state.status):
            continue
        nso_untagged = item.get("untagged_vlan")
        if nso_untagged == 1:
            nso_untagged = None
        untagged = (
            (synced_vlans.get(nso_untagged) or group_vlans.get(nso_untagged)) if nso_untagged is not None else None
        )
        tagged = tuple(
            vlan
            for vlan in (
                synced_vlans.get(vid) or group_vlans.get(vid) for vid in sorted(item.get("tagged_vlans") or [])
            )
            if vlan is not None
        )
        before = canonical_fragment(state)
        candidate = copy.copy(state)
        candidate.mode = _NSO_TO_NETBOX_MODE.get(item.get("mode") or "", "")
        candidate.untagged_vlan = untagged
        candidate._prefetched_objects_cache = dict(state._prefetched_objects_cache)
        candidate._prefetched_objects_cache["tagged_vlans"] = tagged
        if before != canonical_fragment(candidate):
            changes_content = True
            break
    return ReconcileMutationPlan(footprint, changes_content=changes_content)


def _rescope_managed_device_ids(old_vlan) -> set[int]:
    """Return every managed device whose native or overlay rows reference a VLAN."""
    from dcim.models import Interface
    from django.db.models import Q

    from .models import NSODeviceManagement, NSOSVIState, NSOSwitchportState, NSOVLANState

    device_ids = set(NSOVLANState.objects.filter(vlan=old_vlan).values_list("management__device_id", flat=True))
    device_ids.update(
        NSOSwitchportState.objects.filter(Q(untagged_vlan=old_vlan) | Q(tagged_vlans=old_vlan)).values_list(
            "management__device_id", flat=True
        )
    )
    device_ids.update(NSOSVIState.objects.filter(vlan=old_vlan).values_list("management__device_id", flat=True))
    referenced_device_ids = Interface.objects.filter(Q(untagged_vlan=old_vlan) | Q(tagged_vlans=old_vlan)).values_list(
        "device_id", flat=True
    )
    device_ids.update(
        NSODeviceManagement.objects.filter(device_id__in=referenced_device_ids).values_list("device_id", flat=True)
    )
    return device_ids


def _validate_rescope_managed_device_ids(old_vlan, locked_device_ids) -> None:
    """Reject a managed VLAN attachment that arrived before membership locking."""
    if not _rescope_managed_device_ids(old_vlan).issubset(locked_device_ids):
        raise VLANRescopeConflict("a managed device attached to the VLAN while the rescope request waited")


def _merge_vlan_references(old_vlan, existing) -> set[tuple[int, str]]:
    """Repoint every VLAN consumer and return owned devices whose wire name changed."""
    from dcim.models import Interface

    from . import status_machine as sm
    from .models import NSOSVIState, NSOSwitchportState, NSOVLANState
    from .signals import suppress_intent_push

    push_targets = set()
    with suppress_intent_push():
        untagged_interfaces = Interface.objects.filter(untagged_vlan=old_vlan)
        if untagged_interfaces.exists():
            untagged_interfaces.update(untagged_vlan=existing)
        for interface in Interface.objects.filter(tagged_vlans=old_vlan):
            interface.tagged_vlans.remove(old_vlan)
            interface.tagged_vlans.add(existing)
        untagged_switchports = NSOSwitchportState.objects.filter(untagged_vlan=old_vlan)
        if untagged_switchports.exists():
            untagged_switchports.update(untagged_vlan=existing)
        for switchport in NSOSwitchportState.objects.filter(tagged_vlans=old_vlan):
            switchport.tagged_vlans.remove(old_vlan)
            switchport.tagged_vlans.add(existing)
        svis = NSOSVIState.objects.filter(vlan=old_vlan)
        if svis.exists():
            svis.update(vlan=existing)

        locked_states = list(
            NSOVLANState.objects.filter(vlan_id__in=(old_vlan.pk, existing.pk))
            .select_related("management", "vlan")
            .order_by("pk")
        )
        target_states = {state.management_id: state for state in locked_states if state.vlan_id == existing.pk}
        for vlan_state in (state for state in locked_states if state.vlan_id == old_vlan.pk):
            was_owned = sm.is_owned(vlan_state.status)
            source_rendered_name = rendered_vlan_name(vlan_state)
            surviving_state = target_states.get(vlan_state.management_id)
            if surviving_state is not None:
                rendered_name_changed = source_rendered_name != rendered_vlan_name(surviving_state)
                transfer_ownership = was_owned and not sm.is_owned(surviving_state.status)
                if was_owned and (rendered_name_changed or transfer_ownership):
                    surviving_state.status = (
                        "accepted" if rendered_name_changed or vlan_state.status == "deploying" else vlan_state.status
                    )
                    surviving_state.save(update_fields=["status"])
                vlan_state.delete()
            else:
                vlan_state.vlan = existing
                rendered_name_changed = source_rendered_name != rendered_vlan_name(vlan_state)
                update_fields = ["vlan"]
                if rendered_name_changed:
                    vlan_state.status = (
                        "accepted"
                        if vlan_state.status == "deploying"
                        else sm.on_reconcile(vlan_state.status, matches=False)
                    )
                    update_fields.append("status")
                vlan_state.save(update_fields=update_fields)
            if was_owned and rendered_name_changed and vlan_state.management.adapter_device_id is not None:
                push_targets.add((vlan_state.management.device_id, "vlan"))
        old_vlan.delete()
    return push_targets


def _move_vlan_to_group(vlan, target_group):
    from django.db import IntegrityError, transaction

    from .signals import suppress_intent_push

    try:
        with transaction.atomic(), suppress_intent_push():
            vlan.group = target_group
            vlan.save(update_fields=["group"])
    except IntegrityError as exc:
        raise VLANRescopeConflict("the target VLAN changed while the rescope request waited") from exc


def rescope_vlan(state, target_group):
    """Re-scope this device's VLAN into *target_group*, keeping it synced.

    The device↔VLAN link is the ``NSOVLANState`` FK (see module docstring), so re-scoping
    is safe:

    * **move** — if *target_group* has no VLAN with this vid, just change the VLAN's group
      (the overlay FK is unchanged; reconcile keeps following it).
    * **merge** — if *target_group* already has a VLAN with this vid (a shared/site VLAN),
      re-point every reference to the device's per-device VLAN onto that shared VLAN
      (overlay, native ``Interface`` untagged/tagged, switchport overlay mirror), then delete
      the now-orphaned per-device VLAN. A different surviving name re-pends owned VLAN intent.

    Returns ``(action, surviving_vlan)`` where action is ``moved`` / ``merged`` / ``noop``.
    """
    from dcim.models import Interface
    from django.db.models import Q
    from ipam.models import VLAN

    from .intent_state import MutationFootprint, SourceRow, intent_transaction, vlan_footprint
    from .models import NSOSVIState, NSOSwitchportState, NSOVLANState
    from .signals import _schedule_intent_push

    state = NSOVLANState.objects.select_related("management", "vlan").filter(pk=state.pk).first()
    if state is None:
        raise VLANRescopeConflict("the VLAN attachment no longer exists")
    old_vlan = state.vlan
    vid = old_vlan.vid
    if old_vlan.group_id == target_group.pk:
        return "noop", old_vlan
    existing = VLAN.objects.filter(group=target_group, vid=vid).exclude(pk=old_vlan.pk).first()
    source_identity = (old_vlan.vid, old_vlan.group_id)
    target_identity = (existing.vid, existing.group_id) if existing is not None else None
    managed_device_ids = _rescope_managed_device_ids(old_vlan)
    scopes = ("vlan", "svi", "switchport")
    footprints = [
        vlan_footprint(
            old_vlan.pk,
            scopes,
            extra_device_ids=managed_device_ids,
            shared_keys=(("vlan-slot", f"{target_group.pk}:{vid}"),),
        )
    ]
    if existing is not None:
        footprints.append(vlan_footprint(existing.pk, scopes, extra_device_ids=managed_device_ids))
    interface_ids = Interface.objects.filter(Q(untagged_vlan=old_vlan) | Q(tagged_vlans=old_vlan)).values_list(
        "pk", flat=True
    )
    dependencies = MutationFootprint.for_keys(
        (),
        source_rows=(
            SourceRow("ipam.vlangroup", target_group.pk),
            *(SourceRow("dcim.interface", pk) for pk in interface_ids),
            SourceRow("dcim.interface", None),
            SourceRow(Interface._meta.get_field("tagged_vlans").remote_field.through._meta.label_lower, None),
            SourceRow(
                NSOSwitchportState._meta.get_field("tagged_vlans").remote_field.through._meta.label_lower,
                None,
            ),
        ),
        overlay_rows=(
            SourceRow(state._meta.label_lower, state.pk),
            SourceRow(NSOSwitchportState._meta.label_lower, None),
            SourceRow(NSOSVIState._meta.label_lower, None),
        ),
    )
    footprint = MutationFootprint.merge(*footprints, dependencies)

    with intent_transaction(footprint):
        state = NSOVLANState.objects.select_related("management", "vlan").filter(pk=state.pk).first()
        if state is None or state.vlan_id != old_vlan.pk:
            raise VLANRescopeConflict("the VLAN attachment changed while the rescope request waited")
        target_group = type(target_group).objects.filter(pk=target_group.pk).first()
        if target_group is None:
            raise VLANRescopeConflict("the target VLAN group no longer exists")
        vlan_ids = [old_vlan.pk]
        if existing is not None:
            vlan_ids.append(existing.pk)
        _validate_rescope_managed_device_ids(old_vlan, managed_device_ids)
        locked_vlans = {vlan.pk: vlan for vlan in VLAN.objects.filter(pk__in=vlan_ids).order_by("pk")}
        if old_vlan.pk not in locked_vlans or (existing is not None and existing.pk not in locked_vlans):
            raise VLANRescopeConflict("a VLAN changed while the rescope request waited")
        if (locked_vlans[old_vlan.pk].vid, locked_vlans[old_vlan.pk].group_id) != source_identity:
            raise VLANRescopeConflict("the source VLAN changed while the rescope request waited")
        if existing is not None:
            locked_existing = locked_vlans[existing.pk]
            if (locked_existing.vid, locked_existing.group_id) != target_identity:
                raise VLANRescopeConflict("the target VLAN changed while the rescope request waited")
        old_vlan = locked_vlans[old_vlan.pk]
        existing = locked_vlans.get(existing.pk) if existing is not None else None
        # Re-scoping mirrors/moves objects; it is not operator intent to push back to NSO.
        if existing is None:
            _move_vlan_to_group(old_vlan, target_group)
            return "moved", old_vlan

        push_targets = _merge_vlan_references(old_vlan, existing)
        for target in sorted(push_targets):
            _schedule_intent_push(target)
        return "merged", existing


def reconcile_vlan_database(device, payload: dict) -> list:
    """Run VLAN reconciliation behind its complete mutation footprint."""
    from .intent_state import reconcile_transaction
    from .signals import suppress_intent_push

    with reconcile_transaction(vlan_reconcile_plan(device, payload)), suppress_intent_push():
        return _reconcile_vlan_database(device, payload)


def _reconcile_vlan_database(device, payload: dict) -> list:
    """Upsert ipam.VLAN (per-device group) + NSOVLANState from the adapter payload."""
    from django.utils import timezone

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOVLANState

    try:
        management = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    group = _device_vlan_group(device)
    now = timezone.now()
    rows: list = []
    seen_vids: set[int] = set()
    for item in payload.get("vlans", []) or []:
        vid = int(item["vlan_id"])
        seen_vids.add(vid)
        name = item.get("name") or ""
        # Seed the name on first import only. NEVER clobber it afterwards: the
        # NetBox VLAN name is operator-editable, and overwriting it back to the
        # device value would silently revert (and hide) an operator rename.
        # Anchor on the overlay FK so a VLAN moved to a broader group stays synced.
        vlan = _resolve_synced_vlan(management, group, vid, name=name)
        state, _ = NSOVLANState.objects.get_or_create(management=management, vlan=vlan)
        state.last_sync_at = now
        state.device_name = name  # mirror the device value for drift display
        # Value overlay: the editable value is the VLAN name. A nameless device VLAN
        # matches only its untouched placeholder. An operator rename remains pending.
        matches = vlan_name_matches(state)
        state.status = sm.on_reconcile(state.status, matches=matches, settles_deploying=False)
        state.save()
        rows.append(state)

    # rows the payload no longer reports → drift (anchor on the management, not the
    # group, so a VLAN moved out of the per-device group is still tracked).
    for stale in NSOVLANState.objects.filter(management=management).select_related("vlan"):
        if stale.vlan.vid not in seen_vids:
            new_status = sm.on_reconcile(stale.status, present=False)
            if new_status != stale.status:
                stale.status = new_status
                stale.save(update_fields=["status"])
    return rows


def _switchport_content(mode: str, untagged, tagged: list) -> dict:
    """Canonical L2 content (mode + native + tagged vids) for hashing/compare."""
    return {"mode": mode or "", "untagged": untagged, "tagged": sorted(tagged or [])}


def _switchport_object_content(interface) -> dict:
    """Return the live NetBox interface's L2 content."""
    nb_untagged = interface.untagged_vlan.vid if interface.untagged_vlan else None
    nb_tagged = sorted(interface.tagged_vlans.values_list("vid", flat=True))
    return _switchport_content(interface.mode or "", nb_untagged, nb_tagged)


def _switchport_is_pristine(interface) -> bool:
    """Return True if the NetBox interface carries NO operator L2 config (never materialised)."""
    return not (interface.mode or "") and interface.untagged_vlan_id is None and not interface.tagged_vlans.exists()


def _write_switchport(management, interface, group, mode: str, untagged, tagged: list) -> None:
    """Seed/mirror the device's L2 config onto the native NetBox interface.

    VLANs are resolved through the device's synced overlay first (so a moved/shared
    VLAN is referenced, not duplicated); a vid never seen on this device falls back
    to a per-device-group VLAN so the seed is faithful (the tagged set may reference
    vids not in the device's VLAN database export).
    """
    interface.mode = mode
    if untagged is not None:
        interface.untagged_vlan = _resolve_synced_vlan(management, group, untagged)
    else:
        interface.untagged_vlan = None
    interface.save()
    tagged_objs = [_resolve_synced_vlan(management, group, v) for v in (tagged or [])]
    interface.tagged_vlans.set(tagged_objs)


def reconcile_switchport(device, payload: dict, attempt: SwitchportReconcileAttempt | None = None) -> list:
    """Run switchport reconciliation behind its complete mutation footprint."""
    from .intent_state import reconcile_transaction
    from .signals import suppress_intent_push

    if attempt is None:
        attempt = prepare_switchport_reconcile(device, payload)
    with reconcile_transaction(attempt.plan), suppress_intent_push():
        return _reconcile_switchport(device, payload, attempt.interface_pks)


def _reconcile_switchport(device, payload: dict, interface_pks: dict) -> list:
    """Reconcile L2 switchports: seed a pristine NetBox interface from the device, else 3-way.

    Brings switchport in line with every other overlay: when the NetBox interface has no
    L2 config (pristine), the device's mode/native/tagged is *seeded* onto it (read
    mirror) → ``imported``/``in_sync``, so a freshly-imported switchport no longer shows
    as false drift. When NetBox already carries a value, a stored ``device_base_hash``
    drives a 3-way merge: device-side change auto-mirrors when the operator hasn't
    touched it, an operator edit is frozen (``changed``) and survives, both-moved →
    ``conflict``. Owned (accepted) rows keep the value-aware settle and are never
    auto-clobbered. IOS's implicit default native VLAN 1 is normalised to "no native".

    *interface_pks* is the frozen name→pk map of :class:`SwitchportReconcileAttempt`; a
    payload name missing from it is deferred to the next read. The rows are reloaded here,
    under the acquired lock, so the compare and the write see committed operator edits.
    """
    from dcim.models import Interface
    from django.utils import timezone

    from . import merge_util
    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOSwitchportState

    try:
        management = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    group = _device_vlan_group(device)
    now = timezone.now()
    rows: list = []
    seen: set[int] = set()
    interfaces = {
        interface.pk: interface
        for interface in Interface.objects.filter(pk__in=set(interface_pks.values())).select_related("untagged_vlan")
    }
    for item in _switchport_items(payload):
        interface = interfaces.get(interface_pks.get(item.get("interface_name")))
        if interface is None:
            continue  # not resolved before acquisition, or gone since; the next read picks it up

        nso_mode = _NSO_TO_NETBOX_MODE.get(item.get("mode") or "", "")
        nso_untagged = item.get("untagged_vlan")
        if nso_untagged == 1:
            nso_untagged = None  # IOS default native VLAN 1 is not operator intent
        nso_tagged = sorted(item.get("tagged_vlans") or [])

        state, _ = NSOSwitchportState.objects.get_or_create(management=management, interface=interface)
        state.mode = nso_mode
        state.untagged_vlan = (
            _resolve_synced_vlan(management, group, nso_untagged, create=False) if nso_untagged is not None else None
        )
        state.last_sync_at = now
        state.save()
        state.tagged_vlans.set(
            v for v in (_resolve_synced_vlan(management, group, vid, create=False) for vid in nso_tagged) if v
        )

        dev_hash = merge_util.content_hash(_switchport_content(nso_mode, nso_untagged, nso_tagged))
        obj_hash = merge_util.content_hash(_switchport_object_content(interface))

        if sm.is_owned(state.status):
            # Owned (accepted/applied): value-aware settle; never auto-clobber the operator.
            state.status = sm.on_reconcile(state.status, matches=obj_hash == dev_hash)
        elif _switchport_is_pristine(interface):
            # No NetBox value → seed the device's L2 config (read mirror) → imported/in_sync.
            _write_switchport(management, interface, group, nso_mode, nso_untagged, nso_tagged)
            state.device_base_hash = dev_hash
            state.status = sm.on_reconcile(state.status, matches=True)
        elif not state.device_base_hash:
            # NetBox already has a value, first time we 3-way-track it: adopt the base
            # without clobbering; drift if it already differs from the device.
            state.device_base_hash = dev_hash
            state.status = sm.on_reconcile(state.status, matches=obj_hash == dev_hash)
        else:
            action = merge_util.three_way(
                created=False, base=state.device_base_hash, obj_hash=obj_hash, dev_hash=dev_hash
            )
            matches, conflict = True, False
            if action == "mirror":
                _write_switchport(management, interface, group, nso_mode, nso_untagged, nso_tagged)
                state.device_base_hash = dev_hash
            elif action == "insync":
                state.device_base_hash = dev_hash
            elif action == "freeze":
                matches = False
            elif action == "conflict":
                matches, conflict = False, True
            state.status = sm.on_reconcile(state.status, matches=matches, conflict=conflict)

        state.save()
        rows.append(state)
        seen.add(interface.pk)

    _finalise_stale_switchports(management, seen)
    return rows


def _finalise_stale_switchports(management, seen: set) -> None:
    """Prune vestigial stale switchport rows; mark genuine removals ``changed``.

    A row whose interface dropped out of the device payload is vestigial when it
    is not owned and its interface carries no L2 config (e.g. an early-days seed
    onto a 'no switchport' L3 port) → delete it rather than show perpetual drift.
    An owned (accepted) row, or one whose interface still holds an operator L2
    value, is a genuine removal → keep it and mark ``changed``.
    """
    from . import status_machine as sm
    from .models import NSOSwitchportState

    for stale in NSOSwitchportState.objects.filter(management=management).select_related("interface"):
        if stale.interface_id in seen:
            continue
        sm.finalise_stale_overlay(stale, vestigial=_switchport_is_pristine(stale.interface))
