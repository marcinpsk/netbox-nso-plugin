# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared setup for the #1502 Appendix S settlement suites (S4 cursor, S5 verdicts+wiring).

Defined once, here, so the cursor chunk and the settlement chunk cannot drift on what a
device, an owned overlay or a per-route result looks like. The adapter is the real HTTP
double of :mod:`._settlement_adapter`; nothing here mocks the client.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import transaction
from django.test import TransactionTestCase
from django.utils import timezone

from ._settlement_adapter import FakeAdapter, LoopbackOnlySession
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

PUT = "netbox_nso_plugin.adapter_client.put_static_route_intent"
FINGERPRINT = "fp-a"


def _make_device(tag: str):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"Se{tag}Mfg", slug=f"se{tag}mfg")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"Se{tag}Dev", slug=f"se{tag}dev")
    role, _ = DeviceRole.objects.get_or_create(name=f"Se{tag}Role", slug=f"se{tag}role")
    site, _ = Site.objects.get_or_create(name=f"Se{tag}Site", slug=f"se{tag}site")
    return Device.objects.create(name=f"se-{tag}-rtr", device_type=dt, role=role, site=site)


def _make_mgmt(device, tag: str, adapter_device_id: int | None):
    from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

    inst, _ = NSOInstance.objects.get_or_create(
        name=f"se-{tag}-inst", defaults={"adapter_instance_id": f"se-{tag}-inst"}
    )
    with patch("netbox_nso_plugin.signals._sync_committed_scope_to_adapter"):
        return NSODeviceManagement.objects.create(
            device=device,
            nso_instance=inst,
            nso_device_name=f"nso-se-{tag}",
            adapter_device_id=adapter_device_id,
        )


def _route(prefix, next_hop, *, devices=()):
    from netbox_routing.models import StaticRoute

    from netbox_nso_plugin.signals import suppress_intent_push

    sr = StaticRoute.objects.create(prefix=prefix, next_hop=next_hop, metric=1)
    if devices:
        with suppress_intent_push():
            sr.devices.add(*devices)
    return sr


def _own(sr, mgmt, *, generation, expected=True, status="deploying"):
    """An owned overlay at *generation*, with or without a recorded expectation."""
    from netbox_nso_plugin.models import NSOStaticRouteState

    with patch(PUT), transaction.atomic():
        return NSOStaticRouteState.objects.create(
            management=mgmt,
            static_route=sr,
            status=status,
            apply_attempt_id=uuid4() if status == "deploying" else None,
            nso_prefix=str(sr.prefix or ""),
            nso_next_hop=str(sr.next_hop or ""),
            accepted_at=timezone.now(),
            intent_generation=generation,
            generation_started_at=timezone.now(),
            expected_generation=generation if expected else None,
            expected_fingerprint=FINGERPRINT if expected else "",
        )


def _stale_clock(state, minutes=90):
    """Age the generation clock past the stuck-deploying grace (default 10 minutes)."""
    from netbox_nso_plugin.models import NSOStaticRouteState

    NSOStaticRouteState.objects.filter(pk=state.pk).update(
        generation_started_at=timezone.now() - timedelta(minutes=minutes)
    )


def _result(route_id, generation, *, outcome="in_sync", fingerprint=FINGERPRINT, error=None):
    """One ``static_route_results`` entry, in the adapter's wire shape."""
    return {
        "route_id": route_id,
        "row_id": 1,
        "key": ["", "10.0.0.0/8", "10.0.0.1"],
        "fingerprint": fingerprint,
        "generation": generation,
        "outcome": outcome,
        "error": error,
    }


class _AdapterDoubleMixin:
    """Point the plugin at a live adapter double for the duration of one test.

    A mixin rather than a base class because the e2e chunk needs the same wiring under
    ``LiveServerTestCase`` — the adapter has to reach the plugin's own HTTP endpoint there,
    which a plain ``TransactionTestCase`` does not serve.
    """

    #: overridden by a suite whose double needs to answer more than the feed
    adapter_factory = FakeAdapter

    def setUp(self):
        super().setUp()
        from netbox_nso_plugin import adapter_client

        blocked = patch("netbox_nso_plugin.adapter_client.requests.Session", LoopbackOnlySession)
        blocked.start()
        self.addCleanup(blocked.stop)
        adapter_client.reset_session()
        self.addCleanup(adapter_client.reset_session)

        self.adapter = self.adapter_factory()
        self.addCleanup(self.adapter.stop)
        self.addCleanup(self._reset_adapter_config)
        self._point_at(self.adapter)

    def _point_at(self, adapter):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.models import AdapterConnection

        conn = AdapterConnection.objects.first()
        if conn is None:
            AdapterConnection.objects.create(url=adapter.url, enabled=True, timeout_seconds=10)
        else:
            AdapterConnection.objects.filter(pk=conn.pk).update(url=adapter.url, enabled=True)
        adapter_client.reset_config_cache()

    def _reset_adapter_config(self):
        from netbox_nso_plugin import adapter_client

        adapter_client.reset_config_cache()

    def _cursor(self, mgmt):
        from netbox_nso_plugin.models import NSODeviceManagement

        return NSODeviceManagement.objects.get(pk=mgmt.pk)

    def _tick(self):
        """Run one real ``RefreshDeviceSyncCacheJob`` — the plugin's per-device maintenance tick."""
        from uuid import uuid4

        from core.models import Job

        from netbox_nso_plugin.jobs import RefreshDeviceSyncCacheJob

        job = Job.objects.create(name=RefreshDeviceSyncCacheJob.name, job_id=uuid4())
        RefreshDeviceSyncCacheJob(job).run()


class _SettlementCase(IntentPushResetMixin, _CascadeFlushMixin, _AdapterDoubleMixin, TransactionTestCase):
    """The settlement suites' base: a live adapter double and a real transaction boundary."""

    serialized_rollback = False


class _CarrierMixin:
    """Drive Step 4 the way production does: notify endpoint → arbiter → queue → RQ worker.

    The queue is a throwaway **async** one on the configured Redis, and the worker is drained
    in burst mode by :meth:`_drain`. An ``is_async=False`` queue is not an option: it runs the
    body inline and, by its own docstring, bypasses the carrier CAS, so it would prove the
    reachability of a path production never takes. Nothing here calls the consumer,
    ``run_device_reconcile`` or the management command.

    Steps 1-3 of the reconcile (the family read) are stubbed. They are a different subsystem
    with a different adapter surface, and the question this harness exists to answer is
    whether **Step 4** is reached at all through the real transport.
    """

    def setUp(self):
        super().setUp()
        from uuid import uuid4

        import django_rq
        from rest_framework.test import APIClient
        from rq import Queue
        from users.constants import TOKEN_PREFIX
        from users.models import Token, User

        self.api = APIClient()
        user = User.objects.create_user(username=f"s5-carrier-{uuid4().hex[:8]}", is_superuser=True)
        token = Token.objects.create(user=user)
        self.header = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"}

        connection = django_rq.get_queue("default").connection
        self.queue = Queue(f"nso-s5-{uuid4().hex[:8]}", connection=connection)
        assert self.queue.is_async, "an inline queue bypasses the carrier CAS"
        self.addCleanup(self._drop_queue)
        queued = patch("django_rq.get_queue", return_value=self.queue)
        queued.start()
        self.addCleanup(queued.stop)

        read = patch("netbox_nso_plugin.reconcile.reconcile_device", return_value={})
        read.start()
        self.addCleanup(read.stop)

    def _drop_queue(self):
        """Redis outlives the test database, so the carrier pointers must go with the queue."""
        from netbox_nso_plugin.read_gate import carrier_key, marker_key

        for device_id in getattr(self, "_notified", ()):
            self.queue.connection.delete(carrier_key(device_id))
            self.queue.connection.delete(marker_key(device_id))
        self.queue.delete(delete_jobs=True)

    def _notify(self, device_id):
        """The adapter's own callback: ``POST /api/plugins/nso/sync-complete/``, authenticated."""
        self._notified = {*getattr(self, "_notified", set()), device_id}
        return self.api.post(
            "/api/plugins/nso/sync-complete/",
            {"netbox_device_id": device_id},
            format="json",
            **self.header,
        )

    def _drain(self):
        """Run the queued carrier. Asserting straight after the 202 would prove only that
        something was queued — CI starts Redis and no worker, and queued is not run."""
        from rq import SimpleWorker

        worker = SimpleWorker([self.queue], connection=self.queue.connection)
        worker.work(burst=True, with_scheduler=False)
        failed = self.queue.failed_job_registry.get_job_ids()
        assert not failed, f"the carrier job failed: {failed}"


class _CarrierCase(_CarrierMixin, _SettlementCase):
    """A settlement case whose Step 4 is reached through the real queued carrier."""
