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

import contextlib
import copy
import logging
from dataclasses import dataclass

from .adapter_client import AdapterError

logger = logging.getLogger(__name__)


class VLANRescopeConflict(Exception):
    """The VLAN membership changed while a rescope request waited for admission."""


@dataclass(frozen=True)
class _ReconcileExecution:
    """The instances and result rows captured by one frozen renderer plan."""

    operations: tuple
    rows: tuple


def _rescope_plan_ready(plan):
    """Return a completed rescope plan before it enters the lock protocol."""
    return plan


# NSO emits access/trunk/trunk-all; NetBox interface modes are access/tagged/tagged-all.
_NSO_TO_NETBOX_MODE = {"access": "access", "trunk": "tagged", "trunk-all": "tagged-all"}


def _validated_vlan_id(value, field_name):
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise AdapterError(f"{field_name} must be an integer VLAN ID", code="invalid_response")
    try:
        vlan_id = int(value)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"{field_name} must be an integer VLAN ID", code="invalid_response") from exc
    if not 1 <= vlan_id <= 4094:
        raise AdapterError(f"{field_name} must be between 1 and 4094", code="invalid_response")
    return vlan_id


def _validated_vlan_items(payload: dict) -> tuple[dict, ...]:
    """Validate and normalize a complete adapter VLAN document."""
    if not isinstance(payload, dict):
        raise AdapterError("VLAN payload must be an object", code="invalid_response")
    items = payload.get("vlans", [])
    if not isinstance(items, list):
        raise AdapterError("VLAN payload vlans must be a list", code="invalid_response")
    normalized = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise AdapterError("VLAN payload entry must be an object", code="invalid_response")
        try:
            vlan_id = _validated_vlan_id(item.get("vlan_id"), "VLAN payload entry vlan_id")
        except AdapterError as exc:
            raise AdapterError(f"VLAN payload entry is invalid: {exc}", code="invalid_response") from exc
        if vlan_id in seen:
            raise AdapterError(f"VLAN payload contains duplicate vlan_id {vlan_id}", code="invalid_response")
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise AdapterError("VLAN payload entry name must be a string or null", code="invalid_response")
        seen.add(vlan_id)
        normalized.append({**item, "vlan_id": vlan_id})
    return tuple(normalized)


def _validated_switchport_items(payload: dict) -> tuple[dict, ...]:
    """Validate and normalize a complete adapter switchport document."""
    if not isinstance(payload, dict):
        raise AdapterError("switchport payload must be an object", code="invalid_response")
    items = payload.get("interfaces", [])
    if not isinstance(items, list):
        raise AdapterError("switchport payload interfaces must be a list", code="invalid_response")
    normalized = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise AdapterError("switchport payload entry must be an object", code="invalid_response")
        name = item.get("interface_name")
        if not isinstance(name, str) or not name:
            raise AdapterError(
                "switchport payload entry interface_name must be a non-empty string",
                code="invalid_response",
            )
        if name in seen:
            raise AdapterError(
                f"switchport payload contains duplicate interface_name {name}",
                code="invalid_response",
            )
        tagged = item.get("tagged_vlans") or []
        if not isinstance(tagged, list):
            raise AdapterError("switchport payload entry tagged_vlans must be a list", code="invalid_response")
        tagged = [_validated_vlan_id(value, "tagged_vlans entry") for value in tagged]
        if len(tagged) != len(set(tagged)):
            raise AdapterError(
                f"switchport payload entry {name} contains duplicate tagged VLANs",
                code="invalid_response",
            )
        untagged = item.get("untagged_vlan")
        if untagged is not None:
            untagged = _validated_vlan_id(untagged, "untagged_vlan")
        mode = item.get("mode")
        if mode is not None and not isinstance(mode, str):
            raise AdapterError("switchport payload entry mode must be a string or null", code="invalid_response")
        seen.add(name)
        normalized.append({**item, "untagged_vlan": untagged, "tagged_vlans": tagged})
    return tuple(normalized)


def _device_vlan_group_lock(device):
    from .intent_state import MutationFootprint

    return MutationFootprint.for_keys((), shared_keys=(("vlan-group", f"nso-{device.pk}"),))


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
    """Freeze every native VLAN and overlay write before the first lock."""
    from django.utils import timezone

    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    saves, operations, reported_rows = _vlan_reconcile_operations(device, payload, planned_at)
    return RendererMutationPlan.build(
        saves=saves,
        planned_at=planned_at,
        additional_footprints=(_device_vlan_group_lock(device),),
        execution=_ReconcileExecution(tuple(operations), tuple(reported_rows)),
    )


def _vlan_reconcile_operations(device, payload, planned_at):
    """Build the deterministic VLAN writes shared by preflight and apply."""
    from ipam.models import VLAN, VLANGroup

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOVLANState
    from .renderer_writer import planned_save

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return [], [], []
    group = _device_vlan_group(device, create=False)
    states = list(NSOVLANState.objects.filter(management=management).select_related("vlan").order_by("pk"))
    states_by_vid = {}
    for state in states:
        states_by_vid.setdefault(state.vlan.vid, state)
    group_vlans = (
        {vlan.vid: vlan for vlan in VLAN.objects.filter(group=group).order_by("pk")} if group is not None else {}
    )
    saves = []
    operations = []
    reported_rows = []
    seen_vids = set()

    def ensure_group():
        nonlocal group
        if group is not None:
            return group
        group = VLANGroup(name=f"NSO {device.name}", slug=f"nso-{device.pk}")
        saves.append(planned_save(group, force_insert=True, natural_key=("slug",)))
        operations.append((group, None, True))
        return group

    for item in _validated_vlan_items(payload):
        vid = item["vlan_id"]
        seen_vids.add(vid)
        name = item.get("name") or ""
        current = states_by_vid.get(vid)
        vlan = current.vlan if current is not None else group_vlans.get(vid)
        if vlan is None:
            vlan = VLAN(group=ensure_group(), vid=vid, name=name or placeholder_vlan_name(vid))
            group_vlans[vid] = vlan
            proposal = planned_save(
                vlan,
                force_insert=True,
                natural_key=("group", "vid"),
            )
            saves.append(proposal)
            operations.append((vlan, None, True))

        state = (
            copy.copy(current)
            if current is not None
            else NSOVLANState(management=management, vlan=vlan, status="unknown")
        )
        state.last_sync_at = planned_at
        state.device_name = name
        state.status = sm.on_reconcile(
            state.status,
            matches=vlan_name_matches(state),
            settles_deploying=False,
        )
        created = current is None
        proposal = planned_save(
            state,
            force_insert=created,
            natural_key=("management", "vlan"),
        )
        saves.append(proposal)
        operations.append((state, None, created))
        reported_rows.append(state)

    for stale in states:
        if stale.vlan.vid in seen_vids:
            continue
        new_status = sm.on_reconcile(stale.status, present=False)
        if new_status == stale.status:
            continue
        candidate = copy.copy(stale)
        candidate.status = new_status
        fields = ("status",)
        saves.append(planned_save(candidate, update_fields=fields))
        operations.append((candidate, fields, False))

    return saves, operations, reported_rows


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
    """Freeze every native, overlay, and M2M switchport write."""
    from django.utils import timezone

    from .renderer_writer import RendererMutationPlan

    if interface_pks is None:
        interface_pks = _switchport_interface_pks(device, payload)
    planned_at = timezone.now()
    saves, deletes, m2m_writes, operations, rows = _switchport_reconcile_operations(
        device,
        payload,
        planned_at,
        interface_pks,
    )
    return RendererMutationPlan.build(
        saves=saves,
        deletes=deletes,
        m2m_writes=m2m_writes,
        planned_at=planned_at,
        additional_footprints=(_device_vlan_group_lock(device),),
        execution=_ReconcileExecution(tuple(operations), tuple(rows)),
    )


def _switchport_reconcile_operations(device, payload, planned_at, interface_pks):  # noqa: C901
    """Build the deterministic switchport writes shared by preflight and apply.

    *interface_pks* is the name-to-pk map frozen before acquisition; the rows are reloaded
    here, so the apply-time build (under the lock) compares and writes committed values.
    """
    from dcim.models import Interface
    from django.db.models import Prefetch
    from ipam.models import VLAN, VLANGroup

    from . import merge_util
    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOSwitchportState, NSOVLANState
    from .renderer_writer import planned_delete, planned_m2m_set, planned_save

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return [], [], [], [], []
    items = _validated_switchport_items(payload)
    group = _device_vlan_group(device, create=False)
    tagged_vlans = Prefetch(
        "tagged_vlans",
        queryset=VLAN.objects.order_by("pk"),
        to_attr="_intent_tagged_vlans",
    )
    interfaces = {
        row.pk: row
        for row in Interface.objects.filter(pk__in=set(interface_pks.values()))
        .select_related("untagged_vlan")
        .prefetch_related(tagged_vlans)
        .order_by("pk")
    }
    states = {
        row.interface_id: row
        for row in NSOSwitchportState.objects.filter(management=management)
        .select_related("interface", "untagged_vlan")
        .prefetch_related(tagged_vlans)
        .order_by("pk")
    }
    synced_vlans = {
        row.vlan.vid: row.vlan
        for row in NSOVLANState.objects.filter(management=management).select_related("vlan").order_by("pk")
    }
    group_vlans = {row.vid: row for row in VLAN.objects.filter(group=group).order_by("pk")} if group is not None else {}
    group_saves = []
    vlan_saves = []
    native_saves = []
    state_saves = []
    deletes = []
    m2m_writes = []
    vlan_operations = []
    native_operations = []
    state_operations = []
    m2m_operations = []
    delete_operations = []
    group_operations = []
    rows = []
    seen = set()

    def ensure_group():
        nonlocal group
        if group is not None:
            return group
        group = VLANGroup(name=f"NSO {device.name}", slug=f"nso-{device.pk}")
        group_saves.append(planned_save(group, force_insert=True, natural_key=("slug",)))
        group_operations.append(("save", group, None, True, None, ()))
        return group

    def resolve_vlan(vid, *, create):
        vlan = synced_vlans.get(vid) or group_vlans.get(vid)
        if vlan is not None or not create:
            return vlan
        vlan = VLAN(group=ensure_group(), vid=vid, name=placeholder_vlan_name(vid))
        group_vlans[vid] = vlan
        vlan_saves.append(planned_save(vlan, force_insert=True, natural_key=("group", "vid")))
        vlan_operations.append(("save", vlan, None, True, None, ()))
        return vlan

    for item in items:
        interface = interfaces.get(interface_pks.get(item.get("interface_name")))
        if interface is None:
            continue  # not resolved before acquisition, or gone since; the next read picks it up
        nso_mode = _NSO_TO_NETBOX_MODE.get(item.get("mode") or "", "")
        nso_untagged = item.get("untagged_vlan")
        if nso_untagged == 1:
            nso_untagged = None
        nso_tagged = sorted(item.get("tagged_vlans") or [])
        current = states.get(interface.pk)
        state = (
            copy.copy(current)
            if current is not None
            else NSOSwitchportState(management=management, interface=interface, status="unknown")
        )
        state.mode = nso_mode
        state.last_sync_at = planned_at
        dev_hash = merge_util.content_hash(_switchport_content(nso_mode, nso_untagged, nso_tagged))
        obj_hash = merge_util.content_hash(_switchport_object_content(interface))
        native_candidate = None
        native_tagged = None

        if sm.is_owned(state.status):
            state.status = sm.on_reconcile(state.status, matches=obj_hash == dev_hash)
        elif _switchport_is_pristine(interface):
            native_candidate = copy.copy(interface)
            native_candidate.mode = nso_mode
            native_candidate.untagged_vlan = (
                resolve_vlan(nso_untagged, create=True) if nso_untagged is not None else None
            )
            native_tagged = tuple(resolve_vlan(vid, create=True) for vid in nso_tagged)
            state.device_base_hash = dev_hash
            state.status = sm.on_reconcile(state.status, matches=True)
        elif not state.device_base_hash:
            state.device_base_hash = dev_hash
            state.status = sm.on_reconcile(state.status, matches=obj_hash == dev_hash)
        else:
            action = merge_util.three_way(
                created=False,
                base=state.device_base_hash,
                obj_hash=obj_hash,
                dev_hash=dev_hash,
            )
            matches, conflict = True, False
            if action == "mirror":
                native_candidate = copy.copy(interface)
                native_candidate.mode = nso_mode
                native_candidate.untagged_vlan = (
                    resolve_vlan(nso_untagged, create=True) if nso_untagged is not None else None
                )
                native_tagged = tuple(resolve_vlan(vid, create=True) for vid in nso_tagged)
                state.device_base_hash = dev_hash
            elif action == "insync":
                state.device_base_hash = dev_hash
            elif action == "freeze":
                matches = False
            elif action == "conflict":
                matches, conflict = False, True
            state.status = sm.on_reconcile(state.status, matches=matches, conflict=conflict)

        state.untagged_vlan = resolve_vlan(nso_untagged, create=False) if nso_untagged is not None else None
        state_tagged = tuple(
            vlan for vlan in (resolve_vlan(vid, create=False) for vid in nso_tagged) if vlan is not None
        )

        if native_candidate is not None:
            fields = ("mode", "untagged_vlan")
            native_saves.append(planned_save(native_candidate, update_fields=fields))
            native_operations.append(("save", native_candidate, fields, False, None, ()))
            current_tagged = _ordered_tagged_vlans(interface)
            if {row.pk for row in current_tagged} != {row.pk for row in native_tagged if row.pk is not None} or any(
                row.pk is None for row in native_tagged
            ):
                m2m_writes.append(planned_m2m_set(native_candidate, "tagged_vlans", native_tagged))
                m2m_operations.append(("m2m_set", native_candidate, None, False, "tagged_vlans", native_tagged))

        created = current is None
        update_fields = None if created else ("mode", "untagged_vlan", "last_sync_at", "device_base_hash", "status")
        state_saves.append(
            planned_save(
                state,
                update_fields=update_fields,
                force_insert=created,
                natural_key=("management", "interface"),
            )
        )
        state_operations.append(("save", state, update_fields, created, None, ()))
        current_state_tagged = _ordered_tagged_vlans(current) if current is not None else ()
        if {row.pk for row in current_state_tagged} != {row.pk for row in state_tagged}:
            m2m_writes.append(planned_m2m_set(state, "tagged_vlans", state_tagged))
            m2m_operations.append(("m2m_set", state, None, False, "tagged_vlans", state_tagged))
        rows.append(state)
        seen.add(interface.pk)

    for stale in states.values():
        if stale.interface_id in seen:
            continue
        if not sm.is_owned(stale.status) and _switchport_is_pristine(stale.interface):
            deletes.append(planned_delete(stale))
            delete_operations.append(("delete", stale, None, False, None, ()))
            continue
        new_status = sm.on_reconcile(stale.status, present=False)
        if new_status == stale.status:
            continue
        candidate = copy.copy(stale)
        candidate.status = new_status
        fields = ("status",)
        state_saves.append(planned_save(candidate, update_fields=fields))
        state_operations.append(("save", candidate, fields, False, None, ()))

    saves = (*group_saves, *vlan_saves, *native_saves, *state_saves)
    operations = (
        *group_operations,
        *vlan_operations,
        *native_operations,
        *state_operations,
        *m2m_operations,
        *delete_operations,
    )
    return saves, deletes, m2m_writes, operations, rows


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


def _vlan_repoint_plan(old_vlan, target_vlan):  # noqa: C901
    """Freeze all native and overlay references moved by one VLAN merge."""
    from dcim.models import Interface
    from django.db.models import Q

    from . import status_machine as sm
    from .models import NSOSVIState, NSOSwitchportState, NSOVLANState
    from .renderer_writer import RendererMutationPlan, planned_delete, planned_m2m_set, planned_save

    saves = []
    deletes = []
    m2m_writes = []
    save_operations = []
    m2m_operations = []
    delete_operations = []
    push_targets = set()
    device_ids = set()

    for interface in (
        Interface.objects.filter(Q(untagged_vlan=old_vlan) | Q(tagged_vlans=old_vlan)).distinct().order_by("pk")
    ):
        device_ids.add(interface.device_id)
        if interface.untagged_vlan_id == old_vlan.pk:
            candidate = copy.copy(interface)
            candidate.untagged_vlan = target_vlan
            fields = ("untagged_vlan",)
            saves.append(planned_save(candidate, update_fields=fields))
            save_operations.append((candidate, fields))
        if interface.tagged_vlans.filter(pk=old_vlan.pk).exists():
            tagged = tuple(
                dict.fromkeys(
                    target_vlan if row.pk == old_vlan.pk else row for row in interface.tagged_vlans.order_by("pk")
                )
            )
            m2m_writes.append(planned_m2m_set(interface, "tagged_vlans", tagged))
            m2m_operations.append((interface, "tagged_vlans", tagged))

    switchports = (
        NSOSwitchportState.objects.filter(Q(untagged_vlan=old_vlan) | Q(tagged_vlans=old_vlan))
        .select_related("management", "interface")
        .prefetch_related("tagged_vlans")
        .distinct()
        .order_by("pk")
    )
    for switchport in switchports:
        device_ids.add(switchport.management.device_id)
        if switchport.untagged_vlan_id == old_vlan.pk:
            candidate = copy.copy(switchport)
            candidate.untagged_vlan = target_vlan
            fields = ("untagged_vlan",)
            saves.append(planned_save(candidate, update_fields=fields))
            save_operations.append((candidate, fields))
        if switchport.tagged_vlans.filter(pk=old_vlan.pk).exists():
            tagged = tuple(
                dict.fromkeys(
                    target_vlan if row.pk == old_vlan.pk else row for row in switchport.tagged_vlans.order_by("pk")
                )
            )
            m2m_writes.append(planned_m2m_set(switchport, "tagged_vlans", tagged))
            m2m_operations.append((switchport, "tagged_vlans", tagged))

    for svi in NSOSVIState.objects.filter(vlan=old_vlan).select_related("management").order_by("pk"):
        device_ids.add(svi.management.device_id)
        candidate = copy.copy(svi)
        candidate.vlan = target_vlan
        fields = ("vlan",)
        saves.append(planned_save(candidate, update_fields=fields))
        save_operations.append((candidate, fields))

    states = list(
        NSOVLANState.objects.filter(vlan_id__in=(old_vlan.pk, target_vlan.pk))
        .select_related("management", "vlan")
        .order_by("pk")
    )
    target_states = {state.management_id: state for state in states if state.vlan_id == target_vlan.pk}
    for source in (state for state in states if state.vlan_id == old_vlan.pk):
        device_ids.add(source.management.device_id)
        was_owned = sm.is_owned(source.status)
        source_rendered_name = rendered_vlan_name(source)
        survivor = target_states.get(source.management_id)
        if survivor is not None:
            rendered_name_changed = source_rendered_name != rendered_vlan_name(survivor)
            transfer_ownership = was_owned and not sm.is_owned(survivor.status)
            if was_owned and (rendered_name_changed or transfer_ownership):
                candidate = copy.copy(survivor)
                candidate.status = (
                    "accepted" if rendered_name_changed or source.status == "deploying" else source.status
                )
                fields = ("status",)
                saves.append(planned_save(candidate, update_fields=fields))
                save_operations.append((candidate, fields))
            deletes.append(planned_delete(source))
            delete_operations.append(source)
        else:
            candidate = copy.copy(source)
            candidate.vlan = target_vlan
            rendered_name_changed = source_rendered_name != rendered_vlan_name(candidate)
            fields = ["vlan"]
            if rendered_name_changed:
                candidate.status = (
                    "accepted" if source.status == "deploying" else sm.on_reconcile(source.status, matches=False)
                )
                fields.append("status")
            fields = tuple(fields)
            saves.append(planned_save(candidate, update_fields=fields))
            save_operations.append((candidate, fields))
        if was_owned and rendered_name_changed and source.management.adapter_device_id is not None:
            push_targets.add((source.management.device_id, "vlan"))

    plan = RendererMutationPlan.build(saves=saves, deletes=deletes, m2m_writes=m2m_writes)
    return plan, save_operations, m2m_operations, delete_operations, push_targets, device_ids


def rescope_vlan(state, target_group, *, _retry_on_stale=True):
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
    from django.db import IntegrityError, transaction
    from ipam.models import VLAN

    from .intent_state import IntentMutationProtocolError
    from .models import NSOVLANState
    from .ownership_planner import retire_manifest_identity
    from .renderer_writer import (
        IntentPlanStaleError,
        RendererMutationPlan,
        planned_delete,
        planned_save,
        renderer_mirror_writes,
        renderer_writes,
    )
    from .signals import _schedule_intent_push, suppress_intent_push

    state = NSOVLANState.objects.select_related("management", "vlan").filter(pk=state.pk).first()
    if state is None:
        raise VLANRescopeConflict("the VLAN attachment no longer exists")
    old_vlan = state.vlan
    vid = old_vlan.vid
    if old_vlan.group_id == target_group.pk:
        return "noop", old_vlan
    existing = VLAN.objects.filter(group=target_group, vid=vid).exclude(pk=old_vlan.pk).first()
    old_key = {"group_id": old_vlan.group_id, "vid": old_vlan.vid}
    source_identity = (old_vlan.vid, old_vlan.group_id)
    target_identity = (existing.vid, existing.group_id) if existing is not None else None
    managed_device_ids = _rescope_managed_device_ids(old_vlan)
    with transaction.atomic():
        try:
            if existing is None:
                candidate = copy.copy(old_vlan)
                candidate.group = target_group
                attached = list(NSOVLANState.objects.filter(vlan=old_vlan).order_by("pk"))
                overlay_candidates = []
                saves = [planned_save(candidate, update_fields=("group",))]
                for attached_state in attached:
                    overlay = copy.copy(attached_state)
                    overlay.vlan = candidate
                    overlay_candidates.append(overlay)
                    saves.append(planned_save(overlay, update_fields=("status",)))
                plan = _rescope_plan_ready(RendererMutationPlan.build(saves=saves))
                with renderer_mirror_writes(plan) as writer, suppress_intent_push():
                    current_identity = VLAN.objects.filter(pk=old_vlan.pk).values_list("vid", "group_id").first()
                    if current_identity != source_identity:
                        raise IntentPlanStaleError("the source VLAN identity changed after planning")
                    writer.save(candidate, update_fields=("group",))
                    for overlay in overlay_candidates:
                        writer.save(overlay, update_fields=("status",))
                device_ids = {attached_state.management.device_id for attached_state in attached}
                retire_manifest_identity(
                    device_ids=device_ids,
                    scope="vlan",
                    native_model_label="ipam.vlan",
                    native_key=old_key,
                )
                return "moved", candidate

            plan, saves, m2m_sets, deletes, push_targets, device_ids = _vlan_repoint_plan(old_vlan, existing)
            plan = _rescope_plan_ready(plan)
            mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
            with mutation as writer:
                _validate_rescope_managed_device_ids(old_vlan, managed_device_ids)
                current_source = VLAN.objects.filter(pk=old_vlan.pk).values_list("vid", "group_id").first()
                current_target = VLAN.objects.filter(pk=existing.pk).values_list("vid", "group_id").first()
                current_state_vlan = NSOVLANState.objects.filter(pk=state.pk).values_list("vlan_id", flat=True).first()
                if (
                    current_source != source_identity
                    or current_target != target_identity
                    or current_state_vlan != old_vlan.pk
                ):
                    raise IntentPlanStaleError("the VLAN membership changed after planning")
                with suppress_intent_push():
                    for candidate, fields in saves:
                        writer.save(candidate, update_fields=fields)
                    for owner, field_name, related in m2m_sets:
                        writer.m2m_set(owner, field_name, related)
                    for overlay in deletes:
                        writer.delete(overlay)
                for target in sorted(push_targets):
                    _schedule_intent_push(target)
            retire_manifest_identity(
                device_ids=device_ids,
                scope="vlan",
                native_model_label="ipam.vlan",
                native_key=old_key,
            )
            delete_plan = RendererMutationPlan.build(deletes=(planned_delete(old_vlan),))
            delete_mutation = (
                renderer_writes(delete_plan) if delete_plan.changes_content else renderer_mirror_writes(delete_plan)
            )
            with delete_mutation as writer, suppress_intent_push():
                writer.delete(old_vlan)
            return "merged", existing
        except IntegrityError as exc:
            raise VLANRescopeConflict("the VLAN membership changed while the rescope request waited") from exc
        except IntentMutationProtocolError as exc:
            retryable = isinstance(exc, IntentPlanStaleError)
            if not _retry_on_stale or not retryable:
                raise VLANRescopeConflict("the VLAN membership changed while the rescope request waited") from exc
            transaction.set_rollback(True)
    return rescope_vlan(state, target_group, _retry_on_stale=False)


def reconcile_vlan_database(device, payload: dict) -> list:
    """Apply one frozen VLAN reconciliation through the renderer writer."""
    from .renderer_writer import active_renderer_writer, renderer_writes_replanning_once
    from .signals import suppress_intent_push

    active = active_renderer_writer()
    plan = active.plan if active is not None else vlan_reconcile_plan(device, payload)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        return _reconcile_vlan_database(device, payload, writer, plan)


def _sync_cached_foreign_keys(instance):
    """Refresh FK IDs whose related rows were created earlier in this plan."""
    for field in instance._meta.concrete_fields:
        if not field.many_to_one or not field.is_cached(instance):
            continue
        related = field.get_cached_value(instance)
        if related is not None and related.pk is not None:
            setattr(instance, field.attname, related.pk)


def _save_reconcile_instance(writer, instance, update_fields, force_insert):
    """Save one frozen step, adopting an identical raced device VLAN group."""
    from .renderer_writer import IntentPlanStaleError

    if force_insert and instance._meta.label_lower == "ipam.vlangroup":
        matches = list(type(instance).objects.filter(slug=instance.slug).order_by("pk")[:2])
        if len(matches) > 1:
            raise IntentPlanStaleError(f"multiple VLAN groups use planned slug {instance.slug!r}")
        if matches:
            existing = matches[0]
            if not writer.consume_existing_creation(existing):
                raise IntentPlanStaleError(f"VLAN group creation {instance.slug!r} changed after planning")
            instance.pk = existing.pk
            instance._state.adding = False
            instance._state.db = existing._state.db
            return
    _sync_cached_foreign_keys(instance)
    writer.save(instance, update_fields=update_fields, force_insert=force_insert)


def _reconcile_vlan_database(device, payload: dict, writer, plan) -> list:
    """Execute the VLAN operations after their exact write set is frozen."""
    _validated_vlan_items(payload)
    execution = plan.execution
    if not isinstance(execution, _ReconcileExecution):
        raise ValueError("VLAN reconciliation requires its frozen execution steps")
    operations = execution.operations
    for instance, update_fields, force_insert in operations:
        _save_reconcile_instance(writer, instance, update_fields, force_insert)
    return list(execution.rows)


def save_vlan_content(vlan, *, update_fields):
    """Apply one operator VLAN name or VID edit through an exact family plan."""
    from . import status_machine as sm
    from .apply_state import vlan_intent_targets
    from .renderer_writer import RendererMutationPlan, planned_save, renderer_mirror_writes, renderer_writes

    stored = type(vlan).objects.filter(pk=vlan.pk).first()
    if stored is None:
        raise VLANRescopeConflict("the VLAN no longer exists")
    update_fields = set(update_fields)
    changed_fields = {
        field_name for field_name in update_fields if getattr(stored, field_name) != getattr(vlan, field_name)
    }
    if not changed_fields:
        return stored
    candidate = copy.copy(stored)
    for field_name in changed_fields:
        setattr(candidate, field_name, getattr(vlan, field_name))
    scopes = ("vlan", "svi", "switchport") if "vid" in changed_fields else ("vlan",)
    _device_ids, rows = vlan_intent_targets(stored.pk, scopes)
    if (
        "vid" in changed_fields
        and "name" not in changed_fields
        and rows.get("vlan")
        and stored.name == placeholder_vlan_name(stored.vid)
        and all(not state.device_name for state in rows["vlan"])
    ):
        derived_name = placeholder_vlan_name(candidate.vid)
        name_taken = (
            stored.group_id is not None
            and type(stored).objects.filter(group_id=stored.group_id, name=derived_name).exclude(pk=stored.pk).exists()
        ) or (
            stored.qinq_svlan_id is not None
            and type(stored)
            .objects.filter(qinq_svlan_id=stored.qinq_svlan_id, name=derived_name)
            .exclude(pk=stored.pk)
            .exists()
        )
        if not name_taken:
            candidate.name = derived_name
            changed_fields.add("name")
    state_candidates = []
    vid_changed = "vid" in changed_fields
    for scope, states in rows.items():
        if scope in ("svi", "switchport") and not vid_changed:
            continue
        for state in states:
            state_candidate = copy.copy(state)
            if scope == "vlan" and not vid_changed:
                state_candidate.vlan = candidate
                matches = vlan_name_matches(state_candidate)
            else:
                matches = False
            state_candidate.status = (
                "accepted" if state.status == "deploying" else sm.on_reconcile(state.status, matches=matches)
            )
            if state_candidate.status != state.status:
                update_fields = {"status"}
                if state.status == "deploying" and any(
                    field.name == "apply_attempt_id" for field in state_candidate._meta.concrete_fields
                ):
                    state_candidate.apply_attempt_id = None
                    update_fields.add("apply_attempt_id")
                state_candidates.append((state_candidate, update_fields))
    saves = [planned_save(candidate, update_fields=changed_fields)]
    saves.extend(planned_save(state, update_fields=fields) for state, fields in state_candidates)
    plan = RendererMutationPlan.build(saves=saves)
    mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer:
        writer.save(candidate, update_fields=changed_fields)
        for state_candidate, fields in state_candidates:
            writer.save(state_candidate, update_fields=fields)
    return candidate


def _switchport_content(mode: str, untagged, tagged: list) -> dict:
    """Canonical L2 content (mode + native + tagged vids) for hashing/compare."""
    return {"mode": mode or "", "untagged": untagged, "tagged": sorted(tagged or [])}


def _ordered_tagged_vlans(instance) -> tuple:
    """Return tagged VLANs in stable order, using the planning prefetch when present."""
    prefetched = getattr(instance, "_intent_tagged_vlans", None)
    if prefetched is not None:
        return tuple(prefetched)
    return tuple(instance.tagged_vlans.order_by("pk"))


def _switchport_object_content(interface) -> dict:
    """Return the live NetBox interface's L2 content."""
    nb_untagged = interface.untagged_vlan.vid if interface.untagged_vlan else None
    nb_tagged = sorted(vlan.vid for vlan in _ordered_tagged_vlans(interface))
    return _switchport_content(interface.mode or "", nb_untagged, nb_tagged)


def _switchport_is_pristine(interface) -> bool:
    """Return True if the NetBox interface carries NO operator L2 config (never materialised)."""
    return not (interface.mode or "") and interface.untagged_vlan_id is None and not _ordered_tagged_vlans(interface)


def reconcile_switchport(device, payload: dict, attempt: SwitchportReconcileAttempt | None = None) -> list:
    """Apply one frozen switchport reconciliation through the renderer writer."""
    from .renderer_writer import active_renderer_writer, renderer_writes_replanning_once
    from .signals import suppress_intent_push

    if attempt is None:
        attempt = prepare_switchport_reconcile(device, payload)
    active = active_renderer_writer()
    if active is not None:
        with contextlib.nullcontext(active) as writer, suppress_intent_push():
            return _reconcile_switchport(device, payload, writer, active.plan)

    served = False

    def plan_fn():
        # The replan runs INSIDE the acquisition, so it re-reads the frozen pks only: names
        # are never re-resolved and an interface that appeared since waits for the next read.
        nonlocal served
        if not served:
            served = True
            return attempt.plan
        return switchport_reconcile_plan(device, payload, attempt.interface_pks)

    with renderer_writes_replanning_once(plan_fn) as (writer, plan), suppress_intent_push():
        return _reconcile_switchport(device, payload, writer, plan)


def _reconcile_switchport(device, payload: dict, writer, plan) -> list:
    """Execute the switchport operations after their write set is frozen.

    The plan froze the interface pks resolved before acquisition and reloaded exactly those
    rows under the lock, so no row outside the declared footprint is written and no pre-lock
    copy is compared or saved.
    """
    _validated_switchport_items(payload)
    execution = plan.execution
    if not isinstance(execution, _ReconcileExecution):
        raise ValueError("switchport reconciliation requires its frozen execution steps")
    operations = execution.operations
    for operation, instance, update_fields, force_insert, field_name, related in operations:
        if operation == "save":
            _save_reconcile_instance(writer, instance, update_fields, force_insert)
        elif operation == "delete":
            writer.delete(instance)
        else:
            _sync_cached_foreign_keys(instance)
            for related_instance in related:
                _sync_cached_foreign_keys(related_instance)
            writer.m2m_set(instance, field_name, related)
    return list(execution.rows)
