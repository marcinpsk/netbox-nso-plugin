# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Periodic system jobs for the NSO plugin."""

import logging

from core.choices import JobIntervalChoices
from netbox.jobs import JobRunner, system_job

logger = logging.getLogger(__name__)

# Minutes between last-sync mirror refreshes. The adapter polls devices far more often
# than this; the window only bounds how stale a NetBox page can be with nobody on it.
SYNC_CACHE_REFRESH_MINUTES = 5


@system_job(interval=JobIntervalChoices.INTERVAL_HOURLY)
class AdvanceStaleOnboardingJob(JobRunner):
    """Hourly backstop that advances async-onboarding rows stranded in 'provisioning'.

    The onboarding dashboard and the device NSO tab advance a row the moment its provision
    job finishes — but only while an operator has that page open. A job that completes with
    no such page open would otherwise strand the row in 'provisioning' forever: NSO has
    onboarded the node, yet the plugin never maps/scopes/syncs it (the adapter-push signal is
    gated on that status). This sweep polls each stale row's job and advances it, self-healing
    with nobody watching.
    """

    class Meta:
        name = "Advance stale NSO onboarding rows"

    def run(self, *args, **kwargs):
        """Advance every NSODeviceManagement row still in 'provisioning'."""
        from .onboarding import advance_stale_onboarding_rows

        advance_stale_onboarding_rows()


@system_job(interval=SYNC_CACHE_REFRESH_MINUTES)
class RefreshDeviceSyncCacheJob(JobRunner):
    """Mirror each managed device's adapter-side last-sync state into NetBox.

    ``NSODeviceManagement.last_sync_at``/``last_sync_status`` are a cache of the adapter's
    device row, and page renders used to be the only thing that wrote it — so a device
    nobody had opened showed a blank status, and one last opened days ago showed a stale
    'succeeded', while the adapter had been syncing both all along.
    """

    class Meta:
        name = "Refresh NSO device sync cache"

    def run(self, *args, **kwargs):
        """Refresh the cached last-sync mirror, then repair any broken adapter mapping."""
        from .models import NSODeviceManagement
        from .sync_cache import _snapshot, reconcile_device_links, refresh_sync_caches

        rows = list(NSODeviceManagement.objects.select_related("nso_instance"))
        # One adapter snapshot for both passes: they ask the same question of the same data,
        # and a second call could disagree with the first mid-sweep.
        snapshot = _snapshot(rows)
        checked, updated = refresh_sync_caches(rows, snapshot=snapshot)
        # A row whose mapping is broken can't be mirrored at all — repair it so the next pass
        # has something to mirror.
        broken, attempted = reconcile_device_links(rows, snapshot=snapshot)
        logger.info(
            "RefreshDeviceSyncCacheJob: %d checked, %d updated, %d broken, %d repair attempted",
            checked,
            updated,
            broken,
            attempted,
        )
