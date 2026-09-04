# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""NX-P4b: the local logging-levels singleton overlay (NSOLoggingLevelState).

Covers the plugin surface of the levels singleton end to end: reconcile (mirror /
owned-no-clobber / singleton-absence drift — the NSOSnmpSystemInfoState precedent),
the intent push wire shape (a dict of set severities when owned, an EXPLICIT null
when not — null is what makes un-accept an actual retraction instead of a
stale-intent leak), Accept (with the all-blank blocker), the NEW Un-accept flow
(ownership release = retract), the inline field-edit whitelist, and the
category/summary wiring.
"""

from unittest.mock import patch
from uuid import uuid4

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from ._adapter_http import make_session
from ._outbox_case import content_bulk_update, mirror_update, without_commit_drain
from .mixins import IntentPushDeliveryMixin, IntentPushResetMixin, _CascadeFlushMixin, isolate_other_scopes

User = get_user_model()
TEST_PASSWORD = "levelstestpass123"  # noqa: S105

_ADAPTER_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}

_LEVELS_PAYLOAD = {"console_severity": "CRITICAL", "monitor_severity": "NOTICE", "module_severity": "NOTICE"}


def _make_device(suffix):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"LvlMfg{suffix}", slug=f"lvlmfg{suffix}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"LvlDev{suffix}", slug=f"lvldev{suffix}")
    role, _ = DeviceRole.objects.get_or_create(name=f"LvlRole{suffix}", slug=f"lvlrole{suffix}")
    site, _ = Site.objects.get_or_create(name=f"LvlSite{suffix}", slug=f"lvlsite{suffix}")
    return Device.objects.create(name=f"lvl-rtr-{suffix}", device_type=dt, role=role, site=site)


class LevelsTestBase(IntentPushDeliveryMixin, TestCase):
    """Device + management fixtures shared by the levels tests."""

    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device(cls.__name__.lower())
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="lvl-inst", defaults={"adapter_instance_id": "lvl-inst"})
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device,
            nso_instance=inst,
            nso_device_name=f"lvl-dev-{cls.__name__.lower()}",
            adapter_device_id=cls.device.pk,
            manage_logging=True,
        )

    def _payload(self, local_levels=None, hosts=()):
        p = {"hosts": list(hosts), "last_refreshed_at": None, "refresh_source": "test"}
        if local_levels is not None:
            p["local_levels"] = local_levels
        return p

    def _row(self, **kwargs):
        from netbox_nso_plugin.models import NSOLoggingLevelState

        return NSOLoggingLevelState.objects.create(management=self.mgmt, **kwargs)


class TestReconcileLoggingLevels(LevelsTestBase):
    """_reconcile_logging_config's local_levels handling (via the real reconcile entry point)."""

    def _reconcile(self, payload):
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        return _reconcile_logging_config(self.device, payload)

    def test_creates_singleton_imported(self):
        from netbox_nso_plugin.models import NSOLoggingLevelState

        res = self._reconcile(self._payload(local_levels=_LEVELS_PAYLOAD))
        row = NSOLoggingLevelState.objects.get(management=self.mgmt)
        self.assertEqual(res["local_levels"], row)
        self.assertEqual(row.console_severity, "CRITICAL")
        self.assertEqual(row.monitor_severity, "NOTICE")
        self.assertEqual(row.module_severity, "NOTICE")
        self.assertEqual(row.status, "imported")
        self.assertIsNotNone(row.last_sync_at)

    def test_full_device_reconcile_uses_the_logging_mutation_plan(self):
        from netbox_nso_plugin.reconcile import _LeaseOutcome, reconcile_device

        payload = self._payload(local_levels=_LEVELS_PAYLOAD)
        with (
            patch("netbox_nso_plugin.reconcile._acquire_reconcile_lease", return_value=_LeaseOutcome()),
            patch("netbox_nso_plugin.adapter_client.get_logging_config", return_value=payload),
        ):
            result = reconcile_device(self.device, self.mgmt)

        row = result["logging_data"]["local_levels"]
        self.assertEqual(row.console_severity, "CRITICAL")
        self.assertEqual(row.status, "imported")

    def test_partial_payload_leaves_other_destinations_blank(self):
        res = self._reconcile(self._payload(local_levels={"console_severity": "ERROR"}))
        row = res["local_levels"]
        self.assertEqual(row.console_severity, "ERROR")
        self.assertEqual(row.monitor_severity, "")
        self.assertEqual(row.module_severity, "")

    def test_absent_levels_without_row_returns_none_and_creates_nothing(self):
        from netbox_nso_plugin.models import NSOLoggingLevelState

        res = self._reconcile(self._payload())
        self.assertIsNone(res["local_levels"])
        self.assertFalse(NSOLoggingLevelState.objects.filter(management=self.mgmt).exists())

    def test_absent_levels_drifts_owned_in_sync_row(self):
        """The singleton form of 'the device stopped reporting it': owned must not stay green."""
        row = self._row(console_severity="CRITICAL", status="in_sync", accepted_at=timezone.now())
        res = self._reconcile(self._payload())
        row.refresh_from_db()
        self.assertEqual(row.status, "changed")
        self.assertEqual(res["local_levels"], row)

    def test_absent_levels_keeps_accepted_row_pending(self):
        """accepted = intent not yet confirmed on the device; absence is expected, not drift."""
        row = self._row(console_severity="CRITICAL", status="accepted", accepted_at=timezone.now())
        self._reconcile(self._payload())
        row.refresh_from_db()
        self.assertEqual(row.status, "accepted")

    def test_absent_levels_keeps_unowned_row_untouched(self):
        """The sysinfo singleton precedent: an unowned stale row is not rendered (None) but survives."""
        from netbox_nso_plugin.models import NSOLoggingLevelState

        row = self._row(console_severity="NOTICE", status="imported")
        res = self._reconcile(self._payload())
        self.assertIsNone(res["local_levels"])
        row.refresh_from_db()
        self.assertEqual(row.status, "imported")
        self.assertEqual(NSOLoggingLevelState.objects.filter(management=self.mgmt).count(), 1)

    def test_unowned_mirror_follows_device(self):
        row = self._row(console_severity="NOTICE", status="imported")
        self._reconcile(self._payload(local_levels={"console_severity": "CRITICAL"}))
        row.refresh_from_db()
        self.assertEqual(row.console_severity, "CRITICAL")
        self.assertEqual(row.status, "imported")

    def test_owned_values_never_clobbered_and_match_settles_in_sync(self):
        row = self._row(console_severity="WARNING", status="accepted", accepted_at=timezone.now())
        self._reconcile(self._payload(local_levels={"console_severity": "WARNING"}))
        row.refresh_from_db()
        self.assertEqual(row.console_severity, "WARNING")
        self.assertEqual(row.status, "in_sync")

    def test_owned_differing_value_stays_accepted_with_intent_value(self):
        row = self._row(console_severity="WARNING", status="accepted", accepted_at=timezone.now())
        self._reconcile(self._payload(local_levels={"console_severity": "CRITICAL"}))
        row.refresh_from_db()
        self.assertEqual(row.console_severity, "WARNING", "operator intent must never be clobbered by a read")
        self.assertEqual(row.status, "accepted")

    def test_owned_singleton_deleted_after_load_returns_none(self):
        """A concurrent singleton deletion must not crash the read reconciliation.

        The sysinfo precedent: the post-CAS reload of a row another writer removed raises
        DoesNotExist, which takes the whole device's logging reconcile with it.
        """
        from django.db.models.signals import post_init

        from netbox_nso_plugin.models import NSOLoggingLevelState

        row = self._row(console_severity="WARNING", status="accepted", accepted_at=timezone.now())
        deleted = []

        def _delete_after_load(sender, instance, **kwargs):
            # post_init = the reconciler has just SELECTed the row; the delete lands before
            # its post-CAS reload. Queryset delete: no post_init, hence no recursion.
            if deleted or instance.pk != row.pk:
                return
            deleted.append(True)
            NSOLoggingLevelState.objects.filter(pk=instance.pk).delete()

        post_init.connect(_delete_after_load, sender=NSOLoggingLevelState, weak=False)
        self.addCleanup(post_init.disconnect, _delete_after_load, sender=NSOLoggingLevelState)

        res = self._reconcile(self._payload(local_levels={"console_severity": "WARNING"}))

        self.assertEqual(deleted, [True])
        self.assertIsNone(res["local_levels"])

    def test_owned_singleton_concurrent_edit_returns_the_persisted_row(self):
        from django.db.models.signals import post_init

        from netbox_nso_plugin.models import NSOLoggingLevelState

        row = self._row(console_severity="WARNING", status="accepted", accepted_at=timezone.now())
        edited = []

        def edit_after_load(sender, instance, **kwargs):
            if edited or instance.pk != row.pk:
                return
            edited.append(True)
            content_bulk_update(instance, console_severity="ERROR")
            instance.console_severity = "WARNING"

        post_init.connect(edit_after_load, sender=NSOLoggingLevelState, weak=False)
        self.addCleanup(post_init.disconnect, edit_after_load, sender=NSOLoggingLevelState)

        res = self._reconcile(self._payload(local_levels={"console_severity": "WARNING"}))

        self.assertEqual(edited, [True])
        self.assertIsNotNone(res["local_levels"])
        self.assertEqual(res["local_levels"].console_severity, "ERROR")
        self.assertEqual(res["local_levels"].status, "accepted")

    def test_no_mgmt_returns_none(self):
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        res = _reconcile_logging_config(_make_device("nomgmt"), self._payload(local_levels=_LEVELS_PAYLOAD))
        self.assertIsNone(res["local_levels"])


class TestLoggingLevelsPush(LevelsTestBase):
    """The intent push wire shape, recorded at the real HTTP boundary."""

    def _recorded_requests(self, run):
        session = make_session(json_data={})
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
        ):
            run()
        return session.request.call_args_list

    def _push(self):
        from netbox_nso_plugin.delivery import deliver

        return self._recorded_requests(lambda: deliver("logging", self.device.pk, self.mgmt.adapter_device_id))

    def test_owned_row_pushes_set_severities_only(self):
        self._row(console_severity="CRITICAL", monitor_severity="", status="accepted", accepted_at=timezone.now())
        calls = self._push()
        self.assertEqual(len(calls), 1)
        body = calls[0].kwargs["json"]
        self.assertEqual(body["local_levels"], {"console_severity": "CRITICAL"})

    def test_unowned_row_pushes_explicit_null(self):
        """null (not omission!) — the adapter reads the key presence-sensitively; omitting it
        would mean 'preserve', leaving stale adapter intent behind after an un-accept."""
        self._row(console_severity="CRITICAL", status="imported")
        calls = self._push()
        body = calls[0].kwargs["json"]
        self.assertIn("local_levels", body)
        self.assertIsNone(body["local_levels"])

    def test_no_row_pushes_explicit_null(self):
        calls = self._push()
        body = calls[0].kwargs["json"]
        self.assertIn("local_levels", body)
        self.assertIsNone(body["local_levels"])

    def test_owned_all_blank_row_pushes_null(self):
        """An owned row with every severity cleared manages nothing — the #83
        cleared-owned-scalar shape, same wire meaning as deleting the row."""
        self._row(status="accepted", accepted_at=timezone.now())
        calls = self._push()
        self.assertIsNone(calls[0].kwargs["json"]["local_levels"])

    def test_saving_a_levels_row_triggers_the_push(self):
        with (
            patch("netbox_nso_plugin.adapter_client.put_logging_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self._row(console_severity="ERROR", status="accepted", accepted_at=timezone.now())
        mock_put.assert_called_once()
        self.assertEqual(mock_put.call_args.args[2], {"console_severity": "ERROR"})

    def test_deleting_a_levels_row_pushes_null(self):
        with (
            patch("netbox_nso_plugin.adapter_client.put_logging_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = self._row(console_severity="ERROR", status="accepted", accepted_at=timezone.now())
        with (
            patch("netbox_nso_plugin.adapter_client.put_logging_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            row.delete()
        mock_put.assert_called_once()
        self.assertIsNone(mock_put.call_args.args[2])


class TestLoggingLevelsViews(LevelsTestBase):
    """Accept / Un-accept / inline field edit, through the real URLconf and views."""

    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser(
            username=f"lvladmin-{self._testMethodName[:20]}", password=TEST_PASSWORD
        )
        self.client.force_login(self.superuser)

    def _accept(self, row):
        return self.client.post(reverse("plugins:netbox_nso_plugin:logging_accept_levels", kwargs={"pk": row.pk}))

    def _unaccept(self, row):
        return self.client.post(reverse("plugins:netbox_nso_plugin:logging_unaccept_levels", kwargs={"pk": row.pk}))

    def _row_flushed(self, **kwargs):
        """Create the row draining its own creation-time push schedule.

        A bare create inside the test's outer transaction registers the coalesced
        on_commit drain there — a later save inside captureOnCommitCallbacks then
        coalesces into that never-fired drain and the capture sees no push.
        """
        with (
            patch("netbox_nso_plugin.adapter_client.put_logging_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            return self._row(**kwargs)

    def test_accept_matching_imported_row_becomes_owned_in_sync(self):
        row = self._row_flushed(console_severity="CRITICAL", status="imported")
        with (
            patch("netbox_nso_plugin.adapter_client.put_logging_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            r = self._accept(row)
        self.assertEqual(r.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.status, "in_sync")
        self.assertIsNotNone(row.accepted_at)
        mock_put.assert_called_once()
        self.assertEqual(mock_put.call_args.args[2], {"console_severity": "CRITICAL"})

    def test_accept_all_blank_row_is_refused(self):
        row = self._row(status="imported")
        r = self._accept(row)
        self.assertEqual(r.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.status, "imported")
        self.assertIsNone(row.accepted_at)

    def test_unaccept_in_sync_returns_row_to_mirror_and_retracts(self):
        row = self._row_flushed(console_severity="CRITICAL", status="in_sync", accepted_at=timezone.now())
        with (
            patch("netbox_nso_plugin.adapter_client.put_logging_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            r = self._unaccept(row)
        self.assertEqual(r.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.status, "imported")
        self.assertIsNone(row.accepted_at)
        # The retract wire shape: the un-owned snapshot carries an explicit null.
        mock_put.assert_called_once()
        self.assertIsNone(mock_put.call_args.args[2])

    def test_unaccept_apply_failed_row_is_allowed(self):
        row = self._row(console_severity="CRITICAL", status="apply_failed", accepted_at=timezone.now())
        with (
            patch("netbox_nso_plugin.adapter_client.put_logging_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self._unaccept(row)
        row.refresh_from_db()
        self.assertEqual(row.status, "imported")

    def test_unaccept_deploying_row_is_refused(self):
        attempt_id = uuid4()
        row = self._row(
            console_severity="CRITICAL",
            status="accepted",
            accepted_at=timezone.now(),
        )
        mirror_update(row, status="deploying", apply_attempt_id=attempt_id)
        self._unaccept(row)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying", "an in-flight Apply must settle before ownership is released")
        self.assertIsNotNone(row.accepted_at)

    def test_unaccept_unowned_row_is_refused(self):
        row = self._row(console_severity="CRITICAL", status="imported")
        self._unaccept(row)
        row.refresh_from_db()
        self.assertEqual(row.status, "imported")

    def test_unaccept_requires_change_permission(self):
        plain = User.objects.create_user(username="lvl-noc-viewer", password=TEST_PASSWORD)
        self.client.force_login(plain)
        row = self._row(console_severity="CRITICAL", status="in_sync", accepted_at=timezone.now())
        r = self._unaccept(row)
        self.assertEqual(r.status_code, 403)
        row.refresh_from_db()
        self.assertEqual(row.status, "in_sync")

    def test_field_edit_takes_ownership(self):
        row = self._row(console_severity="NOTICE", status="imported")
        url = reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "logging_levels", "pk": row.pk})
        with (
            patch("netbox_nso_plugin.adapter_client.put_logging_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            r = self.client.post(url, {"console_severity": "WARNING"})
        self.assertEqual(r.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.console_severity, "WARNING")
        self.assertEqual(row.status, "accepted")
        self.assertIsNotNone(row.accepted_at)

    def test_field_edit_rejects_a_value_outside_the_closed_oc_enum(self):
        row = self._row(console_severity="NOTICE", status="imported")
        url = reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "logging_levels", "pk": row.pk})
        r = self.client.post(url, {"console_severity": "SUPERBAD"})
        self.assertEqual(r.status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.console_severity, "NOTICE")
        self.assertEqual(row.status, "imported")

    def test_field_edit_rejects_unwhitelisted_fields(self):
        row = self._row(status="imported")
        url = reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "logging_levels", "pk": row.pk})
        r = self.client.post(url, {"status": "in_sync"})
        self.assertEqual(r.status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.status, "imported")

    def test_category_fragment_renders_the_levels_row(self):
        # Patch the category fetch: unpatched it reaches the LIVE adapter, and whether
        # that fetch 404s (persisted fallback) or succeeds (real device's payload —
        # usually WITHOUT local_levels → the seeded row is legitimately dropped)
        # depends on whether this test device's pk collides with a real adapter
        # device id. The suite's pk sequence must never decide this test.
        row = self._row(console_severity="CRITICAL", monitor_severity="NOTICE", status="imported")
        url = reverse("plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "logging"})
        with patch(
            "netbox_nso_plugin.adapter_client.get_logging_config",
            return_value=self._payload(local_levels={"console_severity": "CRITICAL", "monitor_severity": "NOTICE"}),
        ):
            r = self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'data-pe-fields="console_severity:select:Console severity"')
        accept_url = reverse("plugins:netbox_nso_plugin:logging_accept_levels", kwargs={"pk": row.pk})
        self.assertContains(r, accept_url)
        # Unowned row → the un-accept affordance must NOT render.
        unaccept_url = reverse("plugins:netbox_nso_plugin:logging_unaccept_levels", kwargs={"pk": row.pk})
        self.assertNotContains(r, unaccept_url)

    def test_category_fragment_offers_unaccept_with_the_retract_warning_when_owned(self):
        # Deterministic fetch (see test_category_fragment_renders_the_levels_row):
        # the device reports the owned severities → the row stays in_sync/owned.
        row = self._row(console_severity="CRITICAL", status="in_sync", accepted_at=timezone.now())
        url = reverse("plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "logging"})
        with patch(
            "netbox_nso_plugin.adapter_client.get_logging_config",
            return_value=self._payload(local_levels={"console_severity": "CRITICAL"}),
        ):
            r = self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        unaccept_url = reverse("plugins:netbox_nso_plugin:logging_unaccept_levels", kwargs={"pk": row.pk})
        self.assertContains(r, unaccept_url)
        # The honest surfacing (design R3-1): the confirm dialog states the NX consequence.
        self.assertContains(r, "DISABLED, not reverted")


class TestLoggingLevelsApplyLifecycle(LevelsTestBase):
    """The levels singleton rides the device Apply lifecycle.

    Without these, an accepted levels intent whose accept-time PUT was swallowed
    (adapter down) is silently skipped by Apply forever. Generic attempt settlement
    covers gate-off failures for every deploying overlay.
    """

    def test_deploying_row_waits_for_attempt_evidence_when_device_matches(self):
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        attempt_id = uuid4()
        row = self._row(
            console_severity="CRITICAL",
            status="deploying",
            accepted_at=timezone.now(),
            apply_attempt_id=attempt_id,
        )
        _reconcile_logging_config(
            self.device, {"hosts": [], "local_levels": {"console_severity": "CRITICAL"}, "refresh_source": "test"}
        )
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")
        self.assertEqual(row.apply_attempt_id, attempt_id)


class TestLoggingLevelsInlineClearGuard(LevelsTestBase):
    """codex P4b triage: clearing the LAST severity inline must not silently retract.

    An all-blank owned row pushes ``local_levels: null`` — the un-manage/retract wire
    shape — so reaching it through a casual popover edit would bypass the Un-accept
    flow's explicit disable warning. The edit is rejected; Un-accept is the exit.
    """

    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser(
            username=f"lvlclr-{self._testMethodName[:20]}", password=TEST_PASSWORD
        )
        self.client.force_login(self.superuser)

    def _edit(self, row, **data):
        url = reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "logging_levels", "pk": row.pk})
        return self.client.post(url, data)

    def test_clearing_the_last_severity_is_rejected(self):
        row = self._row(console_severity="CRITICAL", status="in_sync", accepted_at=timezone.now())
        r = self._edit(row, console_severity="")
        self.assertEqual(r.status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.console_severity, "CRITICAL", "the destructive clear must not be persisted")
        self.assertEqual(row.status, "in_sync")

    def test_clearing_one_of_two_severities_is_allowed(self):
        row = self._row(
            console_severity="CRITICAL", monitor_severity="NOTICE", status="in_sync", accepted_at=timezone.now()
        )
        with patch("netbox_nso_plugin.adapter_client.put_logging_intent"), self.captureOnCommitCallbacks(execute=True):
            r = self._edit(row, monitor_severity="")
        self.assertEqual(r.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.monitor_severity, "")
        self.assertEqual(row.console_severity, "CRITICAL")
        self.assertEqual(row.status, "accepted")


class TestLoggingLevelsWiring(LevelsTestBase):
    """Summary counts, overlay URL/serializer resolution."""

    def test_summary_counts_include_the_levels_singleton(self):
        from netbox_nso_plugin.summary import category_summaries

        self._row(console_severity="CRITICAL", status="imported")
        cats = {c["key"]: c for c in category_summaries(self.device, self.mgmt)}
        self.assertEqual(cats["logging"]["counts"]["total"], 1)

    def test_overlay_url_and_event_serialization_resolve(self):
        """Deleting a parent with an overlay must not 500 (NoReverseMatch / SerializerNotFound)."""
        from extras.events import serialize_for_event

        row = self._row(console_severity="CRITICAL", status="accepted", accepted_at=timezone.now())
        self.assertEqual(row.get_absolute_url(), reverse("dcim:device_nso", kwargs={"pk": self.device.pk}))
        data = serialize_for_event(row)
        self.assertEqual(data["id"], row.pk)


class TestLoggingLevelsApplyPush(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """The Apply's forced logging push, which needs a committed transaction to take.

    ``drain.push_now`` refuses to run nested in a caller's block (#1503 Appendix O), so this
    half of the Apply lifecycle cannot be pinned from a ``TestCase``. It runs here against
    the real claim, which is also what makes the force meaningful: the acknowledged baseline
    below already names this body, and only ``force`` gets it sent a second time.
    """

    def setUp(self):
        super().setUp()
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOLoggingLevelState

        with without_commit_drain(), transaction.atomic():
            self.device = _make_device("applypush")
            inst, _ = NSOInstance.objects.get_or_create(
                name="lvl-apply-inst", defaults={"adapter_instance_id": "lvl-apply-inst"}
            )
            self.mgmt = NSODeviceManagement.objects.create(
                device=self.device,
                nso_instance=inst,
                nso_device_name="lvl-dev-applypush",
                adapter_device_id=self.device.pk,
                manage_logging=True,
            )
            self.row = NSOLoggingLevelState.objects.create(
                management=self.mgmt, status="accepted", accepted_at=timezone.now()
            )

    def test_apply_sends_an_owned_logging_retraction_and_the_read_keeps_it_deploying(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.template_content import _reconcile_logging_config
        from netbox_nso_plugin.views import _prepare_apply

        # The acknowledged baseline an accept-time push leaves behind, which the Apply overrides.
        with patch("netbox_nso_plugin.adapter_client.put_logging_intent", return_value={}):
            drain.drain_key(self.device.pk, "logging")
        with patch("netbox_nso_plugin.adapter_client.put_logging_intent", return_value={}) as unforced:
            drain.push_now(self.device.pk, "logging")
        unforced.assert_not_called()

        with isolate_other_scopes("logging") as stack:
            mock_put = stack.enter_context(
                patch("netbox_nso_plugin.adapter_client.put_logging_intent", return_value={})
            )
            prepared, selected = _prepare_apply(self.mgmt)

        mock_put.assert_called_once()
        self.assertIsNone(mock_put.call_args.args[2])
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "deploying")
        attempt_id = self.row.apply_attempt_id
        self.assertIsNotNone(attempt_id)
        moved_pks = [pk for stream, _model, pks, _previous in prepared.moved for pk in pks if stream == "logging"]
        self.assertIn(self.row.pk, moved_pks)
        self.assertIn("logging", selected)
        with self.assertRaises(TypeError):
            selected["logging"] = 0

        # Removal-generation settlement is tracked by board card #1656.
        _reconcile_logging_config(
            self.device,
            {"hosts": [], "last_refreshed_at": None, "refresh_source": "test"},
        )

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "deploying")
        self.assertEqual(self.row.apply_attempt_id, attempt_id)
