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

    def test_reconcile_preflights_native_and_overlay_creations(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.svi_reconciler import svi_reconcile_plan

        plan = svi_reconcile_plan(
            self.device,
            {"interfaces": [{"interface_name": "Vlan1627", "vlan_id": 1627, "type": "svi"}]},
        )

        self.assertIsInstance(plan, RendererMutationPlan)
        self.assertEqual(
            [(write.operation, write.model_label) for write in plan.write_set],
            [
                ("save", "dcim.interface"),
                ("save", "netbox_nso_plugin.nsosvistate"),
            ],
        )

    def test_reconcile_plan_does_not_create_the_device_vlan_group(self):
        from ipam.models import VLANGroup

        from netbox_nso_plugin.svi_reconciler import svi_reconcile_plan

        svi_reconcile_plan(
            self.device,
            {"interfaces": [{"interface_name": "Vlan1628", "vlan_id": 1628, "type": "svi"}]},
        )

        self.assertFalse(VLANGroup.objects.filter(slug=f"nso-{self.device.pk}").exists())

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

        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        inserted = None

        def insert_after_interface_read(execute, sql, params, many, context):
            nonlocal inserted
            result = execute(sql, params, many, context)
            if inserted is None and sql.lstrip().upper().startswith("SELECT") and Interface._meta.db_table in sql:
                inserted = Interface.objects.create(device=self.device, name="Vlan201", type="virtual")
            return result

        with connection.execute_wrapper(insert_after_interface_read):
            rows = reconcile_svi(
                self.device,
                {"interfaces": [{"interface_name": "Vlan201", "vlan_id": 201, "type": "svi"}]},
            )

        self.assertIsNotNone(inserted, "the wrapper never reached the interface read seam")
        self.assertEqual(rows[0].interface_id, inserted.pk)
        self.assertEqual(Interface.objects.filter(device=self.device, name="Vlan201").count(), 1)

    def test_a_concurrently_completed_svi_creation_is_reused(self):
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
        )
        from netbox_nso_plugin.svi_reconciler import _reconcile_svi, svi_reconcile_plan

        payload = {"interfaces": [{"interface_name": "Vlan202", "vlan_id": 202, "type": "svi"}]}
        plan = svi_reconcile_plan(self.device, payload)
        interface = Interface(device=self.device, name="Vlan202", type="virtual")
        interface._site = self.device.site
        interface._location = self.device.location
        interface._rack = self.device.rack
        state = NSOSVIState(
            management=self.management,
            interface=interface,
            svi_type="svi",
            status="imported",
            last_sync_at=plan.planned_at,
        )
        winner = RendererMutationPlan.build(
            saves=(
                planned_save(interface, force_insert=True, natural_key=("device", "name")),
                planned_save(
                    state,
                    force_insert=True,
                    natural_key=("management", "interface"),
                ),
            )
        )
        with renderer_mirror_writes(winner) as writer:
            writer.save(interface, force_insert=True)
            writer.save(state, force_insert=True)

        with renderer_mirror_writes(plan) as writer:
            rows = _reconcile_svi(self.device, payload, writer, plan.planned_at)

        self.assertEqual(rows[0].pk, state.pk)
        self.assertEqual(NSOSVIState.objects.filter(management=self.management, interface=interface).count(), 1)

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

    def test_stale_imported_svi_replans_after_status_flip(self):
        from unittest.mock import patch

        from netbox_nso_plugin import svi_reconciler

        payload = {"interfaces": [{"interface_name": "Vlan302", "vlan_id": 302, "type": "svi"}]}
        reconcile_svi = svi_reconciler.reconcile_svi
        reconcile_svi(self.device, payload)
        state = NSOSVIState.objects.get(management=self.management, interface__name="Vlan302")
        real_plan = svi_reconciler.svi_reconcile_plan
        plan_calls = 0

        def plan_then_flip(device, candidate_payload):
            nonlocal plan_calls
            plan_calls += 1
            plan = real_plan(device, candidate_payload)
            if plan_calls == 1:
                from ._outbox_case import content_update

                fresh = NSOSVIState.objects.get(pk=state.pk)
                content_update(fresh, status="in_sync")
            return plan

        with patch.object(svi_reconciler, "svi_reconcile_plan", side_effect=plan_then_flip):
            reconcile_svi(self.device, {"interfaces": []})

        state.refresh_from_db()
        self.assertEqual(plan_calls, 2)
        self.assertEqual(state.status, "changed")


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

        in_flight_rows = tuple(
            NSOSVIState.objects.filter(management=self.management, status="deploying").values_list(
                "pk",
                "apply_attempt_id",
            )
        )
        iface = Interface.objects.create(device=self.device, name=name, type="virtual")
        vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=vid, name=f"V{vid}")
        state = NSOSVIState.objects.create(
            management=self.management,
            interface=iface,
            vlan=vlan,
            svi_type="svi",
            vrf="MGMT",
            status=status,
            apply_attempt_id=uuid4() if status == "deploying" else None,
        )
        from ._outbox_case import mirror_update

        for pk, apply_attempt_id in in_flight_rows:
            mirror_update(
                NSOSVIState.objects.get(pk=pk),
                status="deploying",
                apply_attempt_id=apply_attempt_id,
            )
        return state

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

    def test_deploying_waits_for_correlated_apply_evidence(self):
        """An ordinary device read cannot identify the Apply attempt that it reflects."""
        from netbox_nso_plugin.models import NSOSVIState
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        state = self._state(name="Vlan100", vid=100, status="deploying")
        self._state(name="Vlan200", vid=200, status="in_sync")
        attempt_id = state.apply_attempt_id
        reconcile_svi(self.device, {"interfaces": [{"interface_name": "Vlan100", "vlan_id": 100, "type": "svi"}]})
        state = NSOSVIState.objects.get(interface__name="Vlan100")
        self.assertEqual(state.status, "deploying")
        self.assertEqual(state.apply_attempt_id, attempt_id)

        reconcile_svi(
            self.device,
            {"interfaces": [{"interface_name": "Vlan100", "vlan_id": 100, "type": "svi", "vrf": "MGMT"}]},
        )
        state.refresh_from_db()
        self.assertEqual(state.status, "deploying")
        self.assertEqual(state.apply_attempt_id, attempt_id)

    def test_predicted_scope_change_keeps_deploying_and_advances_revision(self):
        from uuid import uuid4

        from netbox_nso_plugin.intent_state import ReconcileMutationPlan, reconcile_transaction
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.svi_reconciler import reconcile_svi, svi_reconcile_plan

        from ._outbox_case import mirror_update

        deploying = self._state(name="Vlan100", vid=100, status="accepted")
        confirmed = self._state(name="Vlan200", vid=200, status="in_sync")
        mirror_update(deploying, status="deploying", apply_attempt_id=uuid4())
        attempt_id = deploying.apply_attempt_id
        revision, _created = NSOIntentRevision.objects.get_or_create(device=self.device, scope="svi")
        before = revision.revision
        payload = {"interfaces": [{"interface_name": "Vlan100", "vlan_id": 100, "type": "svi", "vrf": "MGMT"}]}
        plan = svi_reconcile_plan(self.device, payload)
        self.assertTrue(plan.changes_content)
        self.assertFalse(plan.settles_deploying)

        outer_plan = ReconcileMutationPlan(plan.lock_footprint, changes_content=True)
        with reconcile_transaction(outer_plan):
            reconcile_svi(self.device, payload)

        deploying.refresh_from_db()
        confirmed.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(confirmed.status, "changed")
        self.assertEqual(deploying.status, "deploying")
        self.assertEqual(deploying.apply_attempt_id, attempt_id)
        self.assertEqual(revision.revision, before + 1)

    def test_owned_state_survives_when_interface_drops_from_payload(self):
        """An owned SVI overlay must NOT be hard-deleted when the device stops reporting it.

        NSOSVIState is in ``_APPLY_DEPLOYING_SCOPES``; a bulk delete of stale rows destroys
        ownership. A deploying row retains its correlated Apply attempt. A confirmed row
        that vanishes surfaces as ``changed`` and advances the scope revision.
        """
        from netbox_nso_plugin.models import NSOIntentRevision, NSOSVIState
        from netbox_nso_plugin.svi_reconciler import reconcile_svi

        deploying = self._state(name="Vlan100", vid=100, status="deploying")
        attempt_id = deploying.apply_attempt_id
        confirmed = self._state(name="Vlan200", vid=200, status="in_sync")
        revision = NSOIntentRevision.objects.get(device=self.device, scope="svi")
        before = revision.revision
        reconcile_svi(self.device, {"interfaces": []})  # device stops reporting all SVIs
        assert NSOSVIState.objects.filter(pk=deploying.pk).exists(), "deploying (apply-in-flight) overlay deleted"
        assert NSOSVIState.objects.filter(pk=confirmed.pk).exists(), "in_sync overlay deleted"
        deploying.refresh_from_db()
        revision.refresh_from_db()
        assert deploying.status == "deploying"
        assert deploying.apply_attempt_id == attempt_id
        assert NSOSVIState.objects.get(pk=confirmed.pk).status == "changed"
        assert revision.revision == before + 1  # the vanished confirmed SVI is a content change

    def test_push_builds_owned_snapshot(self):
        from netbox_nso_plugin.delivery import render
        from netbox_nso_plugin.signals import reset_intent_push_state

        self._state(name="Vlan100", vid=100, status="accepted")
        self._state(name="Vlan200", vid=200, status="imported")  # not owned → excluded
        reset_intent_push_state()
        ifaces = render("svi", self.device.pk, 42).payload
        assert [i["interface_name"] for i in ifaces] == ["Vlan100"]
        assert ifaces[0]["vlan_id"] == 100 and ifaces[0]["vrf"] == "MGMT"

    def test_foreign_overlay_save_does_not_schedule_svi_behavior(self):
        from unittest.mock import patch

        state = self._state(name="Vlan250", vid=250, status="accepted")

        with patch("netbox_nso_plugin.signals._schedule_intent_push") as schedule:
            state.vrf = "FOREIGN"
            state.save(update_fields=("vrf",))

        schedule.assert_not_called()

    def test_accept_marks_owned(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision, NSOOwnershipManifest

        state = self._state(name="Vlan300", vid=300, status="conflict")
        User = get_user_model()
        admin = User.objects.create_superuser(username="svi-admin", password="pw", email="s@x.y")  # noqa: S106
        self.client.force_login(admin)
        with patch("netbox_nso_plugin.adapter_client.put_svi_intent"):
            resp = self.client.post(f"/plugins/nso/svi/state/{state.pk}/accept/")
        assert resp.status_code == 302
        state.refresh_from_db()
        assert state.status == "accepted" and state.accepted_at is not None
        revision = NSOIntentRevision.objects.get(device=self.device, scope="svi")
        assert revision.verified_revision == revision.revision
        assert revision.verified_fingerprint == delivery.canonical_fingerprint(
            delivery.render("svi", self.device.pk, self.management.adapter_device_id).payload
        )
        assert NSOOwnershipManifest.objects.filter(
            device_id=self.device.pk,
            scope="svi",
            native_model_label="dcim.interface",
            native_key={"device_id": state.interface.device_id, "name": state.interface.name},
            ownership_state="owned",
        ).exists()

    def test_exhausted_save_protocol_error_retry_returns_an_operator_error(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model
        from django.contrib.messages import get_messages

        from netbox_nso_plugin.intent_state import IntentMutationProtocolError

        state = self._state(name="Vlan301", vid=301, status="conflict")
        user = get_user_model().objects.create_superuser(
            username="svi-stale-admin",
            password="pw",  # noqa: S106
            email="stale-svi@example.test",
        )
        self.client.force_login(user)

        with patch(
            "netbox_nso_plugin.renderer_writer.RendererWriter.save",
            side_effect=IntentMutationProtocolError("the stored row disappeared"),
        ) as save:
            response = self.client.post(f"/plugins/nso/svi/state/{state.pk}/accept/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(save.call_count, 2)
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            ["Routing state changed. Refresh the page and try again."],
        )
        state.refresh_from_db()
        self.assertEqual(state.status, "conflict")
        self.assertIsNone(state.accepted_at)

    def test_exhausted_plan_protocol_error_retry_returns_an_operator_error(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model
        from django.contrib.messages import get_messages

        from netbox_nso_plugin.intent_state import IntentMutationProtocolError

        state = self._state(name="Vlan302", vid=302, status="conflict")
        user = get_user_model().objects.create_superuser(
            username="svi-plan-stale-admin",
            password="pw",  # noqa: S106
            email="stale-svi-plan@example.test",
        )
        self.client.force_login(user)

        with patch(
            "netbox_nso_plugin.renderer_writer.RendererMutationPlan.build",
            side_effect=IntentMutationProtocolError("the stored row disappeared"),
        ) as build:
            response = self.client.post(f"/plugins/nso/svi/state/{state.pk}/accept/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(build.call_count, 2)
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            ["Routing state changed. Refresh the page and try again."],
        )
        state.refresh_from_db()
        self.assertEqual(state.status, "conflict")
        self.assertIsNone(state.accepted_at)
