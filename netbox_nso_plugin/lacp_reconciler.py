# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Publish LACP observations and mirror unowned native topology."""

from __future__ import annotations

import contextlib
import copy
import logging

from dcim.models import Interface
from django.core.exceptions import ValidationError
from django.utils import timezone

from . import status_machine as sm
from .intent_state import reconcile_family_footprint
from .lacp_topology import (
    bundle_episode_retirement,
    bundle_of,
    execute_frozen_operations,
    owned_manifest_interfaces,
    topology_snapshot,
    topology_validator,
)
from .models import NSODeviceManagement, NSOLACPBundleState, NSOLACPMemberState
from .renderer_writer import RendererMutationPlan, planned_delete, planned_save

logger = logging.getLogger(__name__)


def lacp_bundle_values(item):
    """Return the bundle parameters used by reconcile and comparison."""
    return {
        "lag_id": item.get("lag_id"),
        "min_links": item.get("min_links"),
        "system_priority": item.get("system_priority"),
        "system_id": item.get("system_id") or "",
        "timer": item.get("timer") or "",
        "admin_key": item.get("admin_key"),
        "vpc_sensitive": bool(item.get("vpc_sensitive")),
    }


def lacp_member_values(item):
    """Return the member parameters used by reconcile and comparison."""
    return {"mode": item.get("mode") or "", "port_priority": item.get("port_priority")}


class _LACPReconcilePlanner:
    """Project native topology before deciding any overlay save or prune."""

    def __init__(self, device, management, payload, planned_at):
        self.device = device
        self.management = management
        self.payload = payload
        self.planned_at = planned_at
        self.expected = topology_snapshot(management)
        self.interfaces = {
            row.name: row for row in Interface.objects.filter(device=device).select_related("lag").order_by("pk")
        }
        self.projected = {name: copy.copy(row) for name, row in self.interfaces.items()}
        self.bundle_states = {
            row.interface_id: row
            for row in NSOLACPBundleState.objects.filter(management=management).select_related("interface")
        }
        self.member_rows = {
            row.interface_id: row
            for row in NSOLACPMemberState.objects.filter(management=management).select_related("interface")
        }
        self.manifest_owned = owned_manifest_interfaces(management)
        self.protected = self.manifest_owned | {
            row.interface_id
            for row in (*self.bundle_states.values(), *self.member_rows.values())
            if sm.is_owned(row.status)
        }
        self.bundles = []
        self.reported_bundles = set()
        self.reported_members = set()
        self.native_only_members = set()
        self.dropped = set()
        self.demoted = {}
        self.saves = []
        self.outcomes = {}
        self.deletes = []

    def select_reported_bundles(self):
        for item in self.payload.get("bundles", []) or []:
            bundle = self.interfaces.get(item.get("name"))
            if bundle is None:
                self.dropped.add(item.get("name") or "<unnamed>")
                continue
            if bundle.pk not in self.reported_bundles:
                self.reported_bundles.add(bundle.pk)
                self.bundles.append((bundle, item))

    def retire_lost_overlays(self):
        retired = set()
        for bundle, item in self.bundles:
            candidates = [(bundle, self.bundle_states)]
            candidates.extend(
                (self.interfaces[member["interface_name"]], self.member_rows)
                for member in item.get("members", []) or []
                if member.get("interface_name") in self.interfaces
            )
            for interface, states in candidates:
                if interface.pk in states or interface.pk not in self.manifest_owned:
                    continue
                episode_bundle = interface if states is self.bundle_states else bundle_of(interface)
                if episode_bundle is None or episode_bundle.pk in retired:
                    continue
                retired.add(episode_bundle.pk)
                for save in bundle_episode_retirement(self.management, episode_bundle):
                    if isinstance(save.instance, (NSOLACPBundleState, NSOLACPMemberState)):
                        self.demoted[(save.instance._meta.label_lower, save.instance.interface_id)] = save.instance
                    else:
                        self.saves.append(save)

    def native_write(self, interface, field, value):
        original = self.interfaces[interface.name]
        candidate = copy.copy(interface)
        setattr(candidate, field, value)
        try:
            candidate.full_clean()
            if field == "lag" and value is not None and bundle_of(candidate) is None:
                raise ValidationError(f"{value.name} is not a native LAG on this device.")
        except ValidationError as exc:
            logger.warning(
                "LACP reconcile for %s, interface %s: NetBox refused native topology: %s",
                self.device,
                interface.name,
                "; ".join(exc.messages),
            )
            return
        self.projected[interface.name] = candidate
        self.saves.append(planned_save(candidate, update_fields=(field,), expected_before=original))

    def project_native_topology(self):
        for bundle, _item in self.bundles:
            if bundle.pk not in self.protected and bundle.type != "lag":
                self.native_write(self.projected[bundle.name], "type", "lag")
        self.project_reported_members()
        for interface_id in self.member_rows:
            if interface_id in self.reported_members or interface_id in self.protected:
                continue
            original = self.member_rows[interface_id].interface
            if original.lag_id is not None:
                self.native_write(self.projected[original.name], "lag", None)
        projected_by_pk = {row.pk: row for row in self.projected.values()}
        for row in self.projected.values():
            if row.lag_id in projected_by_pk:
                row.lag = projected_by_pk[row.lag_id]

    def project_reported_members(self):
        for bundle, item in self.bundles:
            for member in item.get("members", []) or []:
                name = member.get("interface_name") or ""
                interface = self.interfaces.get(name)
                if interface is None:
                    self.dropped.add(name or "<unnamed>")
                    continue
                if interface.pk in self.reported_members:
                    continue
                self.reported_members.add(interface.pk)
                if interface.pk not in self.member_rows and interface.lag_id is not None:
                    if interface.lag_id != bundle.pk:
                        self.native_only_members.add(interface.pk)
                    continue
                if interface.pk not in self.protected and interface.lag_id != bundle.pk:
                    self.native_write(self.projected[name], "lag", self.projected[bundle.name])

    def overlay_save(self, model, current, interface, values):
        identity = (model._meta.label_lower, interface.pk)
        baseline = self.demoted.get(identity, current)
        candidate = (
            copy.copy(baseline)
            if baseline is not None
            else model(management=self.management, interface=interface, status="unknown")
        )
        fields = {"last_sync_at", "status"}
        for name, value in values.items():
            if not sm.is_owned(candidate.status) or name in {"observed_members", "device_present", "vpc_sensitive"}:
                setattr(candidate, name, value)
                fields.add(name)
        candidate.last_sync_at = self.planned_at
        candidate.status = sm.on_reconcile(candidate.status, matches=None, settles_deploying=False)
        if identity in self.demoted:
            fields.add("accepted_at")
        self.outcomes[identity] = planned_save(
            candidate,
            update_fields=fields if current is not None else None,
            force_insert=current is None,
            natural_key=("management", "interface"),
            expected_before=current,
        )

    def publish_reported_overlays(self):
        for bundle, item in self.bundles:
            self.overlay_save(
                NSOLACPBundleState,
                self.bundle_states.get(bundle.pk),
                bundle,
                {
                    **lacp_bundle_values(item),
                    "device_present": True,
                    "observed_members": sorted(
                        {
                            member.get("interface_name")
                            for member in item.get("members", []) or []
                            if member.get("interface_name")
                        }
                    ),
                },
            )
            for member in item.get("members", []) or []:
                interface = self.interfaces.get(member.get("interface_name"))
                if (
                    interface is None
                    or interface.pk in self.native_only_members
                    or (NSOLACPMemberState._meta.label_lower, interface.pk) in self.outcomes
                ):
                    continue
                self.overlay_save(
                    NSOLACPMemberState,
                    self.member_rows.get(interface.pk),
                    interface,
                    lacp_member_values(member),
                )

    def plan_stale_overlays(self):
        bearing = {row.lag_id for row in self.projected.values() if bundle_of(row) is not None}
        for model, states, seen in (
            (NSOLACPBundleState, self.bundle_states, self.reported_bundles),
            (NSOLACPMemberState, self.member_rows, self.reported_members),
        ):
            for current in states.values():
                if current.interface_id in seen:
                    continue
                native = self.projected[current.interface.name]
                vestigial = native.pk not in bearing if model is NSOLACPBundleState else native.lag_id is None
                if current.interface_id not in self.protected and vestigial:
                    self.deletes.append(planned_delete(current, expected_before=current))
                else:
                    self.stale_overlay_save(model, current)

    def stale_overlay_save(self, model, current):
        identity = (model._meta.label_lower, current.interface_id)
        candidate = copy.copy(self.demoted.get(identity, current))
        fields = {"last_sync_at"}
        if not sm.is_owned(candidate.status):
            candidate.status = sm.on_reconcile(candidate.status, present=False)
            fields.add("status")
        if model is NSOLACPBundleState:
            candidate.observed_members = []
            candidate.device_present = False
            fields.update(("observed_members", "device_present"))
        if identity in self.demoted:
            fields.update(("status", "accepted_at"))
        candidate.last_sync_at = self.planned_at
        self.outcomes[identity] = planned_save(candidate, update_fields=fields, expected_before=current)

    def plan(self):
        self.select_reported_bundles()
        self.retire_lost_overlays()
        self.project_native_topology()
        self.publish_reported_overlays()
        self.plan_stale_overlays()
        if self.dropped:
            logger.warning(
                "LACP reconcile for %s: interfaces not found in NetBox: %s",
                self.device,
                ", ".join(sorted(self.dropped)),
            )
        return RendererMutationPlan.build(
            saves=(*self.saves, *self.outcomes.values()),
            deletes=self.deletes,
            planned_at=self.planned_at,
            additional_footprints=(reconcile_family_footprint(self.device.pk, ("lacp",)),),
            validate_after_acquire=topology_validator(self.management, self.expected),
            settles_deploying=False,
        )


def lacp_reconcile_plan(device, payload: dict):
    """Freeze native writes and overlay outcomes from one projected topology."""
    management = NSODeviceManagement.objects.filter(device=device).first()
    planned_at = timezone.now()
    if management is None:
        return RendererMutationPlan.build(planned_at=planned_at)
    return _LACPReconcilePlanner(device, management, payload, planned_at).plan()


def reconcile_lag_config(device, payload: dict) -> list:
    """Execute a frozen publication with one retry for a standalone stale plan."""
    from .renderer_writer import active_renderer_writer, renderer_writes_replanning_once
    from .signals import suppress_intent_push

    active = active_renderer_writer()
    mutation = (
        contextlib.nullcontext((active, active.plan))
        if active is not None
        else renderer_writes_replanning_once(lambda: lacp_reconcile_plan(device, payload))
    )
    with mutation as (writer, _plan), suppress_intent_push():
        return _reconcile_lag_config(device, writer)


def _reconcile_lag_config(device, writer):
    """Execute only the frozen operations selected before lock acquisition."""
    execute_frozen_operations(
        writer,
        {
            "dcim.interface",
            "netbox_nso_plugin.nsoownershipmanifest",
            "netbox_nso_plugin.nsolacpbundlestate",
            "netbox_nso_plugin.nsolacpmemberstate",
        },
    )
    return list(NSOLACPBundleState.objects.filter(management__device=device).select_related("interface"))
