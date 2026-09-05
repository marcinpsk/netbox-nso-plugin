# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Conservatively establish trusted renderer baselines during cutover."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from netbox_nso_plugin import delivery
from netbox_nso_plugin.deployment import gate_bypass, quiesce, resume

_STABILITY_PASSES = 3


class Command(BaseCommand):
    help = "Quiesce intent work and conservatively repair every unknown renderer baseline."

    def handle(self, *args, **options):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        created_gate = quiesce()
        repaired = 0
        devices = ()
        try:
            scopes = tuple(delivery.delivery_keys())
            devices = tuple(NSODeviceManagement.objects.order_by("device_id").values_list("device_id", flat=True))
            with gate_bypass():
                for device_id in devices:
                    for _pass in range(_STABILITY_PASSES):
                        result = audit_renderer_scopes(
                            device_id,
                            scopes,
                            trigger="baseline-cutover",
                            pre_capture=True,
                        )
                        repaired += len(result.repaired)
                        if not result.repaired:
                            break
                    else:
                        raise CommandError(
                            f"Renderer baseline for device {device_id} did not stabilize after "
                            f"{_STABILITY_PASSES} complete audits"
                        )
        except BaseException as exc:
            self.stderr.write(self.style.ERROR("Renderer baseline cutover failed; intent work remains quiesced"))
            if not isinstance(exc, Exception) or isinstance(exc, CommandError):
                raise
            raise CommandError(f"Renderer baseline cutover failed: {exc}") from exc

        if created_gate:
            resume()
        self.stdout.write(
            self.style.SUCCESS(f"Renderer baseline cutover passed: {len(devices)} devices, {repaired} repairs")
        )
