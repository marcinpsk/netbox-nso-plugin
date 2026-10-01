# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Classify the explicit operations that may start ownership."""

from dataclasses import dataclass

GRANTS = frozenset({"accept", "create", "intend", "autoassign", "link_role", "operator_edit", "manifest_reown"})


@dataclass(frozen=True)
class OwnershipGrant:
    """The operation that permits one exact writer to acquire overlays."""

    kind: str
    manifest_pk: int | None = None

    def __post_init__(self):
        if not isinstance(self.kind, str) or self.kind not in GRANTS:
            raise ValueError(f"Unknown ownership grant kind {self.kind!r}")
        if (self.kind == "manifest_reown") != (self.manifest_pk is not None):
            raise ValueError("Only manifest_reown requires a manifest primary key")
        if self.manifest_pk is not None and (type(self.manifest_pk) is not int or self.manifest_pk <= 0):
            raise ValueError("The manifest primary key must be a positive integer")


def validate_acquisition(before, after, grant):
    """Refuse an ownership start without evidence from a classified operation."""
    from .intent_state import OVERLAY_MODEL_RANKS, IntentMutationProtocolError
    from .status_machine import OWNED_STATES

    if after._meta.label_lower not in OVERLAY_MODEL_RANKS:
        return
    message = f"{after._meta.label_lower} row {after.pk!r} cannot acquire status {after.status!r}"
    if not isinstance(after.status, str):
        raise IntentMutationProtocolError(message)
    if after.status not in OWNED_STATES:
        return
    if before is not None and before.status in OWNED_STATES:
        return
    if not isinstance(grant, OwnershipGrant) or grant.kind not in GRANTS:
        raise IntentMutationProtocolError(message)
    if grant.kind == "manifest_reown":
        from .models import NSOOwnershipManifest
        from .ownership_planner import manifest_binding

        binding = manifest_binding(after)
        if binding is None:
            raise IntentMutationProtocolError(message)
        _rule, scope, device_id, native_label, native_id, native_key, state_label, state_key = binding
        if not NSOOwnershipManifest.objects.filter(
            pk=grant.manifest_pk,
            ownership_state="owned",
            device_id=device_id,
            scope=scope,
            native_model_label=native_label,
            native_id=native_id,
            native_key=native_key,
            state_model_label=state_label,
            state_key=state_key,
            grant_kind__in=GRANTS,
        ).exists():
            raise IntentMutationProtocolError(message)
