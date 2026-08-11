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
        # Reported FROM what was cleared, never listed separately and cleared afterwards: a
        # degradation recorded between the two would otherwise go unseen and be wiped.
        acknowledged = acknowledge_degraded_deletions(options.get("device_id"), options.get("scope"))
        for device_id, scope, records in acknowledged:
            for record in records:
                self.stdout.write(
                    f"{device_id}/{scope}: {record.get('reason')} "
                    f"route(s) {record.get('route_ids')} at {record.get('at')}"
                )
        self.stdout.write(self.style.SUCCESS(f"Acknowledged {len(acknowledged)} key(s)"))
