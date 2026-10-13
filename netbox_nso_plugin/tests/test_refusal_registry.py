# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
import pytest
from django.apps import apps
from django.db import connection
from django.test.utils import CaptureQueriesContext


def test_registry_accounts_for_every_pushed_family():
    from netbox_nso_plugin.delivery import delivery_keys
    from netbox_nso_plugin.intent_state import _GLOBAL_LIFECYCLE_FIELDS, OVERLAY_MODEL_RANKS, renderer_input_specs
    from netbox_nso_plugin.refusal_registry import BLOCKED_SUBJECTS, EXCLUDED_MODELS, OVERLAYS

    assert set(OVERLAYS) | set(EXCLUDED_MODELS) == set(OVERLAY_MODEL_RANKS)
    assert set(EXCLUDED_MODELS) == {"netbox_nso_plugin.nsobgppeertemplatestate"}
    assert EXCLUDED_MODELS["netbox_nso_plugin.nsobgppeertemplatestate"] == "read_only_not_pushed"
    assert BLOCKED_SUBJECTS == {"bgp.router": "no_router_overlay"}
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


def test_unmapped_router_outcome_is_rejected(request):
    from netbox_nso_plugin.refusal_registry import RegistryError, owner_spec

    request.getfixturevalue("db")
    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(RegistryError, match="blocked"):
            owner_spec("bgp.router")
    assert list(queries) == []


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
