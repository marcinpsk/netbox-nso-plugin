# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Capture the pre-candidate renderer with the populated native graph."""

import json
import os
from inspect import signature
from pathlib import Path

from django.test import TestCase

from netbox_nso_plugin import delivery

from ._candidate_render_case import persist_candidates, render_candidates
from ._outbox_case import make_managed
from .mixins import IntentPushResetMixin


class TestPersistedRenderCapture(IntentPushResetMixin, TestCase):
    def test_capture(self):
        if not os.environ.get("NSO_CAPTURE_PERSISTED_RENDER"):
            self.skipTest("Run only in the detached pre-candidate checkout.")
        assert "candidates" not in signature(delivery.render).parameters, "Capture requires the pre-candidate renderer."
        device, management = make_managed("render-characterization", 1788)
        route_ids = {}
        for index in (2, 1):
            rows, relations = render_candidates(device, management, index, rich=True)
            persist_candidates(rows, relations)
            route_ids[rows[4].static_route_id] = index
        actual = {}
        for key in delivery.delivery_keys():
            payload = delivery.render(key, device.pk, management.adapter_device_id).payload
            if key == "static_route":
                payload = [{**item, "route_id": route_ids[item["route_id"]]} for item in payload]
            actual[key] = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        target = Path(__file__).with_name("_persisted_render_snapshot.py")
        target.write_text(
            "# SPDX-License-Identifier: Apache-2.0\n# Copyright (C) 2026 Marcin Zieba\n"
            "# Captured from d0982649 with the populated native graph.\nPERSISTED_RENDERS = " + repr(actual) + "\n",
            encoding="utf-8",
        )
