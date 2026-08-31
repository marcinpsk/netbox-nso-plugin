# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""plugin SVI/IRB reconciler — materialise virtual interface + VLAN link + overlay."""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOSVIState

from .mixins import IntentPushResetMixin


def _make_device(tag="m35"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"SMfg{tag}", slug=f"smfg{tag}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"SDev{tag}", slug=f"sdev{tag}")
    role, _ = DeviceRole.objects.get_or_create(name=f"SRole{tag}", slug=f"srole{tag}")
    site, _ = Site.objects.get_or_create(name=f"SSite{tag}", slug=f"ssite{tag}")
    return Device.objects.create(name=f"svi-sw-{tag}", device_type=dt, role=role, site=site)


class TestSviReconciler(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device()
        cls.instance = NSOInstance.objects.create(name="nso-dev", adapter_instance_id="nso-dev")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="svi-sw-m35"
        )

    def test_no_mgmt_returns_empty(self):
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        orphan = _make_device("orphan")
        assert reconcile_svi(orphan, {"interfaces": [{"interface_name": "Vlan100", "vlan_id": 100}]}) == []

    def test_creates_virtual_interface_and_state(self):
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        rows = reconcile_svi(
            self.device, {"interfaces": [{"interface_name": "Vlan100", "vlan_id": 100, "type": "svi", "vrf": "MGMT"}]}
        )
        self.assertEqual(len(rows), 1)
        iface = Interface.objects.get(device=self.device, name="Vlan100")
        self.assertEqual(iface.type, "virtual")
        self.assertTrue(NSOSVIState.objects.filter(management=self.management, interface=iface).exists())
        self.assertEqual(rows[0].status, "imported")

    def test_direct_reconcile_does_not_advance_intent_revision(self):
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        revision, _ = NSOIntentRevision.objects.get_or_create(device=self.device, scope="svi")
        before = revision.revision

        reconcile_svi(
            self.device,
            {"interfaces": [{"interface_name": "Vlan1623", "vlan_id": 1623, "type": "svi"}]},
        )

        revision.refresh_from_db()
        self.assertEqual(revision.revision, before)

    def test_vlan_linked_when_present(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.svi_reconciler import reconcile_svi
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        group = _device_vlan_group(self.device)
        VLAN.objects.create(group=group, vid=150, name="DATA")
        rows = reconcile_svi(
            self.device, {"interfaces": [{"interface_name": "Vlan150", "vlan_id": 150, "type": "svi"}]}
        )
        self.assertEqual(rows[0].vlan.vid, 150)

    def test_existing_interface_is_reused_not_duplicated(self):
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        Interface.objects.create(device=self.device, name="Vlan200", type="virtual")
        reconcile_svi(self.device, {"interfaces": [{"interface_name": "Vlan200", "vlan_id": 200, "type": "svi"}]})
        self.assertEqual(Interface.objects.filter(device=self.device, name="Vlan200").count(), 1)

    def test_a_concurrently_created_interface_is_reused(self):
        from django.db import connection

        from netbox_nso_plugin.intent_state import reconcile_transaction
        from netbox_nso_plugin.svi_reconciler import _reconcile_svi, svi_reconcile_plan

        payload = {"interfaces": [{"interface_name": "Vlan201", "vlan_id": 201, "type": "svi"}]}
        plan = svi_reconcile_plan(self.device, payload)
        inserted = None

        def insert_after_interface_read(execute, sql, params, many, context):
            nonlocal inserted
            result = execute(sql, params, many, context)
            if inserted is None and sql.lstrip().upper().startswith("SELECT") and Interface._meta.db_table in sql:
                inserted = Interface.objects.create(device=self.device, name="Vlan201", type="virtual")
            return result

        with reconcile_transaction(plan), connection.execute_wrapper(insert_after_interface_read):
            rows = _reconcile_svi(self.device, payload)

        self.assertEqual(rows[0].interface_id, inserted.pk)
        self.assertEqual(Interface.objects.filter(device=self.device, name="Vlan201").count(), 1)

    def test_irb_type_preserved(self):
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        rows = reconcile_svi(
            self.device, {"interfaces": [{"interface_name": "irb.100", "vlan_id": 100, "type": "irb"}]}
        )
        self.assertEqual(rows[0].svi_type, "irb")
        self.assertTrue(Interface.objects.filter(device=self.device, name="irb.100").exists())

    def test_stale_state_pruned(self):
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        reconcile_svi(self.device, {"interfaces": [{"interface_name": "Vlan300", "vlan_id": 300, "type": "svi"}]})
        # Next refresh no longer reports Vlan300 → its overlay row is pruned.
        reconcile_svi(self.device, {"interfaces": [{"interface_name": "Vlan301", "vlan_id": 301, "type": "svi"}]})
        names = set(NSOSVIState.objects.filter(management=self.management).values_list("interface__name", flat=True))
        self.assertEqual(names, {"Vlan301"})


class TestSviWritePath(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("wp")
        cls.instance = NSOInstance.objects.create(name="nso-wp", adapter_instance_id="nso-wp")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="svi-sw-wp", adapter_device_id=42
        )

    def _state(self, name="Vlan100", vid=100, status="imported"):
        from uuid import uuid4

        from dcim.models import Interface
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOSVIState
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        iface = Interface.objects.create(device=self.device, name=name, type="virtual")
        vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=vid, name=f"V{vid}")
        return NSOSVIState.objects.create(
            management=self.management,
            interface=iface,
            vlan=vlan,
            svi_type="svi",
            vrf="MGMT",
            status=status,
            apply_attempt_id=uuid4() if status == "deploying" else None,
        )

    def test_reconcile_preserves_owned_status(self):
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        self._state(name="Vlan100", vid=100, status="accepted")
        reconcile_svi(self.device, {"interfaces": [{"interface_name": "Vlan100", "vlan_id": 100, "type": "svi"}]})
        from netbox_nso_plugin.models import NSOSVIState

        self.assertEqual(NSOSVIState.objects.get(interface__name="Vlan100").status, "accepted")

    def test_reconcile_preserves_owned_values_until_the_device_matches(self):
        """A refresh must not replace pending NetBox intent with the old device value."""
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        state = self._state(name="Vlan100", vid=100, status="accepted")
        state.vrf = "CUSTOMER"
        state.save(update_fields=["vrf"])

        reconcile_svi(
            self.device,
            {"interfaces": [{"interface_name": "Vlan100", "vlan_id": 100, "type": "svi", "vrf": "MGMT"}]},
        )

        state.refresh_from_db()
        self.assertEqual(state.vrf, "CUSTOMER")
        self.assertEqual(state.status, "accepted")

        reconcile_svi(
            self.device,
            {"interfaces": [{"interface_name": "Vlan100", "vlan_id": 100, "type": "svi", "vrf": "CUSTOMER"}]},
        )
        state.refresh_from_db()
        self.assertEqual(state.vrf, "CUSTOMER")
        self.assertEqual(state.status, "in_sync")

    def test_deploying_waits_for_matching_device_values_before_settling(self):
        """Reappearance alone must not confirm an Apply while the device still has old values."""
        from netbox_nso_plugin.models import NSOSVIState
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        self._state(name="Vlan100", vid=100, status="deploying")
        reconcile_svi(self.device, {"interfaces": [{"interface_name": "Vlan100", "vlan_id": 100, "type": "svi"}]})
        self.assertEqual(NSOSVIState.objects.get(interface__name="Vlan100").status, "deploying")

        reconcile_svi(
            self.device,
            {"interfaces": [{"interface_name": "Vlan100", "vlan_id": 100, "type": "svi", "vrf": "MGMT"}]},
        )
        self.assertEqual(NSOSVIState.objects.get(interface__name="Vlan100").status, "in_sync")

    def test_owned_state_survives_when_interface_drops_from_payload(self):
        """An owned SVI overlay must NOT be hard-deleted when the device stops reporting it.

        NSOSVIState is in ``_APPLY_DEPLOYING_SCOPES``; a bulk delete of stale rows destroys
        ownership. A confirmed row that vanishes surfaces as ``changed``. That content
        change advances the scope revision and re-pends another deploying row.
        """
        from netbox_nso_plugin.models import NSOSVIState
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        deploying = self._state(name="Vlan100", vid=100, status="deploying")
        confirmed = self._state(name="Vlan200", vid=200, status="in_sync")
        reconcile_svi(self.device, {"interfaces": []})  # device stops reporting all SVIs
        assert NSOSVIState.objects.filter(pk=deploying.pk).exists(), "deploying (apply-in-flight) overlay deleted"
        assert NSOSVIState.objects.filter(pk=confirmed.pk).exists(), "in_sync overlay deleted"
        deploying.refresh_from_db()
        assert deploying.status == "accepted"
        assert deploying.apply_attempt_id is None
        assert NSOSVIState.objects.get(pk=confirmed.pk).status == "changed"

    def test_push_builds_owned_snapshot(self):
        from unittest.mock import patch

        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.signals import reset_intent_push_state

        self._state(name="Vlan100", vid=100, status="accepted")
        self._state(name="Vlan200", vid=200, status="imported")  # not owned → excluded
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_svi_intent") as mock_put:
            deliver("svi", self.device.pk, 42)
        mock_put.assert_called_once()
        ifaces = mock_put.call_args[0][1]
        assert [i["interface_name"] for i in ifaces] == ["Vlan100"]
        assert ifaces[0]["vlan_id"] == 100 and ifaces[0]["vrf"] == "MGMT"

    def test_accept_marks_owned(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model

        state = self._state(name="Vlan300", vid=300, status="conflict")
        User = get_user_model()
        admin = User.objects.create_superuser(username="svi-admin", password="pw", email="s@x.y")  # noqa: S106
        self.client.force_login(admin)
        with patch("netbox_nso_plugin.adapter_client.put_svi_intent"):
            resp = self.client.post(f"/plugins/nso/svi/state/{state.pk}/accept/")
        assert resp.status_code == 302
        state.refresh_from_db()
        assert state.status == "accepted" and state.accepted_at is not None
