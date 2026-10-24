# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Validate admission for redistribution protocol partitions."""

from .collection_coverage import collection_gaps
from .observations import ObservationProtocolError

PROTOCOLS = ("bgp", "isis", "ospf")


def covered_protocols(document, coverage):
    """Return complete covered partitions after checking the two coverage declarations."""
    if not isinstance(document, dict) or not isinstance(coverage, dict):
        raise ObservationProtocolError("Invalid redistribution component coverage: missing declarations.")
    components = document.get("components")
    claims = coverage.get("components")
    if not isinstance(components, list) or not isinstance(claims, list):
        raise ObservationProtocolError("Invalid redistribution component coverage: missing components.")
    declared = set()
    admitted = set()
    seen = set()
    for component in components:
        if not isinstance(component, dict):
            raise ObservationProtocolError("Invalid redistribution component coverage: malformed component.")
        protocol = component.get("protocol")
        state = component.get("status")
        if not isinstance(protocol, str) or protocol not in PROTOCOLS or protocol in seen:
            raise ObservationProtocolError("Invalid redistribution component coverage: invalid protocol.")
        seen.add(protocol)
        if state not in ("covered", "unavailable") or not isinstance(component.get("present"), list):
            raise ObservationProtocolError("Invalid redistribution component coverage: invalid status or presence.")
        if state == "unavailable":
            if component.get("inventory") is not None or component["present"]:
                raise ObservationProtocolError("Invalid redistribution component coverage: unavailable inventory.")
            continue
        declared.add(protocol)
        if not collection_gaps(
            "redistribution", component, schema_component=f"{protocol.title()}RedistributionComponent"
        ):
            admitted.add(protocol)
    claimed = []
    for component in claims:
        if not isinstance(component, dict) or not isinstance(component.get("protocol"), str):
            raise ObservationProtocolError("Invalid redistribution component coverage: malformed coverage.")
        claimed.append(component["protocol"])
    if len(set(claimed)) != len(claimed) or set(claimed) != declared:
        raise ObservationProtocolError("Invalid redistribution component coverage: status and coverage disagree.")
    return frozenset(admitted)


def refresh_is_unavailable(snapshot):
    """Keep the applied snapshot visible while a newer unsuccessful read vetoes changes."""
    from .read_gate import _is_authoritative

    state = snapshot.read_state
    return bool(state.observed_outcome) and not _is_authoritative(
        {
            "outcome": state.observed_outcome,
            "result": state.observed_result,
            "succeeded": state.observed_succeeded,
        }
    )


def applied_covered_protocols(management_id):
    """Read current applied admission without authorizing a retained partition."""
    from .models import NSOFamilyObservation

    snapshot = (
        NSOFamilyObservation.objects.filter(
            read_state__management_id=management_id, read_state__family="redistribution"
        )
        .select_related("read_state")
        .first()
    )
    if snapshot is not None:
        read_state = snapshot.read_state
        current = (
            bool(read_state.observed_outcome)
            and not refresh_is_unavailable(snapshot)
            and read_state.applied_publication_sequence == read_state.publication_sequence
            and read_state.applied_payload_revision == snapshot.revision
            and read_state.applied_source_epoch == snapshot.source_epoch
        )
        try:
            if current:
                return covered_protocols(snapshot.document, snapshot.coverage)
        except ObservationProtocolError:
            pass
    return frozenset()


def require_covered_component(state):
    """Require current applied coverage before acquiring a redistribution row."""
    from .ownership_planner import OwnershipNotQualified

    if state.dest_protocol in applied_covered_protocols(state.management_id):
        return
    raise OwnershipNotQualified(
        f"{state.dest_protocol.upper()} redistribution coverage is unavailable. Refresh redistribution before accepting it.",
        device_id=state.management.device_id,
    )
