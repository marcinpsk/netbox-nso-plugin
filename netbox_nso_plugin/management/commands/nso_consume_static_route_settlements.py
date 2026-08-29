# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Walk the adapter's settlement feed for managed devices — the operational entry point.

This is the operator's tool and the backfill driver, not the production carrier: the
system consumes settlements from the device reconcile and from the periodic maintenance
tick. Running it by hand is how an operator drains a device after a manual repair, and how
a stall bound is exercised across a real process restart.
"""

from django.core.management.base import BaseCommand, CommandError

from netbox_nso_plugin.settlement import consume_static_route_settlements


class Command(BaseCommand):
    """Django management command to consume static-route settlement feed from the adapter."""

    help = (
        "Consume the nso-adapter's static-route settlement feed for every linked managed "
        "device, advancing each device's durable cursor."
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
        parser.add_argument(
            "--passes",
            type=int,
            default=1,
            help=(
                "How many consecutive passes to make over each device in this process. One "
                "pass consumes at most one feed page and counts at most one stall attempt."
            ),
        )

    def handle(self, *args, **options):
        from netbox_nso_plugin.models import NSODeviceManagement

        passes = options["passes"]
        if passes < 1:
            raise CommandError("--passes must be at least 1")

        requested = options.get("device_ids")
        rows = NSODeviceManagement.objects.filter(adapter_device_id__isnull=False).select_related("device")
        if requested:
            rows = rows.filter(device_id__in=requested)
        rows = list(rows.order_by("pk"))

        failures = []
        for mgmt in rows:
            for _ in range(passes):
                try:
                    result = consume_static_route_settlements(mgmt.pk)
                except Exception as exc:  # noqa: BLE001 — one device may not abort the fleet
                    failures.append((mgmt, exc))
                    self.stdout.write(self.style.ERROR(f"{mgmt.device} (device {mgmt.device_id}): {exc}"))
                    break
                self.stdout.write(
                    f"{mgmt.device} (device {mgmt.device_id}): consumed {result.consumed}, "
                    f"cursor {result.cursor}"
                    + (", epoch reset" if result.epoch_reset else "")
                    + (", stalled" if result.stalled else "")
                    + (", advanced past a stalled sequence" if result.advanced_past_stall else "")
                )

        missing = sorted(set(requested or []) - {mgmt.device_id for mgmt in rows})
        problems = []
        if failures:
            names = ", ".join(f"{mgmt.device} ({mgmt.device_id})" for mgmt, _ in failures)
            problems.append(f"failed for {len(failures)} device(s): {names}")
        if missing:
            # ``--device`` filters the queryset, so an unknown or unlinked id would
            # otherwise read as a clean pass over zero devices.
            problems.append("found no linked NSO-managed device for id(s): " + ", ".join(str(i) for i in missing))
        if problems:
            raise CommandError("Settlement consumption " + "; ".join(problems))
        self.stdout.write(self.style.SUCCESS(f"Consumed settlements for {len(rows)} device(s)"))
