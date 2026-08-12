# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1) — the delivery registry, pin O1.17.

The registry enumerates every delivery key the real push sites use, marks exactly the
sixteen mirrored scopes in protocol, takes the request mode per call rather than per scope,
and delivers through the push that owns each scope's own success side effects.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import SimpleTestCase, TestCase

from .mixins import IntentPushResetMixin

APP = "netbox_nso_plugin"


def _delivery_keys_at_the_push_sites() -> set[str]:
    """Scrape ``_push_changed((device_id, "<key>"), …)`` out of signals.py with the AST.

    A hand-kept list drifts (``ip`` versus ``interface_ip``, O-P12); the compiler's own
    view of the call sites cannot.
    """
    source = Path(inspect.getsourcefile(__import__(f"{APP}.signals", fromlist=["signals"]))).read_text()
    keys: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "_push_changed" or not node.args:
            continue
        key = node.args[0]
        if isinstance(key, ast.Tuple) and len(key.elts) == 2 and isinstance(key.elts[1], ast.Constant):
            keys.add(key.elts[1].value)
    return keys


class TestDeliveryRegistry(SimpleTestCase):
    """O1.17 — the registry against the real push sites."""

    def test_it_enumerates_exactly_the_keys_the_push_sites_use(self):
        from netbox_nso_plugin.delivery import delivery_keys

        assert set(delivery_keys()) == _delivery_keys_at_the_push_sites()
        assert len(delivery_keys()) == 18

    def test_exactly_the_sixteen_mirrored_scopes_are_in_protocol(self):
        """The two direct-apply keys write to NSO inside the request, so no receipt can be
        atomic with their effect (O-P12c): they stay out of the sequence path entirely."""
        from netbox_nso_plugin.delivery import delivery_keys

        in_protocol = {key for key, entry in delivery_keys().items() if entry.in_protocol}
        out_of_protocol = {key for key, entry in delivery_keys().items() if not entry.in_protocol}

        assert out_of_protocol == {"lacp", "switchport"}
        assert len(in_protocol) == 16
        assert "static_route" in in_protocol

    def test_the_request_mode_is_a_call_argument_not_a_registry_field(self):
        """A per-scope ``query_mode`` would make one scope's store-only resync its identity."""
        import dataclasses

        from netbox_nso_plugin.delivery import DeliveryKey, deliver

        fields = {f.name for f in dataclasses.fields(DeliveryKey)}
        assert fields == {"key", "label", "in_protocol", "marking_mode", "push_name"}
        assert "mode" in inspect.signature(deliver).parameters

    def test_every_drift_scope_names_a_registered_delivery_key(self):
        """The two registries name one scope differently, so the mapping is checked, not trusted.

        ``intent_drift`` declares which delivery key it re-syncs through (``interface_ip``
        there is ``ip`` here, O-P12). A scope naming a key this registry does not hold would
        make the re-sync raise at the one moment an operator is repairing a split brain.
        """
        from netbox_nso_plugin.delivery import delivery_keys
        from netbox_nso_plugin.intent_drift import _delivery_key, _scopes

        registry = delivery_keys()
        unknown = {scope["key"]: _delivery_key(scope) for scope in _scopes() if _delivery_key(scope) not in registry}
        assert unknown == {}

    def test_marking_mode_is_declared_per_key(self):
        """O3.4: only static routes activate; every other key keeps its query flag."""
        from netbox_nso_plugin.delivery import MARKING_PER_OBJECT, MARKING_QUERY_FLAG, delivery_keys

        registry = delivery_keys()
        assert registry["static_route"].marking_mode == MARKING_PER_OBJECT
        assert {key: entry.marking_mode for key, entry in registry.items() if key != "static_route"} == {
            key: MARKING_QUERY_FLAG for key in registry if key != "static_route"
        }

    def test_every_entry_names_a_push_that_exists(self):
        """The registry holds names, so a typo has to fail here rather than at push time."""
        from netbox_nso_plugin import signals
        from netbox_nso_plugin.delivery import delivery_keys

        missing = [entry.push_name for entry in delivery_keys().values() if not hasattr(signals, entry.push_name)]
        assert missing == []


def _fixture(tag: str, adapter_device_id: int):
    from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

    mfg, _ = Manufacturer.objects.get_or_create(name=f"Dr{tag}Mfg", slug=f"dr{tag}mfg")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"Dr{tag}Dev", slug=f"dr{tag}dev")
    role, _ = DeviceRole.objects.get_or_create(name=f"Dr{tag}Role", slug=f"dr{tag}role")
    site, _ = Site.objects.get_or_create(name=f"Dr{tag}Site", slug=f"dr{tag}site")
    device = Device.objects.create(name=f"dr-{tag}-rtr", device_type=dt, role=role, site=site)
    inst, _ = NSOInstance.objects.get_or_create(name=f"dr-{tag}-inst", defaults={"adapter_instance_id": f"dr-{tag}"})
    mgmt = NSODeviceManagement.objects.create(
        device=device, nso_instance=inst, nso_device_name=f"nso-dr-{tag}", adapter_device_id=adapter_device_id
    )
    return device, mgmt


class TestDeliverySuccessHooks(IntentPushResetMixin, TestCase):
    """O1.17 — delivering through the registry keeps every per-scope success side effect."""

    _CFG = {"url": "http://adapter", "token": "tok", "verify_tls": True, "ca_cert_path": None, "timeout": 30}

    def test_an_unknown_mode_is_rejected_before_the_send(self):
        from netbox_nso_plugin.delivery import Rendered, send

        device, _mgmt = _fixture("mode", 7300)
        sent = []
        rendered = Rendered(
            key=(device.pk, "vlan"),
            payload=[],
            do_push=lambda body: sent.append(body),
        )

        with self.assertRaisesRegex(ValueError, "unknown delivery mode"):
            send(rendered, rendered.payload, mode="store-only")

        assert sent == []

    def test_a_backfill_only_deletion_is_rejected_before_the_send(self):
        from netbox_nso_plugin import delivery

        device, _mgmt = _fixture("backfill-mark", 7307)
        sent = []
        rendered = delivery.Rendered(
            key=(device.pk, "vlan"),
            payload=[],
            do_push=lambda body: sent.append(body),
        )

        with self.assertRaisesRegex(ValueError, "a backfill-only request carries no authority"):
            delivery.send(
                rendered,
                rendered.payload,
                mode=delivery.MODE_BACKFILL_ONLY,
                mark=True,
            )

        assert sent == []

    def test_receipt_adapter_device_failure_uses_the_client_transport_exception(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.delivery import deliver

        from ._outbox_case import ReceiptAdapter

        device, _mgmt = _fixture("transport", 7306)
        adapter = ReceiptAdapter()
        adapter.fail_devices = {7306}
        config, session = adapter.patches()
        with config, session, self.assertRaises(AdapterError):
            deliver("vlan", device.pk, 7306)

    def test_a_static_route_delivery_records_the_settlement_expectation(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.intent_generation import allocate_intent_generation
        from netbox_nso_plugin.models import NSOStaticRouteState

        device, mgmt = _fixture("sr", 7301)
        route = StaticRoute.objects.create(prefix="198.51.100.0/24", next_hop="198.51.100.1", metric=1)
        generation = allocate_intent_generation()
        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent"):
            state = NSOStaticRouteState.objects.create(
                management=mgmt,
                static_route=route,
                status="accepted",
                nso_prefix="198.51.100.0/24",
                nso_next_hop="198.51.100.1",
                intent_generation=generation,
            )
        echo = {"count": 1, "routes": [{"route_id": route.pk, "generation": generation, "fingerprint": "fp-1"}]}

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", return_value=echo):
            deliver("static_route", device.pk, 7301)

        state.refresh_from_db()
        assert (state.expected_generation, state.expected_fingerprint) == (generation, "fp-1")

    def test_a_route_policy_delivery_records_the_unsupported_members(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSORoutePolicyState

        device, mgmt = _fixture("rp", 7302)
        with patch("netbox_nso_plugin.adapter_client.put_route_policy_intent"):
            row = NSORoutePolicyState.objects.create(
                management=mgmt,
                family="community_list",
                object_name="dr-cl-1",
                status="accepted",
                unsupported_members=["color:*:1"],
            )
        response = {"unsupported_members": {"dr-cl-1": ["large:1:2:3"]}}

        with patch("netbox_nso_plugin.adapter_client.put_route_policy_intent", return_value=response):
            deliver("route_policy", device.pk, 7302)

        row.refresh_from_db()
        assert row.unsupported_members == ["large:1:2:3"]

    def test_render_builders_do_not_claim_to_return_adapter_responses(self):
        from netbox_nso_plugin import signals

        device, mgmt = _fixture("returns", 7305)
        marker = object()
        with patch.object(signals, "_push_changed", return_value=marker):
            static_result = signals._push_static_route_intent_for_device(device.pk, mgmt.adapter_device_id)
            policy_result = signals._push_route_policy_intent_for_device(device.pk, mgmt.adapter_device_id)

        assert static_result is None
        assert policy_result is None

    def test_a_success_hook_failure_does_not_reclassify_the_send(self):
        from netbox_nso_plugin.delivery import Rendered, send

        device, mgmt = _fixture("hook", 7304)
        response = {"count": 1}
        rendered = Rendered(
            key=(device.pk, "vlan"),
            payload=[],
            do_push=lambda body: response,
            on_response=lambda answer: (_ for _ in ()).throw(RuntimeError("side effect failed")),
        )

        with self.assertLogs("netbox_nso_plugin.signals", level="ERROR"):
            answer = send(rendered, rendered.payload)

        assert answer == response
        mgmt.refresh_from_db()
        assert "vlan" not in (mgmt.intent_push_errors or {})

    def test_a_patch_of_a_push_never_outlives_the_registry_build(self):
        """The registry is built once per process, so a bound function object freezes in
        whatever mock happened to be installed at that moment (CI: "rendered 0 bodies")."""
        from netbox_nso_plugin import delivery

        device, mgmt = _fixture("calltime", 7307)

        with patch.dict(delivery._REGISTRY, clear=True):
            with patch("netbox_nso_plugin.signals._push_ip_intent_for_device") as mock_push:
                with self.assertRaisesRegex(RuntimeError, "rendered 0 bodies"):
                    delivery.render("ip", device.pk, mgmt.adapter_device_id)
            mock_push.assert_called_once_with(device.pk, mgmt.adapter_device_id)

            rendered = delivery.render("ip", device.pk, mgmt.adapter_device_id)

        assert rendered.key == (device.pk, "ip")
        assert rendered.payload == []

    def test_an_out_of_protocol_delivery_carries_no_sequence_header(self):
        """``lacp`` and ``switchport`` keep today's direct client calls (Rev 15 split)."""
        from netbox_nso_plugin.delivery import render, send

        from ._adapter_http import make_response, make_session

        device, _mgmt = _fixture("lacp", 7303)
        session = make_session(response=make_response(200, json_data={"status": "ok"}))
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=self._CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
        ):
            rendered = render("lacp", device.pk, 7303)
            send(rendered, rendered.payload, push_seq=17)

        assert session.request.call_count >= 1
        for call in session.request.call_args_list:
            assert "X-Push-Seq" not in (call.kwargs.get("headers") or {})
