# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the logging/syslog read-path: _reconcile_logging_config + category counts."""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Platform, Site
from django.test import TestCase

from .mixins import IntentPushResetMixin


def _make_device(suffix="log"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"LogMfg{suffix}", slug=f"logmfg{suffix}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"LogDev{suffix}", slug=f"logdev{suffix}")
    role, _ = DeviceRole.objects.get_or_create(name=f"LogRole{suffix}", slug=f"logrole{suffix}")
    site, _ = Site.objects.get_or_create(name=f"LogSite{suffix}", slug=f"logsite{suffix}")
    return Device.objects.create(name=f"log-rtr-{suffix}", device_type=dt, role=role, site=site)


class TestReconcileLoggingConfig(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("main")

    def _mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="log-inst", defaults={"adapter_instance_id": "log-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "log-dev", "adapter_device_id": self.device.pk},
        )[0]

    def _payload(self, *hosts):
        return {"hosts": list(hosts), "last_refreshed_at": None, "refresh_source": "test"}

    def test_no_mgmt_returns_empty(self):
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        res = _reconcile_logging_config(self.device, self._payload({"address": "10.0.0.1"}))
        self.assertEqual(res["hosts"], [])

    def test_creates_host(self):
        self._mgmt()
        from netbox_nso_plugin.models import NSOLoggingHostState
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        res = _reconcile_logging_config(
            self.device,
            self._payload({"address": "198.18.251.86", "severity": "warning", "facility": "any", "source": "1.1.1.1"}),
        )
        self.assertEqual(len(res["hosts"]), 1)
        h = NSOLoggingHostState.objects.get(management__device=self.device, address="198.18.251.86")
        self.assertEqual(h.severity, "warning")
        self.assertEqual(h.source, "1.1.1.1")
        self.assertEqual(h.status, "imported")

    def test_full_replace_deletes_absent(self):
        self._mgmt()
        from netbox_nso_plugin.models import NSOLoggingHostState
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        _reconcile_logging_config(self.device, self._payload({"address": "10.0.0.1"}, {"address": "10.0.0.2"}))
        self.assertEqual(NSOLoggingHostState.objects.filter(management__device=self.device).count(), 2)
        _reconcile_logging_config(self.device, self._payload({"address": "10.0.0.2"}))
        addrs = set(
            NSOLoggingHostState.objects.filter(management__device=self.device).values_list("address", flat=True)
        )
        self.assertEqual(addrs, {"10.0.0.2"})

    def test_idempotent_update(self):
        self._mgmt()
        from netbox_nso_plugin.models import NSOLoggingHostState
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        _reconcile_logging_config(self.device, self._payload({"address": "10.0.0.1", "severity": "info"}))
        _reconcile_logging_config(self.device, self._payload({"address": "10.0.0.1", "severity": "error"}))
        rows = NSOLoggingHostState.objects.filter(management__device=self.device)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().severity, "error")

    def test_omitted_value_suppressed_default_port_matches_owned_intent(self):
        from netbox_nso_plugin.models import NSOLoggingHostState, NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        mgmt = self._mgmt()
        platform = Platform.objects.create(name="Logging Nokia", slug="logging-nokia")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        row = NSOLoggingHostState.objects.create(
            management=mgmt,
            address="198.18.0.20",
            port=514,
            status="accepted",
        )

        _reconcile_logging_config(self.device, self._payload({"address": row.address}))

        row.refresh_from_db()
        self.assertEqual(row.port, 514)
        self.assertEqual(row.status, "in_sync")

    def test_omitted_provenance_explicit_facility_does_not_false_settle(self):
        from netbox_nso_plugin.models import NSOLoggingHostState, NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        mgmt = self._mgmt()
        platform = Platform.objects.create(name="Logging Nokia facility", slug="logging-nokia-facility")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        row = NSOLoggingHostState.objects.create(
            management=mgmt,
            address="198.18.0.21",
            facility="local7",
            status="accepted",
        )

        _reconcile_logging_config(self.device, self._payload({"address": row.address}))

        row.refresh_from_db()
        self.assertEqual(row.facility, "local7")
        self.assertEqual(row.status, "accepted")

    def test_nx_device_default_facility_settles_and_pushes_explicit_intent(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOLoggingHostState, NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        mgmt = self._mgmt()
        platform = Platform.objects.create(name="Logging NX default facility", slug="logging-nx-default-facility")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="cisco-nx-cli-5.32")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        row = NSOLoggingHostState.objects.create(
            management=mgmt,
            address="198.18.0.22",
            facility="local7",
            status="accepted",
        )

        with patch("netbox_nso_plugin.adapter_client.put_logging_intent") as put:
            deliver("logging", self.device.pk, mgmt.adapter_device_id)
        # Keep the explicit semantic intent on the write path: the NSO
        # reconciler needs local7 to retract a brownfield non-default facility.
        # Only observed-state comparison canonicalizes local7 to omission.
        self.assertEqual(put.call_args.args[1][0]["facility"], "local7")

        _reconcile_logging_config(self.device, self._payload({"address": row.address}))

        row.refresh_from_db()
        self.assertEqual(row.facility, "local7")
        self.assertEqual(row.status, "in_sync")

    def test_owned_settle_does_not_clobber_concurrent_operator_edit(self):
        """An operator edit landing between the reconciler's row load and its write must survive
        WHOLE: its field values AND its 'accepted' status. The settle was computed against the
        pre-edit values, so writing it would green-light intent the device has never seen."""
        from django.db.models.signals import post_init

        from netbox_nso_plugin.models import NSOLoggingHostState, NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        mgmt = self._mgmt()
        platform = Platform.objects.create(name="Logging Nokia concurrent", slug="logging-nokia-concurrent")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        row = NSOLoggingHostState.objects.create(
            management=mgmt,
            address="198.18.0.25",
            port=514,
            severity="warning",
            status="accepted",
        )

        fired = []

        def _concurrent_editor(sender, instance, **kwargs):
            # post_init = the reconciler has just SELECTed the row, so its in-memory copy is
            # already stale when the edit lands. .update() writes straight to the DB: no
            # post_init, hence no recursion.
            # Only the row under test: the loop's get_or_create instantiates others too.
            if fired or instance.pk != row.pk:
                return
            fired.append(True)
            NSOLoggingHostState.objects.filter(pk=instance.pk).update(severity="error")

        post_init.connect(_concurrent_editor, sender=NSOLoggingHostState, weak=False)
        self.addCleanup(post_init.disconnect, _concurrent_editor, sender=NSOLoggingHostState)

        _reconcile_logging_config(self.device, self._payload({"address": row.address, "severity": "warning"}))

        row.refresh_from_db()
        self.assertEqual(row.severity, "error")
        self.assertEqual(row.port, 514)
        self.assertEqual(row.status, "accepted")

    def test_owned_settle_does_not_follow_a_concurrent_address_rename(self):
        """The CAS must guard the row's IDENTITY, not just its values: a host renamed between
        the reconciler's SELECT and its write was confirmed at the OLD address only, so the
        settle belongs to a host this row no longer is."""
        from django.db.models.signals import post_init

        from netbox_nso_plugin.models import NSOLoggingHostState, NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        mgmt = self._mgmt()
        platform = Platform.objects.create(name="Logging Nokia rename", slug="logging-nokia-rename")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        row = NSOLoggingHostState.objects.create(
            management=mgmt,
            address="198.18.0.26",
            port=514,
            severity="warning",
            status="accepted",
        )

        fired = []

        def _concurrent_renamer(sender, instance, **kwargs):
            # Only the row under test: the loop's get_or_create instantiates others too.
            if fired or instance.pk != row.pk:
                return
            fired.append(True)
            NSOLoggingHostState.objects.filter(pk=instance.pk).update(address="198.18.0.98")

        post_init.connect(_concurrent_renamer, sender=NSOLoggingHostState, weak=False)
        self.addCleanup(post_init.disconnect, _concurrent_renamer, sender=NSOLoggingHostState)

        _reconcile_logging_config(self.device, self._payload({"address": "198.18.0.26", "severity": "warning"}))

        row.refresh_from_db()
        self.assertEqual(row.address, "198.18.0.98")
        self.assertEqual(row.status, "accepted")

    def test_timos_writer_and_reader_tokens_settle_to_one_canonical_value(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOLoggingHostState, NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        mgmt = self._mgmt()
        platform = Platform.objects.create(name="Logging Nokia tokens", slug="logging-nokia-tokens")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        row = NSOLoggingHostState.objects.create(
            management=mgmt,
            address="198.18.0.24",
            severity="INFORMATIONAL",
            facility="LOCAL6",
            status="accepted",
        )

        with patch("netbox_nso_plugin.adapter_client.put_logging_intent") as put:
            deliver("logging", self.device.pk, mgmt.adapter_device_id)
        host = put.call_args.args[1][0]
        self.assertEqual(host["severity"], "info")
        self.assertEqual(host["facility"], "local6")

        _reconcile_logging_config(
            self.device,
            self._payload({"address": row.address, "severity": "info", "facility": "local6"}),
        )

        row.refresh_from_db()
        self.assertEqual(row.severity, "INFORMATIONAL")
        self.assertEqual(row.facility, "LOCAL6")
        self.assertEqual(row.status, "in_sync")

    def test_junos_writer_and_reader_tokens_settle_to_one_canonical_value(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOLoggingHostState, NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        mgmt = self._mgmt()
        platform = Platform.objects.create(name="Logging Junos tokens", slug="logging-junos-tokens")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="juniper-junos-nc-4.19")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        row = NSOLoggingHostState.objects.create(
            management=mgmt,
            address="198.18.0.25",
            severity="INFORMATIONAL",
            facility="LOCAL7",
            status="accepted",
        )

        with patch("netbox_nso_plugin.adapter_client.put_logging_intent") as put:
            deliver("logging", self.device.pk, mgmt.adapter_device_id)
        host = put.call_args.args[1][0]
        self.assertEqual(host["severity"], "info")
        self.assertEqual(host["facility"], "local7")

        _reconcile_logging_config(
            self.device,
            self._payload({"address": row.address, "severity": "info", "facility": "local7"}),
        )

        row.refresh_from_db()
        self.assertEqual(row.status, "in_sync")

    def test_arcos_writer_and_reader_tokens_settle_to_one_canonical_value(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOLoggingHostState, NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        mgmt = self._mgmt()
        platform = Platform.objects.create(name="Logging ArcOS tokens", slug="logging-arcos-tokens")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="arcos-cli-6.2")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        row = NSOLoggingHostState.objects.create(
            management=mgmt,
            address="198.18.0.26",
            severity="informational",
            facility="any",
            status="accepted",
        )

        with patch("netbox_nso_plugin.adapter_client.put_logging_intent") as put:
            deliver("logging", self.device.pk, mgmt.adapter_device_id)
        host = put.call_args.args[1][0]
        self.assertEqual(host["severity"], "INFORMATIONAL")
        self.assertEqual(host["facility"], "ALL")

        _reconcile_logging_config(
            self.device,
            self._payload({"address": row.address, "severity": "INFORMATIONAL", "facility": "ALL"}),
        )

        row.refresh_from_db()
        self.assertEqual(row.status, "in_sync")

    def test_cisco_writer_and_reader_tokens_settle_to_one_canonical_value(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOLoggingHostState, NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        mgmt = self._mgmt()
        for suffix, ned_id in (
            ("ios", "cisco-ios-cli-6.114"),
            ("iosxr", "cisco-iosxr-cli-7.76"),
        ):
            with self.subTest(ned_id=ned_id):
                platform = Platform.objects.create(
                    name=f"Logging {suffix} tokens",
                    slug=f"logging-{suffix}-tokens",
                )
                NSOPlatformNedMapping.objects.create(platform=platform, ned_id=ned_id)
                self.device.platform = platform
                self.device.save(update_fields=["platform"])
                row = NSOLoggingHostState.objects.create(
                    management=mgmt,
                    address=f"198.18.1.{10 if suffix == 'ios' else 11}",
                    severity="INFORMATIONAL",
                    facility="LOCAL5",
                    status="accepted",
                )

                with patch("netbox_nso_plugin.adapter_client.put_logging_intent") as put:
                    deliver("logging", self.device.pk, mgmt.adapter_device_id)
                host = put.call_args.args[1][0]
                self.assertEqual(host["severity"], "informational")
                self.assertEqual(host["facility"], "local5")

                _reconcile_logging_config(
                    self.device,
                    self._payload(
                        {
                            "address": row.address,
                            "severity": "informational",
                            "facility": "local5",
                        }
                    ),
                )
                row.refresh_from_db()
                self.assertEqual(row.status, "in_sync")
                row.delete()

    def test_value_suppressed_default_port_stays_absent_in_push_payload(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOLoggingHostState, NSOPlatformNedMapping

        mgmt = self._mgmt()
        platform = Platform.objects.create(name="Logging push Nokia", slug="logging-push-nokia")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        NSOLoggingHostState.objects.create(
            management=mgmt,
            address="198.18.0.22",
            port=514,
            status="accepted",
        )

        with patch("netbox_nso_plugin.adapter_client.put_logging_intent") as put:
            deliver("logging", self.device.pk, mgmt.adapter_device_id)

        host = put.call_args.args[1][0]
        self.assertNotIn("port", host)

    def test_default_free_family_preserves_explicit_conventional_port(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOLoggingHostState, NSOPlatformNedMapping

        mgmt = self._mgmt()
        platform = Platform.objects.create(name="Logging push Junos", slug="logging-push-junos")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="juniper-junos-nc-4.19")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        NSOLoggingHostState.objects.create(
            management=mgmt,
            address="198.18.0.23",
            port=514,
            status="accepted",
        )

        with patch("netbox_nso_plugin.adapter_client.put_logging_intent") as put:
            deliver("logging", self.device.pk, mgmt.adapter_device_id)

        self.assertEqual(put.call_args.args[1][0]["port"], 514)

    def test_category_appears_with_counts(self):
        mgmt = self._mgmt()
        mgmt.manage_logging = True
        mgmt.save()
        from netbox_nso_plugin.summary import category_summaries
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        _reconcile_logging_config(self.device, self._payload({"address": "10.0.0.1"}))
        cats = {c["key"]: c for c in category_summaries(self.device, mgmt)}
        self.assertIn("logging", cats)
        self.assertEqual(cats["logging"]["counts"]["total"], 1)
