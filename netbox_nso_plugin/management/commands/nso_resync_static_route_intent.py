# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Backfill the NetBox ``StaticRoute`` pk into the adapter's static-route intent."""

from django.core.management.base import BaseCommand, CommandError

from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet


class Command(BaseCommand):
    help = (
        "Re-push every NSO-managed device's static-route intent store-only, so the adapter "
        "backfills route_id on its stored rows and its replacement fence can open. Owned "
        "overlays still on the generation sentinel are armed in the same pass, so their "
        "apply results can correlate."
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
        requested = options.get("device_ids")
        results = resync_static_route_intent_fleet(device_ids=requested)
        for row in results:
            if row["ok"]:
                self.stdout.write(
                    f"{row['device']} (device {row['device_id']}): {row['count']} route(s) stored, "
                    f"{row['armed']} generation(s) armed"
                )
            else:
                self.stdout.write(self.style.ERROR(f"{row['device']} (device {row['device_id']}): NOT acknowledged"))

        problems = []
        failed = [row for row in results if not row["ok"]]
        if failed:
            names = ", ".join(f"{row['device']} ({row['device_id']})" for row in failed)
            problems.append(f"failed for {len(failed)} device(s): {names}")
        # ``--device`` filters the queryset, so an id that is unknown or unlinked yields no
        # result row at all and would otherwise print a clean "Re-synced 0 device(s)".
        missing = sorted(set(requested or []) - {row["device_id"] for row in results})
        if missing:
            problems.append("found no linked NSO-managed device for id(s): " + ", ".join(str(i) for i in missing))
        if problems:
            # A partial pass leaves those devices' fences shut, so it must not read as success.
            raise CommandError("Static-route intent re-sync " + "; ".join(problems))
        self.stdout.write(self.style.SUCCESS(f"Re-synced {len(results)} device(s)"))
