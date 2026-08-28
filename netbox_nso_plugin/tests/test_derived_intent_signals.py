# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for derived-intent writer and signal behavior.

Uses a real Django/NetBox DB. Exercises the exact recompute helper, foreign-neutral
cable and interface signals, and the full
loop-termination property via django.test.override_settings to inject
sentinel templates into the AppConfig.
"""

import logging
from unittest.mock import patch

from dcim.models import (
    Cable,
    CableTermination,
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Site,
)
from django.apps import apps
from django.test import TestCase

from netbox_nso_plugin.derived_intent import (
    SentinelTemplate,
)
from netbox_nso_plugin.models import NSODerivedIntentTemplate
from netbox_nso_plugin.signals import (
    _affected_interfaces,
    _recompute_on_cable_change,
    _recompute_on_interface_save,
    _recompute_one,
    _templates,
)

SENTINEL_AUTO = SentinelTemplate(
    sentinel="[auto]",
    template="[auto] to {peer_host}:{peer_iface}",
)
TEMPLATES = [SENTINEL_AUTO]


class TestDatabaseTemplateLoading(TestCase):
    """Derived intent uses live NetBox data so operators can change it without a restart."""

    def test_templates_are_loaded_from_the_database(self):
        template_model = apps.get_model("netbox_nso_plugin", "NSODerivedIntentTemplate")
        template_model.objects.create(
            sentinel="[auto]",
            template="[auto] to {peer_host}:{peer_iface}",
        )

        self.assertEqual(
            _templates(),
            [SentinelTemplate(sentinel="[auto]", template="[auto] to {peer_host}:{peer_iface}")],
        )


def _make_device(name, dt, role, site):
    return Device.objects.create(name=name, device_type=dt, role=role, site=site)


def _make_iface(device, name, iface_type="1000base-t"):
    return Interface.objects.create(device=device, name=name, type=iface_type)


def _make_cable(iface_a, iface_b):
    cable = Cable.objects.create(status="connected")
    CableTermination.objects.create(cable=cable, cable_end="A", termination=iface_a)
    CableTermination.objects.create(cable=cable, cable_end="B", termination=iface_b)
    return cable


def _configure_templates(templates):
    """Store the enabled derived-intent templates used by one test."""
    NSODerivedIntentTemplate.objects.all().delete()
    NSODerivedIntentTemplate.objects.bulk_create(
        NSODerivedIntentTemplate(sentinel=item.sentinel, template=item.template) for item in templates
    )


class TestRecomputeOne(TestCase):
    """Tests for the _recompute_one helper."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RcpMfg", slug="rcpmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RcpDev", slug="rcpdev")
        role = DeviceRole.objects.create(name="RcpRole", slug="rcprole")
        site = Site.objects.create(name="RcpSite", slug="rcpsite")
        cls.dev1 = _make_device("rcp-dev1", dt, role, site)
        cls.dev2 = _make_device("rcp-dev2", dt, role, site)

    def test_recompute_updates_managed_interface(self):
        """The recompute writes through the caller's writer, the way its call sites gate it."""
        import copy

        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
            renderer_writes,
        )

        from ._outbox_case import without_commit_drain

        iface1 = _make_iface(self.dev1, "Gi0/1-rcp")
        iface2 = _make_iface(self.dev2, "Gi0/2-rcp")
        _make_cable(iface1, iface2)
        iface1.description = "[auto]"
        iface1.save(update_fields=["description"])

        iface1_fresh = Interface.objects.get(pk=iface1.pk)
        planned = copy.copy(iface1_fresh)
        planned.description = "[auto] to rcp-dev2:Gi0/2-rcp"
        plan = RendererMutationPlan.build(saves=(planned_save(planned, update_fields=("description",)),))
        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
        with without_commit_drain(), mutation(plan):
            _recompute_one(iface1_fresh, TEMPLATES)

        iface1_after = Interface.objects.get(pk=iface1.pk)
        self.assertEqual(iface1_after.description, "[auto] to rcp-dev2:Gi0/2-rcp")

    def test_recompute_skips_unmanaged_interface(self):
        iface = _make_iface(self.dev1, "Gi0/3-unmanaged")
        iface.description = "static description"
        iface.save(update_fields=["description"])

        _recompute_one(iface, TEMPLATES)

        iface_after = Interface.objects.get(pk=iface.pk)
        self.assertEqual(iface_after.description, "static description")

    def test_recompute_idempotent_no_second_write(self):
        """If the computed value equals the current value, no save occurs."""
        iface1 = _make_iface(self.dev1, "Gi0/4-idm")
        iface2 = _make_iface(self.dev2, "Gi0/5-idm")
        _make_cable(iface1, iface2)
        iface1.description = "[auto] to rcp-dev2:Gi0/5-idm"
        iface1.save(update_fields=["description"])

        iface1_fresh = Interface.objects.get(pk=iface1.pk)
        with patch.object(type(iface1_fresh), "save", wraps=iface1_fresh.save) as mock_save:
            _recompute_one(iface1_fresh, TEMPLATES)
        mock_save.assert_not_called()

    def test_recompute_handles_exception_gracefully(self):
        """compute_description exception is logged; no write; no re-raise."""
        iface = _make_iface(self.dev1, "Gi0/6-exc")
        iface.description = "[auto]"
        iface.save(update_fields=["description"])

        with patch(
            "netbox_nso_plugin.derived_intent.compute_description",
            side_effect=RuntimeError("oops"),
        ):
            with self.assertLogs("netbox_nso_plugin.signals", level=logging.ERROR):
                _recompute_one(iface, TEMPLATES)

        iface_after = Interface.objects.get(pk=iface.pk)
        self.assertEqual(iface_after.description, "[auto]")

    def test_recompute_skips_none_result_from_compute(self):
        """_recompute_one leaves interface alone when compute_description returns None (skip)."""
        lag = _make_iface(self.dev1, "ae0-rcp", iface_type="lag")
        member = Interface.objects.create(device=self.dev1, name="Gi0/8-lag-rcp", type="1000base-t", lag=lag)
        member.description = "[auto]"
        member.save(update_fields=["description"])

        _recompute_one(member, TEMPLATES)  # compute_description → None (lag skip)

        member_after = Interface.objects.get(pk=member.pk)
        self.assertEqual(member_after.description, "[auto]")  # unchanged
        """Empty templates = feature off; _recompute_one never calls compute."""
        iface = _make_iface(self.dev1, "Gi0/7-off")
        iface.description = "[auto]"
        iface.save(update_fields=["description"])
        with patch("netbox_nso_plugin.derived_intent.compute_description") as mock_compute:
            _recompute_one(iface, [])
        mock_compute.assert_not_called()


class TestCableSignalHandlers(TestCase):
    """Tests for cable post_save and post_delete signal handlers."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="CblSigMfg", slug="cblsigmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="CblSigDev", slug="cblsigdev")
        role = DeviceRole.objects.create(name="CblSigRole", slug="cblsigrole")
        site = Site.objects.create(name="CblSigSite", slug="cblsigsite")
        cls.dev1 = _make_device("cblsig-dev1", dt, role, site)
        cls.dev2 = _make_device("cblsig-dev2", dt, role, site)

    def test_foreign_cable_save_does_not_recompute(self):
        iface1 = _make_iface(self.dev1, "Gi0/1-cbl")
        iface2 = _make_iface(self.dev2, "Gi0/2-cbl")
        iface1.description = "[auto]"
        iface1.save(update_fields=["description"])
        iface2.description = "[auto]"
        iface2.save(update_fields=["description"])

        _configure_templates(TEMPLATES)

        cable = _make_cable(iface1, iface2)
        cable_fresh = Cable.objects.prefetch_related("terminations__termination").get(pk=cable.pk)
        _recompute_on_cable_change(sender=Cable, instance=cable_fresh)

        iface1_after = Interface.objects.get(pk=iface1.pk)
        iface2_after = Interface.objects.get(pk=iface2.pk)
        self.assertEqual(iface1_after.description, "[auto]")
        self.assertEqual(iface2_after.description, "[auto]")

    def test_foreign_cable_delete_does_not_recompute(self):
        iface1 = _make_iface(self.dev1, "Gi0/3-cbl")
        iface2 = _make_iface(self.dev2, "Gi0/4-cbl")
        iface1.description = "[auto] to cblsig-dev2:Gi0/4-cbl"
        iface1.save(update_fields=["description"])
        iface2.description = "[auto] to cblsig-dev1:Gi0/3-cbl"
        iface2.save(update_fields=["description"])

        _configure_templates(TEMPLATES)

        cable = _make_cable(iface1, iface2)
        cable_with_terms = Cable.objects.prefetch_related("terminations__termination").get(pk=cable.pk)
        cable_with_terms.delete()

        iface1_final = Interface.objects.get(pk=iface1.pk)
        iface2_final = Interface.objects.get(pk=iface2.pk)
        self.assertEqual(iface1_final.description, "[auto] to cblsig-dev2:Gi0/4-cbl")
        self.assertEqual(iface2_final.description, "[auto] to cblsig-dev1:Gi0/3-cbl")

    def test_cable_handler_noop_when_feature_off(self):
        iface1 = _make_iface(self.dev1, "Gi0/5-off")
        iface2 = _make_iface(self.dev2, "Gi0/6-off")
        iface1.description = "[auto]"
        iface1.save(update_fields=["description"])

        _configure_templates([])
        cable = _make_cable(iface1, iface2)
        cable_fresh = Cable.objects.prefetch_related("terminations__termination").get(pk=cable.pk)
        _recompute_on_cable_change(sender=Cable, instance=cable_fresh)

        iface1_after = Interface.objects.get(pk=iface1.pk)
        self.assertEqual(iface1_after.description, "[auto]")


class TestAffectedInterfaces(TestCase):
    """Tests for _affected_interfaces helper."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="AffMfg", slug="affmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="AffDev", slug="affdev")
        role = DeviceRole.objects.create(name="AffRole", slug="affrole")
        site = Site.objects.create(name="AffSite", slug="affsite")
        cls.dev1 = _make_device("aff-dev1", dt, role, site)
        cls.dev2 = _make_device("aff-dev2", dt, role, site)

    def test_yields_both_interface_endpoints(self):
        iface1 = _make_iface(self.dev1, "Gi0/1-aff")
        iface2 = _make_iface(self.dev2, "Gi0/2-aff")
        cable = _make_cable(iface1, iface2)
        cable_fresh = Cable.objects.prefetch_related("terminations__termination").get(pk=cable.pk)
        result = list(_affected_interfaces(cable_fresh))
        pks = {i.pk for i in result}
        self.assertIn(iface1.pk, pks)
        self.assertIn(iface2.pk, pks)
        self.assertEqual(len(result), 2)


class TestInterfaceSaveHandler(TestCase):
    """Tests for _recompute_on_interface_save."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="IfSaveMfg", slug="ifsavemfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="IfSaveDev", slug="ifsavedev")
        role = DeviceRole.objects.create(name="IfSaveRole", slug="ifsaverole")
        site = Site.objects.create(name="IfSaveSite", slug="ifsavesite")
        cls.dev1 = _make_device("ifsave-dev1", dt, role, site)
        cls.dev2 = _make_device("ifsave-dev2", dt, role, site)

    def test_foreign_interface_save_does_not_recompute(self):
        iface1 = _make_iface(self.dev1, "Gi0/1-ifs")
        iface2 = _make_iface(self.dev2, "Gi0/2-ifs")
        _make_cable(iface1, iface2)
        iface1.description = "[auto]"
        iface1.save(update_fields=["description"])

        _configure_templates(TEMPLATES)

        iface1_fresh = Interface.objects.get(pk=iface1.pk)
        _recompute_on_interface_save(sender=Interface, instance=iface1_fresh, created=False)

        iface1_after = Interface.objects.get(pk=iface1.pk)
        self.assertEqual(iface1_after.description, "[auto]")

    def test_interface_save_noop_when_feature_off(self):
        iface = _make_iface(self.dev1, "Gi0/3-off")
        iface.description = "[auto]"
        iface.save(update_fields=["description"])

        _configure_templates([])
        _recompute_on_interface_save(sender=Interface, instance=iface, created=False)

        iface_after = Interface.objects.get(pk=iface.pk)
        self.assertEqual(iface_after.description, "[auto]")
