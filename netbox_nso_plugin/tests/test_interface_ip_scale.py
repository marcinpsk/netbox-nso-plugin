# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Interface-IP reconciliation on large devices."""

import os
from ipaddress import IPv4Address
from time import perf_counter
from unittest import skipUnless

from dcim.models import Interface
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from ipam.models import VRF, IPAddress

from netbox_nso_plugin.models import NSOInterfaceIPState
from netbox_nso_plugin.reconcile import _gated
from netbox_nso_plugin.template_content import _reconcile_interface_ips, interface_ip_reconcile_plan

from ._outbox_case import make_device, make_managed
from .test_gated_reconcile import _rs


class TestInterfaceIpScale(TestCase):
    def test_vrf_replacement_scales_with_reported_rows(self):
        device = make_device("ip-scale")
        interfaces = Interface.objects.bulk_create(
            [Interface(device=device, name=f"Ethernet{i}", type="1000base-t") for i in range(128)]
        )
        NSOInterfaceIPState.objects.bulk_create(
            [
                NSOInterfaceIPState(interface=iface, address=f"198.18.0.{i + 1}/32", vrf="OLD", status="imported")
                for i, iface in enumerate(interfaces)
            ]
        )
        payload = {
            "interfaces": [
                {"interface": iface.name, "addresses": [{"address": f"198.18.0.{i + 1}/32", "vrf": "NEW"}]}
                for i, iface in enumerate(interfaces)
            ]
        }
        started = perf_counter()
        with CaptureQueriesContext(connection) as queries:
            result = _reconcile_interface_ips(device, payload)
        elapsed = perf_counter() - started
        self.assertLess(len(queries), 4096, f"128 overlay replacements: {elapsed:.2f}s, {len(queries)} queries")
        self.assertEqual(len(result), 128)
        self.assertEqual({row.vrf for row in result}, {"NEW"})

    @skipUnless(os.environ.get("NSO_RUN_SCALE_TESTS") == "1", "Run with NSO_RUN_SCALE_TESTS=1 in the scale lane")
    def test_large_device_replacements_finish_inside_the_job_budget(self):
        self._assert_native_replacements(7400, 8800)

    def test_vrf_replacement_unassigns_native_addresses(self):
        self._assert_native_replacements(128, 128)

    def _assert_native_replacements(self, address_count, interface_count):
        device, management = make_managed(f"ip-native-scale-{address_count}", 18110)
        interfaces = Interface.objects.bulk_create(
            [Interface(device=device, name=f"Ethernet{i}", type="1000base-t") for i in range(interface_count)]
        )
        selected = interfaces[:address_count]
        addresses = [f"{IPv4Address(int(IPv4Address('198.18.0.1')) + i)}/32" for i in range(address_count)]
        old_vrf = VRF.objects.create(name="OLD")
        VRF.objects.create(name="NEW")
        interface_type = ContentType.objects.get_for_model(Interface)
        old_states = NSOInterfaceIPState.objects.bulk_create(
            [
                NSOInterfaceIPState(interface=iface, address=address, vrf="OLD", status="imported")
                for iface, address in zip(selected, addresses, strict=True)
            ]
        )
        IPAddress.objects.bulk_create(
            [
                IPAddress(
                    address=address, vrf=old_vrf, assigned_object_type=interface_type, assigned_object_id=iface.pk
                )
                for iface, address in zip(selected, addresses, strict=True)
            ]
        )
        payload = {
            "read_state": _rs(),
            "interfaces": [
                {"interface": iface.name, "addresses": [{"address": address, "vrf": "NEW"}]}
                for iface, address in zip(selected, addresses, strict=True)
            ],
        }
        context = {}
        query_count = 0

        def count_queries(execute, sql, params, many, query_context):
            nonlocal query_count
            query_count += 1
            return execute(sql, params, many, query_context)

        started = perf_counter()
        with connection.execute_wrapper(count_queries):
            outcome = _gated(
                context,
                management,
                "interface_ip",
                payload,
                lambda: _reconcile_interface_ips(device, payload),
                epoch=management.adapter_device_id,
                ctx_key="interface_ips",
                pre_body=lambda: interface_ip_reconcile_plan(device, payload),
            )
        elapsed = perf_counter() - started
        diagnostic = (
            f"NetBox {settings.VERSION}: {address_count} native interface-IP replacements: "
            f"{elapsed:.2f}s, {query_count} queries"
        )
        self.assertEqual(outcome.disposition, "ran")
        self.assertLess(elapsed, 600, diagnostic)
        if address_count == 128:
            self.assertLess(query_count, 6500, diagnostic)
        self.assertEqual(len(context["interface_ips"]), address_count)
        self.assertEqual({row.vrf for row in context["interface_ips"]}, {"NEW"})
        self.assertEqual({row.status for row in context["interface_ips"]}, {"imported"})
        self.assertFalse(NSOInterfaceIPState.objects.filter(pk__in=[row.pk for row in old_states]).exists())
        self.assertFalse(IPAddress.objects.filter(vrf=old_vrf, assigned_object_id__isnull=False).exists())
        self.assertEqual(IPAddress.objects.filter(vrf=old_vrf).count(), address_count)
        self.assertEqual(Interface.objects.filter(device=device).count(), interface_count)
