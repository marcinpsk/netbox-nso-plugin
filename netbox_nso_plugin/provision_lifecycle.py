# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Durable, fenced completion for asynchronous device provisioning."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .deployment import DeploymentQuiesced
from .deployment import guarded as _deployment_guarded

logger = logging.getLogger(__name__)

# One fleet tick polls at most this many attempts. Attempted rows rotate to the back.
_FLEET_SWEEP_LIMIT = 100
# Past this age an attempt the adapter has no record of can no longer be in flight.
_UNKNOWN_ATTEMPT_MAX_AGE = timedelta(hours=6)
_PROVISION_STATUSES = frozenset({"queued", "running", "succeeded", "failed"})
_TERMINAL_PROVISION_STATUSES = frozenset({"succeeded", "failed"})


def _invalid_adapter_response(message):
    from .adapter_client import AdapterError

    return AdapterError(message, code="invalid_response")


def validate_provision_evidence(evidence, *, terminal_required=False) -> dict:
    """Validate the shared adapter and callback provision-evidence contract."""
    if not isinstance(evidence, dict):
        raise ValueError("provision evidence must be an object")
    status = evidence.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError("provision evidence status must be a non-empty string")
    if status not in _PROVISION_STATUSES:
        raise ValueError("provision evidence status is not supported")
    for member in ("result", "error"):
        value = evidence.get(member)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"provision evidence {member} must be an object or null")
    result = evidence.get("result")
    if status == "succeeded" and not isinstance(result, dict):
        raise ValueError("successful provision evidence must include a result object")
    if status == "succeeded" and type(result.get("ok")) is not bool:
        raise ValueError("successful provision evidence result.ok must be a boolean")
    adapter_device_id = result.get("device_id") if isinstance(result, dict) else None
    if adapter_device_id is not None and (type(adapter_device_id) is not int or adapter_device_id <= 0):
        raise ValueError("provision evidence result.device_id must be a positive integer")
    _evidence_job_id(evidence)
    if terminal_required and status not in _TERMINAL_PROVISION_STATUSES:
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
        raise _invalid_adapter_response("Provision receipt job_id does not match the admitted job.")
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
        raise _invalid_adapter_response("Provision evidence job_id does not match the admitted job.")
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


@_deployment_guarded("provisioning")
def sweep_provision_tombstones(provision_attempt_id=None, *, deadline: float | None = None):
    """Advance matching provision tombstones through the fenced completion states."""
    from .models import NSOProvisionTombstone

    tombstones = NSOProvisionTombstone.objects.exclude(state="closed").order_by(
        "updated_at",
        "created_at",
        "provision_attempt_id",
    )
    if provision_attempt_id is not None:
        # One named attempt: its caller (UI poll, callback job) can act on the failure.
        attempts = list(
            tombstones.filter(provision_attempt_id=provision_attempt_id).values_list("provision_attempt_id", flat=True)
        )
        return len(attempts), sum(int(_sweep_one(attempt_id)) for attempt_id in attempts)

    checked = 0
    closed = 0
    for tombstone_id in tombstones.values_list("provision_attempt_id", flat=True)[:_FLEET_SWEEP_LIMIT]:
        if deadline is not None and time.monotonic() >= deadline:
            break
        checked += 1
        try:
            closed += int(_sweep_one(tombstone_id))
        except DeploymentQuiesced:
            raise
        except Exception:  # noqa: BLE001 - one attempt must not stop the fleet sweep
            logger.exception("Provision tombstone sweep failed for attempt %s", tombstone_id)
        finally:
            NSOProvisionTombstone.objects.filter(
                provision_attempt_id=tombstone_id,
            ).exclude(state="closed").update(updated_at=timezone.now())
    return checked, closed


def _sweep_one(provision_attempt_id) -> bool:
    """Advance one attempt. Every adapter call runs outside the device and management locks."""
    from .models import NSOProvisionTombstone

    tombstone = NSOProvisionTombstone.objects.get(provision_attempt_id=provision_attempt_id)
    if tombstone.state == "closed":
        return False
    if tombstone.state == "open":
        if not _poll_open_attempt(tombstone):
            return False
        tombstone.refresh_from_db()
    if tombstone.state == "offboarded":
        return _close_tombstone(provision_attempt_id, expected_state="offboarded")
    if tombstone.state != "terminal":
        return False
    return _complete_terminal_attempt(tombstone)


def _poll_open_attempt(tombstone) -> bool:
    """Poll one open attempt unlocked and compare-and-set whatever verdict it carries."""
    from . import adapter_client
    from .adapter_client import AdapterError
    from .models import NSOProvisionTombstone

    provision_attempt_id = tombstone.provision_attempt_id
    try:
        evidence = validate_provision_evidence(adapter_client.get_provision_attempt(provision_attempt_id))
    except AdapterError as exc:
        if not _is_not_found(exc):
            raise
        return _age_out_unknown_attempt(tombstone)

    with transaction.atomic():
        locked = NSOProvisionTombstone.objects.select_for_update().get(provision_attempt_id=provision_attempt_id)
        if locked.state != "open":
            return True
        _record_open_job_id(locked, evidence)
        if evidence["status"] in {"queued", "running"}:
            return False
        return mark_provision_terminal(provision_attempt_id, evidence)


def _age_out_unknown_attempt(tombstone) -> bool:
    """Fail an attempt the adapter has no record of, once it can no longer be in flight."""
    age = timezone.now() - tombstone.created_at
    if age < _UNKNOWN_ATTEMPT_MAX_AGE:
        return False
    logger.warning(
        "Provision attempt %s is unknown to the adapter after %s. Recording it as failed.",
        tombstone.provision_attempt_id,
        age,
    )
    return mark_provision_terminal(
        tombstone.provision_attempt_id,
        {
            "status": "failed",
            "error": {
                "code": "provision_attempt_unknown",
                "message": "The adapter has no record of this provision attempt.",
            },
        },
    )


def _is_not_found(exc) -> bool:
    """Report whether an adapter failure means the addressed resource does not exist."""
    return exc.status_code == 404 or str(exc.code) == "404"


def _complete_terminal_attempt(tombstone) -> bool:
    """Retire one terminal attempt behind the tombstone fence, holding no foreign row lock."""
    from django.db.models import Q

    from . import adapter_client
    from .adapter_client import AdapterError
    from .models import NSODeviceManagement, NSOProvisionTombstone

    provision_attempt_id = tombstone.provision_attempt_id
    with transaction.atomic():
        # The tombstone row IS the offboard fence: onboarding._lock_provision_identity takes it
        # first, so a re-onboard cannot cross an offboard that is still in flight.
        tombstone = NSOProvisionTombstone.objects.select_for_update().get(
            provision_attempt_id=provision_attempt_id,
        )
        if tombstone.state != "terminal":
            return False
        # of=("self",): the joined NSOInstance row is shared by every device on the instance.
        relevant_management = (
            NSODeviceManagement.objects.select_for_update(of=("self",))
            .select_related("nso_instance")
            .filter(
                Q(device_id=tombstone.netbox_device_id)
                | Q(
                    nso_instance__adapter_instance_id=tombstone.nso_instance,
                    nso_device_name=tombstone.nso_device_name,
                )
            )
        )
        management = relevant_management.filter(
            device_id=tombstone.netbox_device_id,
            nso_instance__adapter_instance_id=tombstone.nso_instance,
            nso_device_name=tombstone.nso_device_name,
            onboard_job_id=tombstone.adapter_job_id,
        ).first()
        if management is not None:
            _apply_terminal_evidence(management, tombstone)
            return _close_tombstone(provision_attempt_id, expected_state="terminal")
        if relevant_management.exists():
            return _close_tombstone(provision_attempt_id, expected_state="terminal")

        # An orphan matches no management row, so the adapter calls below hold the tombstone
        # fence alone. No device, management, or instance row is locked across them.
        adapter_device_id = tombstone.adapter_device_id
        ambiguous = False
        if adapter_device_id is None:
            adapter_device_id, ambiguous = _recover_adapter_device_id(tombstone)
        if ambiguous:
            _record_offboard_error(
                provision_attempt_id,
                "The logical provision identity matches multiple adapter devices.",
            )
            return False
        if adapter_device_id is not None:
            try:
                adapter_client.delete_provisioned_device(adapter_device_id)
            except AdapterError as exc:
                if not _is_not_found(exc):
                    _record_offboard_error(provision_attempt_id, "The adapter offboard request failed.")
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

    return _close_tombstone(provision_attempt_id, expected_state="offboarded")


def _record_offboard_error(provision_attempt_id, message: str) -> None:
    """Record why one terminal attempt could not be offboarded, leaving it retryable."""
    from .models import NSOProvisionTombstone

    NSOProvisionTombstone.objects.filter(
        provision_attempt_id=provision_attempt_id,
        state="terminal",
    ).update(offboard_error=message, updated_at=timezone.now())


def _recover_adapter_device_id(tombstone):
    """Resolve an orphan by logical identity. No match means it is already absent."""
    from . import adapter_client

    inventory = adapter_client.list_devices()
    if not isinstance(inventory, list):
        raise _invalid_adapter_response("Adapter device inventory must be a list.")
    if not all(isinstance(row, dict) for row in inventory):
        raise _invalid_adapter_response("Adapter device inventory entries must be objects.")
    matches = [
        row
        for row in inventory
        if row.get("nso_instance") == tombstone.nso_instance
        and row.get("nso_device_name") == tombstone.nso_device_name
        and row.get("netbox_device_id") in (None, tombstone.netbox_device_id)
    ]
    if len(matches) > 1:
        return None, True
    if not matches:
        return None, False
    adapter_device_id = matches[0].get("id")
    if type(adapter_device_id) is not int or adapter_device_id <= 0:
        raise _invalid_adapter_response("Adapter device inventory has an invalid id.")
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
