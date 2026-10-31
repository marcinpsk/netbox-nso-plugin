# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Paired cutover checks that must pass before the fleet gate can resume."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from . import adapter_client
from .deployment_probe import _require, authority
from .models import NSOAcceptedSelection, NSOApplyAttempt, NSOApplyExecution, NSODeviceManagement

PLUGIN_MIGRATION_HEAD = "0040_snapshot_owner_epoch_index"


def check_contracts():
    """Require the installed plugin migration leaf with no pending migrations."""
    executor = MigrationExecutor(connection)
    _require(not executor.migration_plan(executor.loader.graph.leaf_nodes()), "Database migration heads are pending")
    _require(
        executor.loader.graph.leaf_nodes("netbox_nso_plugin") == [("netbox_nso_plugin", PLUGIN_MIGRATION_HEAD)],
        "Unexpected plugin migration head",
    )


def check_settled_probes(attempt_ids, device_id, *, quiesced_at):
    """Require complete success, peer refusal, and router refusal through settlement."""
    _require(device_id is not None and attempt_ids, "Settled verification probes are required")
    cases = set()
    for attempt_id in attempt_ids:
        probe = authority(attempt_id, device_id, completed=True)
        _require(
            probe.attempt.created_at >= quiesced_at
            and probe.execution.terminal_at >= quiesced_at
            and probe.execution.consumed_at >= quiesced_at,
            "Historical settlement cannot verify the current deployment gate",
        )
        generation = probe.execution.terminal_projection
        outcomes = generation["row_outcomes"]
        for snapshot in probe.attempt.overlay_snapshots.all():
            owner_outcomes = [
                outcome["disposition"]
                for outcome in outcomes
                if any(
                    ref["subject"] == outcome["row"]["subject"] and ref["key"] == outcome["row"]["key"]
                    for ref in (contributor["logical_ref"] for contributor in snapshot.contributors["rows"])
                )
            ]
            _require(owner_outcomes, "Probe snapshot has no matching outcome")
            expected = next(
                disposition
                for disposition in ("refused", "unavailable", "not_applied", "in_sync")
                if disposition in owner_outcomes
            )
            _require(snapshot.settlement_disposition == expected, "Probe contributor disposition changed")
        subjects = [outcome["row"]["subject"] for outcome in outcomes]
        _require(
            subjects.count("bgp.router") == 1 and subjects.count("bgp.router.scope.peer") >= 2,
            "Verification must include the full router and two peers",
        )
        dispositions = {outcome["disposition"] for outcome in outcomes}
        if dispositions == {"in_sync"} and generation["carrier_job_result"]["commit_acknowledged"] is True:
            cases.add("success")
        refused = [outcome for outcome in outcomes if outcome["disposition"] == "refused"]
        if len(refused) == 1 and dispositions == {"refused", "not_applied"}:
            if refused[0]["row"]["subject"] == "bgp.router" and any(
                cause["row"] == refused[0]["row"] and cause["field"] == "bgp.router.router_id"
                for cause in refused[0]["refusals"]
            ):
                cases.add("router_refusal")
            if refused[0]["row"]["subject"] == "bgp.router.scope.peer":
                cases.add("peer_refusal")
    _require(cases == {"success", "router_refusal", "peer_refusal"}, "Settlement verification scenarios are incomplete")
    preview = adapter_client.get_apply_diff(NSODeviceManagement.objects.get(device_id=device_id).adapter_device_id)
    _require(
        set(preview) == {"device_id", "outformat", "diffs", "generation_id", "document_digest", "status", "refusals"},
        "Preview does not use the cutover contract",
    )


def check_active_streams():
    """Require current provenance for local owners and every adapter-held stream."""
    from . import delivery
    from .apply_settlement import (
        certified_projection,
        generation_certificate_candidates,
        terminal_carrier_requires_certificate,
    )
    from .durable_capture import _validate_snapshot_membership, _wire, mapping_epoch
    from .intent_drift import _delivery_key, _scopes
    from .models import NSOIntentRevision
    from .selection_manifest import frozen_manifest
    from .settlement_resolution import acknowledged_obligations

    _require(
        not NSOApplyAttempt.objects.filter(lifecycle="open", trigger_kind="manual").exists()
        and not NSOApplyExecution.objects.filter(consumption_state__in=("unconsumed", "unresolved")).exists(),
        "Unanswered or unconsumed attempts remain",
    )
    table_scopes = {table: _delivery_key(scope) for scope in _scopes() for table in scope["tables"]}
    table_scopes.update({"lag_bundle_intent": "lacp", "switchport_intent": "switchport"})
    for management in NSODeviceManagement.objects.filter(adapter_device_id__isnull=False):
        _page, incarnation = adapter_client.get_settlement_feed(
            management.adapter_device_id,
            after_settle_seq=0,
            limit=1,
        )
        summary = adapter_client.get_intent_summary(management.adapter_device_id)
        _require(
            isinstance(summary, dict)
            and set(summary) == {"device_id", "scopes", "pending_clear"}
            and summary["device_id"] == management.adapter_device_id
            and isinstance(summary["scopes"], dict)
            and summary["pending_clear"] == {},
            "Adapter-held intent inventory is unresolved",
        )
        remote = summary["scopes"]
        _require(
            all(
                isinstance(counts, dict)
                and set(counts) == {"count", "applied", "failed"}
                and all(type(value) is int and value >= 0 for value in counts.values())
                and counts["count"] > 0
                and counts["applied"] <= counts["count"]
                and counts["failed"] <= counts["count"]
                for counts in remote.values()
            ),
            "Adapter-held intent counts are malformed",
        )
        _require(set(remote) <= set(table_scopes) | {"redistribution_intent"}, "Unknown adapter-held intent table")
        required = {table_scopes[table] for table in remote if table in table_scopes}
        selections = list(
            NSOAcceptedSelection.objects.filter(management=management).exclude(binding_state="invalidated")
        )
        for selection in selections:
            _require(
                selection.mapping_epoch == mapping_epoch(management)
                and selection.acknowledged_at is not None
                and selection.publication_state == "sent",
                "An active stream has no verified current preparation",
            )
            _validate_snapshot_membership(selection)
        rendered = {
            scope: delivery.render(scope, management.device_id, management.adapter_device_id)
            for scope in delivery.delivery_keys()
        }
        manifests = {
            scope: frozen_manifest(management, scope, document.payload)[0] for scope, document in rendered.items()
        }
        _require(all(manifests[scope] for scope in required), "Adapter-held intent has no local owner provenance")
        required.update(scope for scope, manifest in manifests.items() if manifest)
        current = {}
        for scope in required:
            entry = delivery.delivery_keys()[scope]
            revision = NSOIntentRevision.objects.filter(device_id=management.device_id, scope=scope).first()
            _require(revision is not None, "An active stream has no captured plugin revision")
            digest, _body = _wire(
                rendered[scope],
                marking_mode=entry.marking_mode,
                direct=(revision.revision, ()) if scope in delivery.direct_keys() else None,
            )
            matches = [
                selection
                for selection in selections
                if selection.stream == entry.section
                and selection.plugin_revision == revision.revision
                and selection.request_digest == digest
            ]
            _require(
                matches and all(selection.store_incarnation == incarnation for selection in matches),
                "An adapter-held or locally owned stream has no exact current selection in this store",
            )
            current[entry.section] = max(matches, key=lambda selection: selection.pk)
        receipts = adapter_client.get_intent_receipts(device_id=management.adapter_device_id)
        for receipt in receipts["receipts"]:
            _require(
                any(
                    selection.stream == receipt["section"]
                    and selection.source_push_seq == receipt["push_seq"]
                    and selection.request_digest == receipt["request_digest"]
                    for selection in selections
                ),
                "An adapter receipt has no captured local selection",
            )
        generations = adapter_client.list_device_generations(management.adapter_device_id)
        adapter_client.raise_evidence_errors(generations)
        terminal = [generation for generation in generations if terminal_carrier_requires_certificate(generation)]
        certificates = generation_certificate_candidates(management, incarnation, terminal)
        acknowledged = acknowledged_obligations(management, incarnation)
        for generation in generations:
            if (generation["generation_id"], generation["carrier_job_id"]) in acknowledged:
                # Resolved without a certificate: no success or provenance claim exists for it.
                continue
            if terminal_carrier_requires_certificate(generation):
                identity = generation["generation_id"], generation["carrier_job_id"]
                _require(
                    certified_projection(generation, certificates.get(identity, ())) is not None,
                    "missing_generation_evidence: A terminal remote carrier has no verified consumed execution evidence",
                )
            for stream, revision in generation["stream_revisions"].items():
                source = generation["source_push_seq"][stream]
                _require(
                    any(
                        selection.stream == stream
                        and (
                            selection.source_push_seq == source
                            if source is not None
                            else selection.selection_revision == revision
                        )
                        for selection in selections
                    ),
                    "A repeatable generation has no captured local selection",
                )
        if "redistribution_intent" in remote:
            count = remote["redistribution_intent"].get("count")
            captured = sum(
                owner["model_label"] == "netbox_nso_plugin.nsoredistributionstate"
                for selection in current.values()
                for owner in selection.contributor_manifest
            )
            _require(
                type(count) is int and count == captured,
                "Adapter redistribution membership has no exact prepared ownership",
            )
        evidence = adapter_client.get_deployment_evidence(management.adapter_device_id, [])
        adapter_client.raise_evidence_errors(evidence)
        _require(
            not evidence["held_jobs"] and not evidence["write_work_pending"] and evidence["pending_generations"] == 0,
            "Adapter work is still in flight",
        )
        _page, final_incarnation = adapter_client.get_settlement_feed(
            management.adapter_device_id,
            after_settle_seq=0,
            limit=1,
        )
        _require(incarnation == final_incarnation, "Adapter store changed during readiness checks")
