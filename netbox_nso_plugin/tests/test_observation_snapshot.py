# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Publish observations with the reconcile fence and invalidate them with the source."""

from uuid import uuid4

from django.db.models import F
from django.test import TestCase

from netbox_nso_plugin.adapter_client import AdapterError, bound_session, reset_interfaces_doc_capability
from netbox_nso_plugin.models import NSOFamilyObservation, NSOFamilyReadState
from netbox_nso_plugin.read_gate import gated_family_run, observe_aggregate
from netbox_nso_plugin.reconcile import reconcile_category

from ._observation_case import ObservationTransport, interface_observation, ip_observation, observation
from ._outbox_case import mirror_update
from .test_gated_reconcile import _make
from .test_read_gate import _INC_B, _rs


class TestObservationSnapshot(TestCase):
    def setUp(self):
        self.device, self.management = _make(f"obs{uuid4().hex[:8]}", manage_interfaces=True, manage_description=True)
        reset_interfaces_doc_capability()
        self.addCleanup(reset_interfaces_doc_capability)

    def _publish(self, family="interface_attributes", *, revision=1, body=lambda: None, pre_body=None, snapshot=None):
        return gated_family_run(
            self.management,
            family,
            _rs(attempt_id=revision),
            body,
            epoch=self.management.adapter_device_id,
            observation=observation(family, revision=revision) if snapshot is None else snapshot,
            pre_body=pre_body,
        )

    def test_category_reconcile_stores_and_replaces_both_observations(self):
        for revision in (1, 2):
            interface = observation("interface_attributes", revision=revision)
            ip = observation("interface_ip", revision=revision)
            transport = ObservationTransport(self.management.adapter_device_id, interface, ip)
            with bound_session(transport.session()):
                context = reconcile_category(self.device, self.management, "interfaces")
            for family in ("interface_attributes", "interface_ip"):
                self.assertEqual(context["_gate"][family], "ran")
                saved = NSOFamilyObservation.objects.get(
                    read_state__management=self.management, read_state__family=family
                )
                self.assertEqual(saved.revision, revision)
                self.assertEqual(saved.document, {"interfaces": [], "unprojectable": []})
                self.assertIsNotNone(saved.observed_at.utcoffset())
                self.assertEqual(saved.read_state.applied_payload_revision, revision)
        self.assertEqual(NSOFamilyObservation.objects.count(), 2)

    def test_observation_is_independent_of_overlay_values_and_apply_telemetry(self):
        from netbox_nso_plugin.models import NSOInterfaceState

        interface = observation("interface_attributes", interfaces=[interface_observation("lag-60", description="")])
        ip = observation("interface_ip", interfaces=[ip_observation("lag-60")])
        transport = ObservationTransport(self.management.adapter_device_id, interface, ip)
        transport.documents["interfaces-doc"]["interfaces"] = [
            {
                "name": "lag-60",
                "attrs": {
                    "description": {
                        "nso_value": "mutable mirror",
                        "status": "imported",
                        "last_apply_error": {"code": "test-refusal"},
                        "last_apply_at": "2026-10-01T11:00:00+00:00",
                    }
                },
            }
        ]
        transport.documents["interface-ips"]["interfaces"] = ip["document"]["interfaces"]
        with bound_session(transport.session()):
            context = reconcile_category(self.device, self.management, "interfaces")
        self.assertEqual(context["_gate"]["interface_attributes"], "ran")
        saved = NSOFamilyObservation.objects.get(read_state__family="interface_attributes")
        self.assertEqual(saved.document, interface["document"])
        state = NSOInterfaceState.objects.get(interface__device=self.device, attribute="description")
        self.assertEqual(state.nso_value, "mutable mirror")
        self.assertEqual(state.last_apply_error, {"code": "test-refusal"})
        self.assertIsNotNone(state.last_apply_at)
        self.assertEqual(NSOFamilyObservation.objects.get(read_state__family="interface_ip").document, ip["document"])

    def test_category_reconcile_rejects_invalid_observation(self):
        for field, value in (
            ("revision", 999),
            ("source_epoch", 999),
            ("family", "interface_ip"),
            ("digest", "0" * 64),
        ):
            with self.subTest(field=field):
                interface = observation("interface_attributes")
                interface[field] = value
                transport = ObservationTransport(
                    self.management.adapter_device_id, interface, observation("interface_ip")
                )
                transport.documents["interfaces-doc"]["read_state"] = _rs()
                with bound_session(transport.session()), self.assertRaises(AdapterError):
                    reconcile_category(self.device, self.management, "interfaces")
                self.assertFalse(NSOFamilyObservation.objects.exists())

    def test_unavailable_read_keeps_previous_snapshot(self):
        for family in ("interface_attributes", "interface_ip"):
            with self.subTest(family=family):
                self._publish(family)
                result = gated_family_run(
                    self.management,
                    family,
                    _rs(attempt_id=2, outcome="unavailable", result="kept", succeeded=False),
                    lambda: None,
                    epoch=self.management.adapter_device_id,
                )
                self.assertEqual(result.disposition, "skipped_unavailable")
                self.assertEqual(NSOFamilyObservation.objects.get(read_state__family=family).revision, 1)

    def test_body_failure_rolls_back_snapshot_and_body_writes(self):
        def fail():
            self.device.comments = "must roll back"
            self.device.save(update_fields=["comments"])
            raise RuntimeError("body failed")

        for family in ("interface_attributes", "interface_ip"):
            with self.subTest(family=family):
                self._publish(family)
                with self.assertRaisesRegex(RuntimeError, "body failed"):
                    self._publish(family, revision=2, body=fail)
                self.device.refresh_from_db()
                self.assertEqual(self.device.comments, "")
                self.assertEqual(NSOFamilyObservation.objects.get(read_state__family=family).revision, 1)
                self.assertEqual(NSOFamilyReadState.objects.get(family=family).applied_payload_revision, 1)

    def test_superseded_publication_keeps_previous_snapshot(self):
        from netbox_nso_plugin.intent_state import ReconcileMutationPlan, reconcile_family_footprint

        for family, scope in (("interface_attributes", "interface"), ("interface_ip", "ip")):
            with self.subTest(family=family):
                self._publish(family)

                def supersede():
                    NSOFamilyReadState.objects.filter(management=self.management, family=family).update(
                        publication_sequence=F("publication_sequence") + 1
                    )
                    return ReconcileMutationPlan(reconcile_family_footprint(self.device.pk, (scope,)))

                result = self._publish(family, revision=2, pre_body=supersede)
                self.assertEqual(result.disposition, "skipped_stale_attempt")
                self.assertEqual(NSOFamilyObservation.objects.get(read_state__family=family).revision, 1)

    def test_invalid_observation_rolls_back_publication(self):
        for family in ("interface_attributes", "interface_ip"):
            other_family = "interface_ip" if family == "interface_attributes" else "interface_attributes"
            for field, value in (
                ("revision", 999),
                ("source_epoch", 999),
                ("family", other_family),
                ("digest", "0" * 64),
            ):
                with self.subTest(family=family, field=field):
                    snapshot = observation(family)
                    snapshot[field] = value
                    with self.assertRaises(AdapterError):
                        self._publish(family, snapshot=snapshot)
                    self.assertFalse(NSOFamilyObservation.objects.exists())
                    self.assertIsNone(NSOFamilyReadState.objects.get(family=family).applied_payload_revision)

    def test_missing_observation_fails_closed(self):
        for family in ("interface_attributes", "interface_ip"):
            with self.subTest(family=family), self.assertRaises(AdapterError):
                gated_family_run(self.management, family, _rs(), lambda: None, epoch=self.management.adapter_device_id)
            self.assertFalse(NSOFamilyObservation.objects.exists())

    def test_invalid_observation_keeps_previous_snapshot(self):
        for family in ("interface_attributes", "interface_ip"):
            with self.subTest(family=family):
                self._publish(family)
                previous = NSOFamilyObservation.objects.get(read_state__family=family)
                snapshot = observation(family, revision=2)
                snapshot["digest"] = "0" * 64
                with self.assertRaises(AdapterError):
                    self._publish(family, revision=2, snapshot=snapshot)
                saved = NSOFamilyObservation.objects.get(read_state__family=family)
                self.assertEqual((saved.pk, saved.revision, saved.recorded_at), (previous.pk, 1, previous.recorded_at))
                self.assertEqual(saved.read_state.applied_payload_revision, 1)

    def test_snapshot_uses_payload_revision_instead_of_latest_attempt(self):
        result = gated_family_run(
            self.management,
            "interface_attributes",
            _rs(attempt_id=9, payload_revision=3),
            lambda: None,
            epoch=self.management.adapter_device_id,
            observation=observation("interface_attributes", revision=3),
        )
        self.assertEqual(result.disposition, "ran")
        self.assertEqual(NSOFamilyObservation.objects.get().revision, 3)
        self.assertEqual(NSOFamilyReadState.objects.get().applied_attempt_id, 9)

    def test_legacy_fallback_has_no_snapshot(self):
        transport = ObservationTransport(
            self.management.adapter_device_id,
            observation("interface_attributes"),
            observation("interface_ip"),
            legacy=True,
        )
        transport.documents["interface-ips"] = {"interfaces": []}
        with bound_session(transport.session()):
            context = reconcile_category(self.device, self.management, "interfaces")
        self.assertEqual(context["_gate"]["interface_attributes"], "legacy")
        self.assertFalse(NSOFamilyObservation.objects.exists())

    def test_null_revision_and_non_observer_keep_previous_snapshot(self):
        self._publish()
        self._publish("interface_ip")
        for family in ("interface_attributes", "interface_ip", "bfd"):
            gated_family_run(
                self.management,
                family,
                _rs(attempt_id=2, payload_revision=None),
                lambda: None,
                epoch=self.management.adapter_device_id,
            )
        self.assertEqual(list(NSOFamilyObservation.objects.values_list("revision", flat=True)), [1, 1])

    def test_incarnation_adoption_deletes_snapshots(self):
        self._publish()
        self._publish("interface_ip")
        gated_family_run(
            self.management,
            "bfd",
            _rs(incarnation=_INC_B[0], incarnation_born=_INC_B[1]),
            lambda: None,
            epoch=self.management.adapter_device_id,
        )
        self.assertFalse(NSOFamilyObservation.objects.exists())

    def test_source_epoch_adoption_deletes_snapshots(self):
        self._publish()
        self._publish("interface_ip")
        gated_family_run(
            self.management, "bfd", _rs(source_epoch=2), lambda: None, epoch=self.management.adapter_device_id
        )
        self.assertFalse(NSOFamilyObservation.objects.exists())

    def test_aggregate_source_change_deletes_snapshots(self):
        self._publish()
        self._publish("interface_ip")
        observe_aggregate(self.management, {"bfd": _rs(source_epoch=2)}, epoch=self.management.adapter_device_id)
        self.assertFalse(NSOFamilyObservation.objects.exists())

    def test_source_rekey_deletes_snapshots(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.signals import _sync_source_change

        self._publish()
        self._publish("interface_ip")
        self.management.refresh_from_db()
        mirror_update(self.management, source_rekey_pending=True)
        transport = ObservationTransport(
            self.management.adapter_device_id, observation("interface_attributes"), observation("interface_ip")
        )
        transport.patch_result = {"source_epoch": 2}
        with bound_session(transport.session()):
            self.assertTrue(_sync_source_change(self.management, adapter_client))
        self.assertFalse(NSOFamilyObservation.objects.exists())

    def test_adapter_relink_deletes_snapshots(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.signals import _onboard_into_adapter

        self._publish()
        self._publish("interface_ip")
        self.management.refresh_from_db()
        transport = ObservationTransport(
            self.management.adapter_device_id, observation("interface_attributes"), observation("interface_ip")
        )
        transport.onboard_result = {"id": self.management.adapter_device_id + 1, "source_epoch": 1}
        with bound_session(transport.session()):
            self.assertTrue(_onboard_into_adapter(self.management, adapter_client))
        self.assertFalse(NSOFamilyObservation.objects.exists())

    def test_adapter_relink_with_reused_id_deletes_snapshots(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.signals import _onboard_into_adapter

        self._publish()
        self.management.refresh_from_db()
        transport = ObservationTransport(
            self.management.adapter_device_id, observation("interface_attributes"), observation("interface_ip")
        )
        transport.onboard_result = {"id": self.management.adapter_device_id, "source_epoch": 1}
        with bound_session(transport.session()):
            self.assertTrue(_onboard_into_adapter(self.management, adapter_client))
        self.assertFalse(NSOFamilyObservation.objects.exists())

    def test_identity_change_during_body_rolls_back_publication(self):
        self._publish()

        def supersede():
            self.device.comments = "must roll back"
            self.device.save(update_fields=["comments"])
            NSOFamilyReadState.objects.filter(management=self.management).update(
                publication_sequence=F("publication_sequence") + 1
            )

        result = self._publish(revision=2, body=supersede)
        self.assertEqual(result.disposition, "skipped_stale_attempt")
        self.device.refresh_from_db()
        self.assertEqual(self.device.comments, "")
        self.assertEqual(NSOFamilyObservation.objects.get().revision, 1)

    def test_adapter_remap_deletes_snapshots(self):
        from unittest.mock import patch

        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        from .test_sync_cache import _adapter_row

        self._publish()
        self._publish("interface_ip")
        self.management.refresh_from_db()
        moved = _adapter_row(self.management, id=self.management.adapter_device_id + 100)
        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[moved]),
            patch("netbox_nso_plugin.adapter_client.set_scope", return_value={}),
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            reconcile_device_links(NSODeviceManagement.objects.filter(pk=self.management.pk))

        self.management.refresh_from_db()
        self.assertEqual(self.management.adapter_device_id, moved["id"])
        self.assertFalse(NSOFamilyObservation.objects.exists())
        self.assertFalse(
            NSOFamilyReadState.objects.filter(
                management=self.management, admitted_payload_revision__isnull=False
            ).exists()
        )
