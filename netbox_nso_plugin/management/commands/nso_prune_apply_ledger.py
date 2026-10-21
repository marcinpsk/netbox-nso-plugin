# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Prune expired Apply history without removing live settlement evidence."""

from django.core.management.base import BaseCommand, CommandError

from netbox_nso_plugin.apply_retention import DEFAULT_BATCH_SIZE, prune_apply_ledger
from netbox_nso_plugin.deployment import DeploymentQuiesced


class Command(BaseCommand):
    help = "Inspect terminal Apply history older than 30 days. Delete only with --execute."

    def add_arguments(self, parser):
        parser.add_argument("--after", help="Continue from the previous result cursor.")
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)

    def handle(self, *args, **options):
        try:
            result = prune_apply_ledger(
                execute=options["execute"], batch_size=options["batch_size"], cursor=options["after"]
            )
        except (ValueError, DeploymentQuiesced) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"examined={result.examined} eligible={result.eligible} deleted={result.deleted} "
            f"execute={options['execute']} next_cursor={result.next_cursor or 'end'}"
        )
