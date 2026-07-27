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

logger = logging.getLogger(__name__)

# NSO emits access/trunk/trunk-all; NetBox interface modes are access/tagged/tagged-all.
_NSO_TO_NETBOX_MODE = {"access": "access", "trunk": "tagged", "trunk-all": "tagged-all"}


def _device_vlan_group(device):
    """Per-device VLAN group — the default landing spot for newly-imported vids."""
    from ipam.models import VLANGroup

    group, _ = VLANGroup.objects.get_or_create(slug=f"nso-{device.pk}", defaults={"name": f"NSO {device.name}"})
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


def rescope_vlan(state, target_group):
    """Re-scope this device's VLAN into *target_group*, keeping it synced.

    The device↔VLAN link is the ``NSOVLANState`` FK (see module docstring), so re-scoping
    is safe:

    * **move** — if *target_group* has no VLAN with this vid, just change the VLAN's group
      (the overlay FK is unchanged; reconcile keeps following it).
    * **merge** — if *target_group* already has a VLAN with this vid (a shared/site VLAN),
      re-point every reference to the device's per-device VLAN onto that shared VLAN
      (overlay, native ``Interface`` untagged/tagged, switchport overlay mirror), then delete
      the now-orphaned per-device VLAN. The vid is unchanged, so drift hashes stay equal.

    Returns ``(action, surviving_vlan)`` where action is ``moved`` / ``merged`` / ``noop``.
    """
    from dcim.models import Interface
    from ipam.models import VLAN

    from .models import NSOSwitchportState, NSOVLANState
    from .signals import suppress_intent_push

    old_vlan = state.vlan
    vid = old_vlan.vid
    if old_vlan.group_id == target_group.pk:
        return "noop", old_vlan

    existing = VLAN.objects.filter(group=target_group, vid=vid).exclude(pk=old_vlan.pk).first()

    # Re-scoping mirrors/moves objects; it is not operator intent to push back to NSO.
    with suppress_intent_push():
        if existing is None:
            old_vlan.group = target_group
            old_vlan.save(update_fields=["group"])
            return "moved", old_vlan

        Interface.objects.filter(untagged_vlan=old_vlan).update(untagged_vlan=existing)
        for iface in Interface.objects.filter(tagged_vlans=old_vlan):
            iface.tagged_vlans.remove(old_vlan)
            iface.tagged_vlans.add(existing)
        NSOSwitchportState.objects.filter(untagged_vlan=old_vlan).update(untagged_vlan=existing)
        for sp in NSOSwitchportState.objects.filter(tagged_vlans=old_vlan):
            sp.tagged_vlans.remove(old_vlan)
            sp.tagged_vlans.add(existing)

        # Re-point overlays, honouring unique_together(management, vlan): if a device already
        # tracks the shared VLAN, drop its now-duplicate per-device overlay row.
        for vs in NSOVLANState.objects.filter(vlan=old_vlan):
            if NSOVLANState.objects.filter(management=vs.management, vlan=existing).exclude(pk=vs.pk).exists():
                vs.delete()
            else:
                vs.vlan = existing
                vs.save(update_fields=["vlan"])

        old_vlan.delete()
    return "merged", existing


def reconcile_vlan_database(device, payload: dict) -> list:
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
        # Value overlay: the editable value is the VLAN name. A device with no name
        # has nothing to drift against, so treat that as a match. The unified machine
        # then settles owned→in_sync (or re-pends to accepted) and rests unowned at
        # imported (or changed on a real rename divergence).
        matches = (not name) or vlan.name == name
        state.status = sm.on_reconcile(state.status, matches=matches)
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


def reconcile_switchport(device, payload: dict) -> list:
    """Reconcile L2 switchports: seed a pristine NetBox interface from the device, else 3-way.

    Brings switchport in line with every other overlay: when the NetBox interface has no
    L2 config (pristine), the device's mode/native/tagged is *seeded* onto it (read
    mirror) → ``imported``/``in_sync``, so a freshly-imported switchport no longer shows
    as false drift. When NetBox already carries a value, a stored ``device_base_hash``
    drives a 3-way merge: device-side change auto-mirrors when the operator hasn't
    touched it, an operator edit is frozen (``changed``) and survives, both-moved →
    ``conflict``. Owned (accepted) rows keep the value-aware settle and are never
    auto-clobbered. IOS's implicit default native VLAN 1 is normalised to "no native".
    """
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
    for item in payload.get("interfaces", []) or []:
        try:
            interface = device.interfaces.get(name=item["interface_name"])
        except Exception:
            continue

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
