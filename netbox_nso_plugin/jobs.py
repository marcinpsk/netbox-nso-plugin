# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Periodic system jobs for the NSO plugin."""

from core.choices import JobIntervalChoices
from netbox.jobs import JobRunner, system_job


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
