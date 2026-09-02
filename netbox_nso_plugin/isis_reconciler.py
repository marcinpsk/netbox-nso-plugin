# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Reconcile the complete IS-IS graph through one exact mutation plan."""

from __future__ import annotations

import contextlib
import copy
import logging

logger = logging.getLogger(__name__)


class _Operations:
    """Collect proposed writes and their deterministic replay data."""

    def __init__(self):
        self.saves = []
        self.deletes = []
        self.operations = []

    def save(
        self,
        instance,
        *,
        update_fields=None,
        force_insert=False,
        natural_key=(),
        references=(),
    ):
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
        self.operations.append(("save", instance, update_fields, force_insert, references))

    def delete(self, instance):
        from .renderer_writer import planned_delete

        self.deletes.append(planned_delete(instance))
        self.operations.append(("delete", instance, None, False, ()))

    def extend(self, other):
        self.saves.extend(other.saves)
        self.deletes.extend(other.deletes)
        self.operations.extend(other.operations)


def isis_reconcile_plan(device, payload):
    """Freeze every native, child, and overlay IS-IS write before reconciliation."""
    from django.utils import timezone

    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    try:
        operations, _dropped = _isis_reconcile_operations(device, payload, planned_at)
    except ImportError:
        return RendererMutationPlan.build(planned_at=planned_at)
    return RendererMutationPlan.build(
        saves=operations.saves,
        deletes=operations.deletes,
        planned_at=planned_at,
    )


def _copy_or_new(current, model, **values):
    if current is None:
        return model(**values), True
    return copy.copy(current), False


def _values_differ(_field_name, current, desired):
    return current != desired


def _changed_fields(instance, values, comparator=_values_differ):
    fields = []
    for name, value in values.items():
        if comparator(name, getattr(instance, name), value):
            setattr(instance, name, value)
            fields.append(name)
    return tuple(fields)


def _srv6_locator_values_differ(field_name, current, desired):
    if field_name == "prefix":
        return str(current) != str(desired)
    return current != desired


def _settings_plan(parent, settings):
    from django.contrib.contenttypes.models import ContentType
    from netbox_routing.choices import ISISSettingChoices
    from netbox_routing.models import ISISSetting

    operations = _Operations()
    valid = {key for key, _label in ISISSettingChoices.CHOICES}
    wanted = {key: str(value) for key, value in (settings or {}).items() if key in valid and value is not None}
    content_type = ContentType.objects.get_for_model(type(parent))
    existing = (
        {
            row.key: row
            for row in ISISSetting.objects.filter(
                assigned_object_type=content_type,
                assigned_object_id=parent.pk,
            ).order_by("pk")
        }
        if parent.pk is not None
        else {}
    )
    for key, value in wanted.items():
        current = existing.get(key)
        row, created = _copy_or_new(
            current,
            ISISSetting,
            assigned_object_type=content_type,
            assigned_object_id=parent.pk,
            key=key,
            value=value,
        )
        if created:
            row.assigned_object = parent
            references = (("assigned_object_id", parent),) if parent.pk is None else ()
            operations.save(
                row,
                force_insert=True,
                natural_key=("assigned_object_type", "assigned_object_id", "key"),
                references=references,
            )
        elif row.value != value:
            row.value = value
            operations.save(row, update_fields=("value",))
    for key, row in existing.items():
        if key not in wanted:
            operations.delete(row)
    return operations, wanted


def _levels_plan(model, parent_field, parent, columns, levels):
    operations = _Operations()
    incoming = {}
    for level in levels or []:
        try:
            incoming[int(level["level"])] = level
        except (KeyError, TypeError, ValueError):
            continue
    existing = (
        {row.level: row for row in model.objects.filter(**{parent_field: parent}).order_by("pk")}
        if parent.pk is not None
        else {}
    )
    desired = []
    for level, data in incoming.items():
        current = existing.get(level)
        row, created = _copy_or_new(
            current,
            model,
            **{parent_field: parent, "level": level},
        )
        values = {}
        for column in columns:
            if column not in data:
                if not created:
                    values[column] = _absent_value(row, column)
                continue
            value = data.get(column)
            if value is not None:
                values[column] = value
        changed = _changed_fields(row, values)
        if created:
            operations.save(
                row,
                force_insert=True,
                natural_key=(parent_field, "level"),
            )
        elif changed:
            operations.save(row, update_fields=changed)
        desired.append({"level": row.level, **{column: getattr(row, column, None) for column in columns}})
    for level, row in existing.items():
        if level not in incoming:
            operations.delete(row)
    return operations, sorted(desired, key=lambda item: item["level"])


def _absent_value(instance, field_name):
    from .template_content import _model_absent_value

    return _model_absent_value(instance, field_name)


def _segment_routing_plan(instance, entry):
    from netbox_routing.models import ISISSegmentRouting

    from .template_content import _sr_instance_cols

    operations = _Operations()
    current = ISISSegmentRouting.objects.filter(instance=instance).first() if instance.pk is not None else None
    reported = entry.get("segment_routing_reported")
    configured = entry.get("segment_routing_configured")
    values = entry.get("segment_routing")
    if reported is True and configured is False:
        if current is not None:
            operations.delete(current)
        return operations, None
    if values is None and not (reported is True and configured is True):
        if current is None:
            return operations, None
        columns = _sr_instance_cols(ISISSegmentRouting)
        return operations, {column: getattr(current, column, None) for column in columns}

    row, created = _copy_or_new(current, ISISSegmentRouting, instance=instance)
    columns = _sr_instance_cols(ISISSegmentRouting)
    desired_values = {}
    for column in columns:
        value = (values or {}).get(column)
        desired_values[column] = _absent_value(row, column) if value is None else value
    changed = _changed_fields(row, desired_values)
    if created:
        operations.save(row, force_insert=True, natural_key=("instance",))
    elif changed:
        operations.save(row, update_fields=changed)
    return operations, {column: getattr(row, column, None) for column in columns}


def _flex_algo_plan(instance, entries):
    from netbox_routing.models import ISISFlexAlgo

    from .template_content import _ISIS_FLEX_COLS

    operations = _Operations()
    incoming = {}
    for entry in entries or []:
        try:
            incoming[int(entry["algo_id"])] = entry
        except (KeyError, TypeError, ValueError):
            continue
    existing = (
        {row.algo_id: row for row in ISISFlexAlgo.objects.filter(instance=instance).order_by("pk")}
        if instance.pk is not None
        else {}
    )
    desired = []
    for algo_id, data in incoming.items():
        current = existing.get(algo_id)
        row, created = _copy_or_new(
            current,
            ISISFlexAlgo,
            instance=instance,
            algo_id=algo_id,
        )
        changed = _changed_fields(
            row,
            {column: data[column] for column in _ISIS_FLEX_COLS if data.get(column) is not None},
        )
        if created:
            operations.save(
                row,
                force_insert=True,
                natural_key=("instance", "algo_id"),
            )
        elif changed:
            operations.save(row, update_fields=changed)
        desired.append({"algo_id": algo_id, **{column: getattr(row, column, None) for column in _ISIS_FLEX_COLS}})
    for algo_id, row in existing.items():
        if algo_id not in incoming:
            operations.delete(row)
    return operations, sorted(desired, key=lambda item: item["algo_id"])


def _srv6_locator_plan(instance, entries):
    from netbox_routing.models import ISISSRv6Locator

    from .template_content import (
        _ISIS_SRV6_LOCATOR_COLS,
        _isis_srv6_locator_omitted_defaults,
        _isis_srv6_locator_value,
    )

    operations = _Operations()
    incoming = {}
    for entry in entries or []:
        try:
            name = str(entry["name"])
        except (KeyError, TypeError):
            continue
        if name and entry.get("prefix"):
            incoming[name] = entry
    existing = (
        {row.name: row for row in ISISSRv6Locator.objects.filter(instance=instance).order_by("pk")}
        if instance.pk is not None
        else {}
    )
    defaults = _isis_srv6_locator_omitted_defaults(instance.device)
    desired = []
    for name, data in incoming.items():
        current_locator = existing.get(name)
        row, created = _copy_or_new(
            current_locator,
            ISISSRv6Locator,
            instance=instance,
            name=name,
            prefix=data["prefix"],
        )
        values = {}
        for column in _ISIS_SRV6_LOCATOR_COLS:
            value = _isis_srv6_locator_value(data, column, defaults)
            if value is None:
                if not created and getattr(row, column, None) not in (None, ""):
                    values[column] = _absent_value(row, column)
                continue
            values[column] = value
        changed = _changed_fields(row, values, comparator=_srv6_locator_values_differ)
        if created:
            operations.save(
                row,
                force_insert=True,
                natural_key=("instance", "name"),
            )
        elif changed:
            operations.save(row, update_fields=changed)
        desired.append(
            {
                "name": name,
                **{
                    column: str(getattr(row, column)) if column == "prefix" else getattr(row, column, None)
                    for column in _ISIS_SRV6_LOCATOR_COLS
                },
            }
        )
    for name, row in existing.items():
        if name not in incoming:
            operations.delete(row)
    return operations, sorted(desired, key=lambda item: item["name"])


def _prefix_sid_plan(interface, entries):
    from netbox_routing.models import ISISPrefixSID

    from .template_content import _ISIS_PREFIX_SID_COLS

    operations = _Operations()
    incoming = {}
    for entry in entries or []:
        try:
            incoming[int(entry["algorithm"])] = entry
        except (KeyError, TypeError, ValueError):
            continue
    existing = (
        {row.algorithm: row for row in ISISPrefixSID.objects.filter(interface=interface).order_by("pk")}
        if interface.pk is not None
        else {}
    )
    desired = []
    for algorithm, data in incoming.items():
        current = existing.get(algorithm)
        row, created = _copy_or_new(
            current,
            ISISPrefixSID,
            interface=interface,
            algorithm=algorithm,
        )
        changed = _changed_fields(
            row,
            {column: data.get(column) for column in _ISIS_PREFIX_SID_COLS},
        )
        if created:
            operations.save(
                row,
                force_insert=True,
                natural_key=("interface", "algorithm"),
            )
        elif changed:
            operations.save(row, update_fields=changed)
        desired.append(
            {
                "algorithm": algorithm,
                **{column: getattr(row, column, None) for column in _ISIS_PREFIX_SID_COLS},
            }
        )
    for algorithm, row in existing.items():
        if algorithm not in incoming:
            operations.delete(row)
    return operations, sorted(desired, key=lambda item: item["algorithm"])


def _instance_device_graph(instance, entry):
    from netbox_routing.models import ISISLevel

    from . import merge_util
    from .template_content import (
        _ISIS_INSTANCE_SCALAR_ATTRS,
        _ISIS_INSTANCE_SCALAR_COLS,
        _ISIS_LEVEL_COLS,
        _isis_instance_omitted_defaults,
    )

    scalar_values = {}
    for name in (
        "net",
        "is_type",
        "metric_style",
        "area_auth_type",
        "area_auth_key",
        "domain_auth_type",
        "domain_auth_key",
    ):
        value = entry.get(name) or ""
        if value or name == "is_type":
            scalar_values[name] = value
    if entry.get("overload_bit") is not None:
        scalar_values["overload_bit"] = entry["overload_bit"]
    omitted_defaults = _isis_instance_omitted_defaults(instance.device)
    for name in _ISIS_INSTANCE_SCALAR_ATTRS:
        if not hasattr(instance, name):
            continue
        if name not in entry:
            if name not in omitted_defaults:
                continue
            value = omitted_defaults[name]
        else:
            value = entry.get(name)
        scalar_values[name] = _absent_value(instance, name) if value is None else value
    changed = _changed_fields(instance, scalar_values)

    settings_ops, settings = _settings_plan(instance, entry.get("settings"))
    levels_ops, levels = _levels_plan(
        ISISLevel,
        "instance",
        instance,
        _ISIS_LEVEL_COLS,
        entry.get("levels"),
    )
    segment_ops, segment = _segment_routing_plan(instance, entry)
    flex_ops, flex = _flex_algo_plan(instance, entry.get("flex_algos"))
    locator_ops, locators = _srv6_locator_plan(instance, entry.get("srv6_locators"))

    content = {name: getattr(instance, name, None) for name in _ISIS_INSTANCE_SCALAR_COLS}
    content.update({name: getattr(instance, name) for name in _ISIS_INSTANCE_SCALAR_ATTRS if hasattr(instance, name)})
    content.update(
        settings=settings,
        levels=levels,
        sr=segment,
        flex=flex,
        srv6=locators,
    )
    return (
        changed,
        (settings_ops, levels_ops, segment_ops, flex_ops, locator_ops),
        merge_util.content_hash(content),
    )


def _interface_device_graph(interface, entry, state):
    from netbox_routing.models import ISISInterfaceLevel

    from . import merge_util
    from .template_content import (
        _ISIS_IFACE_LEVEL_COLS,
        _ISIS_IFACE_SCALAR_ATTRS,
        _isis_interface_routing_fields,
    )

    changed = _changed_fields(
        interface,
        dict(
            _isis_interface_routing_fields(
                state,
                entry,
                interface,
                entry.get("bfd_enabled"),
            )
        ),
    )
    settings_ops, settings = _settings_plan(interface, entry.get("settings"))
    levels_ops, levels = _levels_plan(
        ISISInterfaceLevel,
        "interface",
        interface,
        _ISIS_IFACE_LEVEL_COLS,
        entry.get("levels"),
    )
    prefix_ops, prefix_sids = _prefix_sid_plan(interface, entry.get("prefix_sids"))
    content = {name: getattr(interface, name) for name in _ISIS_IFACE_SCALAR_ATTRS if hasattr(interface, name)}
    content.update(settings=settings, levels=levels, prefix_sids=prefix_sids)
    return changed, (settings_ops, levels_ops, prefix_ops), merge_util.content_hash(content)


def _state_save(operations, state, created, natural_key):
    operations.save(
        state,
        force_insert=created,
        natural_key=natural_key if created else (),
    )


def _plan_stale_states(operations, states, seen, planned_at, native_field):
    from . import status_machine as sm

    for identity, current in states.items():
        if identity in seen:
            continue
        if not sm.is_owned(current.status) and getattr(current, f"{native_field}_id") is None:
            operations.delete(current)
            continue
        status = sm.on_reconcile(current.status, present=False)
        if status == current.status:
            continue
        stale = copy.copy(current)
        stale.status = status
        stale.last_sync_at = planned_at
        operations.save(stale, update_fields=("status", "last_sync_at"))


def _isis_reconcile_operations(device, payload, planned_at):  # noqa: C901, PLR0915
    from dcim.models import Interface
    from netbox_routing.models import ISISInstance, ISISInterface

    from . import merge_util
    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOISISInstanceState, NSOISISInterfaceState
    from .template_content import (
        _isis_device_matches_intent,
        _isis_instance_object_hash,
        _isis_interface_children_match,
        _isis_interface_object_hash,
        _isis_process_device_matches_intent,
    )

    management = NSODeviceManagement.objects.filter(device=device).first()
    operations = _Operations()
    if management is None:
        return operations, []

    process_entries = {}
    if "processes" in payload:
        for raw_entry in payload.get("processes") or []:
            if not isinstance(raw_entry, dict) or raw_entry.get("process_tag") is None:
                continue
            entry = dict(raw_entry)
            process_entries[str(entry["process_tag"])] = entry
    native_instances = {row.process_tag: row for row in ISISInstance.objects.filter(device=device).order_by("pk")}
    prospective_instances = dict(native_instances)
    process_states = {
        row.process_tag: row
        for row in NSOISISInstanceState.objects.filter(management=management)
        .select_related("isis_instance")
        .order_by("pk")
    }
    seen_processes = set()
    for process_tag, entry in process_entries.items():
        seen_processes.add(process_tag)
        current_state = process_states.get(process_tag)
        state, created_state = _copy_or_new(
            current_state,
            NSOISISInstanceState,
            management=management,
            process_tag=process_tag,
        )
        owned = sm.is_owned(state.status)
        if not owned:
            for name, value in {
                "net": entry.get("net") or "",
                "is_type": entry.get("is_type") or "",
                "metric_style": entry.get("metric_style") or "",
                "overload_bit": entry.get("overload_bit"),
                "area_auth_type": entry.get("area_auth_type") or "",
                "area_auth_present": bool(entry.get("area_auth_present", False)),
                "area_auth_key": entry.get("area_auth_key") or "",
                "domain_auth_type": entry.get("domain_auth_type") or "",
                "domain_auth_present": bool(entry.get("domain_auth_present", False)),
                "domain_auth_key": entry.get("domain_auth_key") or "",
                "fast_reroute": entry.get("fast_reroute") or "",
                "microloop_avoidance": entry.get("microloop_avoidance"),
            }.items():
                setattr(state, name, value)
        state.last_sync_at = planned_at

        current_native = native_instances.get(process_tag)
        native, created_native = _copy_or_new(
            current_native,
            ISISInstance,
            device=device,
            process_tag=process_tag,
        )
        native_ops = _Operations()
        changed, child_operations, device_hash = _instance_device_graph(native, entry)
        object_hash = "" if current_native is None else _isis_instance_object_hash(current_native)
        action = merge_util.three_way(
            created=created_native,
            base=state.device_base_hash,
            obj_hash=object_hash,
            dev_hash=device_hash,
        )
        if owned:
            matches = current_native is not None and object_hash == device_hash
            native = current_native
        elif action in {"seed", "mirror"}:
            if created_native:
                native_ops.save(
                    native,
                    force_insert=True,
                    natural_key=("device", "process_tag"),
                )
            elif changed:
                native_ops.save(native, update_fields=changed)
            for child_ops in child_operations:
                native_ops.extend(child_ops)
            operations.extend(native_ops)
            matches = True
            state.device_base_hash = device_hash
        elif action == "insync":
            matches = True
            state.device_base_hash = device_hash
            native = current_native
        else:
            matches = False
            native = current_native
        prospective_instances[process_tag] = native
        state.isis_instance = native
        if owned:
            matches = matches and _isis_process_device_matches_intent(
                entry,
                state,
                device,
                current_native,
            )
        state.status = sm.on_reconcile(state.status, matches=matches)
        _state_save(
            operations,
            state,
            created_state,
            ("management", "process_tag"),
        )

    if "processes" in payload:
        _plan_stale_states(
            operations,
            process_states,
            seen_processes,
            planned_at,
            "isis_instance",
        )

    interfaces = {row.name: row for row in Interface.objects.filter(device=device).order_by("pk")}
    native_interfaces = {
        (row.interface_id, row.address_family): row
        for row in ISISInterface.objects.filter(interface__device=device)
        .select_related("instance", "interface")
        .order_by("pk")
    }
    interface_states = {
        (row.interface_id, row.af): row
        for row in NSOISISInterfaceState.objects.filter(management=management)
        .select_related("isis_interface", "interface")
        .order_by("pk")
    }
    seen_interfaces = set()
    dropped = []
    if "interfaces" in payload:
        for raw_entry in payload.get("interfaces") or []:
            if not isinstance(raw_entry, dict):
                continue
            entry = dict(raw_entry)
            interface_name = entry.get("interface_name") or ""
            address_family = entry.get("af") or ""
            if not interface_name or not address_family:
                continue
            interface = interfaces.get(interface_name)
            if interface is None and entry.get("bound_port"):
                interface = interfaces.get(entry["bound_port"])
            if interface is None:
                dropped.append(interface_name)
                continue
            identity = (interface.pk, address_family)
            seen_interfaces.add(identity)
            current_state = interface_states.get(identity)
            state, created_state = _copy_or_new(
                current_state,
                NSOISISInterfaceState,
                management=management,
                interface=interface,
                af=address_family,
            )
            owned = sm.is_owned(state.status)
            if not owned:
                for name, value in {
                    "process_tag": entry.get("process_tag") or "",
                    "circuit_type": entry.get("circuit_type") or "",
                    "network_type": entry.get("network_type") or "",
                    "metric": entry.get("metric"),
                    "passive": bool(entry.get("passive", False)),
                    "bfd_enabled": entry.get("bfd_enabled"),
                    "frr_enabled": entry.get("frr_enabled"),
                    "frr_protection": entry.get("frr_protection") or "",
                    "hello_auth_type": entry.get("hello_auth_type") or "",
                    "hello_auth_present": bool(entry.get("hello_auth_present", False)),
                }.items():
                    setattr(state, name, value)
            state.last_sync_at = planned_at

            process_tag = state.process_tag
            native_instance = prospective_instances.get(process_tag)
            if process_tag in prospective_instances and native_instance is None:
                state.isis_interface = None
                state.status = sm.on_reconcile(state.status, matches=False)
                _state_save(
                    operations,
                    state,
                    created_state,
                    ("management", "interface", "af"),
                )
                continue
            if native_instance is None:
                native_instance = ISISInstance(device=device, process_tag=process_tag)
                prospective_instances[process_tag] = native_instance
                operations.save(
                    native_instance,
                    force_insert=True,
                    natural_key=("device", "process_tag"),
                )
            current_native = native_interfaces.get(identity)
            native, created_native = _copy_or_new(
                current_native,
                ISISInterface,
                instance=native_instance,
                interface=interface,
                address_family=address_family,
            )
            structural_change = not created_native and native.instance_id != native_instance.pk
            if structural_change:
                native.instance = native_instance
            structural_references = (("instance", native_instance),) if structural_change else ()
            changed, child_operations, device_hash = _interface_device_graph(native, entry, state)
            object_hash = "" if current_native is None else _isis_interface_object_hash(current_native)
            action = merge_util.three_way(
                created=created_native,
                base=state.device_base_hash,
                obj_hash=object_hash,
                dev_hash=device_hash,
            )
            if owned:
                if structural_change:
                    operations.save(
                        native,
                        update_fields=("instance",),
                        references=structural_references,
                    )
                matches = False
                native = current_native if not structural_change else native
            elif created_native:
                operations.save(
                    native,
                    force_insert=True,
                    natural_key=("interface", "address_family"),
                )
                for child_ops in child_operations:
                    operations.extend(child_ops)
                matches = True
                state.device_base_hash = device_hash
            elif action in {"seed", "mirror"}:
                structural_fields = ("instance",) if structural_change else ()
                fields = tuple(dict.fromkeys((*structural_fields, *changed)))
                if fields:
                    operations.save(
                        native,
                        update_fields=fields,
                        references=structural_references,
                    )
                for child_ops in child_operations:
                    operations.extend(child_ops)
                matches = True
                state.device_base_hash = device_hash
            elif action == "insync":
                if structural_change:
                    operations.save(
                        native,
                        update_fields=("instance",),
                        references=structural_references,
                    )
                matches = True
                state.device_base_hash = device_hash
                native = current_native if not structural_change else native
            else:
                if structural_change:
                    operations.save(
                        native,
                        update_fields=("instance",),
                        references=structural_references,
                    )
                matches = False
                native = current_native if not structural_change else native
            native_interfaces[identity] = native
            state.isis_interface = native
            if owned:
                matches = current_native is not None and (
                    _isis_device_matches_intent(
                        entry,
                        state,
                        current_native,
                        device,
                    )
                    and _isis_interface_children_match(entry, current_native)
                )
            state.status = sm.on_reconcile(state.status, matches=matches)
            _state_save(
                operations,
                state,
                created_state,
                ("management", "interface", "af"),
            )

        _plan_stale_states(
            operations,
            interface_states,
            seen_interfaces,
            planned_at,
            "isis_interface",
        )

    return operations, dropped


def reconcile_isis(device, payload):
    """Apply one preflighted IS-IS graph reconciliation."""
    try:
        from netbox_routing.models import ISISInstance  # noqa: F401
    except ImportError:
        logger.warning("netbox_routing not installed; skipping IS-IS reconcile")
        return {"processes": [], "interfaces": []}

    from .models import NSODeviceManagement, NSOISISInstanceState, NSOISISInterfaceState
    from .renderer_writer import (
        active_renderer_writer,
        renderer_mirror_writes,
        renderer_writes,
        replay_creation_references,
    )
    from .signals import suppress_intent_push

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return {"processes": [], "interfaces": []}
    active = active_renderer_writer()
    plan = active.plan if active is not None else isis_reconcile_plan(device, payload)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        operations, dropped = _isis_reconcile_operations(device, payload, plan.planned_at)
        for operation, instance, update_fields, force_insert, references in operations.operations:
            if operation == "delete":
                writer.delete(instance)
                continue
            replay_creation_references(instance, references)
            writer.save(
                instance,
                update_fields=update_fields,
                force_insert=force_insert,
            )

    if dropped:
        logger.warning(
            "IS-IS reconcile for %s: %d interface(s) not found in NetBox, dropped: %s",
            device,
            len(dropped),
            ", ".join(sorted(set(dropped))),
        )
    return {
        "processes": list(NSOISISInstanceState.objects.filter(management=management).select_related("isis_instance")),
        "interfaces": list(
            NSOISISInterfaceState.objects.filter(management=management).select_related(
                "interface",
                "isis_interface",
            )
        ),
    }


def reconcile_isis_process(device, process_list):
    """Apply the process half of an exact IS-IS reconciliation."""
    return reconcile_isis(device, {"processes": process_list})["processes"]


def reconcile_isis_interfaces(device, interfaces):
    """Apply the interface half of an exact IS-IS reconciliation."""
    return reconcile_isis(device, {"interfaces": interfaces})["interfaces"]
