# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Acquire native intent through real dependencies, permissions and delivery."""

from copy import deepcopy

from dcim.models import Device, Interface
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from ipam.models import ASN, RIR, VLAN, VRF, IPAddress, VLANGroup
from netbox_routing.models import (
    BGPAddressFamily,
    BGPRouter,
    BGPScope,
    ISISInstance,
    OSPFInstance,
    Redistribution,
    StaticRoute,
)

from netbox_nso_plugin import delivery, drain
from netbox_nso_plugin.intent_acquisition import confirm_intend, preview_intend
from netbox_nso_plugin.models import (
    NSOBGPRouterState,
    NSOFamilyObservation,
    NSOFamilyReadState,
    NSOIntentOutboxEntry,
    NSOIntentOutboxState,
    NSOIntentRevision,
    NSOInterfaceIPState,
    NSOInterfaceMtuState,
    NSOISISInstanceState,
    NSOLACPBundleState,
    NSOLACPMemberState,
    NSOOSPFInstanceState,
    NSOOwnershipManifest,
    NSORedistributionState,
    NSOStaticRouteState,
    NSOSwitchportState,
)
from netbox_nso_plugin.refusal_protocol import MESSAGES, row_ref

from ._adapter_http import make_apply_preflight, make_response
from ._authority_case import operator
from ._outbox_case import make_managed, without_commit_drain
from ._scope_observation_case import scope_observation
from .mixins import IntentPushResetMixin, _CascadeFlushMixin
from .test_intent_acquisition import IntendAdapter


def native_selection(scope, native, **options):
    return {
        "scope": scope,
        "native_model": native._meta.label_lower,
        "native_id": native.pk,
        "options": options,
    }


class JunosPhysicalIPAdapter(IntendAdapter):
    """Return the attributed Junos physical-interface IP refusal over HTTP."""

    def _handle(self, method, url, **kwargs):
        if not url.endswith("/actions/apply-preflight"):
            return super()._handle(method, url, **kwargs)
        assert method == "POST"
        candidates = deepcopy(kwargs["json"]["candidates"])
        self.candidates.append(candidates)
        address = candidates["ip"]["addresses"][0]
        assert address["interface"] == "me0"
        assert address["address"] == "198.18.0.1/24"
        key = row_ref(
            "interface", "interface.ipv4-address", (("interface", ("me0",)), ("ipv4-address", ("198.18.0.1",)))
        )
        identity = {
            "stream": "ip",
            "row": {"subject": key.subject, "key": key.key},
            "source_push_seq": 1,
            "frozen_revision": 1,
            "generation_id": None,
            "settlement_cohort": None,
        }
        catalog = MESSAGES["write-blocked"]
        refusal = {
            "family": "interface",
            "row": identity["row"],
            "field": "interface.ip.v4",
            "reason": catalog.reason,
            "message_id": catalog.id,
            "message": catalog.text,
            "related": None,
            "logical_rows": [
                {
                    **identity,
                    "acceptance_identity": address["accepted_at"],
                    "carrier": {**identity, "accepted_at": address["accepted_at"]},
                }
            ],
            "provenance": "current",
            "sender_phase": "pre_commit_dry_run",
        }
        wire = make_apply_preflight(
            dict.fromkeys(candidates, 1),
            blocked={"ip": {"status": "blocked", "refusals": [refusal]}},
            status="refused",
        )
        wire["refusals"] = [refusal]
        wire["attempts"] = [{"selection": dict.fromkeys(candidates, 1), "dropped": []}]
        return make_response(200, wire)


class _AcquisitionCase:
    adapter_class = IntendAdapter

    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("intend-contract", 17881)
        self.interface = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t", mtu=9000)
        self.user = get_user_model().objects.create_user(username="intend-contract-operator", is_superuser=True)
        self.adapter = self.adapter_class()
        for patcher in self.adapter.patches():
            patcher.start()
            self.addCleanup(patcher.stop)

    def bookkeeping(self):
        return {
            model._meta.model_name: list(model.objects.order_by("pk").values())
            for model in (NSOOwnershipManifest, NSOIntentRevision, NSOIntentOutboxEntry, NSOIntentOutboxState)
        }

    def preview(self, selection, user=None):
        result = preview_intend(self.management, user or self.user, selection)
        self.assertEqual(result.status, "ready", result.message)
        self.assertTrue(result.confirmation)
        return result

    def acquire(self, selection):
        preview = self.preview(selection)
        with without_commit_drain():
            result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "acquired", result.message)
        return result

    def vlan(self):
        group = VLANGroup.objects.create(name="Intend VLANs", slug=f"nso-{self.device.pk}")
        return VLAN.objects.create(group=group, name="Intend native VLAN", vid=101)


class TestIntendDependencyContract(_AcquisitionCase, _CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def peer_policy_graph(self):
        from netbox_routing.models import BGPPeer, BGPPeerAddressFamily

        registry = RIR.objects.create(name="Intend private ASNs", slug="intend-private-asns")
        asn = ASN.objects.create(rir=registry, asn=64512)
        router = BGPRouter.objects.create(assigned_object=self.device, asn=asn, router_id="198.18.0.1")
        scope = BGPScope.objects.create(router=router)
        family = BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
        peer = BGPPeer.objects.create(scope=scope, peer=IPAddress.objects.create(address="198.18.0.2/32"))
        peer_family = BGPPeerAddressFamily.objects.create(assigned_object=peer, address_family=family)
        return [native_selection("bgp", router), native_selection("bgp", peer)], peer_family

    def test_peer_address_family_requires_each_explicit_native_policy(self):
        from netbox_routing.models import PrefixList, RouteMap

        selection, family = self.peer_policy_graph()
        for field, model in (
            ("routemap_in", RouteMap),
            ("routemap_out", RouteMap),
            ("prefixlist_in", PrefixList),
            ("prefixlist_out", PrefixList),
        ):
            with self.subTest(field=field):
                policy = model.objects.create(name=f"INTEND-{field}", **({"family": 4} if model is PrefixList else {}))
                setattr(family, field, policy)
                family.save(update_fields=(field,))
                before = self.bookkeeping()
                result = preview_intend(self.management, self.user, selection)
                self.assertEqual(result.status, "refused", result.message)
                self.assertIn(policy.name, result.message)
                self.assertEqual(self.bookkeeping(), before)
                self.assertFalse(self.adapter.candidates)
                setattr(family, field, None)
                family.save(update_fields=(field,))

    def test_peer_policy_closure_requires_route_map_contributors(self):
        from netbox_routing.models import PrefixList, RouteMap, RouteMapEntry

        selection, family = self.peer_policy_graph()
        policy = RouteMap.objects.create(name="INTEND-PEER-POLICY")
        contributor = PrefixList.objects.create(name="INTEND-PEER-PREFIXES", family=4)
        entry = RouteMapEntry.objects.create(route_map=policy, sequence=10, action="permit")
        entry.match_prefix_list.add(contributor)
        family.routemap_in = policy
        family.save(update_fields=("routemap_in",))

        result = preview_intend(self.management, self.user, selection)
        self.assertEqual(result.status, "refused", result.message)
        self.assertIn(policy.name, result.message)
        selection.append(native_selection("route_policy", policy))
        result = preview_intend(self.management, self.user, selection)
        self.assertEqual(result.status, "refused", result.message)
        self.assertIn(contributor.name, result.message)
        self.assertFalse(self.adapter.candidates)
        self.acquire([*selection, native_selection("route_policy", contributor)])

    def test_owned_peer_policy_is_frozen_and_requires_an_acceptance_timestamp(self):
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState

        selection, family = self.peer_policy_graph()
        policy = PrefixList.objects.create(name="INTEND-OWNED-PREFIXES", family=4)
        self.acquire([native_selection("route_policy", policy)])
        owner = NSORoutePolicyState.objects.get(management=self.management, object_name=policy.name)
        family.prefixlist_in = policy
        family.save(update_fields=("prefixlist_in",))
        preview = self.preview(selection)
        policy.description = "Changed policy inputs"
        policy.save(update_fields=("description",))
        refused = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(refused.status, "refused", refused.message)
        self.assertIn("inputs changed", refused.message)
        self.preview(selection)

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE netbox_nso_plugin_nsoroutepolicystate SET accepted_at = NULL WHERE id = %s", [owner.pk]
            )
        before = self.bookkeeping()
        result = preview_intend(self.management, self.user, selection)
        self.assertEqual(result.status, "refused", result.message)
        self.assertIn(policy.name, result.message)
        self.assertIn("acceptance timestamp", result.message)
        self.assertEqual(self.bookkeeping(), before)

    def test_related_native_values_stale_confirmation_without_changing_the_wire_content(self):
        vrf = VRF.objects.create(name="INTEND-VRF")
        route = StaticRoute.objects.create(vrf=vrf, prefix="198.18.70.0/24", next_hop="198.18.0.1")
        route.devices.add(self.device)
        preview = self.preview([native_selection("static_route", route)])
        vrf.description = "Changed acquisition inputs"
        vrf.save(update_fields=("description",))
        before = self.bookkeeping()

        result = confirm_intend(self.management, self.user, preview.confirmation)

        self.assertEqual(result.status, "refused", result.message)
        self.assertIn("inputs changed", result.message)
        self.assertEqual(len(self.adapter.candidates), 1)
        self.assertEqual(self.bookkeeping(), before)

    def assert_pending_switching_deletion_is_preflighted(self, scope):
        from netbox_nso_plugin.models import NSOSwitchingRootDeletion

        if scope == "lacp":
            root = Interface.objects.create(device=self.device, name="Port-channel2", type="lag")
            selected = native_selection(scope, root, lag_id=2)
            deleted = "Port-channel1"
        else:
            self.interface.mode = "access"
            self.interface.save(update_fields=("mode",))
            selected = native_selection(scope, self.interface)
            deleted = "Ethernet99"
        deletion = NSOSwitchingRootDeletion.objects.create(
            management=self.management, scope=scope, root_name=deleted, event_version=1
        )

        preview = self.preview([selected])

        stream = delivery.delivery_keys()[scope].section
        self.assertEqual(self.adapter.candidates[-1][stream]["deleted_roots"], [deleted])
        NSOSwitchingRootDeletion.objects.filter(pk=deletion.pk).update(event_version=2)
        before = self.bookkeeping()
        refused = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(refused.status, "refused", refused.message)
        self.assertEqual(len(self.adapter.candidates), 1)
        self.assertEqual(self.bookkeeping(), before)

        self.adapter.promotes = True
        refused = preview_intend(self.management, self.user, [selected])
        self.assertEqual(refused.status, "refused", refused.message)
        self.assertIn(f"{stream}: removal", refused.message)
        self.assertEqual(self.bookkeeping(), before)
        self.adapter.promotes = False
        preview = self.preview([selected])
        with without_commit_drain():
            acquired = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(acquired.status, "acquired", acquired.message)
        self.assertTrue(NSOSwitchingRootDeletion.objects.filter(pk=deletion.pk).exists())
        self.assertEqual(drain.drain_key(self.device.pk, scope, chain=0), drain.SUCCEEDED)
        self.assertEqual(self.adapter.requests[-1]["body"], self.adapter.candidates[-1][stream])

    def test_pending_lacp_root_deletion_is_in_candidate_evidence(self):
        self.assert_pending_switching_deletion_is_preflighted("lacp")

    def test_pending_switchport_root_deletion_is_in_candidate_evidence(self):
        self.assert_pending_switching_deletion_is_preflighted("switchport")

    def test_confirm_refuses_changed_switching_envelope_after_locked_comparison(self):
        from django.db.models.signals import post_save

        from netbox_nso_plugin.models import NSOSwitchingRootDeletion

        self.interface.mode = "access"
        self.interface.save(update_fields=("mode",))
        preview = self.preview([native_selection("switchport", self.interface)])
        before = self.bookkeeping()

        def add_deletion(sender, instance, **kwargs):
            NSOSwitchingRootDeletion.objects.create(
                management=self.management, scope="switchport", root_name="Ethernet99", event_version=1
            )

        post_save.connect(add_deletion, sender=NSOSwitchportState, weak=False)
        try:
            with without_commit_drain():
                result = confirm_intend(self.management, self.user, preview.confirmation)
        finally:
            post_save.disconnect(add_deletion, sender=NSOSwitchportState)
        self.assertEqual(result.status, "refused", result.message)
        self.assertIn("rendering", result.message)
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSOSwitchingRootDeletion.objects.filter(management=self.management).exists())
        self.assertFalse(NSOSwitchportState.objects.filter(management=self.management).exists())

    def test_intend_lacp_member_requires_renderable_bundle(self):
        bundle = Interface.objects.create(device=self.device, name="Port-channel1", type="lag")
        member = Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t", lag=bundle)
        before = self.bookkeeping()

        refused = preview_intend(self.management, self.user, [native_selection("lacp", member)])

        self.assertEqual(refused.status, "refused")
        self.assertIn("bundle", refused.message.lower())
        self.assertIsNone(refused.confirmation)
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSOLACPMemberState.objects.filter(interface=member).exists())
        self.assertFalse(NSOLACPBundleState.objects.filter(interface=bundle).exists())
        self.assertEqual(self.adapter.candidates, [])

        self.acquire([native_selection("lacp", member), native_selection("lacp", bundle, lag_id=1)])

        self.assertEqual(NSOLACPMemberState.objects.get(interface=member).status, "accepted")
        self.assertEqual(NSOLACPBundleState.objects.get(interface=bundle).status, "accepted")
        rendered = delivery.render("lacp", self.device.pk, self.management.adapter_device_id).payload
        self.assertEqual(rendered["bundles"][0]["name"], bundle.name)
        self.assertEqual(rendered["bundles"][0]["members"][0]["interface_name"], member.name)

    def test_intend_redistribution_requires_renderable_destination(self):
        destination = ISISInstance.objects.create(
            device=self.device, process_tag="CORE", net="49.0001.0000.0000.0001.00"
        )
        redistribution = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(ISISInstance),
            destination_id=destination.pk,
            source_protocol="static",
            metric=17,
        )
        before = self.bookkeeping()

        refused = preview_intend(self.management, self.user, [native_selection("redistribution", redistribution)])

        self.assertEqual(refused.status, "refused")
        self.assertIn("destination", refused.message.lower())
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSORedistributionState.objects.filter(redistribution=redistribution).exists())
        self.assertEqual(self.adapter.candidates, [])

        result = self.acquire(
            [native_selection("redistribution", redistribution), native_selection("isis", destination)]
        )

        self.assertEqual(result.keys, ("isis",))
        self.assertEqual(NSOISISInstanceState.objects.get(isis_instance=destination).status, "accepted")
        self.assertEqual(NSORedistributionState.objects.get(redistribution=redistribution).status, "accepted")
        rendered = delivery.render("isis", self.device.pk, self.management.adapter_device_id).payload
        self.assertEqual(
            rendered[1][0]["redistribution"], [{"source_protocol": "static", "source_ref": "", "metric": 17}]
        )

    def test_route_map_requires_explicit_policy_contributors(self):
        from netbox_routing.models import PrefixList, RouteMap, RouteMapEntry

        from netbox_nso_plugin.models import NSORoutePolicyState

        prefix_list = PrefixList.objects.create(name="INTEND-PREFIXES", family=4)
        route_map = RouteMap.objects.create(name="INTEND-POLICY")
        entry = RouteMapEntry.objects.create(route_map=route_map, sequence=10, action="permit")
        entry.match_prefix_list.add(prefix_list)
        before = self.bookkeeping()
        refusal = preview_intend(self.management, self.user, [native_selection("route_policy", route_map)])
        self.assertEqual(refusal.status, "refused")
        self.assertIn("dependency", refusal.message)
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSORoutePolicyState.objects.filter(management=self.management).exists())
        self.acquire([native_selection("route_policy", route_map), native_selection("route_policy", prefix_list)])
        self.assertEqual(
            set(NSORoutePolicyState.objects.filter(management=self.management).values_list("family", flat=True)),
            {"prefix_list", "route_map"},
        )
        self.assertEqual(list(entry.match_prefix_list.values_list("pk", flat=True)), [prefix_list.pk])

    def test_intend_candidate_batch_validates_identity_conflicts(self):
        selected = native_selection("interface_mtu", self.interface)
        before = self.bookkeeping()

        result = preview_intend(self.management, self.user, [selected, deepcopy(selected)])

        self.assertEqual(result.status, "refused")
        self.assertIn("duplicate", result.message.lower())
        self.assertIsNone(result.confirmation)
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSOInterfaceMtuState.objects.filter(interface=self.interface).exists())
        self.assertEqual(self.adapter.candidates, [])

    def test_native_visibility_and_overlay_permission_are_both_required(self):
        hidden_native = operator(
            (("view",), Interface, {"id": self.interface.pk + 1}),
            (("add",), NSOInterfaceMtuState, None),
        )
        missing_overlay_permission = operator((("view",), Interface, None))
        before = self.bookkeeping()

        for user in (hidden_native, missing_overlay_permission):
            with self.subTest(user=user.username):
                result = preview_intend(self.management, user, [native_selection("interface_mtu", self.interface)])
                self.assertEqual(result.status, "refused")
                self.assertIsNone(result.confirmation)
                self.assertEqual(self.bookkeeping(), before)
                self.assertFalse(NSOInterfaceMtuState.objects.filter(interface=self.interface).exists())

        self.assertEqual(self.adapter.candidates, [])

    def test_confirm_rechecks_native_visibility(self):
        user = operator((("view",), Interface, None), (("add",), NSOInterfaceMtuState, None))
        preview = self.preview([native_selection("interface_mtu", self.interface)], user=user)
        user.object_permissions.filter(object_types__app_label="dcim", object_types__model="interface").delete()
        user = get_user_model().objects.get(pk=user.pk)
        before = self.bookkeeping()

        result = confirm_intend(self.management, user, preview.confirmation)

        self.assertEqual(result.status, "refused")
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSOInterfaceMtuState.objects.filter(interface=self.interface).exists())
        self.assertEqual(len(self.adapter.candidates), 1)

    def test_permission_revoked_during_preflight_refuses_before_acquisition(self):
        user = operator((("view",), Interface, None), (("add",), NSOInterfaceMtuState, None))
        preview = self.preview([native_selection("interface_mtu", self.interface)], user=user)

        def revoke_permission():
            user.object_permissions.filter(object_types__app_label="dcim", object_types__model="interface").delete()

        self.adapter.before_response = revoke_permission
        before = self.bookkeeping()
        with without_commit_drain():
            result = confirm_intend(self.management, user, preview.confirmation)
        self.assertEqual(result.status, "refused", result.message)
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSOInterfaceMtuState.objects.filter(interface=self.interface).exists())

    def test_native_edit_during_preflight_is_rechecked_under_writer_locks(self):
        preview = self.preview([native_selection("interface_mtu", self.interface)])

        def edit_native():
            self.interface.mtu = 8000
            self.interface.save(update_fields=("mtu",))

        self.adapter.before_response = edit_native
        before = self.bookkeeping()
        result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "refused", result.message)
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSOInterfaceMtuState.objects.filter(interface=self.interface).exists())
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.mtu, 8000)

    def test_native_delete_during_preflight_refuses_without_acquisition(self):
        preview = self.preview([native_selection("interface_mtu", self.interface)])
        self.adapter.before_response = lambda: Interface.objects.filter(pk=self.interface.pk).delete()
        with without_commit_drain():
            result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "refused", result.message)
        self.assertIn("Preview again", result.message)
        self.assertFalse(Interface.objects.filter(pk=self.interface.pk).exists())
        self.assertFalse(NSOInterfaceMtuState.objects.filter(management=self.management).exists())
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk).exists())

    def test_selected_policy_with_unresolved_references_refuses(self):
        from netbox_routing.models import RouteMap, RouteMapEntry

        from netbox_nso_plugin.models import NSORoutePolicyState

        route_map = RouteMap.objects.create(name="INTEND-UNRESOLVED")
        entry = RouteMapEntry.objects.create(route_map=route_map, sequence=10, action="permit")
        RouteMapEntry.objects.filter(pk=entry.pk).update(vendor_ext={"unmapped": {"match_prefix_list": ["MISSING"]}})
        before = self.bookkeeping()
        result = preview_intend(self.management, self.user, [native_selection("route_policy", route_map)])
        self.assertEqual(result.status, "refused", result.message)
        self.assertIn("unresolved references", result.message)
        self.assertIsNone(result.confirmation)
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSORoutePolicyState.objects.filter(management=self.management).exists())
        self.assertEqual(self.adapter.candidates, [])

    def test_acquisition_preserves_native_relations_and_device_observations(self):
        vlan = self.vlan()
        self.interface.mode = "tagged"
        self.interface.save(update_fields=("mode",))
        self.interface.tagged_vlans.add(vlan)
        state = NSOSwitchportState.objects.create(
            management=self.management, interface=self.interface, status="imported"
        )
        read_state = NSOFamilyReadState.objects.create(management=self.management, family="interface_mtu")
        observed = scope_observation("interface_mtu")
        observed.pop("family")
        observed["observed_at"] = timezone.now()
        NSOFamilyObservation.objects.create(read_state=read_state, **observed)
        native_before = Interface.objects.filter(pk=self.interface.pk).values().get()
        tags_before = list(self.interface.tagged_vlans.order_by("pk").values_list("pk", flat=True))
        observation_before = NSOFamilyObservation.objects.filter(read_state=read_state).values().get()
        reads_before = NSOFamilyReadState.objects.filter(pk=read_state.pk).values().get()
        selection = [native_selection("switchport", self.interface), native_selection("interface_mtu", self.interface)]
        before = self.bookkeeping()

        preview = self.preview(selection)

        self.assertEqual(self.bookkeeping(), before)
        state.refresh_from_db()
        self.assertEqual(state.status, "imported")
        self.assertFalse(state.tagged_vlans.exists())
        with without_commit_drain():
            result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "acquired", result.message)

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertEqual(list(state.tagged_vlans.values_list("pk", flat=True)), tags_before)
        self.assertEqual(Interface.objects.filter(pk=self.interface.pk).values().get(), native_before)
        self.assertEqual(list(self.interface.tagged_vlans.order_by("pk").values_list("pk", flat=True)), tags_before)
        self.assertEqual(NSOFamilyObservation.objects.filter(read_state=read_state).values().get(), observation_before)
        self.assertEqual(NSOFamilyReadState.objects.filter(pk=read_state.pk).values().get(), reads_before)

    def test_changed_static_route_keeps_observation_and_renders_native_next_hop(self):
        route = StaticRoute.objects.create(prefix="198.18.20.0/24", next_hop="198.18.0.2", metric=17)
        route.devices.add(self.device)
        observed = {"nso_vrf": "OBSERVED", "nso_prefix": "198.18.19.0/24", "nso_next_hop": "198.18.0.1"}
        state = NSOStaticRouteState.objects.create(
            management=self.management, static_route=route, status="changed", **observed
        )
        native_before = StaticRoute.objects.filter(pk=route.pk).values().get()

        self.acquire([native_selection("static_route", route)])

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertEqual({field: getattr(state, field) for field in observed}, observed)
        self.assertEqual(StaticRoute.objects.filter(pk=route.pk).values().get(), native_before)
        rendered = delivery.render("static_route", self.device.pk, self.management.adapter_device_id).payload
        self.assertEqual(rendered[0]["prefix"], "198.18.20.0/24")
        self.assertEqual(rendered[0]["next_hop"], "198.18.0.2")
        self.assertEqual(rendered[0]["vrf"], "")

    def test_existing_ospf_instance_keeps_disabled_option(self):
        instance = OSPFInstance.objects.create(device=self.device, name="17", process_id="17", router_id="198.18.0.17")
        state = NSOOSPFInstanceState.objects.create(
            management=self.management,
            ospf_instance=instance,
            process_id="17",
            router_id="198.18.0.17",
            enabled=False,
            status="imported",
        )
        native_before = OSPFInstance.objects.filter(pk=instance.pk).values().get()

        self.acquire([native_selection("ospf", instance)])

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertIs(state.enabled, False)
        self.assertIs(self.adapter.candidates[0]["ospf"]["instances"][0]["enabled"], False)
        rendered = delivery.render("ospf", self.device.pk, self.management.adapter_device_id).payload
        self.assertIs(rendered["instances"][0]["enabled"], False)
        self.assertEqual(OSPFInstance.objects.filter(pk=instance.pk).values().get(), native_before)

    def test_matching_unlinked_bgp_router_conflict_is_acquired_in_place(self):
        registry = RIR.objects.create(name="Intend private ASNs", slug="intend-private-asns")
        asn = ASN.objects.create(rir=registry, asn=64512)
        router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=self.device.pk,
            asn=asn,
            router_id="198.18.0.1",
        )
        scope = BGPScope.objects.create(router=router)
        BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
        state = NSOBGPRouterState.objects.create(
            management=self.management, asn_str="64512", router_id="198.18.0.2", status="conflict", bgp_router=None
        )
        native_before = BGPRouter.objects.filter(pk=router.pk).values().get()

        self.acquire([native_selection("bgp", router)])

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertEqual(state.bgp_router_id, router.pk)
        self.assertEqual(NSOBGPRouterState.objects.filter(management=self.management, asn_str="64512").count(), 1)
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="bgp")
        self.assertEqual(manifest.native_id, router.pk)
        self.assertEqual(manifest.state_model_label, state._meta.label_lower)
        self.assertEqual(manifest.state_key, {"asn_str": "64512"})
        self.assertEqual(BGPRouter.objects.filter(pk=router.pk).values().get(), native_before)


class TestIntendJunosRefusal(_AcquisitionCase, IntentPushResetMixin, TestCase):
    adapter_class = JunosPhysicalIPAdapter

    def test_junos_physical_ip_refusal_creates_no_ownership(self):
        interface = Interface.objects.create(device=self.device, name="me0", type="1000base-t")
        address = IPAddress.objects.create(address="198.18.0.1/24", assigned_object=interface)
        native_before = IPAddress.objects.filter(pk=address.pk).values().get()
        before = self.bookkeeping()

        result = preview_intend(self.management, self.user, [native_selection("ip", address)])

        self.assertEqual(result.status, "refused")
        self.assertIsNone(result.confirmation)
        self.assertIsNotNone(result.preflight)
        self.assertEqual(result.preflight["streams"]["ip"]["status"], "blocked")
        self.assertEqual(result.preflight["refusals"][0]["reason"], "inexpressible-on-ned")
        self.assertEqual(self.bookkeeping(), before)
        self.assertEqual(IPAddress.objects.filter(pk=address.pk).values().get(), native_before)
        self.assertFalse(NSOInterfaceIPState.objects.filter(interface=interface).exists())
        self.assertEqual(len(self.adapter.candidates), 1)
        self.assertEqual(self.adapter.requests, [])


class TestIntendRelatedRows(_AcquisitionCase, _CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def assert_related_edit_is_locked(self, selection, related, field, value):
        import json
        from concurrent.futures import ThreadPoolExecutor

        from django.db import OperationalError
        from django.db.models.signals import pre_save

        preview = self.preview(selection)
        admitted = deepcopy(self.adapter.candidates[-1])
        edits, contributions = [], []

        def edit_related():
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '200ms'")
                    cursor.execute(
                        f"UPDATE {connection.ops.quote_name(related._meta.db_table)} "
                        f"SET {connection.ops.quote_name(field)} = %s WHERE id = %s",
                        [value, related.pk],
                    )
                return "edited"
            except OperationalError as exc:
                if getattr(exc.__cause__, "sqlstate", None) != "55P03":
                    raise
                return "blocked"
            finally:
                connection.close()

        def after_locked_comparison(sender, instance, **kwargs):
            if instance.device_id != self.device.pk:
                return
            contributions.append(instance.scope)
            if len(contributions) != 2:
                return
            with ThreadPoolExecutor(max_workers=1) as executor:
                edits.append(executor.submit(edit_related).result(timeout=10))

        pre_save.connect(after_locked_comparison, sender=NSOIntentOutboxEntry, weak=False)
        try:
            with without_commit_drain():
                result = confirm_intend(self.management, self.user, preview.confirmation)
        finally:
            pre_save.disconnect(after_locked_comparison, sender=NSOIntentOutboxEntry)

        self.assertEqual(result.status, "acquired", result.message)
        for key in result.keys:
            rendered = delivery.render(key, self.device.pk, self.management.adapter_device_id)
            self.assertEqual(json.loads(delivery.wire_body(rendered, rendered.payload)), admitted[key])
        self.assertEqual(edits, ["blocked"])

    def test_static_route_vrf_edit_after_locked_comparison_blocks(self):
        vrf = VRF.objects.create(name="INTEND-VRF")
        route = StaticRoute.objects.create(vrf=vrf, prefix="198.18.70.0/24", next_hop="198.18.0.1")
        route.devices.add(self.device)
        self.assert_related_edit_is_locked([native_selection("static_route", route)], vrf, "name", "CHANGED-VRF")

    def test_bgp_asn_edit_after_locked_comparison_blocks(self):
        registry = RIR.objects.create(name="Intend private ASNs", slug="intend-private-asns")
        asn = ASN.objects.create(rir=registry, asn=64512)
        router = BGPRouter.objects.create(assigned_object=self.device, asn=asn, router_id="198.18.0.1")
        scope = BGPScope.objects.create(router=router)
        BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
        self.assert_related_edit_is_locked([native_selection("bgp", router)], asn, "asn", 64513)

    def test_policy_ipam_prefix_edit_after_locked_comparison_blocks(self):
        from ipam.models import Prefix
        from netbox_routing.models import PrefixList, PrefixListEntry

        prefix = Prefix.objects.create(prefix="198.18.80.0/24")
        policy = PrefixList.objects.create(name="INTEND-PREFIXES", family=4)
        PrefixListEntry.objects.create(prefix_list=policy, assigned_prefix=prefix, sequence=10, action="permit")
        self.assert_related_edit_is_locked(
            [native_selection("route_policy", policy)], prefix, "prefix", "198.18.81.0/24"
        )

    def test_ospf_area_edit_after_locked_comparison_blocks(self):
        from netbox_routing.models import OSPFArea, OSPFInterface

        instance = OSPFInstance.objects.create(device=self.device, name="17", process_id="17", router_id="198.18.0.17")
        area = OSPFArea.objects.create(area_id="0.0.0.0")
        OSPFInterface.objects.create(instance=instance, interface=self.interface, area=area)
        self.assert_related_edit_is_locked([native_selection("ospf", instance)], area, "area_id", "0.0.0.1")

    def test_isis_level_edit_after_locked_comparison_blocks(self):
        from netbox_routing.models import ISISLevel

        instance = ISISInstance.objects.create(device=self.device, process_tag="CORE", net="49.0001.0000.0000.0001.00")
        level = ISISLevel.objects.create(instance=instance, level=2, labeled_preference=120)
        self.assert_related_edit_is_locked([native_selection("isis", instance)], level, "labeled_preference", 130)


class TestIntendOrdinaryDelivery(_AcquisitionCase, _CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def selection(self):
        vlan = self.vlan()
        return [native_selection("interface_mtu", self.interface), native_selection("vlan", vlan)]

    def assert_acknowledged(self, keys):
        for key in keys:
            with self.subTest(key=key):
                revision = NSOIntentRevision.objects.get(device=self.device, scope=key)
                self.assertEqual(revision.verified_revision, revision.revision)
                self.assertEqual(
                    revision.verified_fingerprint,
                    delivery.canonical_fingerprint(
                        delivery.render(key, self.device.pk, self.management.adapter_device_id).payload
                    ),
                )
                self.assertIsNotNone(revision.verified_at)
                state = NSOIntentOutboxState.objects.get(device=self.device, scope=key)
                self.assertIsNotNone(state.last_success_at)
                self.assertTrue(state.last_success_identity)
        self.assertFalse(NSOIntentOutboxEntry.objects.filter(device=self.device, scope__in=keys).exists())

    def test_multi_key_acquisition_is_scheduled_atomically_and_commit_drains_normally(self):
        preview = self.preview(self.selection())

        with transaction.atomic():
            result = confirm_intend(self.management, self.user, preview.confirmation)
            self.assertEqual(result.status, "acquired", result.message)
            entries = list(NSOIntentOutboxEntry.objects.filter(device=self.device, scope__in=result.keys))
            self.assertEqual({entry.scope for entry in entries}, {"interface_mtu", "vlan"})
            self.assertEqual({entry.kind for entry in entries}, {"ordinary"})
            self.assertEqual(len({entry.batch_id for entry in entries}), 1)
            self.assertTrue(all(entry.consumed_by_push_seq is None for entry in entries))
            self.assertEqual(
                NSOOwnershipManifest.objects.filter(device_id=self.device.pk, grant_kind="intend").count(), 2
            )
            self.assertEqual(self.adapter.requests, [])

        self.assert_acknowledged(result.keys)
        self.assertEqual(len(self.adapter.applied), 2)
        self.assertTrue(all(request["params"].get("store_only") != "true" for request in self.adapter.requests))
        self.assertTrue(all(request["params"].get("backfill_only") != "true" for request in self.adapter.requests))

    def test_lost_commit_callback_recovers_every_key_through_real_drain(self):
        result = self.acquire(self.selection())

        self.assertEqual(result.message, "acquired; delivery pending")
        self.assertEqual(self.adapter.requests, [])
        self.assertEqual(
            set(NSOIntentOutboxEntry.objects.filter(device=self.device).values_list("scope", flat=True)),
            {"interface_mtu", "vlan"},
        )

        self.assertEqual(drain.drain_intent_outbox(), (2, 0))

        self.assert_acknowledged(result.keys)
        self.assertEqual(len(self.adapter.applied), 2)
        self.assertEqual(drain.drain_intent_outbox(), (0, 0))
        self.assertEqual(len(self.adapter.applied), 2)

    def test_interrupted_delivery_recovers_the_remaining_key_without_callbacks(self):
        from netbox_nso_plugin import signals

        result = self.acquire(self.selection())
        self.assertEqual(drain.drain_key(self.device.pk, result.keys[0], chain=0), drain.SUCCEEDED)
        self.assertEqual(
            set(NSOIntentOutboxEntry.objects.filter(device=self.device).values_list("scope", flat=True)),
            {result.keys[1]},
        )
        signals.reset_intent_push_state()
        self.assertEqual(drain.drain_intent_outbox(), (1, 0))
        self.assert_acknowledged(result.keys)
        self.assertEqual(len(self.adapter.applied), 2)

    def test_both_switching_scopes_use_ordinary_preparation_and_acknowledgement(self):
        from netbox_nso_plugin.models import NSOAcceptedSelection

        bundle = Interface.objects.create(device=self.device, name="Port-channel1", type="lag")
        member = Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t", lag=bundle)
        self.interface.mode = "access"
        self.interface.save(update_fields=("mode",))
        result = self.acquire(
            [
                native_selection("lacp", bundle, lag_id=1),
                native_selection("lacp", member),
                native_selection("switchport", self.interface),
            ]
        )
        self.assertEqual(result.keys, ("lacp", "switchport"))
        self.assertEqual(drain.drain_intent_outbox(), (2, 0))
        self.assertFalse(NSOIntentOutboxEntry.objects.filter(device=self.device).exists())
        for scope, stream in (("lacp", "lag"), ("switchport", "switchport")):
            revision = NSOIntentRevision.objects.get(device=self.device, scope=scope)
            selection = NSOAcceptedSelection.objects.get(management=self.management, stream=stream)
            self.assertIsNotNone(selection.acknowledged_at)
            self.assertEqual(selection.publication_state, "sent")
            self.assertEqual(selection.plugin_revision, revision.revision)
            self.assertGreater(selection.selection_revision, 0)
        for request in self.adapter.requests:
            stream = "lag" if "bundles" in request["body"] else "switchport"
            self.assertEqual(request["body"], self.adapter.candidates[0][stream])

    def test_constrained_overlay_add_permission_refuses_confirm_without_committed_writes(self):
        user = operator(
            (("view",), Interface, None),
            (("add",), NSOInterfaceMtuState, {"management_id": self.management.pk + 1}),
        )
        preview = self.preview([native_selection("interface_mtu", self.interface)], user=user)
        before = self.bookkeeping()

        with without_commit_drain():
            result = confirm_intend(self.management, user, preview.confirmation)

        self.assertEqual(result.status, "refused")
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSOInterfaceMtuState.objects.filter(interface=self.interface).exists())
        self.assertEqual(self.adapter.requests, [])
        self.assertEqual(drain.drain_intent_outbox(), (0, 0))

    def test_second_key_database_failure_rolls_back_ownership_relations_and_delivery(self):
        vlan = self.vlan()
        self.interface.mode = "tagged"
        self.interface.save(update_fields=("mode",))
        self.interface.tagged_vlans.add(vlan)
        switchport = NSOSwitchportState.objects.create(
            management=self.management, interface=self.interface, status="imported"
        )
        preview = self.preview(
            [native_selection("interface_mtu", self.interface), native_selection("switchport", self.interface)]
        )
        before = self.bookkeeping()
        table = connection.ops.quote_name(NSOIntentOutboxEntry._meta.db_table)
        relation_table = connection.ops.quote_name(
            NSOSwitchportState._meta.get_field("tagged_vlans").remote_field.through._meta.db_table
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE FUNCTION pg_temp.reject_switchport_delivery() RETURNS trigger AS $$
                BEGIN
                    IF NEW.scope = 'switchport' AND EXISTS (SELECT 1 FROM {relation_table}) THEN
                        RAISE EXCEPTION 'switchport delivery rejected' USING ERRCODE = '23514';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
                """
            )
            cursor.execute(
                f"CREATE TRIGGER reject_switchport_delivery BEFORE INSERT ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_switchport_delivery()"
            )
        try:
            with without_commit_drain(), self.assertRaisesRegex(IntegrityError, "switchport delivery rejected"):
                confirm_intend(self.management, self.user, preview.confirmation)
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP TRIGGER reject_switchport_delivery ON {table}")
                cursor.execute("DROP FUNCTION pg_temp.reject_switchport_delivery()")

        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSOInterfaceMtuState.objects.filter(interface=self.interface).exists())
        switchport.refresh_from_db()
        self.assertEqual(switchport.status, "imported")
        self.assertIsNone(switchport.accepted_at)
        self.assertFalse(switchport.tagged_vlans.exists())
        self.assertEqual(list(self.interface.tagged_vlans.values_list("pk", flat=True)), [vlan.pk])
        self.assertEqual(self.adapter.requests, [])
        self.assertEqual(drain.drain_intent_outbox(), (0, 0))


class TestIntendRestart(_AcquisitionCase, _CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def test_pending_route_deletion_refuses_until_drain_and_keeps_verified_lineage(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        from ._ownership_case import acquire_overlay

        route = StaticRoute.objects.create(prefix="198.18.50.0/24", next_hop="198.18.0.1")
        route.devices.add(self.device)
        acknowledged = {"vrf": "", "prefix": "198.18.50.0/24", "next_hop": "198.18.0.1"}
        with without_commit_drain():
            row = acquire_overlay(
                NSOStaticRouteState,
                management=self.management,
                static_route=route,
                status="accepted",
                last_acked_triple=acknowledged,
                nso_prefix="198.18.50.0/24",
                nso_next_hop="198.18.0.1",
            )
        self.assertEqual(drain.drain_key(self.device.pk, "static_route", chain=0), drain.SUCCEEDED)
        plan = RendererMutationPlan.build(deletes=(planned_delete(row),))
        with without_commit_drain(), renderer_writes(plan) as writer:
            writer.delete(row)
        ended = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="static_route")
        self.assertNotEqual(ended.ownership_state, "owned")
        self.assertEqual(ended.acknowledged_lineage, [acknowledged])
        selection = [native_selection("static_route", route, restart=True)]
        before = self.bookkeeping()

        refused = preview_intend(self.management, self.user, selection)

        self.assertEqual(refused.status, "refused")
        self.assertIn("pending deletion", refused.message)
        self.assertIn("ordinary delivery", refused.message)
        self.assertNotIn("manual Apply", refused.message)
        self.assertEqual(self.bookkeeping(), before)
        self.assertFalse(NSOStaticRouteState.objects.filter(static_route=route).exists())
        import requests

        self.adapter.fail_with = requests.exceptions.ConnectionError("ordinary delivery interrupted")
        self.assertEqual(drain.drain_key(self.device.pk, "static_route", chain=0), drain.FAILED)
        state = NSOIntentOutboxState.objects.get(device=self.device, scope="static_route")
        self.assertEqual(state.claim_deletions[0]["route_id"], route.pk)
        captured_before = self.bookkeeping()
        captured_refusal = preview_intend(self.management, self.user, selection)
        self.assertEqual(captured_refusal.status, "refused")
        self.assertEqual(self.bookkeeping(), captured_before)
        self.adapter.fail_with = None
        self.assertEqual(drain.drain_key(self.device.pk, "static_route", chain=0), drain.SUCCEEDED)
        route.next_hop = "198.18.0.2"
        route.save(update_fields=("next_hop",))

        self.acquire(selection)

        acquired = NSOStaticRouteState.objects.get(static_route=route)
        self.assertEqual(acquired.last_acked_triple, acknowledged)
        owned = NSOOwnershipManifest.objects.get(
            device_id=self.device.pk, scope="static_route", ownership_state="owned"
        )
        self.assertEqual(owned.grant_kind, "intend")
        self.assertEqual(owned.acknowledged_lineage, [acknowledged])
        ended.refresh_from_db()
        self.assertEqual(ended.acknowledged_lineage, [acknowledged])
        self.assertEqual(delivery.render("static_route", self.device.pk, 17881).payload[0]["next_hop"], "198.18.0.2")
        deletion = RendererMutationPlan.build(deletes=(planned_delete(acquired),))
        with without_commit_drain(), renderer_writes(deletion) as writer:
            writer.delete(acquired)
        transitions = [
            transition
            for entry in NSOIntentOutboxEntry.objects.filter(device=self.device, scope="static_route")
            for transition in entry.transitions
        ]
        self.assertEqual(
            transitions[-1]["triples"],
            [acknowledged, {"vrf": "", "prefix": "198.18.50.0/24", "next_hop": "198.18.0.2"}],
        )

    def test_observed_redistribution_protocol_is_not_mapped_to_a_native_choice(self):
        destination = ISISInstance.objects.create(
            device=self.device, process_tag="CORE", net="49.0001.0000.0000.0001.00"
        )
        native = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(ISISInstance),
            destination_id=destination.pk,
            source_protocol="ospfv3",
        )
        row = NSORedistributionState.objects.create(
            management=self.management,
            redistribution=native,
            dest_protocol="isis",
            dest_ref="CORE",
            source_protocol="ospfv3",
            status="imported",
        )
        self.acquire([native_selection("isis", destination), native_selection("redistribution", native)])

        row.refresh_from_db()
        native.refresh_from_db()
        self.assertEqual(row.source_protocol, "ospfv3")
        self.assertEqual(native.source_protocol, "ospfv3")
        self.assertEqual(
            self.adapter.candidates[0]["isis"]["processes"][0]["redistribution"][0]["source_protocol"], "ospfv3"
        )

    def test_ended_owned_redistribution_restarts_even_when_wire_content_is_unchanged(self):
        destination = ISISInstance.objects.create(
            device=self.device, process_tag="CORE", net="49.0001.0000.0000.0001.00"
        )
        native = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(ISISInstance),
            destination_id=destination.pk,
            source_protocol="connected",
        )
        self.acquire([native_selection("isis", destination), native_selection("redistribution", native)])
        row = NSORedistributionState.objects.get(redistribution=native)
        accepted_before = row.accepted_at
        NSOOwnershipManifest.objects.filter(device_id=self.device.pk, state_model_label=row._meta.label_lower).update(
            ownership_state="retired"
        )
        before = delivery.render("isis", self.device.pk, 17881).payload
        preview = self.preview([native_selection("redistribution", native, restart=True)])
        with without_commit_drain():
            result = confirm_intend(self.management, self.user, preview.confirmation)
        self.assertEqual(result.status, "acquired", result.message)
        row.refresh_from_db()
        self.assertGreater(row.accepted_at, accepted_before)
        self.assertEqual(delivery.render("isis", self.device.pk, 17881).payload, before)
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, state_model_label=row._meta.label_lower)
        self.assertEqual(manifest.ownership_state, "owned")
        self.assertEqual(manifest.grant_kind, "intend")

    def test_restart_requires_the_current_episode_to_be_ended(self):
        route = StaticRoute.objects.create(prefix="198.18.51.0/24", next_hop="198.18.0.1")
        route.devices.add(self.device)
        self.acquire([native_selection("static_route", route)])
        NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="static_route").update(
            ownership_state="retired"
        )
        route.next_hop = "198.18.0.2"
        route.save(update_fields=("next_hop",))
        selection = [native_selection("static_route", route, restart=True)]
        self.acquire(selection)
        before = self.bookkeeping()

        result = preview_intend(self.management, self.user, selection)

        self.assertEqual(result.status, "refused", result.message)
        self.assertEqual(self.bookkeeping(), before)

    def test_native_route_cascade_keeps_acknowledged_and_acquired_identities(self):
        from ._static_route_case import _delete_owned_route

        route = StaticRoute.objects.create(prefix="198.18.52.0/24", next_hop="198.18.0.2")
        route.devices.add(self.device)
        acknowledged = {"vrf": "", "prefix": "198.18.52.0/24", "next_hop": "198.18.0.1"}
        NSOStaticRouteState.objects.create(
            management=self.management,
            static_route=route,
            status="changed",
            last_acked_triple=acknowledged,
            nso_prefix="198.18.52.0/24",
            nso_next_hop="198.18.0.1",
        )
        self.acquire([native_selection("static_route", route)])
        with without_commit_drain():
            _delete_owned_route(route)
        transitions = [
            transition
            for entry in NSOIntentOutboxEntry.objects.filter(device=self.device, scope="static_route")
            for transition in entry.transitions
        ]
        self.assertEqual(
            transitions[-1]["triples"],
            [acknowledged, {"vrf": "", "prefix": "198.18.52.0/24", "next_hop": "198.18.0.2"}],
        )
        self.assertFalse(NSOStaticRouteState.objects.filter(static_route=route).exists())
        self.assertFalse(StaticRoute.objects.filter(pk=route.pk).exists())
