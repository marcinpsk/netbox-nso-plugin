# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Durable, fenced completion for asynchronous device provisioning."""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def validate_provision_evidence(evidence, *, terminal_required=False) -> dict:
    """Validate the shared adapter and callback provision-evidence contract."""
    if not isinstance(evidence, dict):
        raise ValueError("provision evidence must be an object")
    status = evidence.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError("provision evidence status must be a non-empty string")
    for member in ("result", "error"):
        value = evidence.get(member)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"provision evidence {member} must be an object or null")
    if status == "succeeded" and not isinstance(evidence.get("result"), dict):
        raise ValueError("successful provision evidence must include a result object")
    _evidence_job_id(evidence)
    if terminal_required and status in {"queued", "running"}:
        raise ValueError("provision completion evidence must be terminal")
    return dict(evidence)


def _evidence_job_id(evidence) -> str:
    raw_job_id = evidence.get("job_id")
    if raw_job_id in (None, ""):
        return ""
    if isinstance(raw_job_id, bool) or not isinstance(raw_job_id, (int, str)):
        raise ValueError("provision evidence job_id must be an integer or string")
    job_id = str(raw_job_id)
    if len(job_id) > 64:
        raise ValueError("provision evidence job_id is too long")
    return job_id


def _record_open_job_id(tombstone, evidence) -> None:
    """Persist the admitted adapter job carried by an attempt receipt."""
    from .models import NSOProvisionTombstone

    adapter_job_id = _evidence_job_id(evidence)
    if not adapter_job_id:
        return
    if tombstone.adapter_job_id not in ("", adapter_job_id):
        raise ValueError("provision receipt job_id does not match the admitted job")
    NSOProvisionTombstone.objects.filter(
        provision_attempt_id=tombstone.provision_attempt_id,
        state="open",
        adapter_job_id="",
    ).update(adapter_job_id=adapter_job_id, updated_at=timezone.now())
    tombstone.adapter_job_id = adapter_job_id


def mark_provision_terminal(provision_attempt_id, evidence: dict) -> bool:
    """CAS one open attempt to terminal and preserve the first terminal evidence."""
    from .models import NSOProvisionTombstone

    evidence = validate_provision_evidence(evidence, terminal_required=True)
    terminal_status = evidence["status"]
    adapter_job_id = _evidence_job_id(evidence)
    result = evidence.get("result")
    adapter_device_id = result.get("device_id") if isinstance(result, dict) else None
    open_attempt = NSOProvisionTombstone.objects.filter(
        provision_attempt_id=provision_attempt_id,
        state="open",
    )
    if adapter_job_id and open_attempt.exclude(adapter_job_id__in=("", adapter_job_id)).exists():
        raise ValueError("provision evidence job_id does not match the admitted job")
    updates = {
        "state": "terminal",
        "terminal_status": terminal_status,
        "terminal_evidence": dict(evidence),
        "adapter_device_id": adapter_device_id,
        "updated_at": timezone.now(),
    }
    if adapter_job_id:
        open_attempt = open_attempt.filter(adapter_job_id__in=("", adapter_job_id))
        updates["adapter_job_id"] = adapter_job_id
    updated = open_attempt.update(
        **updates,
    )
    if updated:
        return True
    return NSOProvisionTombstone.objects.filter(
        provision_attempt_id=provision_attempt_id,
        state__in=("terminal", "offboarded", "closed"),
    ).exists()


def sweep_provision_tombstones(provision_attempt_id=None):
    """Advance matching provision tombstones through the fenced completion states."""
    from .models import NSOProvisionTombstone

    tombstones = NSOProvisionTombstone.objects.exclude(state="closed").order_by("created_at", "provision_attempt_id")
    if provision_attempt_id is not None:
        tombstones = tombstones.filter(provision_attempt_id=provision_attempt_id)

    checked = 0
    closed = 0
    for tombstone_id in tombstones.values_list("provision_attempt_id", flat=True):
        checked += 1
        try:
            closed += int(_sweep_one(tombstone_id))
        except Exception:  # noqa: BLE001 - one attempt must not stop the fleet sweep
            logger.exception("Provision tombstone sweep failed for attempt %s", tombstone_id)
    return checked, closed


def _sweep_one(provision_attempt_id) -> bool:
    from dcim.models import Device
    from django.db.models import Q

    from . import adapter_client
    from .adapter_client import AdapterError
    from .models import NSODeviceManagement, NSOProvisionTombstone

    with transaction.atomic():
        tombstone = NSOProvisionTombstone.objects.select_for_update().get(provision_attempt_id=provision_attempt_id)
        if tombstone.state == "closed":
            return False
        if tombstone.state == "offboarded":
            return _close_tombstone(provision_attempt_id, expected_state="offboarded")
        if tombstone.state == "open":
            evidence = adapter_client.get_provision_attempt(provision_attempt_id)
            evidence = validate_provision_evidence(evidence)
            _record_open_job_id(tombstone, evidence)
            status = evidence["status"]
            if status in {"", "queued", "running"}:
                return False
            mark_provision_terminal(provision_attempt_id, evidence)
            tombstone.refresh_from_db()
        if tombstone.state != "terminal":
            return False

        Device.objects.select_for_update().filter(pk=tombstone.netbox_device_id).first()
        relevant_management = (
            NSODeviceManagement.objects.select_for_update()
            .select_related("nso_instance")
            .filter(
                Q(device_id=tombstone.netbox_device_id)
                | Q(
                    nso_instance__adapter_instance_id=tombstone.nso_instance,
                    nso_device_name=tombstone.nso_device_name,
                )
            )
        )
        management = (
            relevant_management.select_related("nso_instance")
            .filter(
                device_id=tombstone.netbox_device_id,
                nso_instance__adapter_instance_id=tombstone.nso_instance,
                nso_device_name=tombstone.nso_device_name,
                onboard_job_id=tombstone.adapter_job_id,
            )
            .first()
        )
        if management is None:
            if relevant_management.exists():
                return _close_tombstone(provision_attempt_id, expected_state="terminal")
            adapter_device_id = tombstone.adapter_device_id
            if adapter_device_id is None:
                adapter_device_id, ambiguous = _recover_adapter_device_id(tombstone)
            else:
                ambiguous = False
            if ambiguous:
                NSOProvisionTombstone.objects.filter(
                    provision_attempt_id=provision_attempt_id,
                    state="terminal",
                ).update(
                    offboard_error="The logical provision identity matches multiple adapter devices.",
                    updated_at=timezone.now(),
                )
                return False
            if adapter_device_id is not None:
                try:
                    adapter_client.delete_provisioned_device(adapter_device_id)
                except AdapterError as exc:
                    if exc.status_code != 404 and str(exc.code) != "404":
                        NSOProvisionTombstone.objects.filter(
                            provision_attempt_id=provision_attempt_id,
                            state="terminal",
                        ).update(
                            offboard_error="The adapter offboard request failed.",
                            updated_at=timezone.now(),
                        )
                        return False
            moved = NSOProvisionTombstone.objects.filter(
                provision_attempt_id=provision_attempt_id,
                state="terminal",
            ).update(
                state="offboarded",
                adapter_device_id=adapter_device_id,
                offboard_error="",
                updated_at=timezone.now(),
            )
            if not moved:
                return False

        else:
            _apply_terminal_evidence(management, tombstone)
            return _close_tombstone(provision_attempt_id, expected_state="terminal")

    return _close_tombstone(provision_attempt_id, expected_state="offboarded")


def _recover_adapter_device_id(tombstone):
    """Resolve an orphan by logical identity. No match means it is already absent."""
    from . import adapter_client

    inventory = adapter_client.list_devices() or []
    if not isinstance(inventory, list):
        raise ValueError("adapter device inventory must be a list")
    matches = [
        row
        for row in inventory
        if isinstance(row, dict)
        and row.get("nso_instance") == tombstone.nso_instance
        and row.get("nso_device_name") == tombstone.nso_device_name
        and row.get("netbox_device_id") in (None, tombstone.netbox_device_id)
    ]
    if len(matches) > 1:
        return None, True
    if not matches:
        return None, False
    adapter_device_id = matches[0].get("id")
    if type(adapter_device_id) is not int or adapter_device_id <= 0:
        raise ValueError("adapter device inventory has an invalid id")
    return adapter_device_id, False


def _close_tombstone(provision_attempt_id, *, expected_state: str) -> bool:
    from .models import NSOProvisionTombstone

    now = timezone.now()
    return bool(
        NSOProvisionTombstone.objects.filter(
            provision_attempt_id=provision_attempt_id,
            state=expected_state,
        ).update(
            state="closed",
            closed_at=now,
            updated_at=now,
        )
    )


def _apply_terminal_evidence(management, tombstone) -> None:
    from .management_lifecycle import save_management

    evidence = tombstone.terminal_evidence if isinstance(tombstone.terminal_evidence, dict) else {}
    result = evidence.get("result") if isinstance(evidence.get("result"), dict) else {}
    steps = result.get("steps")
    management.onboard_steps = steps if isinstance(steps, list) else []
    if tombstone.terminal_status == "succeeded" and result.get("ok"):
        management.onboard_status = ""
        management.onboard_error = ""
        save_management(management)
        return

    management.onboard_status = "provision_failed"
    management.onboard_error = "Provisioning failed. See the server log."
    save_management(
        management,
        update_fields=["onboard_status", "onboard_steps", "onboard_error"],
    )
