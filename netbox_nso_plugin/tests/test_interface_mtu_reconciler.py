# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Phase 2b: plugin interface-MTU reconciler — read-only mirror overlay."""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase
from django.utils import timezone

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceMtuState

from .mixins import IntentPushResetMixin


def _make_device(tag="mtu"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"MtuMfg{tag}", slug=f"mtumfg{tag}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"MtuDev{tag}", slug=f"mtudev{tag}")
    role, _ = DeviceRole.objects.get_or_create(name=f"MtuRole{tag}", slug=f"mturole{tag}")
    site, _ = Site.objects.get_or_create(name=f"MtuSite{tag}", slug=f"mtusite{tag}")
    return Device.objects.create(name=f"rtr-{tag}", device_type=dt, role=role, site=site)


class TestInterfaceMtuReconciler(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device()
        cls.instance = NSOInstance.objects.create(name="nso-mtu", adapter_instance_id="nso-mtu")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="rtr-mtu"
        )
        cls.po1 = Interface.objects.create(device=cls.device, name="Port-channel1", type="lag")
        cls.lag99 = Interface.objects.create(device=cls.device, name="LAG99:99", type="virtual")

    def test_no_mgmt_returns_empty(self):
        from netbox_nso_plugin.interface_mtu_reconciler import reconcile_interface_mtu

        orphan = _make_device("orphan")
        assert reconcile_interface_mtu(orphan, {"interfaces": [{"interface_name": "X", "mtu": 9000}]}) == []

    def test_mirrors_l2_and_ip_mtu(self):
        from netbox_nso_plugin.interface_mtu_reconciler import reconcile_interface_mtu

        rows = reconcile_interface_mtu(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "Port-channel1",
                        "mtu": 9216,
                        "ip_mtu": None,
                        "mpls_mtu": None,
                        "bound_port": "",
                    },
                    {
                        "interface_name": "LAG99:99",
                        "mtu": None,
                        "ip_mtu": 9170,
                        "mpls_mtu": None,
                        "bound_port": "lag-99",
                    },
                ]
            },
        )
        self.assertEqual(len(rows), 2)
        po = NSOInterfaceMtuState.objects.get(interface=self.po1)
        self.assertEqual(po.l2_mtu, 9216)
        self.assertIsNone(po.ip_mtu)
        self.assertEqual(po.status, "imported")
        lag = NSOInterfaceMtuState.objects.get(interface=self.lag99)
        self.assertEqual(lag.ip_mtu, 9170)
        self.assertEqual(lag.bound_port, "lag-99")

    def test_interface_absent_in_netbox_is_skipped(self):
        from netbox_nso_plugin.interface_mtu_reconciler import reconcile_interface_mtu

        rows = reconcile_interface_mtu(
            self.device,
            {"interfaces": [{"interface_name": "TenGig9/9/9", "mtu": 9216}]},
        )
        self.assertEqual(rows, [])
        self.assertEqual(NSOInterfaceMtuState.objects.count(), 0)

    def test_stale_state_pruned(self):
        from netbox_nso_plugin.interface_mtu_reconciler import reconcile_interface_mtu

        reconcile_interface_mtu(self.device, {"interfaces": [{"interface_name": "Port-channel1", "mtu": 9216}]})
        reconcile_interface_mtu(self.device, {"interfaces": [{"interface_name": "LAG99:99", "ip_mtu": 9170}]})
        names = set(
            NSOInterfaceMtuState.objects.filter(management=self.management).values_list("interface__name", flat=True)
        )
        self.assertEqual(names, {"LAG99:99"})

    def test_value_update_on_resync(self):
        from netbox_nso_plugin.interface_mtu_reconciler import reconcile_interface_mtu

        reconcile_interface_mtu(self.device, {"interfaces": [{"interface_name": "Port-channel1", "mtu": 9216}]})
        reconcile_interface_mtu(self.device, {"interfaces": [{"interface_name": "Port-channel1", "mtu": 1500}]})
        self.assertEqual(NSOInterfaceMtuState.objects.get(interface=self.po1).l2_mtu, 1500)

    def test_category_reconcile_declares_interface_mtu_rows(self):
        from netbox_nso_plugin.reconcile import _LeaseOutcome, reconcile_category

        from ._outbox_case import mirror_update

        payload = {"interfaces": [{"interface_name": "Port-channel1", "mtu": 9216}]}
        mirror_update(self.management, adapter_device_id=76)
        self.addCleanup(mirror_update, self.management, adapter_device_id=None)
        with (
            patch("netbox_nso_plugin.reconcile._acquire_reconcile_lease", return_value=_LeaseOutcome()),
            patch("netbox_nso_plugin.adapter_client.get_interface_mtu", return_value=payload),
        ):
            result = reconcile_category(self.device, self.management, "interface_mtu")

        self.assertEqual(result["interface_mtu_states"][0].l2_mtu, 9216)


class TestInterfaceMtuWritePath(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("wp")
        cls.instance = NSOInstance.objects.create(name="nso-mtuwp", adapter_instance_id="nso-mtuwp")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="rtr-mtuwp", adapter_device_id=77
        )
        cls.po1 = Interface.objects.create(device=cls.device, name="Port-channel1", type="lag")

    def _state(self, l2_mtu=9216, status="accepted"):
        from uuid import uuid4

        return NSOInterfaceMtuState.objects.create(
            management=self.management,
            interface=self.po1,
            l2_mtu=l2_mtu,
            status=status,
            apply_attempt_id=uuid4() if status == "deploying" else None,
        )

    def test_owned_values_not_clobbered_by_device_read(self):
        from netbox_nso_plugin.interface_mtu_reconciler import reconcile_interface_mtu

        self._state(l2_mtu=9216, status="accepted")
        # Device still reports the OLD mtu (operator's change not applied yet).
        reconcile_interface_mtu(self.device, {"interfaces": [{"interface_name": "Port-channel1", "mtu": 1500}]})
        state = NSOInterfaceMtuState.objects.get(interface=self.po1)
        self.assertEqual(state.l2_mtu, 9216)  # operator intent preserved, not overwritten
        self.assertEqual(state.status, "accepted")  # device mismatch → holds accepted

    def test_deploying_waits_for_correlated_apply_evidence(self):
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.reconcile import _LeaseOutcome, reconcile_category

        from ._outbox_case import mirror_update

        state = self._state(l2_mtu=9000, status="deploying")
        attempt_id = state.apply_attempt_id
        other = Interface.objects.create(device=self.device, name="Port-channel2", type="lag")
        confirmed = NSOInterfaceMtuState.objects.create(
            management=self.management,
            interface=other,
            l2_mtu=1500,
            status="in_sync",
        )
        revision, _created = NSOIntentRevision.objects.get_or_create(device=self.device, scope="interface_mtu")
        matching = {
            "interfaces": [
                {"interface_name": "Port-channel1", "mtu": 9000},
                {"interface_name": "Port-channel2", "mtu": 1500},
            ]
        }
        non_matching_with_content_delta = {"interfaces": [{"interface_name": "Port-channel1", "mtu": 1500}]}
        mirror_update(state, status="deploying", apply_attempt_id=attempt_id)
        state.refresh_from_db()
        self.assertEqual(state.status, "deploying")

        with (
            patch("netbox_nso_plugin.reconcile._acquire_reconcile_lease", return_value=_LeaseOutcome()),
            patch(
                "netbox_nso_plugin.adapter_client.get_interface_mtu",
                side_effect=(matching, non_matching_with_content_delta),
            ),
        ):
            reconcile_category(self.device, self.management, "interface_mtu")
            state.refresh_from_db()
            self.assertEqual(state.status, "deploying")
            self.assertEqual(state.apply_attempt_id, attempt_id)

            revision.refresh_from_db()
            revision_before_content_delta = revision.revision
            reconcile_category(self.device, self.management, "interface_mtu")

        state.refresh_from_db()
        confirmed.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(state.status, "deploying")
        self.assertEqual(state.apply_attempt_id, attempt_id)
        self.assertEqual(confirmed.status, "changed")
        self.assertGreater(revision.revision, revision_before_content_delta)

    def test_owned_row_unreported_not_pruned(self):
        from netbox_nso_plugin.interface_mtu_reconciler import reconcile_interface_mtu

        self._state(l2_mtu=9216, status="accepted")
        # Device stops reporting MTU for this interface — owned intent must survive.
        reconcile_interface_mtu(self.device, {"interfaces": []})
        self.assertTrue(NSOInterfaceMtuState.objects.filter(interface=self.po1).exists())

    def test_push_builds_owned_snapshot(self):
        from unittest.mock import patch

        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.signals import reset_intent_push_state

        self._state(l2_mtu=9216, status="accepted")
        # An unowned mirror row must be excluded from the pushed intent.
        other = Interface.objects.create(device=self.device, name="TenGig0/0/0", type="10gbase-t")
        NSOInterfaceMtuState.objects.create(management=self.management, interface=other, l2_mtu=1500, status="imported")
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_interface_mtu_intent") as mock_put:
            deliver("interface_mtu", self.device.pk, 77)
        mock_put.assert_called_once()
        ifaces = mock_put.call_args[0][1]
        self.assertEqual([i["interface_name"] for i in ifaces], ["Port-channel1"])
        self.assertEqual(ifaces[0]["mtu"], 9216)

    def test_accept_marks_owned_and_writes_native_mtu(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model
        from django.db.models.signals import pre_save

        from netbox_nso_plugin.intent_state import revision_was_acquired
        from netbox_nso_plugin.models import NSOIntentRevision

        state = self._state(l2_mtu=9216, status="imported")
        revision, _created = NSOIntentRevision.objects.get_or_create(device=self.device, scope="interface_mtu")
        revision_before = revision.revision
        interface_scope_acquired = []

        def _record_footprint(sender, instance, **kwargs):
            if instance.pk == state.pk:
                interface_scope_acquired.append(revision_was_acquired(self.device.pk, "interface"))

        pre_save.connect(_record_footprint, sender=type(state), weak=False)
        self.addCleanup(pre_save.disconnect, _record_footprint, sender=type(state))
        User = get_user_model()
        admin = User.objects.create_superuser(username="mtu-admin", password="pw", email="m@x.y")  # noqa: S106
        self.client.force_login(admin)
        with patch("netbox_nso_plugin.adapter_client.put_interface_mtu_intent"):
            resp = self.client.post(f"/plugins/nso/interface-mtu/state/{state.pk}/accept/")
        self.assertEqual(resp.status_code, 302)
        state.refresh_from_db()
        self.po1.refresh_from_db()
        # Accepting an already-matching (imported) value → NetBox owns what's there → in_sync.
        self.assertEqual(state.status, "in_sync")
        self.assertIsNotNone(state.accepted_at)
        self.assertEqual(self.po1.mtu, 9216)  # native L2 mtu written onto dcim.Interface
        revision.refresh_from_db()
        self.assertEqual(revision.revision, revision_before + 1)
        self.assertTrue(interface_scope_acquired)
        self.assertTrue(all(interface_scope_acquired))

    def test_accept_differing_value_marks_accepted_pending_apply(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model

        state = self._state(l2_mtu=9216, status="changed")
        User = get_user_model()
        admin = User.objects.create_superuser(username="mtu-admin2", password="pw", email="m2@x.y")  # noqa: S106
        self.client.force_login(admin)
        with patch("netbox_nso_plugin.adapter_client.put_interface_mtu_intent"):
            resp = self.client.post(f"/plugins/nso/interface-mtu/state/{state.pk}/accept/")
        self.assertEqual(resp.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")  # differing value → pending apply
        self.assertIsNotNone(state.accepted_at)

    def test_edit_form_flags_unowned_changed(self):
        from netbox_nso_plugin.forms import NSOInterfaceMtuStateForm

        state = self._state(l2_mtu=9216, status="imported")
        form = NSOInterfaceMtuStateForm(data={"l2_mtu": 9000, "ip_mtu": "", "mpls_mtu": ""}, instance=state)
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save()
        self.assertEqual(obj.l2_mtu, 9000)
        self.assertEqual(obj.status, "changed")  # diverged from device → needs accept

    def test_edit_form_repends_an_owned_row_for_apply(self):
        from netbox_nso_plugin.forms import NSOInterfaceMtuStateForm

        state = self._state(l2_mtu=9216, status="in_sync")
        state.accepted_at = timezone.now()
        state.save(update_fields=["accepted_at"])
        form = NSOInterfaceMtuStateForm(data={"l2_mtu": 9100, "ip_mtu": "", "mpls_mtu": ""}, instance=state)
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save()
        self.assertEqual(obj.l2_mtu, 9100)
        self.assertEqual(obj.status, "accepted")  # changed owned intent must be applied again

    def test_edit_then_accept_owns_and_writes_native(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model

        from netbox_nso_plugin.forms import NSOInterfaceMtuStateForm

        state = self._state(l2_mtu=9216, status="imported")
        form = NSOInterfaceMtuStateForm(data={"l2_mtu": 9000, "ip_mtu": "", "mpls_mtu": ""}, instance=state)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        User = get_user_model()
        admin = User.objects.create_superuser(username="mtu-edit", password="pw", email="e@x.y")  # noqa: S106
        self.client.force_login(admin)
        with patch("netbox_nso_plugin.adapter_client.put_interface_mtu_intent"):
            self.client.post(f"/plugins/nso/interface-mtu/state/{state.pk}/accept/")
        state.refresh_from_db()
        self.po1.refresh_from_db()
        self.assertEqual(state.status, "accepted")  # edited (changed) → accept → pending apply
        self.assertEqual(self.po1.mtu, 9000)  # native L2 mtu = the edited value
