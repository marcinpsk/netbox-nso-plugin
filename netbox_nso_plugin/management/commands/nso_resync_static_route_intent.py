# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Backfill the NetBox ``StaticRoute`` pk into the adapter's static-route intent."""

from django.core.management.base import BaseCommand, CommandError

from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet


class Command(BaseCommand):
    help = (
        "Re-push every NSO-managed device's static-route intent store-only, so the adapter "
        "backfills route_id on its stored rows and its replacement fence can open."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--device",
            type=int,
            action="append",
            dest="device_ids",
            metavar="ID",
            help="Limit the pass to this NetBox device id (repeatable). Default: every managed device.",
        )

    def handle(self, *args, **options):
        results = resync_static_route_intent_fleet(device_ids=options.get("device_ids"))
        for row in results:
            if row["ok"]:
                self.stdout.write(f"{row['device']} (device {row['device_id']}): {row['count']} route(s) stored")
            else:
                self.stdout.write(self.style.ERROR(f"{row['device']} (device {row['device_id']}): NOT acknowledged"))

        failed = [row for row in results if not row["ok"]]
        if failed:
            # A partial pass leaves those devices' fences shut, so it must not read as success.
            names = ", ".join(f"{row['device']} ({row['device_id']})" for row in failed)
            raise CommandError(f"Static-route intent re-sync failed for {len(failed)} device(s): {names}")
        self.stdout.write(self.style.SUCCESS(f"Re-synced {len(results)} device(s)"))
