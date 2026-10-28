# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Protocol coverage fences redistribution imports and ownership."""

import copy
import hashlib
import json
from unittest import mock
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from ipam.models import ASN, RIR
from netbox_routing.models import BGPAddressFamily, BGPRouter, BGPScope, OSPFInstance, Redistribution

from netbox_nso_plugin.adapter_client import AdapterError, bound_session
from netbox_nso_plugin.device_differences import differences
from netbox_nso_plugin.models import (
    NSOFamilyObservation,
    NSOFamilyReadState,
    NSOIntentOutboxEntry,
    NSORedistributionState,
)
from netbox_nso_plugin.reconcile import reconcile_category
from netbox_nso_plugin.routing_protocol_projection import component_gaps

from ._observation_case import ObservationTransport, observation
from ._routing_observation_case import routing_observation
from ._scope_observation_case import entry
from .test_gated_reconcile import _make
from .test_read_gate import _rs


def redistribution_entry(protocol, source="static", metric=10):
    return {
        "dest_protocol": protocol,
        "dest_ref": "64512//ipv4-unicast" if protocol == "bgp" else "1",
        "source_protocol": source,
        "source_ref": "",
        "metric": metric,
    }


def redistribution_payload(entries, covered=("bgp", "ospf"), *, attempt=1):
    components = []
    for protocol in ("bgp", "isis", "ospf"):
        if protocol not in covered:
            components.append({"protocol": protocol, "status": "unavailable", "inventory": None, "present": []})
            continue
        sources = [
            entry(**{key: value for key, value in item.items() if key not in {"dest_protocol", "dest_ref"}})
            for item in entries
            if item["dest_protocol"] == protocol
        ]
        if protocol == "bgp":
            inventory = [
                entry(
                    asn="64512",
                    scope=[entry(vrf="", address_family=[entry(afi="ipv4-unicast", redistribute=sources)])],
                )
            ]
        elif protocol == "ospf":
            inventory = [entry(process_id="1", vrf="", redistribute=sources)]
        else:
            inventory = []
        components.append({"protocol": protocol, "status": "covered", "inventory": inventory, "present": ["inventory"]})
    document = {
        "entries": [entry(**item) for item in entries if item["dest_protocol"] in covered],
        "components": components,
        "unprojectable": [],
    }
    snapshot = routing_observation(
        "redistribution",
        document=document,
        coverage={
            "attributes": ["metric", "metric_type", "route_map"],
            "components": [
                {
                    "protocol": protocol,
                    "destinations": ["64512//ipv4-unicast"]
                    if protocol == "bgp"
                    else (["1"] if protocol == "ospf" else []),
                    "sources": [
                        {"protocol": source_protocol, "reference": source_ref}
                        for source_protocol, source_ref in sorted(
                            {
                                (item["source_protocol"], item["source_ref"])
                                for item in entries
                                if item["dest_protocol"] == protocol
                            }
                        )
                    ],
                }
                for protocol in covered
            ],
        },
        revision=attempt,
    )
    return {"entries": copy.deepcopy(entries), "observation": snapshot, "read_state": _rs(attempt_id=attempt)}


def test_unavailable_component_status_is_projection_gap():
    observed = redistribution_payload([], covered=("ospf",))["observation"]
    snapshot = NSOFamilyObservation(
        read_state=NSOFamilyReadState(), document=observed["document"], coverage=observed["coverage"]
    )

    assert component_gaps(snapshot)["bgp"] == "redistribution component is unavailable"


def test_newer_unavailable_read_hides_previous_component_differences():
    observed = redistribution_payload([])["observation"]
    snapshot = NSOFamilyObservation(
        read_state=NSOFamilyReadState(observed_outcome="unavailable", observed_reason="not_covered"),
        document=observed["document"],
        coverage=observed["coverage"],
    )

    assert set(component_gaps(snapshot, read_unavailable=True)) == {"bgp", "isis", "ospf"}


class TestRedistributionCoverage(TestCase):
    def setUp(self):
        self.device, self.management = _make(
            f"coverage{uuid4().hex[:8]}", manage_routing=True, manage_redistribution=True
        )
        self.user = get_user_model().objects.create_superuser(username=f"coverage{uuid4().hex[:8]}")
        self.client.force_login(self.user)
        rir = RIR.objects.create(name="Private routing", slug="private-routing", is_private=True)
        asn = ASN.objects.create(asn=64512, rir=rir)
        router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(self.device),
            assigned_object_id=self.device.pk,
            asn=asn,
        )
        scope = BGPScope.objects.create(router=router)
        BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
        OSPFInstance.objects.create(device=self.device, name="OSPF", process_id=1, router_id="198.18.0.1")
        self.transport = ObservationTransport(
            self.device.pk, observation("interface_attributes"), observation("interface_ip")
        )

    def refresh(self, payload):
        self.transport.documents["redistribution"] = payload
        with bound_session(self.transport.session()):
            return reconcile_category(self.device, self.management, "redistribution")

    def prime(self):
        self.refresh(
            redistribution_payload(
                [
                    redistribution_entry(protocol, source)
                    for protocol in ("bgp", "ospf")
                    for source in ("static", "connected")
                ]
            )
        )

    def own_ospf_connected(self):
        row = NSORedistributionState.objects.get(
            management=self.management, dest_protocol="ospf", source_protocol="connected"
        )
        response = self.client.post(reverse("plugins:netbox_nso_plugin:routing_accept_redistribution", args=[row.pk]))
        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.status, "in_sync")
        self.assertIsNotNone(row.accepted_at)

    def incomplete_payload(self, protocol, variant, *, entries=(), attempt=2):
        payload = redistribution_payload(list(entries), attempt=attempt)
        snapshot = payload["observation"]
        component = next(item for item in snapshot["document"]["components"] if item["protocol"] == protocol)
        parent = component["inventory"][0]
        name = "redistribute"
        if protocol == "bgp":
            parent["scope"].append(entry(vrf="EXAMPLE", address_family=[]))
            parent = parent["scope"][-1]
            name = "address_family"
        if variant == "null":
            parent[name] = None
        elif variant == "missing":
            del parent[name]
        else:
            parent["present"].remove(name)
        payload["observation"] = routing_observation(
            "redistribution", document=snapshot["document"], coverage=snapshot["coverage"], revision=attempt
        )
        return payload

    def test_unknown_ospf_sources_keep_imported_and_owned_rows_and_refuse_accept(self):
        self.prime()
        self.own_ospf_connected()
        ospf = NSORedistributionState.objects.filter(management=self.management, dest_protocol="ospf")
        imported = ospf.get(source_protocol="static")
        before = list(ospf.order_by("pk").values())
        native_ids = list(ospf.values_list("redistribution_id", flat=True))
        natives_before = list(Redistribution.objects.filter(pk__in=native_ids).order_by("pk").values())
        for attempt, variant in enumerate(("null", "missing", "unmarked"), start=2):
            with self.subTest(collection=variant):
                context = self.refresh(self.incomplete_payload("ospf", variant, attempt=attempt))

                self.assertEqual(context["_gate"]["redistribution"], "ran")
                self.assertEqual(list(ospf.order_by("pk").values()), before)
                self.assertEqual(
                    list(Redistribution.objects.filter(pk__in=native_ids).order_by("pk").values()), natives_before
                )
                for name, pk in (
                    ("routing_accept_redistribution", imported.pk),
                    ("routing_bulk_accept_redistribution", self.device.pk),
                ):
                    response = self.client.post(reverse(f"plugins:netbox_nso_plugin:{name}", args=[pk]))
                    self.assertEqual(response.status_code, 302)
                    self.assertIn(
                        "OSPF redistribution coverage is unavailable",
                        " ".join(str(item) for item in get_messages(response.wsgi_request)),
                    )
                    self.assertEqual(list(ospf.order_by("pk").values()), before)
                rows = [item for item in differences(self.management, user=self.user) if item.scope == "redistribution"]
                self.assertEqual(
                    [(item.kind, item.attribute) for item in rows if item.attribute == "ospf"],
                    [("unavailable", "ospf")],
                )
                self.assertFalse(any(isinstance(item.identity, tuple) and item.identity[0] == "ospf" for item in rows))

    def test_covered_empty_ospf_sources_remove_imported_rows_and_mark_owned_rows_changed(self):
        self.prime()
        self.own_ospf_connected()
        ospf = NSORedistributionState.objects.filter(management=self.management, dest_protocol="ospf")
        imported = ospf.get(source_protocol="static")
        native_id = imported.redistribution_id
        owned = ospf.get(source_protocol="connected")
        native_before = Redistribution.objects.filter(pk=owned.redistribution_id).values().get()

        self.refresh(redistribution_payload([], attempt=2))

        self.assertFalse(NSORedistributionState.objects.filter(pk=imported.pk).exists())
        self.assertFalse(Redistribution.objects.filter(pk=native_id).exists())
        owned.refresh_from_db()
        self.assertEqual(owned.status, "changed")
        self.assertEqual(Redistribution.objects.filter(pk=owned.redistribution_id).values().get(), native_before)

    def test_unknown_ospf_sources_refuse_single_and_bulk_accept(self):
        self.prime()
        row = NSORedistributionState.objects.get(
            management=self.management, dest_protocol="ospf", source_protocol="static"
        )
        entries = [redistribution_entry("ospf", source) for source in ("static", "connected")]
        self.refresh(self.incomplete_payload("ospf", "null", entries=entries))
        for name, pk in (
            ("routing_accept_redistribution", row.pk),
            ("routing_bulk_accept_redistribution", self.device.pk),
        ):
            with self.subTest(view=name):
                response = self.client.post(reverse(f"plugins:netbox_nso_plugin:{name}", args=[pk]))
                self.assertEqual(response.status_code, 302)
                self.assertIn(
                    "OSPF redistribution coverage is unavailable",
                    " ".join(str(item) for item in get_messages(response.wsgi_request)),
                )
                row.refresh_from_db()
                self.assertEqual(row.status, "imported")
                self.assertIsNone(row.accepted_at)

    def test_unknown_bgp_scope_families_keep_the_entire_component(self):
        self.prime()
        bgp = NSORedistributionState.objects.filter(management=self.management, dest_protocol="bgp")
        before = list(bgp.order_by("pk").values())
        native_ids = list(bgp.values_list("redistribution_id", flat=True))
        natives_before = list(Redistribution.objects.filter(pk__in=native_ids).order_by("pk").values())

        self.refresh(self.incomplete_payload("bgp", "null"))

        self.assertEqual(list(bgp.order_by("pk").values()), before)
        self.assertEqual(list(Redistribution.objects.filter(pk__in=native_ids).order_by("pk").values()), natives_before)
        rows = [item for item in differences(self.management, user=self.user) if item.scope == "redistribution"]
        self.assertEqual(
            [(item.kind, item.attribute) for item in rows if item.attribute == "bgp"], [("unavailable", "bgp")]
        )

    def test_complete_bgp_reconciles_while_ospf_sources_are_unknown(self):
        self.prime()
        ospf = NSORedistributionState.objects.filter(management=self.management, dest_protocol="ospf")
        before = list(ospf.order_by("pk").values())
        native_ids = list(ospf.values_list("redistribution_id", flat=True))
        natives_before = list(Redistribution.objects.filter(pk__in=native_ids).order_by("pk").values())

        self.refresh(self.incomplete_payload("ospf", "unmarked", entries=[redistribution_entry("bgp", metric=20)]))

        self.assertEqual(list(ospf.order_by("pk").values()), before)
        self.assertEqual(list(Redistribution.objects.filter(pk__in=native_ids).order_by("pk").values()), natives_before)
        bgp = NSORedistributionState.objects.get(management=self.management, dest_protocol="bgp")
        self.assertEqual((bgp.metric, bgp.redistribution.metric), (20, 20))

    def test_mixed_refresh_changes_only_covered_native_and_overlay_rows(self):
        self.prime()
        bgp = NSORedistributionState.objects.filter(management=self.management, dest_protocol="bgp")
        overlays_before = list(bgp.order_by("pk").values())
        native_ids = list(bgp.values_list("redistribution_id", flat=True))
        natives_before = list(Redistribution.objects.filter(pk__in=native_ids).order_by("pk").values())

        context = self.refresh(
            redistribution_payload(
                [
                    redistribution_entry("bgp", metric=99),
                    redistribution_entry("bgp", "ospf", metric=99),
                    redistribution_entry("ospf", metric=20),
                ],
                covered=("ospf",),
                attempt=2,
            )
        )

        self.assertEqual(context["_gate"]["redistribution"], "ran")
        self.assertEqual(list(bgp.order_by("pk").values()), overlays_before)
        self.assertEqual(list(Redistribution.objects.filter(pk__in=native_ids).order_by("pk").values()), natives_before)
        ospf = NSORedistributionState.objects.get(management=self.management, dest_protocol="ospf")
        self.assertEqual((ospf.metric, ospf.redistribution.metric), (20, 20))
        self.assertEqual(Redistribution.objects.count(), 3)

    def mixed_refresh(self):
        self.prime()
        self.refresh(
            redistribution_payload(
                [redistribution_entry("bgp", metric=99), redistribution_entry("ospf", metric=20)],
                covered=("ospf",),
                attempt=2,
            )
        )

    def test_single_accept_refuses_unavailable_component_and_accepts_covered_component(self):
        self.mixed_refresh()
        bgp = NSORedistributionState.objects.get(
            management=self.management, dest_protocol="bgp", source_protocol="static"
        )
        before = NSORedistributionState.objects.filter(pk=bgp.pk).values().get()

        response = self.client.post(reverse("plugins:netbox_nso_plugin:routing_accept_redistribution", args=[bgp.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "BGP redistribution coverage is unavailable",
            " ".join(str(item) for item in get_messages(response.wsgi_request)),
        )
        self.assertEqual(NSORedistributionState.objects.filter(pk=bgp.pk).values().get(), before)
        ospf = NSORedistributionState.objects.get(management=self.management, dest_protocol="ospf")
        response = self.client.post(reverse("plugins:netbox_nso_plugin:routing_accept_redistribution", args=[ospf.pk]))
        self.assertEqual(response.status_code, 302)
        ospf.refresh_from_db()
        self.assertEqual(ospf.status, "in_sync")
        self.assertIsNotNone(ospf.accepted_at)

    def test_bulk_accept_refuses_unavailable_component_without_acquiring_rows(self):
        self.mixed_refresh()
        before = list(NSORedistributionState.objects.filter(management=self.management).order_by("pk").values())

        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:routing_bulk_accept_redistribution", args=[self.device.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "BGP redistribution coverage is unavailable",
            " ".join(str(item) for item in get_messages(response.wsgi_request)),
        )
        self.assertEqual(
            list(NSORedistributionState.objects.filter(management=self.management).order_by("pk").values()), before
        )

    def test_newer_unavailable_family_vetoes_accept_from_older_covered_snapshot(self):
        self.prime()
        snapshot = NSOFamilyObservation.objects.get(
            read_state__management=self.management, read_state__family="redistribution"
        )
        previous_document = snapshot.document
        payload = redistribution_payload([], covered=(), attempt=2)
        payload["read_state"] = _rs(
            outcome="unavailable", reason="not_covered", freshness=None, result="kept", attempt_id=2, payload_revision=1
        )
        payload["observation"] = None
        self.assertEqual(self.refresh(payload)["_gate"]["redistribution"], "skipped_unavailable")
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.document, previous_document)
        row = NSORedistributionState.objects.get(
            management=self.management, dest_protocol="bgp", source_protocol="static"
        )
        for name, pk in (
            ("routing_accept_redistribution", row.pk),
            ("routing_bulk_accept_redistribution", self.device.pk),
        ):
            with self.subTest(view=name):
                response = self.client.post(reverse(f"plugins:netbox_nso_plugin:{name}", args=[pk]))
                self.assertEqual(response.status_code, 302)
                self.assertIn(
                    "redistribution coverage is unavailable",
                    " ".join(str(item) for item in get_messages(response.wsgi_request)),
                )
                row.refresh_from_db()
                self.assertEqual(row.status, "imported")
                self.assertIsNone(row.accepted_at)
        rows = [item for item in differences(self.management, user=self.user) if item.scope == "redistribution"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(item.kind == "unavailable" for item in rows))

    def test_unavailable_components_project_gaps_without_kept_entry_differences(self):
        self.mixed_refresh()
        rows = [item for item in differences(self.management, user=self.user) if item.scope == "redistribution"]
        bgp = [item for item in rows if item.attribute == "bgp"]
        self.assertEqual(
            [(item.kind, item.reason) for item in bgp], [("unavailable", "redistribution component is unavailable")]
        )
        self.assertFalse(any(isinstance(item.identity, tuple) and item.identity[0] == "bgp" for item in rows))

    def test_status_and_coverage_disagreement_rejects_refresh_without_writes(self):
        self.prime()
        before = list(NSORedistributionState.objects.order_by("pk").values())
        for covered, claimed in ((("ospf",), "bgp"), (("bgp", "ospf"), "isis")):
            with self.subTest(covered=covered, claimed=claimed):
                payload = redistribution_payload([redistribution_entry("ospf", metric=99)], covered=covered, attempt=2)
                payload["observation"]["coverage"]["components"].append(
                    {"protocol": claimed, "destinations": [], "sources": []}
                )
                with self.assertRaises(AdapterError) as raised:
                    self.refresh(payload)
                self.assertEqual(raised.exception.code, "invalid_response")
                self.assertEqual(list(NSORedistributionState.objects.order_by("pk").values()), before)

    def test_covered_null_inventory_fails_closed_without_reconciliation(self):
        self.prime()
        before = list(NSORedistributionState.objects.filter(dest_protocol="bgp").order_by("pk").values())
        payload = redistribution_payload([redistribution_entry("bgp", metric=99)], covered=("bgp",), attempt=2)
        snapshot = payload["observation"]
        snapshot["document"]["components"][0]["inventory"] = None
        snapshot["digest"] = hashlib.sha256(
            json.dumps(snapshot["document"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        self.refresh(payload)

        self.assertEqual(
            list(NSORedistributionState.objects.filter(dest_protocol="bgp").order_by("pk").values()), before
        )

    def test_direct_reconcile_without_coverage_keeps_all_rows(self):
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        self.prime()
        before = list(NSORedistributionState.objects.order_by("pk").values())

        reconcile_redistribution(self.device, {"entries": []})

        self.assertEqual(list(NSORedistributionState.objects.order_by("pk").values()), before)

    def test_bulk_accept_of_covered_ospf_does_not_schedule_owned_unavailable_bgp(self):
        self.prime()
        bgp = NSORedistributionState.objects.filter(management=self.management, dest_protocol="bgp")
        for row in bgp:
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:routing_accept_redistribution", args=[row.pk])
            )
            self.assertEqual(response.status_code, 302)
            row.refresh_from_db()
            self.assertEqual(row.status, "in_sync")
        before = list(bgp.order_by("pk").values())
        queued_before = list(
            NSOIntentOutboxEntry.objects.filter(device=self.device, scope="bgp").order_by("pk").values()
        )
        self.refresh(redistribution_payload([redistribution_entry("ospf", metric=20)], covered=("ospf",), attempt=2))

        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:routing_bulk_accept_redistribution", args=[self.device.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(bgp.order_by("pk").values()), before)
        self.assertEqual(
            list(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="bgp").order_by("pk").values()),
            queued_before,
        )
        ospf = NSORedistributionState.objects.get(management=self.management, dest_protocol="ospf")
        self.assertEqual(ospf.status, "in_sync")
        self.assertIsNotNone(ospf.accepted_at)

    def test_bulk_accept_reads_coverage_once_per_phase(self):
        self.prime()
        rows = NSORedistributionState.objects.filter(management=self.management)
        self.assertEqual(rows.count(), 4)
        table = NSOFamilyObservation._meta.db_table

        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:routing_bulk_accept_redistribution", args=[self.device.pk])
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(rows.values_list("status", flat=True)), {"in_sync"})
        lookups = [query for query in queries if f'FROM "{table}"' in query["sql"]]
        # Before the writer locks, after it locks, and for the intent push.
        self.assertEqual(len(lookups), 3)

    def test_coverage_lost_after_the_precheck_refuses_accept_after_the_writer_locks(self):
        from django.db.models import F

        from netbox_nso_plugin.views import NSORedistributionBulkAcceptView, NSORedistributionStateAcceptView

        self.prime()
        rows = NSORedistributionState.objects.filter(management=self.management)
        row = rows.get(dest_protocol="bgp", source_protocol="static")
        before = list(rows.order_by("pk").values())
        outbox = list(NSOIntentOutboxEntry.objects.filter(device=self.device).values_list("pk", flat=True))
        for view, hook, name, pk in (
            (NSORedistributionStateAcceptView, "_accept_blocker", "routing_accept_redistribution", row.pk),
            (
                NSORedistributionBulkAcceptView,
                "_first_accept_blocker",
                "routing_bulk_accept_redistribution",
                self.device.pk,
            ),
        ):
            with self.subTest(view=name):
                precheck = getattr(view, hook)

                def lose_coverage(view_self, *args, precheck=precheck):
                    result = precheck(view_self, *args)
                    # A newer publication lands between the pre-check and the writer lock.
                    NSOFamilyReadState.objects.filter(management=self.management, family="redistribution").update(
                        publication_sequence=F("publication_sequence") + 1
                    )
                    return result

                with mock.patch.object(view, hook, lose_coverage):
                    response = self.client.post(reverse(f"plugins:netbox_nso_plugin:{name}", args=[pk]))

                self.assertRedirects(
                    response, reverse("dcim:device_nso", args=[self.device.pk]), fetch_redirect_response=False
                )
                self.assertIn(
                    "BGP redistribution coverage is unavailable",
                    " ".join(str(item) for item in get_messages(response.wsgi_request)),
                )
                self.assertEqual(list(rows.order_by("pk").values()), before)
                self.assertEqual(
                    list(NSOIntentOutboxEntry.objects.filter(device=self.device).values_list("pk", flat=True)), outbox
                )
