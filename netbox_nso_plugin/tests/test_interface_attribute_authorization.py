# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Interface attribute ownership through refresh, operator requests, and native guards."""

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.urls import reverse

from netbox_nso_plugin import native_authorization as native
from netbox_nso_plugin.models import NSODeviceManagement, NSOInterfaceState, NSOOwnershipManifest
from netbox_nso_plugin.summary import interface_row_state

from ._authority_case import AdapterCase, FakeAdapter, operator
from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay
from .test_native_authorization import _plan_save, _system_scope


class InterfaceAttributeCases:
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed(f"attribute-{cls.attribute}", 17201)
        NSODeviceManagement.objects.filter(pk=cls.management.pk).update(manage_description=True, manage_enabled=True)
        cls.interface = Interface.objects.create(
            device=cls.device, name="ge-0/0/1", type="1000base-t", description="intended", enabled=True
        )
        cls.admin = get_user_model().objects.create_superuser(username=f"attribute-{cls.attribute}-admin")

    def setUp(self):
        super().setUp()
        FakeAdapter.documents = {
            "/interfaces-doc": {
                "interfaces": [
                    {
                        "name": self.interface.name,
                        "attrs": {
                            "description": {"nso_value": "observed", "status": "imported"},
                            "enabled": {"nso_value": "false", "status": "imported"},
                        },
                    }
                ]
            },
            "/state": {},
            "/svi": {"interfaces": []},
            "/subinterface": {"interfaces": []},
            "/interface-ips": {"interfaces": []},
            "/interface-mtu": {"interfaces": []},
            "/vlan-database": {"vlans": []},
            "/switchport": {"interfaces": []},
        }

    @property
    def initial(self):
        return "intended" if self.attribute == "description" else True

    @property
    def observed(self):
        return "observed" if self.attribute == "description" else False

    def own(self):
        with without_commit_drain():
            return acquire_overlay(
                NSOInterfaceState,
                interface=self.interface,
                attribute=self.attribute,
                status="accepted",
                nso_value=str(self.observed).lower(),
            )

    def manifest_only(self):
        return NSOOwnershipManifest.objects.create(
            device_id=self.device.pk,
            scope="interface",
            native_model_label="dcim.interface",
            native_id=self.interface.pk,
            native_key={"device_id": self.device.pk, "name": self.interface.name},
            state_model_label="netbox_nso_plugin.nsointerfacestate",
            state_key={"attribute": self.attribute},
            grant_kind="accept",
        )

    def assertNative(self, value):
        self.interface.refresh_from_db()
        self.assertEqual(getattr(self.interface, self.attribute), value)

    def refresh_response(self, user):
        self.client.force_login(user)
        url = reverse("plugins:netbox_nso_plugin:device_nso_category", args=[self.device.pk, "interface"])
        response = self.client.get(url, {"refresh": "1", "format": "json"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload["adapter_error"])
        return payload

    def refresh(self, user):
        payload = self.refresh_response(user)
        state = NSOInterfaceState.objects.get(interface=self.interface, attribute=self.attribute)
        self.assertIsNotNone(state.last_sync_at)
        self.assertEqual(state.nso_value, str(self.observed).lower())
        return payload

    def accept(self, user, state):
        self.client.force_login(user)
        return self.client.post(reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept_device", args=[state.pk]))

    def test_owned_refresh_preserves_native_and_shows_gap(self):
        state = self.own()
        payload = self.refresh(self.admin)
        self.assertNative(self.initial)
        state.refresh_from_db()
        self.assertEqual(state.nso_value, str(self.observed).lower())
        self.assertNotEqual(state.status, "in_sync")
        self.assertNotEqual(interface_row_state(state, self.interface)[0], "in_sync")
        self.assertNotEqual(payload["rows"][0][self.attribute]["state"], "in_sync")

    def test_permitted_accept_materializes_the_device_value(self):
        state = self.own()
        response = self.accept(operator((("change",), Interface, {"pk": self.interface.pk})), state)
        self.assertEqual(response.status_code, 302)
        self.assertNative(self.observed)

    def test_accept_without_native_permission_is_refused_whole(self):
        state = self.own()
        with self.assert_refused_whole():
            response = self.accept(operator(), state)
        self.assertEqual(response.status_code, 403)
        self.assertNative(self.initial)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    def test_accept_with_excluding_constraint_is_refused_whole(self):
        state = self.own()
        with self.assert_refused_whole():
            response = self.accept(operator((("change",), Interface, {"pk": -1})), state)
        self.assertEqual(response.status_code, 403)
        self.assertNative(self.initial)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    @property
    def other_attribute(self):
        return "enabled" if self.attribute == "description" else "description"

    def observe(self, attribute, value):
        candidate = Interface.objects.get(pk=self.interface.pk)
        setattr(candidate, attribute, value)
        with _system_scope() as run:
            run(lambda: _plan_save(candidate, (attribute,), origin=native.Observed("interface")))

    def assertOtherNative(self, value):
        self.interface.refresh_from_db()
        self.assertEqual(getattr(self.interface, self.other_attribute), value)

    def test_observed_write_succeeds_for_unowned_attribute(self):
        self.observe(self.attribute, self.observed)
        self.assertNative(self.observed)

    def test_owned_attribute_allows_other_observation_and_refuses_its_own(self):
        self.own()
        other_value = False if self.other_attribute == "enabled" else "observed"
        self.observe(self.other_attribute, other_value)
        self.assertOtherNative(other_value)
        with self.assertRaises(native.OwnedIntentRefused):
            self.observe(self.attribute, self.observed)
        self.assertNative(self.initial)
        self.assertOtherNative(other_value)

    def test_manifest_only_ownership_allows_the_other_attribute(self):
        self.manifest_only()
        other_value = False if self.other_attribute == "enabled" else "observed"
        self.observe(self.other_attribute, other_value)
        self.assertOtherNative(other_value)
        self.assertNative(self.initial)

    def test_observed_save_by_name_is_refused_for_owned_attribute(self):
        self.own()
        candidate = Interface.objects.get(pk=self.interface.pk)
        setattr(candidate, self.attribute, self.observed)
        with self.assertRaises(native.OwnedIntentRefused), _system_scope() as run:
            run(lambda: _plan_save(candidate, (self.attribute,), origin=native.Observed("interface")))
        self.assertNative(self.initial)

    def test_observed_save_by_attname_is_refused_for_owned_attribute(self):
        self.own()
        attname = Interface._meta.get_field(self.attribute).attname
        candidate = Interface.objects.get(pk=self.interface.pk)
        setattr(candidate, attname, self.observed)

        def write():
            with native.active_native_writer().save(
                candidate, update_fields=(attname,), created=False, origin=native.Observed("switchport")
            ):
                candidate.save(update_fields=(attname,))

        with self.assertRaises(native.OwnedIntentRefused), _system_scope() as run:
            run(write)
        self.assertNative(self.initial)

    def test_observed_queryset_update_is_refused_for_owned_attribute(self):
        self.own()
        values = {Interface._meta.get_field(self.attribute).attname: self.observed}

        def write():
            with native.active_native_writer().set_update(
                Interface, [self.interface.pk], values, origin=native.Observed("interface")
            ):
                Interface.objects.filter(pk=self.interface.pk).update(**values)

        with self.assertRaises(native.OwnedIntentRefused), _system_scope() as run:
            run(write)
        self.assertNative(self.initial)

    def test_observed_write_is_refused_for_manifest_only_ownership(self):
        self.manifest_only()
        candidate = Interface.objects.get(pk=self.interface.pk)
        setattr(candidate, self.attribute, self.observed)
        with self.assertRaises(native.OwnedIntentRefused), _system_scope() as run:
            run(lambda: _plan_save(candidate, (self.attribute,), origin=native.Observed("interface")))
        self.assertNative(self.initial)

    def test_bulk_update_cannot_bypass_the_writer(self):
        self.own()
        candidate = Interface.objects.get(pk=self.interface.pk)
        setattr(candidate, self.attribute, self.observed)
        with self.assertRaises(native.NativeWriteRefused), _system_scope() as run:
            run(lambda: Interface.objects.bulk_update([candidate], [self.attribute]))
        self.assertNative(self.initial)


class TestDescriptionAuthorization(InterfaceAttributeCases, AdapterCase):
    attribute = "description"


class TestEnabledAuthorization(InterfaceAttributeCases, AdapterCase):
    attribute = "enabled"
