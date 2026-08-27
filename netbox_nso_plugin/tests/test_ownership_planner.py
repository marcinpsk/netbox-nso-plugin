# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Pure ownership lifecycle rules for the first converted scopes."""

from django.test import SimpleTestCase


class TestOwnershipStateSignatures(SimpleTestCase):
    def test_greenfield_native_state_creates_or_acquires_an_overlay(self):
        from netbox_nso_plugin.ownership_planner import (
            OwnershipAction,
            OwnershipSignature,
            converted_scope_rules,
            plan_ownership,
        )

        rule = converted_scope_rules()["vlan"]

        assert (
            plan_ownership(
                rule,
                OwnershipSignature(native_present=True, native_qualifies=True),
            )
            is OwnershipAction.CREATE
        )
        assert (
            plan_ownership(
                rule,
                OwnershipSignature(
                    native_present=True,
                    native_qualifies=True,
                    overlay_present=True,
                ),
            )
            is OwnershipAction.ACQUIRE
        )

    def test_manifest_distinguishes_native_deletion_from_foreign_overlay_deletion(self):
        from netbox_nso_plugin.ownership_planner import (
            OwnershipAction,
            OwnershipSignature,
            converted_scope_rules,
            plan_ownership,
        )

        rule = converted_scope_rules()["switchport"]

        assert (
            plan_ownership(
                rule,
                OwnershipSignature(
                    native_present=False,
                    native_qualifies=False,
                    overlay_present=False,
                    manifest_state="owned",
                ),
            )
            is OwnershipAction.RETRACT
        )
        assert (
            plan_ownership(
                rule,
                OwnershipSignature(
                    native_present=True,
                    native_qualifies=True,
                    overlay_present=False,
                    manifest_state="owned",
                ),
            )
            is OwnershipAction.REOWN
        )

    def test_cleared_ownership_detaches_without_device_retraction(self):
        from netbox_nso_plugin.ownership_planner import (
            OwnershipAction,
            OwnershipSignature,
            converted_scope_rules,
            plan_ownership,
        )

        action = plan_ownership(
            converted_scope_rules()["svi"],
            OwnershipSignature(
                native_present=True,
                native_qualifies=True,
                overlay_present=True,
                overlay_owned=False,
                manifest_state="owned",
            ),
        )

        assert action is OwnershipAction.DETACH

    def test_owned_overlay_without_manifest_records_durable_evidence(self):
        from netbox_nso_plugin.ownership_planner import (
            OwnershipAction,
            OwnershipSignature,
            converted_scope_rules,
            plan_ownership,
        )

        action = plan_ownership(
            converted_scope_rules()["lacp"],
            OwnershipSignature(
                native_present=True,
                native_qualifies=True,
                overlay_present=True,
                overlay_owned=True,
            ),
        )

        assert action is OwnershipAction.RECORD_MANIFEST


class TestConvertedScopeRuleTable(SimpleTestCase):
    def test_first_tranche_has_reviewed_acquisition_and_retirement_entries(self):
        from netbox_nso_plugin.ownership_planner import converted_scope_rules

        rules = converted_scope_rules()

        assert set(rules) == {"lacp", "vlan", "svi", "switchport"}
        for scope, rule in rules.items():
            assert rule.scope == scope
            assert rule.native_model_labels
            assert rule.overlay_model_labels
            assert rule.deletion_authority
            assert rule.intentional_semantic_delta
            assert rule.foreign_overlay_delete == "reown"
