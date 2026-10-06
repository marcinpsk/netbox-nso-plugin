# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Redistribution push refusals through real Accept requests and category views."""

from dcim.models import Platform
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils.html import escape
from ipam.models import ASN, RIR
from netbox_routing.models import BGPAddressFamily, BGPRouter, BGPScope, ISISInstance, OSPFInstance, Redistribution

from netbox_nso_plugin.models import (
    NSOISISInstanceState,
    NSOOSPFInstanceState,
    NSOPlatformNedMapping,
    NSORedistributionState,
)

from ._outbox_case import ReceiptAdapter, make_managed, mirror_update
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

SOURCE_AS_MESSAGE = "The device's NED requires a source AS for this BGP redistribution."
GENERIC_MESSAGE = "The NSO adapter request failed. See the server log."
PRIVATE_MESSAGE = "private-upstream-diagnostic"


class TestRedistributionPushErrors(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser(username="redistribution-admin"))
        self.device, self.mgmt = make_managed("redist-banner", 17801)
        platform = Platform.objects.create(name="Example IOS", slug="example-ios")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="cisco-ios-cli-6.114")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        isis = ISISInstance.objects.create(device=self.device, process_tag="CORE")
        ospf = OSPFInstance.objects.create(
            device=self.device, name="Example OSPF", process_id=1, router_id="198.18.0.1"
        )
        router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(self.device),
            assigned_object_id=self.device.pk,
            asn=ASN.objects.create(asn=64512, rir=RIR.objects.create(name="Example RIR", slug="example-rir")),
            name="Example BGP",
        )
        bgp = BGPAddressFamily.objects.create(
            scope=BGPScope.objects.create(router=router), address_family="ipv4-unicast"
        )
        self.destinations = {"isis": isis, "ospf": ospf, "bgp": bgp}
        acquire_overlay(
            NSOISISInstanceState, management=self.mgmt, isis_instance=isis, process_tag="CORE", status="in_sync"
        )
        acquire_overlay(
            NSOOSPFInstanceState, management=self.mgmt, ospf_instance=ospf, process_id="1", status="in_sync"
        )
        self.rejections = {"isis": "bgp_source_as_required"}
        self.adapter = ReceiptAdapter(respond=self._adapter_response)
        for patcher in self.adapter.patches():
            patcher.start()
            self.addCleanup(patcher.stop)

    def _adapter_response(self, body):
        scope = next(
            (scope for key, scope in (("processes", "isis"), ("instances", "ospf"), ("routers", "bgp")) if key in body),
            None,
        )
        code = self.rejections.get(scope)
        if code:
            return 409, {"error": {"code": code, "message": PRIVATE_MESSAGE, "detail": {"reason": PRIVATE_MESSAGE}}}
        return ReceiptAdapter._default_response(body)

    def _accept(self, destination="isis", *, source="bgp"):
        native = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(self.destinations[destination]),
            destination_id=self.destinations[destination].pk,
            source_protocol=source,
            source_ref="",
        )
        state = NSORedistributionState.objects.create(
            management=self.mgmt,
            redistribution=native,
            dest_protocol=destination,
            dest_ref={"isis": "CORE", "ospf": "1", "bgp": "64512//ipv4-unicast"}[destination],
            source_protocol=source,
            source_ref="",
            status="changed",
        )
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:routing_accept_redistribution", kwargs={"pk": state.pk})
        )
        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertEqual(state.source_ref, "")
        self.assertIsNotNone(state.accepted_at)
        self.mgmt.refresh_from_db()
        return state

    def _category(self, *, refresh=False):
        response = self.client.get(
            reverse(
                "plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "redistribution"}
            ),
            {"format": "json"} if refresh else {},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(PRIVATE_MESSAGE, response.content.decode())
        return response

    def test_empty_bgp_source_accept_records_refusal_and_renders_safe_message(self):
        state = self._accept()

        self.assertEqual(self.mgmt.intent_push_errors["isis"]["code"], "bgp_source_as_required")
        requests = [request for request in self.adapter.requests if request["url"].endswith("/isis-interface-intent")]
        self.assertTrue(requests)
        self.assertEqual(
            requests[-1]["body"]["processes"][0]["redistribution"], [{"source_protocol": "bgp", "source_ref": ""}]
        )
        fragment = self._category()
        self.assertContains(fragment, "nso-push-banner")
        self.assertContains(fragment, "nso-push-detail")
        self.assertContains(fragment, "IS-IS:")
        self.assertContains(fragment, escape(SOURCE_AS_MESSAGE))
        error = self._category(refresh=True).json()["push_error"]
        self.assertIsInstance(error, dict)
        self.assertEqual(error["kind"], "rejected")
        self.assertIn(f"IS-IS: {SOURCE_AS_MESSAGE}", error["message"])
        self.assertTrue(any(row["pk"] == state.pk for row in self._category(refresh=True).json()["rows"]))

    def test_all_stream_failures_remain_visible_after_an_ospf_success(self):
        self.rejections.update(ospf="invalid_response", bgp="asn_rule_violation")
        self._accept()
        self._accept("ospf")
        self._accept("bgp")
        isis_error = self.mgmt.intent_push_errors["isis"]
        error = self._category(refresh=True).json()["push_error"]
        self.assertIn(f"IS-IS: {SOURCE_AS_MESSAGE}", error["message"])
        self.assertIn("OSPF: The NSO adapter returned an invalid response. See the server log.", error["message"])
        self.assertIn(f"BGP: {GENERIC_MESSAGE}", error["message"])

        del self.rejections["ospf"]
        self._accept("ospf", source="connected")

        self.assertNotIn("ospf", self.mgmt.intent_push_errors)
        self.assertEqual(self.mgmt.intent_push_errors["isis"], isis_error)
        error = self._category(refresh=True).json()["push_error"]
        self.assertNotIn("OSPF:", error["message"])
        self.assertIn(f"IS-IS: {SOURCE_AS_MESSAGE}", error["message"])
        self.assertIn(f"BGP: {GENERIC_MESSAGE}", error["message"])
        self.assertContains(self._category(), escape(SOURCE_AS_MESSAGE))

    def test_asn_rule_violation_keeps_generic_public_text(self):
        self.rejections = {"bgp": "asn_rule_violation"}
        self._accept("bgp", source="static")

        self.assertEqual(self.mgmt.intent_push_errors["bgp"]["code"], "asn_rule_violation")
        fragment = self._category()
        self.assertContains(fragment, GENERIC_MESSAGE)
        self.assertNotContains(fragment, escape(SOURCE_AS_MESSAGE))
        error = self._category(refresh=True).json()["push_error"]
        self.assertEqual(error["message"], f"BGP: {GENERIC_MESSAGE}")

    def test_empty_category_keeps_the_refresh_banner_element(self):
        fragment = self._category()

        self.assertContains(fragment, "nso-push-banner d-none")
        self.assertContains(fragment, "nso-push-headline")
        self.assertContains(fragment, "nso-push-detail")
        self.assertContains(fragment, "nso-push-meta")
        self.assertIsNone(self._category(refresh=True).json()["push_error"])

    def test_mixed_outcomes_keep_each_streams_own_headline(self):
        mirror_update(
            self.mgmt,
            intent_push_errors={
                "isis": {"code": "bgp_source_as_required", "message": PRIVATE_MESSAGE},
                "ospf": {"code": "nso_timeout", "message": PRIVATE_MESSAGE},
                "bgp": {"code": "configuration_error", "message": PRIVATE_MESSAGE},
            },
        )

        error = self._category(refresh=True).json()["push_error"]

        self.assertIn("IS-IS: The adapter rejected", error["headline"])
        self.assertIn("OSPF: The last intent push for this category did not complete", error["headline"])
        self.assertIn("BGP: The last intent push for this category never reached the adapter", error["headline"])
        self.assertIn(f"IS-IS: {SOURCE_AS_MESSAGE}", error["message"])
        self.assertIn("OSPF:", error["message"])
        self.assertIn("BGP:", error["message"])
