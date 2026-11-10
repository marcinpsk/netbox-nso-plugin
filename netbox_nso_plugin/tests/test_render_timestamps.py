# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Acceptance times stay stable between candidate and persisted delivery."""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from netbox_nso_plugin import delivery, switching_preparation
from netbox_nso_plugin.candidate_render import CandidateBatch

from ._candidate_render_case import ACCEPTED_AT, persist_candidates, render_candidates
from ._outbox_case import make_managed


def wire_body(scope, rendered):
    if scope in delivery.direct_keys():
        with switching_preparation._bound((), 7):
            return json.loads(delivery.wire_body(rendered, rendered.payload))
    return json.loads(delivery.wire_body(rendered, rendered.payload))


@pytest.mark.django_db
@pytest.mark.parametrize(
    "scope",
    (
        "interface",
        "ip",
        "interface_mtu",
        "vlan",
        "static_route",
        "isis",
        "isis_flex_algo",
        "bgp",
        "ospf",
        "route_policy",
        "lacp",
        "switchport",
    ),
)
def test_candidate_and_persisted_wire_bodies_keep_explicit_acceptance_times(scope):
    device, management = make_managed("timestamp-parity", 17889)
    rows, relations = render_candidates(device, management, 1, rich=True)
    for row in rows:
        row.accepted_at = ACCEPTED_AT.astimezone(timezone(timedelta(hours=2)))
    batch = CandidateBatch(rows, relations)
    candidate = wire_body(scope, delivery.render(scope, device.pk, management.adapter_device_id, candidates=batch))

    def check_timestamps(value):
        if isinstance(value, list):
            for item in value:
                check_timestamps(item)
        elif isinstance(value, dict):
            if scope in delivery.direct_keys():
                assert value["accepted_at"] == ACCEPTED_AT.isoformat()
                return
            for key, item in value.items():
                if key in {
                    "interfaces",
                    "vlans",
                    "routes",
                    "objects",
                    "processes",
                    "instances",
                    "flex_algos",
                    "routers",
                    "addresses",
                    "attributes",
                }:
                    for row in item:
                        assert row["accepted_at"] == ACCEPTED_AT.isoformat()

    check_timestamps(candidate)
    persist_candidates(rows, relations)
    confirmed = wire_body(scope, delivery.render(scope, device.pk, management.adapter_device_id))
    assert candidate == confirmed


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ("lacp", "switchport"))
def test_switching_envelope_uses_latest_contributing_acceptance_time(scope):
    device, management = make_managed("switching-timestamp", 17890)
    rows, relations = render_candidates(device, management, 1)
    later = datetime(2026, 11, 2, 12, 30, tzinfo=UTC)
    rows[14 if scope == "lacp" else 15].accepted_at = later
    rendered = delivery.render(
        scope, device.pk, management.adapter_device_id, candidates=CandidateBatch(rows, relations)
    )
    body = wire_body(scope, rendered)
    assert body["accepted_at"] == later.isoformat()
    assert "accepted_at" not in body["bundles" if scope == "lacp" else "interfaces"][0]
    persist_candidates(rows, relations)
    assert body == wire_body(scope, delivery.render(scope, device.pk, management.adapter_device_id))


@pytest.mark.django_db
def test_other_delivery_serializers_keep_the_persisted_row_acceptance_time():
    from dcim.models import Interface

    from netbox_nso_plugin import models, signals

    from ._ownership_case import acquire_overlay

    device, management = make_managed("auxiliary-timestamps", 17891)
    parent = Interface.objects.create(device=device, name="Ethernet1", type="1000base-t")
    interface = Interface.objects.create(device=device, name="Ethernet1.101", type="virtual", parent=parent)
    common = {"management": management, "status": "accepted", "accepted_at": ACCEPTED_AT}
    cases = (
        (
            models.NSOSnmpCommunityState,
            {"community_hash": "a" * 16, "vault_ref": "secret/example"},
            signals.snmp_community_intent_item,
            (),
        ),
        (models.NSOSnmpV3UserState, {"username": "example"}, signals.snmp_v3_user_intent_item, ()),
        (models.NSOSnmpHostState, {"address": "198.18.0.1"}, signals.snmp_host_intent_item, ("",)),
        (models.NSOSnmpSystemInfoState, {"location": "example"}, signals.snmp_system_info_intent_item, ()),
        (models.NSOLoggingHostState, {"address": "198.18.0.2"}, signals.logging_host_intent_item, ("",)),
        (models.NSOLoggingLevelState, {"console_severity": "WARNING"}, signals.logging_levels_intent_item, ()),
        (models.NSOSVIState, {"interface": parent, "svi_type": "svi"}, signals.svi_intent_item, ()),
        (
            models.NSOSubinterfaceState,
            {"interface": interface, "parent_interface": parent, "dot1q_vlan": 101},
            signals.subinterface_intent_item,
            (),
        ),
        (models.NSOBFDInterfaceState, {"interface": parent, "min_tx": 100}, signals.bfd_intent_item, ()),
        (
            models.NSOL2SapState,
            {"service_name": "example", "service_type": "epipe", "sap_id": "1:101", "port": "1", "outer_tag": 101},
            signals.l2_sap_intent_item,
            (),
        ),
    )
    for model, values, serialize, arguments in cases:
        row = acquire_overlay(model, **common, **values)
        persisted = model.objects.get(pk=row.pk)
        assert serialize(persisted, *arguments)["accepted_at"] == ACCEPTED_AT.isoformat()
    blank_levels = models.NSOLoggingLevelState(management=management, accepted_at=ACCEPTED_AT, status="accepted")
    assert signals.logging_levels_intent_item(blank_levels) is None


@pytest.mark.django_db
def test_candidate_render_refuses_a_naive_acceptance_timestamp():
    device, management = make_managed("naive-timestamp", 17892)
    rows, _ = render_candidates(device, management, 1)
    rows[2].accepted_at = datetime(2026, 11, 1)  # noqa: DTZ001 - malformed boundary input
    with pytest.raises(ValueError, match="timezone-aware"):
        delivery.render("interface_mtu", device.pk, management.adapter_device_id, candidates=CandidateBatch((rows[2],)))


@pytest.mark.django_db
@pytest.mark.parametrize("scope,index", (("lacp", 13), ("lacp", 14), ("switchport", 15)))
def test_switching_acceptance_time_changes_advance_the_delivery_revision(scope, index):
    import copy

    from netbox_nso_plugin.models import NSOIntentRevision

    device, management = make_managed("timestamp-revision", 17893)
    rows, relations = render_candidates(device, management, 1)
    persist_candidates(rows, relations)
    before = NSOIntentRevision.objects.get(device=device, scope=scope).revision
    candidate = copy.copy(rows[index])
    candidate.accepted_at = ACCEPTED_AT + timedelta(days=1)
    persist_candidates((candidate,))
    rendered = delivery.render(scope, device.pk, management.adapter_device_id)
    assert wire_body(scope, rendered)["accepted_at"] == candidate.accepted_at.isoformat()
    revision = NSOIntentRevision.objects.get(device=device, scope=scope)
    assert revision.revision > before
    assert revision.verified_revision == revision.revision
    assert revision.verified_fingerprint == delivery.canonical_fingerprint(rendered.payload)


@pytest.mark.django_db
@pytest.mark.parametrize("scope,field", (("lacp", "bundles"), ("switchport", "interfaces")))
def test_empty_switching_snapshots_have_stable_explicit_acceptance_times(scope, field):
    device, management = make_managed("empty-timestamp", 17894)
    first = wire_body(scope, delivery.render(scope, device.pk, management.adapter_device_id))
    assert first[field] == []
    assert datetime.fromisoformat(first["accepted_at"]).tzinfo is not None
    assert first == wire_body(scope, delivery.render(scope, device.pk, management.adapter_device_id))


@pytest.mark.django_db
@pytest.mark.parametrize("status", ("accepted", "in_sync", "apply_failed", "deploying"))
@pytest.mark.parametrize("operation", ("create", "save", "set_update", "omitted_field"))
def test_writer_refuses_owned_rows_without_persisted_acceptance(status, operation):
    import copy
    from uuid import uuid4

    from ipam.models import VLAN

    from netbox_nso_plugin.intent_state import IntentMutationProtocolError
    from netbox_nso_plugin.models import NSOVLANState
    from netbox_nso_plugin.ownership_grants import OwnershipGrant
    from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, planned_set_update

    device, management = make_managed("missing-acceptance", 17895)
    vlan = VLAN.objects.create(vid=101, name="ACCEPTANCE")
    state = NSOVLANState(management=management, vlan=vlan, status="imported")
    if operation != "create":
        state.save()
    values = {"status": status, "apply_attempt_id": uuid4() if status == "deploying" else None}
    candidate = copy.copy(state)
    for name, value in values.items():
        setattr(candidate, name, value)
    if operation == "omitted_field":
        candidate.accepted_at = ACCEPTED_AT
    fields = None if operation == "create" else tuple(values)
    saves = (
        ()
        if operation == "set_update"
        else (
            planned_save(
                candidate, update_fields=fields, force_insert=operation == "create", natural_key=("management", "vlan")
            ),
        )
    )
    sets = (
        (planned_set_update(NSOVLANState.objects.filter(pk=state.pk), **values),) if operation == "set_update" else ()
    )
    with pytest.raises(IntentMutationProtocolError, match="accepted_at"):
        RendererMutationPlan.build(grant=OwnershipGrant("create"), saves=saves, set_updates=sets)
    assert not NSOVLANState.objects.filter(management=management, status=status).exists()
