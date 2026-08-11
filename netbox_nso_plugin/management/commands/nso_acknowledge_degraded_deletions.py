# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Clear the durable record of deletions that degraded to a detach (#1503 Appendix O §4.3(c))."""

from django.core.management.base import BaseCommand

from netbox_nso_plugin.drain import acknowledge_degraded_deletions


class Command(BaseCommand):
    help = (
        "Acknowledge the recorded deletions that degraded to a detach, and clear them. This "
        "is the ONLY thing that clears them: no push outcome ever does, because a success "
        "would otherwise erase the warning before an operator could read it."
    )

    def add_arguments(self, parser):
        parser.add_argument("--device", type=int, dest="device_id", metavar="ID", help="Limit to one NetBox device id.")
        parser.add_argument("--scope", dest="scope", metavar="KEY", help="Limit to one delivery key.")

    def handle(self, *args, **options):
        from netbox_nso_plugin.models import NSOIntentOutboxState

        rows = NSOIntentOutboxState.objects.exclude(degraded_deletions=[])
        if options.get("device_id"):
            rows = rows.filter(device_id=options["device_id"])
        if options.get("scope"):
            rows = rows.filter(scope=options["scope"])
        for state in rows:
            for record in state.degraded_deletions:
                self.stdout.write(
                    f"{state.device_id}/{state.scope}: {record.get('reason')} "
                    f"route(s) {record.get('route_ids')} at {record.get('at')}"
                )

        cleared = acknowledge_degraded_deletions(options.get("device_id"), options.get("scope"))
        self.stdout.write(self.style.SUCCESS(f"Acknowledged {cleared} key(s)"))
