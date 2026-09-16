# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Task 2: reconcile_l2_services → vpn.L2VPN + L2VPNTermination + NSOL2SapState."""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase
from vpn.models import L2VPN, L2VPNTermination

from netbox_nso_plugin.l2_service_reconciler import reconcile_l2_services
from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOL2SapState


def _payload(services):
    return {"device_id": 1, "services": services}


def _l2_table_snapshot():
    def rows(model):
        fields = [field.attname for field in model._meta.concrete_fields]
        return list(model.objects.order_by("pk").values_list(*fields))

    return {
        "l2vpns": rows(L2VPN),
        "terminations": rows(L2VPNTermination),
        "states": rows(NSOL2SapState),
    }


class TestReconcileL2Services(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="L2RMfg", slug="l2rmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="L2RDev", slug="l2rdev")
        role = DeviceRole.objects.create(name="L2RRole", slug="l2rrole")
        site = Site.objects.create(name="L2RSite", slug="l2rsite")
        cls.device = Device.objects.create(name="l2r-rtr", device_type=dt, role=role, site=site)
        cls.inst = NSOInstance.objects.create(name="l2r-inst", adapter_instance_id="l2r-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.inst, nso_device_name="l2r-rtr"
        )
        cls.port = Interface.objects.create(device=cls.device, name="1/1/c31/3", type="other")
        cls.lag = Interface.objects.create(device=cls.device, name="lag-60", type="lag")
        cls.extra_port = Interface.objects.create(device=cls.device, name="1/1/c31/4", type="other")

    def _assert_invalid_service_type_rejected(self, invalid_service, expected_value):
        from netbox_nso_plugin.adapter_client import AdapterError

        existing_service = {
            "service_name": "EXISTING",
            "service_type": "epipe",
            "service_id": 4022,
            "saps": [{"sap_id": "1/1/c31/3:4022", "port": self.port.name, "outer_tag": 4022}],
        }
        valid_service = {
            "service_name": "VALID",
            "service_type": "vpls",
            "service_id": 9000,
            "saps": [{"sap_id": "lag-60:9000", "port": self.lag.name, "outer_tag": 9000}],
        }
        reconcile_l2_services(self.device, _payload([existing_service]))
        before = _l2_table_snapshot()
        error = None

        try:
            reconcile_l2_services(self.device, _payload([valid_service, invalid_service]))
        except AdapterError as exc:
            error = exc

        new_service_names = (valid_service["service_name"], invalid_service["service_name"])
        new_slugs = [f"nso-{self.device.pk}-{service_name}" for service_name in new_service_names]
        created_vpns = list(L2VPN.objects.filter(slug__in=new_slugs).order_by("slug").values_list("slug", "type"))
        created_states = list(
            NSOL2SapState.objects.filter(
                management=self.mgmt,
                service_name__in=new_service_names,
            )
            .order_by("service_name")
            .values_list("service_name", "status", "service_type")
        )
        self.assertEqual((created_vpns, created_states), ([], []))
        self.assertEqual(_l2_table_snapshot(), before)
        self.assertIsNotNone(error)
        self.assertEqual(error.code, "invalid_response")
        self.assertIn(invalid_service["service_name"], str(error))
        self.assertIn(expected_value, str(error))

    def test_unsupported_service_type_rejects_the_entire_document(self):
        self._assert_invalid_service_type_rejected(
            {
                "service_name": "UNSUPPORTED",
                "service_type": "foo",
                "service_id": 9001,
                "saps": [{"sap_id": "1/1/c31/4:9001", "port": self.extra_port.name, "outer_tag": 9001}],
            },
            "'foo'",
        )

    def test_missing_service_type_rejects_the_entire_document(self):
        self._assert_invalid_service_type_rejected(
            {
                "service_name": "MISSING",
                "service_id": 9002,
                "saps": [{"sap_id": "1/1/c31/4:9002", "port": self.extra_port.name, "outer_tag": 9002}],
            },
            "<missing>",
        )

    def test_null_service_type_rejects_the_entire_document(self):
        self._assert_invalid_service_type_rejected(
            {
                "service_name": "NULL",
                "service_type": None,
                "service_id": 9003,
                "saps": [{"sap_id": "1/1/c31/4:9003", "port": self.extra_port.name, "outer_tag": 9003}],
            },
            "None",
        )

    def test_creates_l2vpn_termination_and_state(self):
        rows = reconcile_l2_services(
            self.device,
            _payload(
                [
                    {
                        "service_name": "701",
                        "service_type": "vpls",
                        "service_id": None,
                        "saps": [{"sap_id": "1/1/c31/3:701", "port": "1/1/c31/3", "outer_tag": 701, "inner_tag": None}],
                    },
                    {
                        "service_name": "TL",
                        "service_type": "epipe",
                        "service_id": 4022,
                        "saps": [{"sap_id": "lag-60:3999", "port": "lag-60", "outer_tag": 3999, "inner_tag": None}],
                    },
                ]
            ),
        )
        assert len(rows) == 2
        vpls = L2VPN.objects.get(slug=f"nso-{self.device.pk}-701")
        epipe = L2VPN.objects.get(slug=f"nso-{self.device.pk}-TL")
        assert vpls.type == "vpls"
        assert (epipe.type, epipe.identifier) == ("vpws", 4022)
        # termination on the right port
        term = L2VPNTermination.objects.get(l2vpn=vpls)
        assert term.assigned_object == self.port
        st = NSOL2SapState.objects.get(management=self.mgmt, service_name="701")
        assert (st.status, st.outer_tag, st.termination_id) == ("imported", 701, term.pk)

    def test_missing_port_is_conflict(self):
        rows = reconcile_l2_services(
            self.device,
            _payload(
                [
                    {
                        "service_name": "v9",
                        "service_type": "vpls",
                        "saps": [{"sap_id": "9/9/9:9", "port": "9/9/9", "outer_tag": 9}],
                    }
                ]
            ),
        )
        assert rows[0].status == "conflict"
        assert rows[0].termination is None

    def test_port_already_terminated_elsewhere_is_conflict(self):
        other = L2VPN.objects.create(name="other", slug="other", type="vpls")
        L2VPNTermination.objects.create(l2vpn=other, assigned_object=self.port)
        rows = reconcile_l2_services(
            self.device,
            _payload(
                [
                    {
                        "service_name": "701",
                        "service_type": "vpls",
                        "saps": [{"sap_id": "1/1/c31/3:701", "port": "1/1/c31/3", "outer_tag": 701}],
                    }
                ]
            ),
        )
        assert rows[0].status == "conflict"

    def test_full_replace_marks_stale_changed(self):
        reconcile_l2_services(
            self.device,
            _payload(
                [
                    {
                        "service_name": "TL",
                        "service_type": "epipe",
                        "service_id": 4022,
                        "saps": [{"sap_id": "lag-60:3999", "port": "lag-60", "outer_tag": 3999}],
                    }
                ]
            ),
        )
        # Next sync no longer reports it → marked changed (drift), native objects left intact.
        reconcile_l2_services(self.device, _payload([]))
        st = NSOL2SapState.objects.get(management=self.mgmt, service_name="TL")
        assert st.status == "changed"
        assert L2VPN.objects.filter(slug=f"nso-{self.device.pk}-TL").exists()

    def test_direct_reconcile_replans_after_status_changes_during_acquisition(self):
        from unittest.mock import patch

        from netbox_nso_plugin import l2_service_reconciler

        from ._outbox_case import content_update

        service = {
            "service_name": "REPLAN",
            "service_type": "vpls",
            "service_id": 701,
            "saps": [{"sap_id": "1/1/c31/3:701", "port": "1/1/c31/3", "outer_tag": 701}],
        }
        l2_service_reconciler.reconcile_l2_services(self.device, _payload([service]))
        real_plan = l2_service_reconciler.l2_service_reconcile_plan
        plan_calls = 0

        def plan_then_flip(device, observed):
            nonlocal plan_calls
            plan_calls += 1
            plan = real_plan(device, observed)
            if plan_calls == 1:
                state = NSOL2SapState.objects.get(management=self.mgmt, service_name=service["service_name"])
                content_update(state, status="in_sync")
            return plan

        with patch.object(l2_service_reconciler, "l2_service_reconcile_plan", side_effect=plan_then_flip):
            rows = l2_service_reconciler.reconcile_l2_services(self.device, _payload([]))

        state = NSOL2SapState.objects.get(management=self.mgmt, service_name=service["service_name"])
        self.assertEqual(plan_calls, 2)
        self.assertEqual(rows, [state])
        self.assertEqual(state.status, "changed")

    def test_idempotent_no_duplicate_terminations(self):
        p = _payload(
            [
                {
                    "service_name": "701",
                    "service_type": "vpls",
                    "saps": [{"sap_id": "1/1/c31/3:701", "port": "1/1/c31/3", "outer_tag": 701}],
                }
            ]
        )
        reconcile_l2_services(self.device, p)
        reconcile_l2_services(self.device, p)
        assert L2VPNTermination.objects.filter(assigned_object_id=self.port.pk).count() == 1
        assert NSOL2SapState.objects.filter(management=self.mgmt, service_name="701").count() == 1

    def test_duplicate_services_and_saps_use_the_first_observation(self):
        service = {
            "service_name": "DUPLICATE",
            "service_type": "vpls",
            "service_id": 701,
            "saps": [
                {"sap_id": "1/1/c31/3:701", "port": "1/1/c31/3", "outer_tag": 701},
                {"sap_id": "1/1/c31/3:701", "port": "lag-60", "outer_tag": 999},
            ],
        }
        duplicate = {**service, "service_type": "epipe", "service_id": 999}

        rows = reconcile_l2_services(self.device, _payload([service, duplicate]))

        self.assertEqual(len(rows), 1)
        state = NSOL2SapState.objects.get(management=self.mgmt, service_name="DUPLICATE")
        self.assertEqual((state.port, state.outer_tag), ("1/1/c31/3", 701))
        l2vpn = L2VPN.objects.get(slug=f"nso-{self.device.pk}-DUPLICATE")
        self.assertEqual((l2vpn.type, l2vpn.identifier), ("vpls", 701))

    def test_owned_sap_keeps_service_type_intent_when_device_differs(self):
        reconcile_l2_services(
            self.device,
            _payload(
                [
                    {
                        "service_name": "701",
                        "service_type": "vpls",
                        "saps": [
                            {
                                "sap_id": "1/1/c31/3:701",
                                "port": "1/1/c31/3",
                                "outer_tag": 701,
                                "inner_tag": None,
                            }
                        ],
                    }
                ]
            ),
        )
        state = NSOL2SapState.objects.get(management=self.mgmt, service_name="701")
        state.status = "accepted"
        state.service_type = "epipe"
        state.save(update_fields=["status", "service_type"])

        reconcile_l2_services(
            self.device,
            _payload(
                [
                    {
                        "service_name": "701",
                        "service_type": "vpls",
                        "saps": [
                            {
                                "sap_id": "1/1/c31/3:701",
                                "port": "1/1/c31/3",
                                "outer_tag": 701,
                                "inner_tag": None,
                            }
                        ],
                    }
                ]
            ),
        )

        state.refresh_from_db()
        assert state.service_type == "epipe"
        assert state.status == "accepted"
