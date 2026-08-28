# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Pure ownership lifecycle rules for the first converted scopes."""

from django.test import SimpleTestCase, TestCase


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

    def test_owned_manifest_and_overlay_need_no_transition(self):
        from netbox_nso_plugin.ownership_planner import (
            OwnershipAction,
            OwnershipSignature,
            converted_scope_rules,
            plan_ownership,
        )

        action = plan_ownership(
            converted_scope_rules()["vlan"],
            OwnershipSignature(
                native_present=True,
                native_qualifies=True,
                overlay_present=True,
                overlay_owned=True,
                manifest_state="owned",
            ),
        )

        assert action is OwnershipAction.NONE

    def test_owned_overlay_without_native_content_retracts(self):
        from netbox_nso_plugin.ownership_planner import (
            OwnershipAction,
            OwnershipSignature,
            converted_scope_rules,
            plan_ownership,
        )

        action = plan_ownership(
            converted_scope_rules()["svi"],
            OwnershipSignature(
                native_present=False,
                native_qualifies=False,
                overlay_present=True,
                overlay_owned=True,
            ),
        )

        assert action is OwnershipAction.RETRACT

    def test_absent_unowned_identity_needs_no_transition(self):
        from netbox_nso_plugin.ownership_planner import (
            OwnershipAction,
            OwnershipSignature,
            converted_scope_rules,
            plan_ownership,
        )

        action = plan_ownership(
            converted_scope_rules()["switchport"],
            OwnershipSignature(native_present=False, native_qualifies=False),
        )

        assert action is OwnershipAction.NONE


class TestManifestRetirement(TestCase):
    def test_retire_manifest_identity_only_retires_the_owned_match(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import retire_manifest_identity

        identity = {
            "device_id": 1627,
            "scope": "vlan",
            "native_model_label": "ipam.vlan",
            "native_key": {"group_id": 16, "vid": 27},
        }
        owned = NSOOwnershipManifest.objects.create(**identity)
        detached = NSOOwnershipManifest.objects.create(
            **{**identity, "device_id": 1628},
            ownership_state="detached",
        )

        retire_manifest_identity(
            device_ids={1627, 1628}, **{key: identity[key] for key in identity if key != "device_id"}
        )

        owned.refresh_from_db()
        detached.refresh_from_db()
        self.assertEqual(owned.ownership_state, "retired")
        self.assertEqual(detached.ownership_state, "detached")


class TestConvertedScopeRuleTable(SimpleTestCase):
    def test_converted_scopes_have_reviewed_acquisition_and_retirement_entries(self):
        from netbox_nso_plugin.ownership_planner import converted_scope_rules

        rules = converted_scope_rules()

        assert set(rules) == {
            "bfd",
            "bgp",
            "interface",
            "interface_mtu",
            "ip",
            "isis",
            "isis_flex_algo",
            "lacp",
            "l2_sap",
            "logging",
            "ospf",
            "redistribution",
            "route_policy",
            "snmp",
            "static_route",
            "subinterface",
            "vlan",
            "svi",
            "switchport",
        }
        for scope, rule in rules.items():
            assert rule.scope == scope
            assert rule.native_model_labels
            assert rule.overlay_model_labels
            assert rule.deletion_authority
            assert rule.intentional_semantic_delta
            assert rule.foreign_overlay_delete == "reown"

        retirement_clauses = {
            "bgp": "Foreign native peer deletes no longer delete linked overlays and push a reduced snapshot",
            "route_policy": "Native policy deletes no longer delete per-device overlays and push reduced snapshots",
        }
        for scope, clause in retirement_clauses.items():
            assert clause in rules[scope].intentional_semantic_delta
        assert "missing graph dependencies fail fast" in rules["bgp"].intentional_semantic_delta

    def test_static_route_rule_names_only_acknowledged_lineage(self):
        from netbox_nso_plugin.ownership_planner import converted_scope_rules

        rule = converted_scope_rules()["static_route"]

        assert rule.acknowledged_lineage_field == "last_acked_triple"

    def test_flex_algo_rule_uses_the_native_process_and_algorithm_identity(self):
        from netbox_nso_plugin.ownership_planner import converted_scope_rules

        rule = converted_scope_rules()["isis_flex_algo"]

        assert rule.native_model_labels == ("netbox_routing.isisflexalgo",)
        assert rule.native_key_fields == ("instance_id", "algo_id")
        assert rule.overlay_native_fields == (("netbox_nso_plugin.nsoisisflexalgostate", "isis_flex_algo"),)

    def test_redistribution_rule_uses_its_destination_protocol_as_manifest_scope(self):
        from netbox_nso_plugin.ownership_planner import converted_scope_rules

        rule = converted_scope_rules()["redistribution"]

        assert rule.native_model_labels == ("netbox_routing.redistribution",)
        assert rule.native_key_fields == (
            "destination_type_id",
            "destination_id",
            "source_protocol",
            "source_ref",
        )
        assert rule.manifest_scope_field == "dest_protocol"

    def test_ospf_rule_names_process_and_interface_native_identities(self):
        from netbox_nso_plugin.ownership_planner import converted_scope_rules

        rule = converted_scope_rules()["ospf"]

        assert rule.native_model_labels == (
            "netbox_routing.ospfinstance",
            "netbox_routing.ospfinterface",
        )
        assert rule.native_key_fields_by_model == (
            ("netbox_routing.ospfinstance", ("device_id", "process_id")),
            ("netbox_routing.ospfinterface", ("interface_id",)),
        )

    def test_isis_rule_names_graph_roots_and_keeps_children_as_dependencies(self):
        from netbox_nso_plugin.ownership_planner import converted_scope_rules

        rule = converted_scope_rules()["isis"]

        assert rule.native_model_labels == (
            "netbox_routing.isisinstance",
            "netbox_routing.isisinterface",
        )
        assert rule.native_key_fields_by_model == (
            ("netbox_routing.isisinstance", ("device_id", "process_tag")),
            ("netbox_routing.isisinterface", ("interface_id", "address_family")),
        )
        assert rule.overlay_native_fields == (
            ("netbox_nso_plugin.nsoisisinstancestate", "isis_instance"),
            ("netbox_nso_plugin.nsoisisinterfacestate", "isis_interface"),
        )

    def test_bgp_rule_names_peer_root_and_keeps_graph_rows_as_dependencies(self):
        from netbox_nso_plugin.ownership_planner import converted_scope_rules

        rule = converted_scope_rules()["bgp"]

        assert rule.native_model_labels == ("netbox_routing.bgppeer",)
        assert rule.native_key_fields == ("scope_id", "peer_id", "name")
        assert rule.overlay_native_fields == (("netbox_nso_plugin.nsobgppeerstate", "bgp_peer"),)

    def test_route_policy_rule_names_shared_roots_and_keeps_graph_rows_as_dependencies(self):
        from netbox_nso_plugin.ownership_planner import converted_scope_rules

        rule = converted_scope_rules()["route_policy"]

        assert rule.native_model_labels == (
            "netbox_routing.prefixlist",
            "netbox_routing.communitylist",
            "netbox_routing.aspath",
            "netbox_routing.routemap",
        )
        assert rule.native_key_fields == ("name",)
        assert rule.overlay_native_fields == (("netbox_nso_plugin.nsoroutepolicystate", "assigned_object"),)
