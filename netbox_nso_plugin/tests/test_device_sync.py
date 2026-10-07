# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Sync from NSO end to end: fresh read, preview, digest, and the exact confirmed plan."""

from urllib.parse import urlsplit
from uuid import uuid4

from core.models import ObjectType
from dcim.models import Device, Interface
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from ipam.models import IPAddress
from netbox.signals import post_clean
from users.models import ObjectPermission

from netbox_nso_plugin.adapter_client import bound_session, reset_interfaces_doc_capability
from netbox_nso_plugin.models import (
    AdapterConnection,
    NSODeviceManagement,
    NSOIntentOutboxEntry,
    NSOInterfaceIPState,
    NSOInterfaceState,
)
from netbox_nso_plugin.signals import _pending_intent_keys, reset_intent_push_state

from ._adapter_http import make_response
from ._observation_case import ObservationTransport, interface_observation, ip_observation, observation
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin
from .test_gated_reconcile import _make
from .test_read_gate import _rs


class _FailingIPTransport(ObservationTransport):
    def send(self, request, **kwargs):
        if urlsplit(request.url).path.rsplit("/", 1)[-1] == "interface-ips":
            self.requests.append(request)
            response = make_response(503, {"detail": "adapter unavailable"})
            response.request = request
            response.url = request.url
            return response
        return super().send(request, **kwargs)


class SyncCase(IntentPushResetMixin, TestCase):
    def setUp(self):
        super().setUp()
        reset_interfaces_doc_capability()
        self.addCleanup(reset_interfaces_doc_capability)
        self.device, self.management = _make(
            f"sync{uuid4().hex[:8]}", manage_interfaces=True, manage_description=True, manage_enabled=True
        )
        self.lag = Interface.objects.get(device=self.device, name="lag-60")
        self.user = get_user_model().objects.create_user(username=f"sync{uuid4().hex[:8]}", is_superuser=True)
        self.client.force_login(self.user)
        self.preview_url = reverse("plugins:netbox_nso_plugin:device_nso_sync_preview", kwargs={"pk": self.device.pk})
        self.confirm_url = reverse("plugins:netbox_nso_plugin:device_nso_sync_confirm", kwargs={"pk": self.device.pk})

    def interface(self, name, **fields):
        return Interface.objects.create(device=self.device, name=name, type="virtual", **fields)

    def transport(self, *, interfaces=(), ips=(), unprojectable=(), transport_class=ObservationTransport):
        interface_snapshot = observation(
            "interface_attributes", interfaces=list(interfaces), unprojectable=list(unprojectable)
        )
        ip_snapshot = observation("interface_ip", interfaces=list(ips), unprojectable=list(unprojectable))
        transport = transport_class(self.management.adapter_device_id, interface_snapshot, ip_snapshot)
        transport.documents["interfaces-doc"]["interfaces"] = [
            {
                "name": item["name"],
                "attrs": {
                    "description": {"nso_value": item["description"] or "", "status": "imported"},
                    "enabled": {"nso_value": str(item["enabled"]), "status": "imported"},
                },
            }
            for item in interfaces
        ]
        transport.documents["interface-ips"]["interfaces"] = list(ips)
        return transport

    def preview(self, transport, data):
        with bound_session(transport.session()):
            response = self.client.post(self.preview_url, data)
        self.assertEqual(response.status_code, 200)
        return response, response.context["plan"]

    def confirm(self, plan, data):
        transport = self.transport()
        reset_intent_push_state()
        self.outbox_before = set(NSOIntentOutboxEntry.objects.filter(device=self.device).values_list("pk", flat=True))
        with bound_session(transport.session()):
            response = self.client.post(self.confirm_url, {**data, "digest": plan.digest})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(transport.requests, [])
        return [str(message) for message in get_messages(response.wsgi_request)]

    def changes(self, plan):
        return [step for step in plan.steps if step.action != "status"]

    def assert_no_delivery(self):
        entries = set(NSOIntentOutboxEntry.objects.filter(device=self.device).values_list("pk", flat=True))
        self.assertEqual(entries - self.outbox_before, set())
        self.assertEqual(_pending_intent_keys(), set())

    def grant(self, model, actions, constraints=None):
        permission = ObjectPermission.objects.create(
            name=f"{model._meta.model_name} {actions} {uuid4().hex[:6]}", actions=actions, constraints=constraints
        )
        permission.object_types.add(ObjectType.objects.get_for_model(model))
        permission.users.add(self.user)


class TestIPSync(SyncCase):
    def test_ac7_shape_moves_the_primary_address_in_place(self):
        AdapterConnection.objects.create(interface_ip_auto_create=True)
        me0 = self.interface("me0", mgmt_only=True)
        lo0 = self.interface("lo0.0")
        address = IPAddress.objects.create(address="198.18.0.101/24", assigned_object=me0)
        Device.objects.filter(pk=self.device.pk).update(primary_ip4=address)
        transport = self.transport(
            interfaces=[interface_observation("me0"), interface_observation("lo0.0")],
            ips=[ip_observation("lo0.0", "198.18.0.101/32", prefix_length=32)],
        )
        response, plan = self.preview(transport, {"scope": "ip"})

        overlay = NSOInterfaceIPState.objects.get(interface=lo0, address="198.18.0.101/32")
        self.assertEqual(overlay.status, "conflict")
        (step,) = self.changes(plan)
        self.assertEqual((step.action, step.target), ("modify and move", f"198.18.0.101/24 (IP #{address.pk})"))
        self.assertEqual(step.detail, "198.18.0.101/24 on me0 -> 198.18.0.101/32 on lo0.0")
        self.assertEqual(plan.blockers, [])
        self.assertEqual([note.reason for note in plan.notes], ["the move of lo0.0 198.18.0.101/32 keeps this address"])
        self.assertContains(response, "modify and move")
        self.assertContains(response, plan.digest[:16])
        address.refresh_from_db()
        self.assertEqual((str(address.address), address.assigned_object), ("198.18.0.101/24", me0))

        messages = self.confirm(plan, {"scope": "ip"})

        self.assertIn("Sync from NSO wrote 1 NetBox change(s) and scheduled no device delivery.", messages)
        moved = IPAddress.objects.get(pk=address.pk)
        self.assertEqual((str(moved.address), moved.assigned_object), ("198.18.0.101/32", lo0))
        self.assertEqual(Device.objects.get(pk=self.device.pk).primary_ip4_id, address.pk)
        overlay.refresh_from_db()
        self.assertEqual(overlay.status, "imported")
        self.assert_no_delivery()

    def test_changed_digest_is_stale_and_writes_nothing(self):
        native = IPAddress.objects.create(address="198.18.1.1/24", assigned_object=self.lag)
        transport = self.transport(ips=[ip_observation("lag-60", "198.18.1.1/25", prefix_length=25)])
        _response, plan = self.preview(transport, {"scope": "ip"})
        self.assertEqual([step.action for step in self.changes(plan)], ["modify"])
        IPAddress.objects.filter(pk=native.pk).update(description="edited after the preview", status="reserved")

        messages = self.confirm(plan, {"scope": "ip"})

        self.assertIn(
            "STALE: the plan changed after the preview. Sync from NSO wrote nothing. Preview again.", messages
        )
        native.refresh_from_db()
        self.assertEqual((str(native.address), native.status), ("198.18.1.1/24", "reserved"))
        overlay = NSOInterfaceIPState.objects.get(interface=self.lag, address="198.18.1.1/25")
        self.assertEqual(overlay.status, "imported")

    def test_validation_failure_at_execution_rolls_back_the_plan_and_names_the_row(self):
        primary = IPAddress.objects.create(address="198.18.2.1/24", assigned_object=self.lag)
        other = IPAddress.objects.create(address="198.18.2.20/24", assigned_object=self.lag)
        Device.objects.filter(pk=self.device.pk).update(primary_ip4=primary)
        transport = self.transport(ips=[ip_observation("lag-60", "198.18.2.20/25", prefix_length=25)])
        _response, plan = self.preview(transport, {"scope": "ip"})
        (choice,) = plan.primary_choices
        data = {"scope": "ip", f"primary-{choice[0]}": "clear"}
        _response, plan = self.preview(transport, data)
        self.assertEqual(
            [(step.action, step.target) for step in self.changes(plan)],
            [
                ("modify", f"198.18.2.20/24 (IP #{other.pk})"),
                ("delete", f"198.18.2.1/24 (IP #{primary.pk})"),
                ("modify", self.device.name),
            ],
        )

        def require_primary(sender, instance, **kwargs):
            if instance.pk == other.pk and Device.objects.get(pk=self.device.pk).primary_ip4_id is None:
                raise ValidationError("The device needs a primary IPv4 address.")

        post_clean.connect(require_primary, sender=IPAddress)
        self.addCleanup(post_clean.disconnect, require_primary, sender=IPAddress)
        messages = self.confirm(plan, data)

        self.assertIn(
            f"Sync from NSO wrote nothing. 198.18.2.20/24 (IP #{other.pk}): NetBox validation failed on: the row",
            messages,
        )
        self.assertEqual(Device.objects.get(pk=self.device.pk).primary_ip4_id, primary.pk)
        self.assertTrue(IPAddress.objects.filter(pk=primary.pk).exists())
        other.refresh_from_db()
        self.assertEqual(str(other.address), "198.18.2.20/24")

    def test_deleting_the_primary_address_needs_a_choice_and_takes_a_replacement(self):
        primary = IPAddress.objects.create(address="198.18.3.1/24", assigned_object=self.lag)
        replacement = IPAddress.objects.create(address="198.18.3.2/24", assigned_object=self.lag)
        Device.objects.filter(pk=self.device.pk).update(primary_ip4=primary)
        transport = self.transport(ips=[ip_observation("lag-60", "198.18.3.2/24")])
        response, plan = self.preview(transport, {"scope": "ip"})

        self.assertEqual(self.changes(plan), [])
        self.assertEqual(plan.operations, [])
        (blocker,) = plan.blockers
        self.assertEqual(blocker.reason, "it is the device primary IPv4: choose a replacement or Clear")
        (choice,) = plan.primary_choices
        self.assertEqual([ip.pk for ip in choice[3]], [replacement.pk])
        self.assertContains(response, f"Replace with 198.18.3.2/24 (IP #{replacement.pk})")
        self.assertIn("Sync from NSO had nothing to write.", self.confirm(plan, {"scope": "ip"}))
        self.assertTrue(IPAddress.objects.filter(pk=primary.pk).exists())

        data = {"scope": "ip", f"primary-{choice[0]}": str(replacement.pk)}
        _response, plan = self.preview(transport, data)
        self.assertEqual(plan.blockers, [])
        self.confirm(plan, data)

        self.assertFalse(IPAddress.objects.filter(pk=primary.pk).exists())
        self.assertEqual(Device.objects.get(pk=self.device.pk).primary_ip4_id, replacement.pk)

    def test_device_only_address_is_created_on_its_interface(self):
        lo0 = self.interface("lo0.0")
        transport = self.transport(ips=[ip_observation("lo0.0", "198.18.4.1/32", prefix_length=32)])
        _response, plan = self.preview(transport, {"scope": "ip"})
        self.assertEqual([(step.action, step.detail) for step in self.changes(plan)], [("create", "on lo0.0")])

        self.confirm(plan, {"scope": "ip"})

        created = IPAddress.objects.get(address="198.18.4.1/32")
        self.assertEqual(created.assigned_object, lo0)
        self.assertEqual(NSOInterfaceIPState.objects.get(interface=lo0, address="198.18.4.1/32").status, "imported")

    def test_owned_rows_are_refused_with_release_first(self):
        native = IPAddress.objects.create(address="198.18.5.1/24", assigned_object=self.lag)
        acquire_overlay(NSOInterfaceIPState, interface=self.lag, address="198.18.5.1/24", status="accepted")
        Interface.objects.filter(pk=self.lag.pk).update(description="netbox text")
        acquire_overlay(
            NSOInterfaceState, interface=self.lag, attribute="description", nso_value="device text", status="accepted"
        )
        transport = self.transport(
            interfaces=[interface_observation("lag-60", description="device text")],
            ips=[ip_observation("lag-60", "198.18.5.1/25", prefix_length=25)],
        )
        _response, plan = self.preview(transport, {"scope": ["ip", "interface"]})

        self.assertEqual(self.changes(plan), [])
        self.assertEqual(
            sorted((blocker.scope, blocker.target, blocker.reason) for blocker in plan.blockers),
            [("interface", "lag-60", "owned: Release first"), ("ip", "lag-60 198.18.5.1/25", "owned: Release first")],
        )
        native.refresh_from_db()
        self.assertEqual(str(native.address), "198.18.5.1/24")

    def test_unprojectable_entries_block_absence_based_deletes(self):
        IPAddress.objects.create(address="198.18.10.1/24", assigned_object=self.lag)
        bare = self.interface("ge-0/0/8")
        transport = self.transport(
            interfaces=[interface_observation("lag-60", description="")],
            unprojectable=[{"index": 1, "reason": "invalid device value"}],
        )
        Interface.objects.filter(pk=self.lag.pk).update(description="")
        _response, plan = self.preview(transport, {"scope": ["ip", "interface"]})

        self.assertEqual([step for step in self.changes(plan) if step.action == "delete"], [])
        reason = "the device observation has entries that Sync cannot compare, so absence is not proven"
        self.assertEqual(
            sorted((blocker.scope, blocker.target) for blocker in plan.blockers if blocker.reason == reason),
            [("interface", "ge-0/0/8"), ("ip", "lag-60 198.18.10.1/24")],
        )
        self.confirm(plan, {"scope": ["ip", "interface"]})
        self.assertTrue(Interface.objects.filter(pk=bare.pk).exists())
        self.assertTrue(IPAddress.objects.filter(address="198.18.10.1/24").exists())

    def test_ip_delete_that_changes_other_objects_or_is_protected_is_blocked(self):
        from ipam.models import ASN, RIR
        from netbox_routing.models import BGPPeer, BGPRouter, BGPScope

        inside = IPAddress.objects.create(address="198.18.11.1/24", assigned_object=self.lag)
        protected = IPAddress.objects.create(address="198.18.11.2/24", assigned_object=self.lag)
        other_device, _other_management = _make(f"other{uuid4().hex[:8]}")
        other_interface = Interface.objects.get(device=other_device, name="lag-60")
        outside = IPAddress.objects.create(address="198.18.12.1/24", assigned_object=other_interface, nat_inside=inside)
        rir = RIR.objects.create(name=f"Sync RIR {uuid4().hex[:6]}", slug=f"sync-rir-{uuid4().hex[:6]}")
        router = BGPRouter.objects.create(
            assigned_object=other_device, asn=ASN.objects.create(asn=64540, rir=rir), name="64540"
        )
        BGPPeer.objects.create(
            scope=BGPScope.objects.create(router=router),
            peer=protected,
            remote_as=ASN.objects.create(asn=64541, rir=rir),
            enabled=True,
        )
        _response, plan = self.preview(self.transport(), {"scope": "ip"})

        self.assertEqual(self.changes(plan), [])
        self.assertEqual(
            sorted((blocker.target, blocker.reason) for blocker in plan.blockers),
            [
                ("lag-60 198.18.11.1/24", "it has IP addresses"),
                ("lag-60 198.18.11.2/24", "NetBox protects it with dependent objects"),
            ],
        )
        self.assertIn("Sync from NSO had nothing to write.", self.confirm(plan, {"scope": "ip"}))
        outside.refresh_from_db()
        self.assertEqual(outside.nat_inside_id, inside.pk)
        self.assertTrue(IPAddress.objects.filter(pk=protected.pk).exists())

    def test_concurrent_edit_outside_the_digest_is_not_overwritten(self):
        native = IPAddress.objects.create(address="198.18.13.1/24", assigned_object=self.lag)
        transport = self.transport(ips=[ip_observation("lag-60", "198.18.13.1/25", prefix_length=25)])
        _response, plan = self.preview(transport, {"scope": "ip"})
        edits = []

        def edit_once(sender, instance, **kwargs):
            if instance.pk == native.pk and not edits:
                edits.append(instance.pk)
                IPAddress.objects.filter(pk=native.pk).update(dns_name="concurrent.example")

        post_clean.connect(edit_once, sender=IPAddress)
        self.addCleanup(post_clean.disconnect, edit_once, sender=IPAddress)
        self.confirm(plan, {"scope": "ip"})

        native.refresh_from_db()
        self.assertEqual(edits, [native.pk])
        self.assertEqual(native.dns_name, "concurrent.example")

    def test_protected_reference_created_before_the_execution_plan_is_stale(self):
        from ipam.models import ASN, RIR
        from netbox_routing.models import BGPPeer, BGPRouter, BGPScope

        doomed = IPAddress.objects.create(address="198.18.16.1/24", assigned_object=self.lag)
        Interface.objects.filter(pk=self.lag.pk).update(description="netbox text")
        transport = self.transport(interfaces=[interface_observation("lag-60", description="device text")])
        data = {"scope": ["ip", "interface"]}
        _response, plan = self.preview(transport, data)
        self.assertEqual(
            sorted(step.action for step in self.changes(plan)), ["delete", "modify"], [vars(b) for b in plan.blockers]
        )
        other_device, _other_management = _make(f"bgp{uuid4().hex[:8]}")
        rir = RIR.objects.create(name=f"Late RIR {uuid4().hex[:6]}", slug=f"late-rir-{uuid4().hex[:6]}")
        router = BGPRouter.objects.create(
            assigned_object=other_device, asn=ASN.objects.create(asn=64550, rir=rir), name="64550"
        )
        references = []

        def reference_once(sender, instance, **kwargs):
            if instance.pk == self.lag.pk and not references:
                references.append(
                    BGPPeer.objects.create(
                        scope=BGPScope.objects.create(router=router),
                        peer=doomed,
                        remote_as=ASN.objects.create(asn=64551, rir=rir),
                        enabled=True,
                    )
                )

        post_clean.connect(reference_once, sender=Interface)
        self.addCleanup(post_clean.disconnect, reference_once, sender=Interface)
        messages = self.confirm(plan, data)

        self.assertEqual(len(references), 1)
        self.assertIn(
            "STALE: the plan changed after the preview. Sync from NSO wrote nothing. Preview again.", messages
        )
        self.assertTrue(IPAddress.objects.filter(pk=doomed.pk).exists())
        self.lag.refresh_from_db()
        self.assertEqual(self.lag.description, "netbox text")

    def test_same_name_vrf_replacement_is_stale(self):
        from ipam.models import VRF

        self.interface("lo0.0")
        first = VRF.objects.create(name="blue")
        transport = self.transport(ips=[ip_observation("lo0.0", "198.18.17.1/32", vrf="blue", prefix_length=32)])
        _response, plan = self.preview(transport, {"scope": "ip"})
        self.assertEqual([step.action for step in self.changes(plan)], ["create"])
        VRF.objects.filter(pk=first.pk).update(name="blue-retired")
        VRF.objects.create(name="blue")

        messages = self.confirm(plan, {"scope": "ip"})

        self.assertIn(
            "STALE: the plan changed after the preview. Sync from NSO wrote nothing. Preview again.", messages
        )
        self.assertFalse(IPAddress.objects.filter(address="198.18.17.1/32").exists())


class TestSyncPermissions(SyncCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username=f"restricted{uuid4().hex[:8]}")
        self.client.force_login(self.user)
        self.grant(Device, ["view"])
        self.grant(Interface, ["view"])
        self.grant(NSODeviceManagement, ["view", "change"])

    def test_restricted_object_is_refused_and_hidden_objects_do_not_leak(self):
        visible = IPAddress.objects.create(address="198.18.6.5/24", assigned_object=self.lag)
        hidden = IPAddress.objects.create(address="198.18.6.9/24", assigned_object=self.lag)
        elsewhere = IPAddress.objects.create(address="198.18.6.200/24")
        self.grant(IPAddress, ["view"], {"pk": visible.pk})
        self.grant(IPAddress, ["change"], {"pk": elsewhere.pk})
        transport = self.transport(
            ips=[
                ip_observation("lag-60", "198.18.6.5/25", prefix_length=25),
                ip_observation("lag-60", "198.18.6.9/32", prefix_length=32),
            ]
        )
        response, plan = self.preview(transport, {"scope": "ip"})

        self.assertEqual(self.changes(plan), [])
        self.assertEqual(
            sorted((blocker.target, blocker.reason) for blocker in plan.blockers),
            [
                ("lag-60 198.18.6.5/25", "permission denied: you cannot change this IP address"),
                ("lag-60 198.18.6.9/32", "NetBox object is not visible to you"),
            ],
        )
        self.assertNotContains(response, "198.18.6.9/24")
        self.assertNotContains(response, f"IP #{hidden.pk}")
        self.assertNotContains(response, "198.18.6.200")
        visible.refresh_from_db()
        self.assertEqual(str(visible.address), "198.18.6.5/24")

    def test_move_from_a_hidden_interface_does_not_name_it(self):
        me0 = self.interface("me0", mgmt_only=True)
        lo0 = self.interface("lo0.0")
        IPAddress.objects.create(address="198.18.14.1/24", assigned_object=me0)
        ObjectPermission.objects.filter(users=self.user, object_types__model="interface").delete()
        self.grant(Interface, ["view"], {"pk__in": [lo0.pk, self.lag.pk]})
        self.grant(IPAddress, ["view", "change"])
        transport = self.transport(ips=[ip_observation("lo0.0", "198.18.14.1/32", prefix_length=32)])
        response, plan = self.preview(transport, {"scope": "ip"})

        self.assertEqual(self.changes(plan), [])
        self.assertEqual(
            [(blocker.target, blocker.reason) for blocker in plan.blockers],
            [("lo0.0 198.18.14.1/32", "NetBox object is not visible to you")],
        )
        self.assertNotContains(response, "me0")
        differences = self.client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_differences", kwargs={"pk": self.device.pk})
        )
        self.assertNotContains(differences, "me0")

    def test_validation_failure_never_shows_a_hidden_range(self):
        from ipam.models import IPRange
        from netaddr import IPNetwork

        self.interface("lo0.0")
        IPRange.objects.create(
            start_address=IPNetwork("198.18.18.10/24"), end_address=IPNetwork("198.18.18.90/24"), mark_populated=True
        )
        self.grant(IPAddress, ["view", "add"])
        transport = self.transport(ips=[ip_observation("lo0.0", "198.18.18.50/32", prefix_length=32)])
        response, plan = self.preview(transport, {"scope": "ip"})

        self.assertEqual(self.changes(plan), [])
        self.assertEqual(
            [(blocker.target, blocker.reason) for blocker in plan.blockers],
            [("lo0.0 198.18.18.50/32", "NetBox validation failed on: address")],
        )
        self.assertNotContains(response, "198.18.18.10")
        self.assertNotContains(response, "198.18.18.90")

    def test_user_without_management_change_permission_is_refused(self):
        ObjectPermission.objects.filter(users=self.user, object_types__model="nsodevicemanagement").delete()
        self.grant(NSODeviceManagement, ["view"])
        response = self.client.post(self.preview_url, {"scope": "ip"})
        self.assertEqual(response.status_code, 403)


class TestInterfaceSync(SyncCase):
    def test_description_and_enabled_follow_the_device(self):
        Interface.objects.filter(pk=self.lag.pk).update(description="netbox text", enabled=True)
        transport = self.transport(
            interfaces=[interface_observation("lag-60", description="device text", enabled=False)]
        )
        for attribute in transport.documents["interfaces-doc"]["interfaces"][0]["attrs"].values():
            attribute["status"] = "changed"
        _response, plan = self.preview(transport, {"scope": "interface"})
        self.assertEqual(
            sorted((step.target, step.detail) for step in plan.steps if step.action == "status"),
            [("lag-60 description", "changed -> imported"), ("lag-60 enabled", "changed -> imported")],
        )
        (step,) = self.changes(plan)
        self.assertEqual(
            (step.action, step.target, step.detail),
            ("modify", "lag-60", 'description: "netbox text" -> "device text"; enabled: True -> False'),
        )

        self.confirm(plan, {"scope": "interface"})

        self.lag.refresh_from_db()
        self.assertEqual((self.lag.description, self.lag.enabled), ("device text", False))
        self.assertEqual(
            set(NSOInterfaceState.objects.filter(interface=self.lag).values_list("status", flat=True)), {"imported"}
        )
        self.assert_no_delivery()

    def test_sync_schedules_no_delivery_for_owned_content_it_changes(self):
        Interface.objects.filter(pk=self.lag.pk).update(description="netbox text", enabled=True)
        acquire_overlay(NSOInterfaceState, interface=self.lag, attribute="enabled", nso_value="True", status="in_sync")
        transport = self.transport(
            interfaces=[interface_observation("lag-60", description="device text", enabled=True)]
        )
        _response, plan = self.preview(transport, {"scope": "interface"})
        self.assertEqual([step.action for step in self.changes(plan)], ["modify"])

        self.confirm(plan, {"scope": "interface"})

        self.lag.refresh_from_db()
        self.assertEqual(self.lag.description, "device text")
        self.assert_no_delivery()

    def test_netbox_only_interface_with_an_address_is_blocked(self):
        stale = self.interface("ge-0/0/9")
        IPAddress.objects.create(address="198.18.7.1/24", assigned_object=stale)
        bare = self.interface("ge-0/0/8")
        transport = self.transport(interfaces=[interface_observation("lag-60", description="")])
        Interface.objects.filter(pk=self.lag.pk).update(description="")
        _response, plan = self.preview(transport, {"scope": "interface"})

        self.assertEqual([(step.action, step.target) for step in self.changes(plan)], [("delete", "ge-0/0/8")])
        self.assertIn(
            ("interface", "ge-0/0/9", "it has IP addresses"),
            [(blocker.scope, blocker.target, blocker.reason) for blocker in plan.blockers],
        )

        self.confirm(plan, {"scope": "interface"})

        self.assertFalse(Interface.objects.filter(pk=bare.pk).exists())
        self.assertTrue(Interface.objects.filter(pk=stale.pk).exists())

    def test_replaced_interface_with_the_same_fields_is_stale(self):
        first = self.interface("ge-0/0/8")
        transport = self.transport(interfaces=[interface_observation("lag-60", description="")])
        Interface.objects.filter(pk=self.lag.pk).update(description="")
        self.preview(transport, {"scope": "interface"})
        panel = self.client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_differences", kwargs={"pk": self.device.pk})
        )
        token = next(row["token"] for row in panel.context["page_obj"] if row["identity"] == "ge-0/0/8")
        _response, plan = self.preview(transport, {"row": token})
        self.assertEqual([(step.action, step.target) for step in self.changes(plan)], [("delete", "ge-0/0/8")])
        Interface.objects.filter(pk=first.pk).update(name="ge-0/0/8-old")
        second = self.interface("ge-0/0/8")

        messages = self.confirm(plan, {"row": token})

        self.assertIn(
            "STALE: the plan changed after the preview. Sync from NSO wrote nothing. Preview again.", messages
        )
        self.assertTrue(Interface.objects.filter(pk=second.pk).exists())

    def test_device_only_interface_is_blocked_without_a_type(self):
        transport = self.transport(interfaces=[interface_observation("lag-60"), interface_observation("xe-0/0/1")])
        _response, plan = self.preview(transport, {"scope": "interface"})
        self.assertIn(
            ("interface", "xe-0/0/1", "NetBox needs an interface type, which the device does not report"),
            [(blocker.scope, blocker.target, blocker.reason) for blocker in plan.blockers],
        )


class TestSyncReadAndScopes(SyncCase):
    def test_failed_fresh_read_fails_closed(self):
        native = IPAddress.objects.create(address="198.18.8.1/24", assigned_object=self.lag)
        unavailable = self.transport(ips=[ip_observation("lag-60", "198.18.8.1/25", prefix_length=25)])
        unavailable.documents["interface-ips"]["read_state"] = _rs(
            outcome="unavailable", result="kept", succeeded=False
        )
        failing = self.transport(transport_class=_FailingIPTransport)
        for transport, error in (
            (unavailable, "The fresh read did not publish interface_ip (skipped_unavailable). Nothing was planned."),
            (failing, "The fresh read failed:"),
        ):
            with self.subTest(error=error):
                response, plan = self.preview(transport, {"scope": "ip"})
                self.assertIsNone(plan)
                self.assertContains(response, error)
                self.assertContains(response, "disabled")
        native.refresh_from_db()
        self.assertEqual(str(native.address), "198.18.8.1/24")

    def test_unsupported_scope_is_reported(self):
        transport = self.transport()
        _response, plan = self.preview(transport, {"scope": "bgp"})
        self.assertEqual(
            [(blocker.scope, blocker.reason) for blocker in plan.blockers], [("bgp", "sync not supported yet (#1790)")]
        )
        self.assertEqual(plan.operations, [])

    def test_malformed_selection_is_refused(self):
        response = self.client.post(self.preview_url, {"row": "ip:<script>"})
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, "<script>", status_code=400)

    def test_differences_panel_offers_row_and_scope_selection(self):
        IPAddress.objects.create(address="198.18.9.1/24", assigned_object=self.lag)
        transport = self.transport(ips=[ip_observation("lag-60", "198.18.9.1/25", prefix_length=25)])
        self.preview(transport, {"scope": "ip"})
        response = self.client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_differences", kwargs={"pk": self.device.pk})
        )
        self.assertContains(response, 'name="scope" value="ip"')
        self.assertContains(response, 'name="row" value="ip:')
        self.assertContains(response, "> Sync from NSO")
        self.assertContains(response, self.preview_url)
