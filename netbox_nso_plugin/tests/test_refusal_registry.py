# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
import pytest
from django.apps import apps


def test_registry_accounts_for_every_pushed_family():
    from netbox_nso_plugin.delivery import delivery_keys
    from netbox_nso_plugin.intent_state import _GLOBAL_LIFECYCLE_FIELDS, OVERLAY_MODEL_RANKS, renderer_input_specs
    from netbox_nso_plugin.refusal_registry import EXCLUDED_MODELS, OVERLAYS

    assert set(OVERLAYS) | set(EXCLUDED_MODELS) == set(OVERLAY_MODEL_RANKS)
    assert set(EXCLUDED_MODELS) == {"netbox_nso_plugin.nsobgppeertemplatestate"}
    assert EXCLUDED_MODELS["netbox_nso_plugin.nsobgppeertemplatestate"] == "read_only_not_pushed"
    assert "netbox_nso_plugin.nsobgprouterstate" in OVERLAYS
    assert {key for entry in OVERLAYS.values() for key in entry.delivery_keys} == set(delivery_keys())
    for label, spec in OVERLAYS.items():
        model = apps.get_model(label)
        assert spec.device_lookup in {"management__device_id", "interface__device_id"}
        assert spec.read_families and spec.subjects and spec.counter_keys
        for name, expected in (
            ("apply_attempt_id", "UUIDField"),
            ("last_apply_at", "DateTimeField"),
            ("last_apply_error", spec.error_type),
        ):
            assert model._meta.get_field(name).get_internal_type() == expected
            assert name in _GLOBAL_LIFECYCLE_FIELDS
            assert name in renderer_input_specs()[label].lifecycle_fields


def test_router_and_two_peers_have_exhaustive_settlement_owners(db):
    import json
    from pathlib import Path

    from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
    from netbox_nso_plugin.delivery import render
    from netbox_nso_plugin.refusal_protocol import RowRef
    from netbox_nso_plugin.refusal_registry import owner_spec

    from ._outbox_case import content_update, make_managed, without_commit_drain
    from .test_bgp_router_reconcile import peer_entry, router_document, scope_entry

    Model = apps.get_model("netbox_nso_plugin", "NSOBGPRouterState")
    device, management = make_managed("router-registry", 1767)
    fixture = json.loads((Path(__file__).parent / "fixtures" / "refusal_settlement.json").read_text(encoding="utf-8"))[
        "bgp"
    ]
    terminal = json.loads((Path(__file__).parent / "fixtures" / "bgp_router_terminal.json").read_text(encoding="utf-8"))
    peers = _reconcile_bgp_config(
        device,
        router_document(
            device,
            scopes=[
                scope_entry(peers=[peer_entry("192.0.2.1"), peer_entry("192.0.2.2")]),
            ],
        ),
    )
    router = Model.objects.get(management=management)
    with without_commit_drain():
        router = content_update(router, status="accepted")
        peers = [content_update(peer, status="accepted") for peer in peers]
    payload = render("bgp", device.pk, management.adapter_device_id).payload
    assert isinstance(payload, list) and len(payload) == 1
    entry = payload[0]
    owners = {}
    ref = owner_spec("bgp.router").logical_ref("bgp.router", (("router", (entry["asn"],)),))
    owners[ref] = router
    by_address = {peer.peer_address_str: peer for peer in peers}
    for scope in entry["scopes"]:
        for peer in scope["peers"]:
            subject = "bgp.router.scope.peer"
            path = (("router", (entry["asn"],)), ("scope", (scope["vrf"],)), ("peer", (peer["peer_address"],)))
            ref = owner_spec(subject).logical_ref(subject, path)
            assert ref not in owners
            owners[ref] = by_address[peer["peer_address"]]
    outcomes = terminal["row_outcomes"]
    expected = [RowRef(**value["row"]) for value in outcomes]
    assert len(expected) == len(set(expected)) == len(owners) == 3
    assert set(owners) == set(expected)
    assert {value["row"]["key"]: value["disposition"] for value in outcomes} == {
        owner_spec("bgp.router").logical_ref("bgp.router", (("router", (entry["asn"],)),)).key: "not_applied",
        fixture["expected"][0]["row"]["key"]: "refused",
        fixture["expected"][1]["row"]["key"]: "not_applied",
    }
    assert {owner.pk for owner in owners.values() if owner._meta.label_lower == router._meta.label_lower} == {router.pk}
    assert len({value["carrier"]["row"]["key"] for value in outcomes}) == fixture["failed_carrier_count"] == 1
    assert terminal["bgp_count_by_outcome"] == {"in_sync": 0, "apply_failed": 1, "not_applied": 0, "unavailable": 0}


def test_every_pushed_overlay_has_settlement_owner():
    from netbox_nso_plugin.delivery import delivery_keys
    from netbox_nso_plugin.intent_state import OVERLAY_MODEL_RANKS
    from netbox_nso_plugin.refusal_registry import EXCLUDED_MODELS, OVERLAYS, owner_spec

    assert set(OVERLAY_MODEL_RANKS) - set(EXCLUDED_MODELS) == set(OVERLAYS)
    assert {key for spec in OVERLAYS.values() for key in spec.delivery_keys} == set(delivery_keys())
    for label, spec in OVERLAYS.items():
        assert apps.get_model(label) is spec.model
        for subject in spec.subjects:
            assert owner_spec(subject) is spec
    assert owner_spec("bgp.router").model_label == "netbox_nso_plugin.nsobgprouterstate"


def test_ospf_interface_row_ref_uses_only_emitted_interface_key():
    from netbox_nso_plugin.refusal_protocol import RefusalProtocolError, RowRef
    from netbox_nso_plugin.refusal_registry import owner_spec

    spec = owner_spec("ospf.interface-config")
    path = (("interface-config", ("Ethernet1",)),)
    assert spec.logical_ref("ospf.interface-config", path) == RowRef(
        "ospf.interface-config", "19125bd23a4b3074575893fb6ceaefaa2003899328da1f6d38519ee5e9eaeb14"
    )
    with pytest.raises(RefusalProtocolError):
        spec.logical_ref("ospf.interface-config", (("interface-config", ("Ethernet1", "ipv4", "")),))


def test_registry_rejects_unpushed_subjects_and_models():
    from netbox_nso_plugin.refusal_registry import RegistryError, model_spec, owner_spec

    for subject in ("bgp.router.scope.peer.peer-address-family", "route-policy.prefix-list.entry", "unknown"):
        with pytest.raises(RegistryError):
            owner_spec(subject)
    with pytest.raises(RegistryError):
        model_spec("netbox_nso_plugin.nsobgppeertemplatestate")
    with pytest.raises(RegistryError):
        model_spec("dcim.interface")


def test_isis_contributors_share_one_instance_owner():
    from netbox_nso_plugin.refusal_registry import RegistryError, owner_spec

    spec = owner_spec("isis.process-config")
    assert spec is owner_spec("isis.process-config.level")
    paths = (
        ("isis.process-config", (("process-config", ("placeholder",)),)),
        ("isis.process-config.level", (("process-config", ("placeholder",)), ("level", (1,)))),
    )
    assert len(spec.contributor_refs(paths)) == 2
    with pytest.raises(RegistryError):
        spec.contributor_refs((paths[0], paths[0]))
