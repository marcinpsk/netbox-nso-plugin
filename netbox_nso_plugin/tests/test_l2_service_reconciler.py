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
