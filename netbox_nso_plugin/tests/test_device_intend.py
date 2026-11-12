# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Exercise Intend through authorized HTTP requests and ordinary delivery."""

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from netbox_nso_plugin.models import (
    NSOIntentOutboxEntry,
    NSOIntentRevision,
    NSOInterfaceMtuState,
    NSOOwnershipAcquisition,
    NSOOwnershipManifest,
)

from ._adapter_http import make_apply_preflight, make_response
from ._authority_case import grant, operator
from ._outbox_case import content_update, make_managed, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin
from .test_intent_acquisition import IntendAdapter


class OperatorAdapter(IntendAdapter):
    def _handle(self, method, url, **kwargs):
        if url.endswith("/actions/apply-preflight") and "selected" in kwargs.get("json", {}):
            return make_response(200, make_apply_preflight(kwargs["json"]["selected"]))
        response = super()._handle(method, url, **kwargs)
        if method == "PUT":
            self.promotes = False
        return response


class IntendViewFixture:
    adapter_class = OperatorAdapter

    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("intend-ui", 1788)
        self.interface = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t", mtu=9000)
        self.user = get_user_model().objects.create_superuser(username="intend-ui-operator")
        self.client.force_login(self.user)
        self.adapter = self.adapter_class()
        for patcher in self.adapter.patches():
            patcher.start()
            self.addCleanup(patcher.stop)

    def url(self, action):
        return reverse(f"plugins:netbox_nso_plugin:device_nso_{action}", kwargs={"pk": self.device.pk})

    def selection(self, scope="interface_mtu", native=None, **options):
        native = native or self.interface
        choices = self.client.get(self.url("differences")).context["intend_choices"]
        choice = next(
            choice
            for choice in choices
            if choice["scope"] == scope
            and choice["selection"]["native_id"] == native.pk
            and choice["selection"]["native_model"] == native._meta.label_lower
        )
        token = choice["token"]
        return {"intend_row": [token], **{f"{name}-{token}": str(value).lower() for name, value in options.items()}}

    def preview(self, data=None):
        return self.client.post(self.url("intend_preview"), data or self.selection())

    def confirm(self, response):
        return self.client.post(self.url("intend_confirm"), {"confirmation": response.context["result"].confirmation})

    def bookkeeping(self):
        return [
            list(model.objects.order_by("pk").values())
            for model in (
                NSOInterfaceMtuState,
                NSOOwnershipManifest,
                NSOOwnershipAcquisition,
                NSOIntentRevision,
                NSOIntentOutboxEntry,
            )
        ]


class TestIntendRoutes(SimpleTestCase):
    def test_preview_and_confirm_routes_exist(self):
        for action in ("preview", "confirm"):
            self.assertTrue(reverse(f"plugins:netbox_nso_plugin:device_nso_intend_{action}", kwargs={"pk": 1}))


class TestIntendControls(IntendViewFixture, IntentPushResetMixin, TestCase):
    def test_post_routes_and_explicit_selection_controls(self):
        for action in ("intend_preview", "intend_confirm"):
            self.assertEqual(self.client.get(self.url(action)).status_code, 405)
        response = self.client.get(self.url("differences"))
        self.assertContains(response, "data-intend-form")
        self.assertContains(response, "data-intend-row")
        self.assertContains(response, "Preview Intend")
        self.assertContains(response, "Dependencies and native rows")

    def test_native_mtu_preview_confirm_persists_and_shows_pending(self):
        NSOInterfaceMtuState.objects.create(
            management=self.management, interface=self.interface, status="error", l2_mtu=1500
        )
        before = self.bookkeeping()
        response = self.preview()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"].status, "ready")
        self.assertContains(response, "9000")
        self.assertContains(response, "Selected overlays")
        self.assertContains(response, "+ placeholder configuration")
        self.assertContains(response, "+ device-intent")
        self.assertEqual(self.bookkeeping(), before)
        result = self.confirm(response)
        row = NSOInterfaceMtuState.objects.get(interface=self.interface)
        self.assertEqual((row.l2_mtu, row.status), (9000, "accepted"))
        self.assertContains(result, "acquired; delivery pending")
        self.assertContains(result, "Acquisition is not deployment")
        self.assertEqual(NSOOwnershipManifest.objects.get(device_id=self.device.pk).grant_kind, "intend")
        self.assertEqual(
            set(NSOIntentOutboxEntry.objects.filter(device=self.device).values_list("kind", flat=True)), {"ordinary"}
        )
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.mtu, 9000)
        self.assertContains(self.client.get(self.url("differences")), "acquired; delivery pending")

    def test_filter_and_page_requests_keep_the_native_catalog_and_stable_identities(self):
        bundle = Interface.objects.create(device=self.device, name="Port-channel1", type="lag")
        selected = self.selection("lacp", bundle)
        response = self.client.get(self.url("differences"), {"scope": "interface_mtu", "kind": "mismatch", "page": 2})
        choice = next(
            choice for choice in response.context["intend_choices"] if choice["token"] == selected["intend_row"][0]
        )
        self.assertNotIn(choice, [item for row in response.context["page_obj"] for item in row["intend_choices"]])
        self.assertContains(response, f'name="lag_id-{choice["token"]}"')

    def test_a_native_choice_hidden_after_selection_refuses_preview(self):
        data = self.selection()
        user = operator((("add",), NSOInterfaceMtuState, None))
        self.client.force_login(user)
        response = self.client.get(self.url("differences"))
        self.assertEqual(response.context["intend_choices"], [])
        self.assertNotContains(response, "Ethernet1")
        self.assertContains(self.preview(data), "No acquisition occurred")

    def test_delivery_effect_refuses_at_preview_and_confirm_without_writes(self):
        before = self.bookkeeping()
        self.adapter.promotes = True
        response = self.preview()
        self.assertContains(response, "No acquisition occurred")
        self.assertContains(response, "interface_mtu: removal")
        self.assertContains(response, "ordinary delivery")
        self.assertNotContains(response, "Confirm Intend")
        self.assertEqual(self.bookkeeping(), before)
        self.adapter.promotes = False
        ready = self.preview()
        self.adapter.promotes = True
        response = self.confirm(ready)
        self.assertContains(response, "No acquisition occurred")
        self.assertContains(response, "interface_mtu: removal")
        self.assertEqual(self.bookkeeping(), before)

    def test_posted_rendered_content_cannot_override_native_values(self):
        data = self.selection()
        data["candidates"] = '{"interface_mtu":{"interfaces":[{"mtu":42}]}}'
        response = self.preview(data)
        self.assertEqual(response.context["result"].status, "ready")
        self.assertEqual(self.adapter.candidates[-1]["interface_mtu"]["interfaces"][0]["mtu"], 9000)
        token = data["intend_row"][0]
        data["intend_row"] = [token + "tampered"]
        before = self.bookkeeping()
        self.assertContains(self.preview(data), "No acquisition occurred")
        self.assertEqual(self.bookkeeping(), before)
        self.assertEqual(len(self.adapter.candidates), 1)

    def test_native_visibility_and_overlay_add_and_change_permissions(self):
        data = self.selection()
        users = [
            operator((("add",), NSOInterfaceMtuState, None)),
            operator((("view",), Interface, None)),
            operator((("view",), Interface, {"pk": self.interface.pk + 1}), (("add",), NSOInterfaceMtuState, None)),
        ]
        for user in users:
            with self.subTest(user=user.username):
                self.client.force_login(user)
                before = self.bookkeeping()
                self.assertContains(self.preview(data), "No acquisition occurred")
                self.assertEqual(self.bookkeeping(), before)
        NSOInterfaceMtuState.objects.create(
            management=self.management, interface=self.interface, status="imported", l2_mtu=1500
        )
        user = operator((("view",), Interface, None), (("add",), NSOInterfaceMtuState, None))
        self.client.force_login(user)
        before = self.bookkeeping()
        self.assertContains(self.preview(data), "change")
        self.assertEqual(self.bookkeeping(), before)
        self.assertEqual(self.adapter.candidates, [])

    def test_restricted_management_and_overlay_objects_are_refused(self):
        from dcim.models import Device

        from netbox_nso_plugin.models import NSODeviceManagement

        data = self.selection()
        user = get_user_model().objects.create_user(username="intend-restricted-management")
        grant(user, ("view",), Device)
        grant(user, ("view", "change"), NSODeviceManagement, {"pk": self.management.pk + 1})
        self.client.force_login(user)
        self.assertEqual(self.preview(data).status_code, 404)
        self.assertEqual(self.client.post(self.url("intend_confirm"), {"confirmation": "invalid"}).status_code, 404)
        row = NSOInterfaceMtuState.objects.create(
            management=self.management, interface=self.interface, status="imported", l2_mtu=1500
        )
        user = operator((("view",), Interface, None), (("change",), NSOInterfaceMtuState, {"pk": row.pk + 1}))
        self.client.force_login(user)
        before = self.bookkeeping()
        self.assertContains(self.preview(data), "not visible")
        self.assertEqual(self.bookkeeping(), before)
        self.assertEqual(self.adapter.candidates, [])

    def test_confirmation_is_bound_to_operator_device_age_and_inputs(self):
        import time
        from unittest.mock import patch

        from netbox_nso_plugin.intent_acquisition import CONFIRMATION_MAX_AGE

        preview = self.preview()
        token = preview.context["result"].confirmation
        other_device, _other_management = make_managed("intend-other-device", 1789)
        before = self.bookkeeping()
        self.assertContains(
            self.client.post(self.url("intend_confirm"), {"confirmation": token + "tampered"}),
            "No acquisition occurred",
        )
        other = get_user_model().objects.create_superuser(username="intend-other-operator")
        self.client.force_login(other)
        self.assertContains(self.client.post(self.url("intend_confirm"), {"confirmation": token}), "another operator")
        self.client.force_login(self.user)
        other_url = reverse("plugins:netbox_nso_plugin:device_nso_intend_confirm", kwargs={"pk": other_device.pk})
        self.assertContains(self.client.post(other_url, {"confirmation": token}), "another operator or management")
        with patch("django.core.signing.time.time", return_value=time.time() - CONFIRMATION_MAX_AGE - 10):
            expired = self.preview()
        self.assertContains(self.confirm(expired), "invalid or expired")
        self.assertEqual(self.bookkeeping(), before)
        self.interface.mtu = 8500
        self.interface.save(update_fields=("mtu",))
        before = self.bookkeeping()
        self.assertContains(self.confirm(preview), "inputs changed")
        self.assertEqual(self.bookkeeping(), before)

    def test_duplicate_confirmation_creates_no_second_acquisition(self):
        preview = self.preview()
        self.assertContains(self.confirm(preview), "acquired; delivery pending")
        before = self.bookkeeping()
        self.assertContains(self.confirm(preview), "No acquisition occurred")
        self.assertEqual(self.bookkeeping(), before)

    def test_confirm_rechecks_native_and_overlay_permissions(self):
        data = self.selection()
        user = operator((("add",), NSOInterfaceMtuState, None))
        permission = grant(user, ("view",), Interface)
        self.client.force_login(user)
        preview = self.preview(data)
        self.assertEqual(preview.context["result"].status, "ready")
        permission.delete()
        before = self.bookkeeping()
        self.assertContains(self.confirm(preview), "No acquisition occurred")
        self.assertEqual(self.bookkeeping(), before)

        grant(user, ("view",), Interface)
        row = NSOInterfaceMtuState.objects.create(
            management=self.management, interface=self.interface, status="imported", l2_mtu=1500
        )
        change = grant(user, ("change",), NSOInterfaceMtuState, {"pk": row.pk})
        preview = self.preview(data)
        self.assertEqual(preview.context["result"].status, "ready")
        change.constraints = {"pk": row.pk + 1}
        change.save()
        before = self.bookkeeping()
        self.assertContains(self.confirm(preview), "not visible")
        self.assertEqual(self.bookkeeping(), before)

    def test_native_view_and_overlay_add_allow_acquisition_without_native_change_permission(self):
        data = self.selection()
        user = operator((("view",), Interface, {"pk": self.interface.pk}), (("add",), NSOInterfaceMtuState, None))
        self.client.force_login(user)
        self.assertFalse(user.has_perm("dcim.change_interface"))
        preview = self.preview(data)
        self.assertEqual(preview.context["result"].status, "ready", preview.content)
        self.assertContains(self.confirm(preview), "acquired; delivery pending")
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.mtu, 9000)
        self.assertEqual(NSOInterfaceMtuState.objects.get(interface=self.interface).status, "accepted")

    def test_csrf_and_confirmation_form_token(self):
        import re

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        data = self.selection()
        self.assertEqual(csrf_client.post(self.url("intend_preview"), data).status_code, 403)
        page = csrf_client.get(self.url("differences"))
        csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        data["csrfmiddlewaretoken"] = csrf
        preview = csrf_client.post(self.url("intend_preview"), data)
        self.assertEqual(preview.context["result"].status, "ready")
        html = preview.content.decode()
        form = html.split("data-intend-confirm>", 1)[1].split("</form>", 1)[0]
        self.assertEqual(set(re.findall(r'name="([^"]+)"', form)), {"confirmation", "csrfmiddlewaretoken"})
        token = preview.context["result"].confirmation
        self.assertEqual(csrf_client.post(self.url("intend_confirm"), {"confirmation": token}).status_code, 403)
        result = csrf_client.post(self.url("intend_confirm"), {"confirmation": token, "csrfmiddlewaretoken": csrf})
        self.assertContains(result, "acquired; delivery pending")


class TestIntendDependencies(IntendViewFixture, IntentPushResetMixin, TestCase):
    def test_peer_policy_dependency_can_be_selected_without_a_difference(self):
        from ipam.models import ASN, RIR, IPAddress
        from netbox_routing.models import (
            BGPAddressFamily,
            BGPPeer,
            BGPPeerAddressFamily,
            BGPRouter,
            BGPScope,
            PrefixList,
        )

        from netbox_nso_plugin.models import NSOBGPPeerState, NSORoutePolicyState

        registry = RIR.objects.create(name="Intend private ASNs", slug="intend-private-asns")
        router = BGPRouter.objects.create(
            assigned_object=self.device, asn=ASN.objects.create(rir=registry, asn=64512), router_id="198.18.0.1"
        )
        scope = BGPScope.objects.create(router=router)
        family = BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
        peer = BGPPeer.objects.create(scope=scope, peer=IPAddress.objects.create(address="198.18.0.2/32"))
        policy = PrefixList.objects.create(name="INTEND-PEER-PREFIXES", family=4)
        BGPPeerAddressFamily.objects.create(assigned_object=peer, address_family=family, prefixlist_in=policy)
        data = {"intend_row": self.selection("bgp", router)["intend_row"] + self.selection("bgp", peer)["intend_row"]}
        before = self.bookkeeping()
        self.assertContains(self.preview(data), policy.name)
        self.assertEqual(self.bookkeeping(), before)
        data["intend_row"] += self.selection("route_policy", policy)["intend_row"]
        preview = self.preview(data)
        self.assertEqual(preview.context["result"].status, "ready", preview.content)
        self.assertContains(self.confirm(preview), "acquired; delivery pending")
        self.assertEqual(NSOBGPPeerState.objects.get(bgp_peer=peer).status, "accepted")
        self.assertEqual(NSORoutePolicyState.objects.get(object_name=policy.name).status, "accepted")

    def test_lacp_member_refusal_is_corrected_with_explicit_bundle_without_a_difference(self):
        from netbox_nso_plugin.models import NSOLACPBundleState, NSOLACPMemberState

        bundle = Interface.objects.create(device=self.device, name="Port-channel1", type="lag")
        member = Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t", lag=bundle)
        member_data = self.selection("lacp", member)
        before = self.bookkeeping()
        self.assertContains(self.preview(member_data), "bundle")
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSOLACPMemberState.objects.exists())
        self.assertFalse(NSOLACPBundleState.objects.exists())
        bundle_data = self.selection("lacp", bundle, lag_id=7)
        data = {**bundle_data, "intend_row": member_data["intend_row"] + bundle_data["intend_row"]}
        preview = self.preview(data)
        self.assertEqual(preview.context["result"].status, "ready", preview.content)
        self.assertContains(preview, "lag_id")
        self.assertContains(self.confirm(preview), "acquired; delivery pending")
        self.assertEqual(NSOLACPBundleState.objects.get(interface=bundle).lag_id, 7)
        self.assertEqual(NSOLACPMemberState.objects.get(interface=member).status, "accepted")

    def test_restart_is_explicit_and_invalid_options_write_nothing(self):
        self.confirm(self.preview())
        row = NSOInterfaceMtuState.objects.get(interface=self.interface)
        accepted_at = row.accepted_at
        NSOOwnershipManifest.objects.filter(device_id=self.device.pk).update(ownership_state="retired")
        before = self.bookkeeping()
        self.assertContains(self.preview(), "Explicit restart")
        self.assertEqual(self.bookkeeping(), before)
        invalid = self.selection(restart="false")
        self.assertContains(self.preview(invalid), "No acquisition occurred")
        preview = self.preview(self.selection(restart=True))
        self.assertContains(preview, "restart")
        self.assertContains(self.confirm(preview), "acquired; delivery pending")
        row.refresh_from_db()
        self.assertGreater(row.accepted_at, accepted_at)
        bundle = Interface.objects.create(device=self.device, name="Port-channel1", type="lag")
        before = self.bookkeeping()
        self.assertContains(self.preview(self.selection("lacp", bundle, lag_id=0)), "No acquisition occurred")
        self.assertEqual(self.bookkeeping(), before)

    def test_restart_control_appears_only_for_a_native_row_with_an_ended_episode(self):
        token = self.selection()["intend_row"][0]
        catalog = self.client.get(self.url("differences")).content.decode()
        self.assertNotIn(f'name="restart-{token}"', catalog)
        self.assertEqual(catalog.count(token), 1)
        self.confirm(self.preview())
        NSOOwnershipManifest.objects.filter(device_id=self.device.pk).update(ownership_state="retired")
        catalog = self.client.get(self.url("differences")).content.decode()
        self.assertIn(f'name="restart-{token}"', catalog)
        bundle = Interface.objects.create(device=self.device, name="Port-channel1", type="lag")
        other = self.selection("lacp", bundle)["intend_row"][0]
        self.assertNotIn(f'name="restart-{other}"', self.client.get(self.url("differences")).content.decode())

    def test_preview_names_overlays_keys_and_diffs_for_operators(self):
        NSOInterfaceMtuState.objects.create(
            management=self.management, interface=self.interface, status="error", l2_mtu=1500
        )
        ready = self.preview()
        self.assertContains(ready, "Overlay: NSO Interface MTU State.")
        self.assertContains(ready, "Explicit options: none.")
        self.assertContains(ready, "Retained owners (0)")
        self.assertContains(ready, "CLI diff")
        self.assertNotContains(ready, "netbox_nso_plugin.nsointerfacemtustate")
        self.confirm(ready)
        NSOOwnershipManifest.objects.filter(device_id=self.device.pk).update(ownership_state="retired")
        refused = self.preview()
        self.assertContains(refused, f"interface_mtu: Interface #{self.interface.pk}. State key: <code>none</code>")

    def test_preview_lists_only_dependencies_the_operator_can_view(self):
        self.confirm(self.preview())
        owner = NSOInterfaceMtuState.objects.get(interface=self.interface)
        other = Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t", mtu=9100)
        data = self.selection(native=other)
        self.client.force_login(
            operator((("view",), Interface, {"pk": other.pk}), (("add",), NSOInterfaceMtuState, None))
        )
        response = self.preview(data)
        details = response.context["result"].details
        self.assertEqual(response.context["result"].status, "ready")
        self.assertEqual((details["retained"], details["hidden_retained"]), ([], 1))
        self.assertEqual(
            {(row["model"], row["pk"]) for row in details["dependencies"]},
            {
                ("dcim.device", self.device.pk),
                ("dcim.interface", other.pk),
                ("netbox_nso_plugin.nsodevicemanagement", self.management.pk),
            },
        )
        self.assertEqual(details["hidden_dependencies"], 3)
        self.assertContains(response, "Retained owners (1)")
        self.assertContains(response, "3 not visible to you.")
        self.assertNotContains(response, f"Interface #{self.interface.pk}<")
        self.assertNotContains(response, f"NSO Interface MTU State #{owner.pk}")

    def test_preview_validates_tokens_without_difference_projections(self):
        from unittest import mock

        from netbox_nso_plugin import difference_projection

        data = self.selection()
        real = difference_projection.native_entries
        with mock.patch.object(difference_projection, "native_entries", autospec=True, side_effect=real) as spy:
            response = self.preview(data)
        self.assertEqual(response.context["result"].status, "ready")
        spy.assert_not_called()

    def test_ip_prefix_mismatch_row_offers_its_native_choice(self):
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOFamilyObservation, NSOFamilyReadState
        from netbox_nso_plugin.observations import observation_defaults

        from ._observation_case import ip_observation, observation

        address = IPAddress.objects.create(address="198.18.0.1/24", assigned_object=self.interface)
        state, _ = NSOFamilyReadState.objects.get_or_create(management=self.management, family="interface_ip")
        for vrf in (None, ""):
            with self.subTest(device_vrf=vrf):
                snapshot = observation(
                    "interface_ip", interfaces=[ip_observation(address="198.18.0.1/32", prefix_length=32, vrf=vrf)]
                )
                NSOFamilyObservation.objects.update_or_create(
                    read_state=state, defaults=observation_defaults("interface_ip", 1, 1, snapshot)
                )
                response = self.client.get(self.url("differences"), {"scope": "ip"})
                (row,) = [row for row in response.context["page_obj"] if row["kind"] == "mismatch"]
                self.assertEqual([choice["selection"]["native_id"] for choice in row["intend_choices"]], [address.pk])

    def _routing_snapshot(self, family, document, coverage=None):
        from netbox_nso_plugin.models import NSOFamilyObservation, NSOFamilyReadState
        from netbox_nso_plugin.observations import observation_defaults

        from ._routing_observation_case import routing_observation

        state, _ = NSOFamilyReadState.objects.get_or_create(management=self.management, family=family)
        observed = routing_observation(family, document=document, coverage=coverage)
        NSOFamilyObservation.objects.update_or_create(
            read_state=state, defaults=observation_defaults(family, 1, 1, observed)
        )

    def _row_choices(self, scope, kind):
        response = self.client.get(self.url("differences"), {"scope": scope})
        (row,) = [row for row in response.context["page_obj"] if row["kind"] == kind]
        return [
            (choice["selection"]["native_model"], choice["selection"]["native_id"]) for choice in row["intend_choices"]
        ]

    def test_route_policy_root_difference_offers_its_native_choice(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState

        from ._routing_observation_case import route_policy_document

        policy = PrefixList.objects.create(name="INTEND-POLICY", family=4)
        NSORoutePolicyState.objects.create(
            management=self.management,
            family="prefix_list",
            object_name=policy.name,
            content_type=ContentType.objects.get_for_model(PrefixList),
            object_id=policy.pk,
            status="imported",
        )
        self._routing_snapshot("route_policy", route_policy_document({}))
        self.assertEqual(self._row_choices("route_policy", "netbox_only"), [("netbox_routing.prefixlist", policy.pk)])

    def test_redistribution_difference_offers_its_native_choice(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import ISISInstance, Redistribution

        process = ISISInstance.objects.create(device=self.device, process_tag="CORE")
        native = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(ISISInstance),
            destination_id=process.pk,
            source_protocol="static",
            metric=10,
        )
        fields = ("metric", "metric_type", "route_map", "dest_protocol", "dest_ref", "source_protocol", "source_ref")
        entry = dict(zip(fields, (20, None, None, "isis", "CORE", "static", ""), strict=True), present=list(fields))
        document = {
            "entries": [entry],
            "components": [{"protocol": "isis", "inventory": [], "present": ["inventory"]}],
            "unprojectable": [],
        }
        coverage = {
            "attributes": ["metric", "metric_type", "route_map"],
            "components": [{"protocol": "isis", "destinations": [], "sources": []}],
        }
        self._routing_snapshot("redistribution", document, coverage)
        self.assertEqual(
            self._row_choices("redistribution", "mismatch"), [("netbox_routing.redistribution", native.pk)]
        )

    def test_catalog_shows_state_keys_as_named_fields(self):
        from netbox_routing.models import PrefixList

        PrefixList.objects.create(name="INTEND-KEYS", family=4)
        response = self.client.get(self.url("differences"))
        self.assertContains(response, "family=prefix_list, object_name=INTEND-KEYS")
        self.assertNotContains(response, "{&#x27;family&#x27;")
        self.assertContains(response, "data-intend-filter")

    def test_native_policy_roots_are_selectable_without_observations(self):
        from netbox_routing.models import PrefixList

        policy = PrefixList.objects.create(name="INTEND-PREFIXES", family=4)
        response = self.client.get(self.url("differences"), {"scope": "interface_mtu", "page": "2"})
        choice = next(
            choice
            for choice in response.context["intend_choices"]
            if choice["scope"] == "route_policy"
            and choice["native"].pk == policy.pk
            and choice["native"]._meta.label_lower == policy._meta.label_lower
        )
        self.assertContains(response, policy.name)
        self.assertEqual(choice["selection"]["state_key"], {"family": "prefix_list", "object_name": policy.name})


class TestIntendJunosView(IntendViewFixture, IntentPushResetMixin, TestCase):
    from .test_intent_acquisition_contract import JunosPhysicalIPAdapter

    adapter_class = JunosPhysicalIPAdapter

    def test_junos_physical_ip_refusal_is_visible_without_ownership(self):
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState

        interface = Interface.objects.create(device=self.device, name="me0", type="1000base-t")
        address = IPAddress.objects.create(address="198.18.0.1/24", assigned_object=interface)
        data = self.selection("ip", address)
        before = self.bookkeeping()
        response = self.preview(data)
        self.assertContains(response, "No acquisition occurred")
        self.assertContains(response, "inexpressible-on-ned")
        self.assertNotContains(response, "Confirm Intend")
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSOInterfaceIPState.objects.exists())


class TestIntendDelivery(IntendViewFixture, _CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def test_new_episode_is_pending_until_its_own_acknowledgement(self):
        self.confirm(self.preview())
        row = NSOInterfaceMtuState.objects.get(interface=self.interface)
        accepted_at = row.accepted_at
        self.assertContains(self.client.get(self.url("differences")), "acquired; stored")
        NSOOwnershipManifest.objects.filter(device_id=self.device.pk).update(ownership_state="retired")
        with transaction.atomic():
            preview = self.preview(self.selection(restart=True))
            self.assertContains(self.confirm(preview), "acquired; delivery pending")
            self.assertContains(self.client.get(self.url("differences")), "acquired; delivery pending")
            row.refresh_from_db()
            self.assertGreater(row.accepted_at, accepted_at)
        self.assertContains(self.client.get(self.url("differences")), "acquired; stored")

    def test_outcome_query_count_does_not_grow_with_acquired_rows(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.device_intend import acquisition_outcomes

        def counted():
            with CaptureQueriesContext(connection) as queries:
                outcomes = acquisition_outcomes(self.management)
            return [outcome["text"] for outcome in outcomes], len(queries)

        from ipam.models import VRF, IPAddress

        vrf = VRF.objects.create(name="INTEND-OUTCOMES")
        addresses = [
            IPAddress.objects.create(address=f"198.18.0.{index}/24", vrf=vrf, assigned_object=self.interface)
            for index in (1, 2, 3, 4)
        ]
        data = {"intend_row": [*self.selection()["intend_row"], *self.selection("ip", addresses[0])["intend_row"]]}
        self.assertContains(self.confirm(self.preview(data)), "acquired; delivery pending")
        one = counted()
        others = [
            Interface.objects.create(device=self.device, name=f"Ethernet{index}", type="1000base-t", mtu=9000 + index)
            for index in (2, 3, 4)
        ]
        tokens = [self.selection(native=interface)["intend_row"][0] for interface in others]
        tokens += [self.selection("ip", address)["intend_row"][0] for address in addresses[1:]]
        self.assertContains(self.confirm(self.preview({"intend_row": tokens})), "acquired; delivery pending")
        self.assertEqual(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, grant_kind="intend").count(), 8)
        self.assertEqual(one[0], ["acquired; stored", "acquired; stored"])
        self.assertEqual(counted(), one)

    def test_acquisition_ordinary_acknowledgement_and_apply_available(self):
        from netbox_nso_plugin import drain

        preview = self.preview()
        with transaction.atomic():
            self.assertContains(self.confirm(preview), "acquired; delivery pending")
            self.assertContains(self.client.get(self.url("differences")), "acquired; delivery pending")
            self.assertEqual(
                set(NSOIntentOutboxEntry.objects.filter(device=self.device).values_list("kind", flat=True)),
                {"ordinary"},
            )
        self.assertEqual(drain.drain_intent_outbox(), (0, 0))
        self.assertEqual(len(self.adapter.applied), 1)
        response = self.client.get(self.url("differences"))
        self.assertContains(response, "acquired; stored")
        self.assertNotContains(response, "acquired; delivery pending")
        row = NSOInterfaceMtuState.objects.get(interface=self.interface)
        self.assertEqual(row.status, "accepted")
        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        apply = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(apply.status_code, 200, apply.content)
        self.assertTrue(apply.json()["can_apply"], apply.content)
        self.assertTrue(apply.json()["apply_preview"])

    def test_delivery_failure_is_visible_and_retry_becomes_stored(self):
        import requests

        from netbox_nso_plugin import drain

        preview = self.preview()
        self.adapter.fail_with = requests.ConnectionError("placeholder transport failure")
        self.assertContains(self.confirm(preview), "acquired; delivery pending")
        response = self.client.get(self.url("differences"))
        self.assertContains(response, "acquired; ordinary delivery failed")
        self.assertNotContains(response, "acquired; stored")
        self.adapter.fail_with = None
        self.assertEqual(drain.drain_intent_outbox(), (1, 0))
        self.assertContains(self.client.get(self.url("differences")), "acquired; stored")

    def test_the_contribution_check_renders_each_row_s_own_key_first(self):
        from unittest.mock import patch

        from ipam.models import IPAddress

        from netbox_nso_plugin import delivery

        address = IPAddress.objects.create(address="198.18.0.9/24", assigned_object=self.interface)
        data = {"intend_row": [*self.selection()["intend_row"], *self.selection("ip", address)["intend_row"]]}
        masked = []
        render = delivery.render

        def counted(key, *args, candidates=None, **kwargs):
            if candidates is not None and any(row.status == "imported" for row in candidates.overlays):
                masked.append(key)
            return render(key, *args, candidates=candidates, **kwargs)

        with patch.object(delivery, "render", side_effect=counted):
            self.assertEqual(self.preview(data).context["result"].status, "ready")
        self.assertEqual(len(masked), 2, masked)

    def test_a_push_error_older_than_the_last_success_is_not_reported(self):
        import datetime

        import requests

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSODeviceManagement, NSOIntentOutboxState

        self.adapter.fail_with = requests.ConnectionError("placeholder transport failure")
        self.confirm(self.preview())
        stale = NSODeviceManagement.objects.get(pk=self.management.pk).intent_push_errors
        self.adapter.fail_with = None
        self.assertEqual(drain.drain_intent_outbox(), (1, 0))
        # The success could not clear the management record, as when its bookkeeping write fails.
        NSODeviceManagement.objects.filter(pk=self.management.pk).update(intent_push_errors=stale)
        self.assertContains(self.client.get(self.url("differences")), "acquired; stored")

        [(scope, entry)] = stale.items()
        success = NSOIntentOutboxState.objects.get(device=self.device, scope=scope).last_success_at
        newer = {scope: {**entry, "at": (success + datetime.timedelta(seconds=1)).isoformat()}}
        NSODeviceManagement.objects.filter(pk=self.management.pk).update(intent_push_errors=newer)
        self.assertContains(self.client.get(self.url("differences")), "acquired; ordinary delivery failed")

    def test_switchport_preserves_native_vlans_and_is_stored_after_ordinary_preparation(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOSwitchportState

        group = VLANGroup.objects.create(name="Intend VLANs", slug=f"nso-{self.device.pk}")
        untagged = VLAN.objects.create(group=group, vid=100, name="UNTAGGED")
        tagged = VLAN.objects.create(group=group, vid=200, name="TAGGED")
        self.interface.mode = "tagged"
        self.interface.untagged_vlan = untagged
        self.interface.save(update_fields=("mode", "untagged_vlan"))
        self.interface.tagged_vlans.add(tagged)
        preview = self.preview(self.selection("switchport"))
        self.assertEqual(preview.context["result"].status, "ready", preview.content)
        self.assertContains(preview, "tagged_vlans")
        self.assertContains(self.confirm(preview), "acquired; delivery pending")
        row = NSOSwitchportState.objects.get(interface=self.interface)
        self.assertEqual(row.untagged_vlan_id, untagged.pk)
        self.assertEqual(list(row.tagged_vlans.values_list("pk", flat=True)), [tagged.pk])
        self.interface.refresh_from_db()
        self.assertEqual((self.interface.mode, self.interface.untagged_vlan_id), ("tagged", untagged.pk))
        self.assertEqual(list(self.interface.tagged_vlans.values_list("pk", flat=True)), [tagged.pk])
        self.assertEqual(drain.drain_intent_outbox(), (0, 0))
        self.assertEqual(len(self.adapter.applied), 1)
        self.assertContains(self.client.get(self.url("differences")), "acquired; stored")

    def test_switchport_preview_shows_replaced_and_unchanged_vlan_membership(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOSwitchportState

        group = VLANGroup.objects.create(name="Intend VLANs", slug=f"nso-{self.device.pk}")
        observed = VLAN.objects.create(group=group, vid=300, name="OBSERVED")
        native = VLAN.objects.create(group=group, vid=400, name="NATIVE")
        self.interface.mode = "tagged"
        self.interface.save(update_fields=("mode",))
        self.interface.tagged_vlans.add(native)
        state = NSOSwitchportState.objects.create(
            management=self.management, interface=self.interface, mode="tagged", status="imported"
        )
        state.tagged_vlans.add(observed)
        preview = self.preview(self.selection("switchport"))
        self.assertEqual(preview.context["result"].status, "ready", preview.content)
        (row,) = preview.context["result"].details["selected"]
        self.assertIn({"name": "tagged_vlans", "before": [observed.pk], "after": [native.pk]}, row["fields"])
        state.tagged_vlans.set([native])
        (row,) = self.preview(self.selection("switchport")).context["result"].details["selected"]
        self.assertNotIn("tagged_vlans", [field["name"] for field in row["fields"]])

    def test_query_flag_refusal_recovers_through_ordinary_drain(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        other = Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t", mtu=1500)
        row = NSOInterfaceMtuState.objects.create(
            management=self.management, interface=other, status="imported", l2_mtu=1500
        )
        content_update(row, status="accepted")
        self.assertEqual(drain.drain_intent_outbox(), (1, 0))
        plan = RendererMutationPlan.build(deletes=(planned_delete(row),))
        with without_commit_drain(), renderer_writes(plan) as writer:
            writer.delete(row)
        entry = NSOIntentOutboxEntry.objects.get(device=self.device, scope="interface_mtu")
        self.assertTrue(entry.mark_and)
        self.adapter.promotes = True
        before = self.bookkeeping()
        refused = self.preview()
        self.assertContains(refused, "No acquisition occurred")
        self.assertContains(refused, "ordinary delivery")
        self.assertNotContains(refused, "manual Apply")
        self.assertEqual(self.bookkeeping(), before)
        self.assertEqual(drain.drain_intent_outbox(), (1, 0))
        self.assertEqual(self.adapter.requests[-1]["params"].get("delete_origin"), "true")
        self.assertEqual(self.preview().context["result"].status, "ready")
        self.assertContains(self.confirm(self.preview()), "acquired; delivery pending")

    def test_old_acknowledgement_does_not_certify_a_new_owner_or_mapping(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOAcceptedSelection, NSODeviceManagement

        self.confirm(self.preview())
        self.assertEqual(drain.drain_intent_outbox(), (0, 0))
        self.assertContains(self.client.get(self.url("differences")), "acquired; stored")
        NSOAcceptedSelection.objects.filter(management=self.management).update(mapping_epoch="0" * 64)
        self.assertContains(self.client.get(self.url("differences")), "acquired; delivery pending")
        NSODeviceManagement.objects.filter(pk=self.management.pk).update(adapter_device_id=1789)
        self.assertNotContains(self.client.get(self.url("differences")), "acquired; stored")
