# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared test mixins."""


class IntentPushResetMixin:
    """Reset signals.py module-global intent-push caches before each test.

    ``signals._push_changed`` skips a PUT when the snapshot hash equals the last
    one cached in the module-global ``_last_pushed_hashes`` (a per-process
    change-detection optimisation). ``reset_intent_push_state()`` clears it, but
    it is wired only as a pytest autouse fixture (``conftest.py``); the Django
    ``manage.py test`` runner — which CI uses — ignores conftest, so push-asserting
    tests would see a leaked hash from an earlier test and the push gets skipped
    (``assert_called_once`` → "Called 0 times"). Mix this in BEFORE the TestCase
    base so its ``setUp`` runs and chains via ``super()``.
    """

    def setUp(self):
        super().setUp()
        from netbox_nso_plugin.signals import reset_intent_push_state

        reset_intent_push_state()
