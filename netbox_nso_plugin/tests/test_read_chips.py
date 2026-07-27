# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""READSEM S4 Slice B4 — D10 UI honesty: per-category read chips.

Red-first: the D10 state matrix (healthy = fresh-present OR authoritative-empty —
a successful clear must never render unknown), refresh-pending when observed >
applied (never healthy from an unapplied observation), non-adopted/legacy rows
ignored, worst-first merge across a category's families (D8), BFD visible for any
of bgp/isis/ospf (R3-7), the counts endpoint carrying the chip states for the
dynamic renderBadges path, the durable reset-pending banner (R11/R12), and the
tab's live read-state fetch (R5-5 short timeout; observe-only upsert; adapter-down
falls back to persisted chips).
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from netbox_nso_plugin.models import NSODeviceManagement, NSOFamilyReadState, NSOInstance

User = get_user_model()

_INC = ("66666666-ffff-4fff-8fff-666666666666", "2026-07-02T00:00:00Z")
_INC_NEW = ("77777777-aaaa-4aaa-8aaa-777777777777", "2026-07-03T00:00:00Z")


def _make(tag, **mgmt_flags):
    mfg = Manufacturer.objects.create(name=f"{tag}Mfg", slug=f"{tag}mfg")
    dt = DeviceType.objects.create(manufacturer=mfg, model=f"{tag}Dev", slug=f"{tag}dev")
    role = DeviceRole.objects.create(name=f"{tag}Role", slug=f"{tag}role")
    site = Site.objects.create(name=f"{tag}Site", slug=f"{tag}site")
    device = Device.objects.create(name=f"{tag}-rtr", device_type=dt, role=role, site=site)
    inst = NSOInstance.objects.create(name=f"{tag}-inst", adapter_instance_id=f"{tag}-inst")
    mgmt = NSODeviceManagement.objects.create(
        device=device,
        nso_instance=inst,
        nso_device_name=f"{tag}-rtr",
        adapter_device_id=device.pk,
        adapter_incarnation=_INC[0],
        adapter_incarnation_born=parse_datetime(_INC[1]),
        **mgmt_flags,
    )
    return device, mgmt


def _row(
    mgmt,
    family,
    *,
    outcome="present",
    reason="",
    freshness="fresh",
    result="replaced",
    succeeded=True,
    observed_attempt_id=5,
    applied_attempt_id=5,
    payload_revision=5,
    incarnation=None,
):
    inc = incarnation if incarnation is not None else mgmt.adapter_incarnation
    return NSOFamilyReadState.objects.create(
        management=mgmt,
        family=family,
        observed_outcome=outcome,
        observed_reason=reason or "",
        observed_freshness=freshness or "",
        observed_result=result or "",
        observed_succeeded=succeeded,
        observed_attempt_id=observed_attempt_id,
        observed_incarnation=inc,
        observed_incarnation_born=mgmt.adapter_incarnation_born,
        observed_epoch=mgmt.adapter_device_id,
        observed_payload_revision=payload_revision,
        applied_attempt_id=applied_attempt_id,
        applied_incarnation=inc if applied_attempt_id is not None else "",
        admitted_payload_revision=payload_revision,
        applied_payload_revision=payload_revision,
        publication_sequence=1,
        applied_publication_sequence=1,
    )


def _chip(device, mgmt, key):
    from netbox_nso_plugin.summary import category_summaries

    for cat in category_summaries(device, mgmt):
        if cat["key"] == key:
            return cat.get("read")
    return "CATEGORY-ABSENT"


class TestFamilyChipMatrix(TestCase):
    """The D10 per-state matrix, driven through category_summaries (public surface)."""

    def setUp(self):
        self.device, self.mgmt = _make(f"ch{uuid.uuid4().hex[:6]}", manage_l2=True)

    def test_fresh_present_is_healthy_no_chip(self):
        _row(self.mgmt, "l2_service")
        self.assertIsNone(_chip(self.device, self.mgmt, "l2_services"))

    def test_authoritative_empty_is_healthy_no_chip(self):
        """R2-7: a successful clear must NEVER render unknown despite null freshness."""
        _row(
            self.mgmt,
            "l2_service",
            outcome="absent_authoritative",
            freshness="",
            result="cleared",
        )
        self.assertIsNone(_chip(self.device, self.mgmt, "l2_services"))

    def test_missing_payload_revision_renders_unproven(self):
        """A compatibility publication may apply, but must not look atomically proven."""
        _row(self.mgmt, "l2_service", payload_revision=None)
        chip = _chip(self.device, self.mgmt, "l2_services")
        self.assertEqual(chip["state"], "unproven")

    def test_stale_renders_amber_last_known(self):
        _row(self.mgmt, "l2_service", freshness="stale")
        chip = _chip(self.device, self.mgmt, "l2_services")
        self.assertEqual(chip["state"], "stale")

    def test_unavailable_reasons_render_red(self):
        for reason in ("export_down", "read_error", "not_ready"):
            NSOFamilyReadState.objects.filter(management=self.mgmt).delete()
            _row(
                self.mgmt,
                "l2_service",
                outcome="unavailable",
                reason=reason,
                freshness="",
                result="kept",
                succeeded=False,
            )
            chip = _chip(self.device, self.mgmt, "l2_services")
            self.assertEqual(chip["state"], "unavailable", reason)

    def test_not_authoritative_and_unsupported_render_muted(self):
        for reason, state in (("not_authoritative", "not_authoritative"), ("unsupported", "unsupported")):
            NSOFamilyReadState.objects.filter(management=self.mgmt).delete()
            _row(
                self.mgmt,
                "l2_service",
                outcome="unavailable",
                reason=reason,
                freshness="",
                result="kept",
                succeeded=True,
            )
            chip = _chip(self.device, self.mgmt, "l2_services")
            self.assertEqual(chip["state"], state)

    def test_result_error_renders_red(self):
        _row(self.mgmt, "l2_service", result="error", succeeded=False)
        chip = _chip(self.device, self.mgmt, "l2_services")
        self.assertEqual(chip["state"], "unavailable")

    def test_unknown_future_value_renders_unknown(self):
        _row(self.mgmt, "l2_service", outcome="quantum_flux")
        chip = _chip(self.device, self.mgmt, "l2_services")
        self.assertEqual(chip["state"], "unknown")

    def test_observed_newer_than_applied_is_refresh_pending_never_healthy(self):
        """R5-3: the 12-vs-11 scenario — a healthy OBSERVATION with older APPLIED
        rows must render refresh-pending, not healthy."""
        _row(self.mgmt, "l2_service", observed_attempt_id=12, applied_attempt_id=11)
        chip = _chip(self.device, self.mgmt, "l2_services")
        self.assertEqual(chip["state"], "refresh_pending")

    def test_missing_row_on_an_adopted_device_renders_no_authoritative_read(self):
        """codex B5-R2-3: a family with NO read-state row at all on an ADOPTED device
        (e.g. pre-S4 overlay data right after the 0014 upgrade) must not render
        healthy — it gets the muted 'no authoritative read' chip until first read."""
        chip = _chip(self.device, self.mgmt, "l2_services")
        self.assertIsNotNone(chip)
        self.assertEqual(chip["state"], "not_authoritative")

    def test_missing_row_on_a_never_adopted_device_renders_no_chip(self):
        """Pre-S4 continuity: no adopted incarnation → no chips at all (R1-F9)."""
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(
            adapter_incarnation="", adapter_incarnation_born=None
        )
        self.mgmt.refresh_from_db()
        self.assertIsNone(_chip(self.device, self.mgmt, "l2_services"))

    def test_legacy_blank_row_renders_no_chip(self):
        # legacy row (blank outcome, e.g. a pre-S4 rollback blanked it) — ignored
        _row(self.mgmt, "l2_service", outcome="", freshness="", result="", succeeded=None)
        self.assertIsNone(_chip(self.device, self.mgmt, "l2_services"))

    def test_non_adopted_incarnation_rows_are_ignored(self):
        _row(self.mgmt, "l2_service", freshness="stale", incarnation="dead-beef")
        self.assertIsNone(_chip(self.device, self.mgmt, "l2_services"))

    def test_reset_pending_marker_forces_device_wide_state(self):
        """R11/R12: while a reset is pending, old-incarnation rows never render
        healthy — every category shows the reset-pending state."""
        _row(self.mgmt, "l2_service")  # would be healthy
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(
            reset_pending_incarnation=_INC_NEW[0],
            reset_pending_born=parse_datetime(_INC_NEW[1]),
        )
        self.mgmt.refresh_from_db()
        chip = _chip(self.device, self.mgmt, "l2_services")
        self.assertEqual(chip["state"], "reset_pending")


class TestCategoryMergeAndVisibility(TestCase):
    def test_worst_first_merge_across_families(self):
        device, mgmt = _make(f"cm{uuid.uuid4().hex[:6]}", manage_interfaces=True)
        _row(mgmt, "interface_attributes")  # healthy
        _row(
            mgmt,
            "interface_mtu",
            outcome="unavailable",
            reason="export_down",
            freshness="",
            result="kept",
            succeeded=False,
        )
        _row(mgmt, "switchport", freshness="stale")
        chip = _chip(device, mgmt, "interface")
        self.assertEqual(chip["state"], "unavailable")  # worst wins

    def test_bfd_visible_for_any_of_bgp_isis_ospf(self):
        """R3-7: BFD is reconciled whenever ANY of bgp/isis/ospf is managed — its
        category must be visible for BGP-only and OSPF-only devices too."""
        from netbox_nso_plugin.summary import category_summaries

        for flag in ("manage_isis", "manage_bgp", "manage_ospf"):
            device, mgmt = _make(f"cb{uuid.uuid4().hex[:4]}{flag[-4:]}", manage_routing=True, **{flag: True})
            keys = {c["key"] for c in category_summaries(device, mgmt)}
            self.assertIn("bfd", keys, flag)

    def test_bfd_hidden_without_any_rider_protocol(self):
        device, mgmt = _make(f"cn{uuid.uuid4().hex[:6]}", manage_routing=True, manage_static=True)
        from netbox_nso_plugin.summary import category_summaries

        keys = {c["key"] for c in category_summaries(device, mgmt)}
        self.assertNotIn("bfd", keys)


class TestCountsEndpointCarriesReadState(TestCase):
    def setUp(self):
        self.device, self.mgmt = _make(f"cc{uuid.uuid4().hex[:6]}", manage_l2=True)
        self.user = User.objects.create_superuser(username=f"cc-{uuid.uuid4().hex[:6]}")
        self.client.force_login(self.user)

    def _get(self):
        url = reverse("plugins:netbox_nso_plugin:device_nso_category_counts", kwargs={"device_pk": self.device.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_counts_payload_includes_chip_state(self):
        _row(self.mgmt, "l2_service", freshness="stale")
        data = self._get()
        self.assertEqual(data["categories"]["l2_services"]["read"]["state"], "stale")

    def test_healthy_family_serializes_null_chip(self):
        _row(self.mgmt, "l2_service")
        data = self._get()
        self.assertIsNone(data["categories"]["l2_services"]["read"])

    def test_device_reset_pending_flag_rides_the_payload(self):
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(
            reset_pending_incarnation=_INC_NEW[0],
            reset_pending_born=parse_datetime(_INC_NEW[1]),
        )
        data = self._get()
        self.assertTrue(data["reset_pending"])


class TestTabLiveReadStateFetch(TestCase):
    """D8(b): the tab render fetches /read-state LIVE beside get_device with a SHORT
    budget; success upserts observed_* through the R6-3 protocol; failure falls back
    to persisted chips (get_device's sync cache stays valid)."""

    def setUp(self):
        self.device, self.mgmt = _make(f"ct{uuid.uuid4().hex[:6]}", manage_l2=True)
        self.user = User.objects.create_superuser(username=f"ct-{uuid.uuid4().hex[:6]}")
        self.client.force_login(self.user)

    def _tab(self, read_state_side_effect=None, read_state_return=None):
        from netbox_nso_plugin.adapter_client import AdapterError  # noqa: F401 (side effects use it)

        url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
        with (
            patch("netbox_nso_plugin.adapter_client.get_device", return_value={"id": self.mgmt.adapter_device_id}),
            patch("netbox_nso_plugin.adapter_client.get_device_capability", return_value={"known": False}),
            patch(
                "netbox_nso_plugin.adapter_client.get_device_read_state",
                side_effect=read_state_side_effect,
                return_value=read_state_return,
            ) as m_rs,
        ):
            resp = self.client.get(url)
        return resp, m_rs

    def test_success_upserts_observed_rows(self):
        families = {
            "l2_service": {
                "outcome": "present",
                "reason": None,
                "freshness": "fresh",
                "result": "replaced",
                "succeeded": True,
                "read_at": "2026-07-21T10:00:00Z",
                "attempt_id": 42,
                "incarnation": _INC[0],
                "incarnation_born": _INC[1],
            }
        }
        resp, m_rs = self._tab(
            read_state_return={"device_id": self.mgmt.adapter_device_id, "families_version": 1, "families": families}
        )
        self.assertEqual(resp.status_code, 200)
        m_rs.assert_called_once()
        row = NSOFamilyReadState.objects.get(management=self.mgmt, family="l2_service")
        self.assertEqual(row.observed_attempt_id, 42)
        self.assertIsNone(row.applied_attempt_id)  # observation NEVER touches applied

    def test_adapter_error_falls_back_to_persisted(self):
        from netbox_nso_plugin.adapter_client import AdapterError

        _row(self.mgmt, "l2_service", freshness="stale")
        resp, _ = self._tab(read_state_side_effect=AdapterError("hung", code="nso_timeout"))
        self.assertEqual(resp.status_code, 200)  # the tab renders; chips = persisted

    def test_unlinked_device_skips_the_fetch(self):
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(adapter_device_id=None)
        url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
        with patch("netbox_nso_plugin.adapter_client.get_device_read_state") as m_rs:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        m_rs.assert_not_called()


class TestReadStateClientTimeout(TestCase):
    def test_read_state_uses_short_endpoint_budget(self):
        """R5-5: connect 5s / read 5s — NOT the configurable 30s default."""
        from netbox_nso_plugin import adapter_client

        captured = {}

        class _Sess:
            def request(self, method, url, **kw):
                captured["timeout"] = kw.get("timeout")

                class R:
                    ok = True
                    content = b"{}"

                    @staticmethod
                    def json():
                        return {"device_id": 1, "families_version": 1, "families": {}}

                return R()

        cfg = {
            "url": "http://adapter.test",
            "token": "t",
            "timeout": 30,
            "verify_tls": True,
            "ca_cert_path": "",
        }
        with (
            patch.object(adapter_client, "_resolve_config", return_value=cfg),
            patch.object(adapter_client, "_get_session", return_value=_Sess()),
        ):
            adapter_client.get_device_read_state(1)
        self.assertEqual(captured["timeout"], (5, 5))
