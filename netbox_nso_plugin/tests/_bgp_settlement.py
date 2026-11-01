# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Share BGP settlement helpers between test classes built on BGPCaptureCase."""

import copy
from datetime import timedelta
from uuid import UUID

from django.urls import reverse

from netbox_nso_plugin.models import NSOApplyAttempt
from netbox_nso_plugin.refusal_registry import OVERLAYS

from ._settlement_wire import deployment_wire, generation_wire, job_wire
from .test_apply_selector import _promoted


def promoted_streams(selected, streams, *, mode="networked"):
    """Promote only the fixture's populated streams and skip its empty preparations."""
    response = _promoted(selected)
    response["generations"] = response["generations"][:1]
    link = response["generations"][0]
    link["mode"] = mode
    for key in ("stream_revisions", "source_push_seq"):
        link[key] = {stream: value for stream, value in link[key].items() if stream in streams}
    response["skipped"] = {stream: "no_receipt" for stream in selected if stream not in streams}
    return response


class BGPSettlementHelpers:
    def prepare(self, *, redistribution=False, mode="networked"):
        def admission(selected):
            return 202, promoted_streams(selected, {"bgp"}, mode=mode)

        self.adapter.apply_response = admission
        scope = self.scope()
        self.owners = [
            self.router_owner(),
            self.peer(scope, "198.18.0.2"),
            self.peer(scope, "198.18.0.3", status="in_sync"),
        ]
        if redistribution:
            self.owners.append(self.redistribution(scope))
        self.prior = {
            (row._meta.label_lower, row.pk): (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id)
            for row in self.owners
        }
        answer = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(answer.status_code, 200, answer.content)
        self.attempt = NSOApplyAttempt.objects.get(pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"]))

    def prepare_automatic(self):
        from netbox_nso_plugin import drain

        scope = self.scope()
        self.owners = [
            self.router_owner(),
            self.peer(scope, "198.18.0.2"),
            self.peer(scope, "198.18.0.3", status="in_sync"),
        ]
        self.prior = {
            (row._meta.label_lower, row.pk): (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id)
            for row in self.owners
        }
        drain.drain_intent_outbox()
        self.attempt = NSOApplyAttempt.objects.get(
            trigger_kind="push", overlay_snapshots__model_label=self.owners[0]._meta.label_lower
        )

    def terminal(self, refused=1, *, disposition=None):
        import json
        from pathlib import Path

        cause = json.loads((Path(__file__).parent / "fixtures/refusal_settlement.json").read_text(encoding="utf-8"))[
            "bgp"
        ]["refusal"]
        snapshots = list(self.attempt.overlay_snapshots.order_by("model_label", "overlay_pk"))
        selected = self.attempt.selected

        def identity(ref, accepted_key="acceptance_identity"):
            return {
                "stream": "bgp",
                "row": {"subject": ref["subject"], "key": ref["key"]},
                "source_push_seq": selected["bgp"],
                "frozen_revision": 7,
                accepted_key: self.stamp.isoformat(),
                "generation_id": 81,
                "settlement_cohort": 9,
            }

        owner = self.owners[refused] if refused is not None else None
        outcomes = []
        for snapshot in snapshots:
            ref = snapshot.contributors["rows"][0]["logical_ref"]
            failed = owner is not None and (snapshot.model_label, snapshot.overlay_pk) == (
                owner._meta.label_lower,
                owner.pk,
            )
            outcome_cause = {**cause, "row": {"subject": ref["subject"], "key": ref["key"]}}
            outcomes.append(
                {
                    **identity(ref),
                    "carrier": identity(snapshot.contributors["rows"][0]["carrier_ref"], "accepted_at"),
                    "disposition": disposition or ("refused" if failed else "not_applied" if owner else "in_sync"),
                    "refusals": [outcome_cause] if failed else [],
                }
            )
        failed = next((row for row in outcomes if row["disposition"] == "refused"), None)
        causes = (
            []
            if failed is None
            else [
                {
                    **failed["refusals"][0],
                    "logical_rows": [
                        {key: value for key, value in failed.items() if key not in {"disposition", "refusals"}}
                    ],
                    "provenance": "current",
                    "sender_phase": "pre_commit_dry_run",
                }
            ]
        )
        counter = dict.fromkeys(("in_sync", "apply_failed", "not_applied", "unavailable"), 0)
        carriers = {}
        priority = {"in_sync": 0, "not_applied": 1, "unavailable": 2, "refused": 3}
        for outcome in outcomes:
            key = json.dumps(outcome["carrier"], sort_keys=True)
            previous = carriers.get(key)
            if previous is None or priority[outcome["disposition"]] > priority[previous["disposition"]]:
                carriers[key] = {**outcome["carrier"], "disposition": outcome["disposition"]}
        for carrier in carriers.values():
            counter["apply_failed" if carrier["disposition"] == "refused" else carrier["disposition"]] += 1
        result = {
            "row_outcomes": outcomes,
            "refusals": causes,
            "settlement_owners": list(carriers.values()),
            "bgp_count_by_outcome": counter,
            "commit_acknowledged": owner is None,
        }
        return {
            "generation_id": 81,
            "seq": 4,
            "status": "failed" if owner else "settled",
            "sections": ["bgp"],
            "stream_revisions": {"bgp": 7},
            "source_push_seq": {"bgp": selected["bgp"]},
            "carrier_job_id": 501,
            "carrier_job_status": "succeeded",
            "carrier_job_result": result,
            "carrier_job_error": None,
            "updated_at": (self.stamp + timedelta(days=1)).isoformat(),
            "row_outcomes": outcomes,
            "refusals": causes,
        }

    def serve_terminal_evidence(
        self,
        generation,
        *,
        epoch_change=False,
        manual=False,
        head=True,
        pruned=False,
        settle_seq=1,
        earlier_generations=(),
        listed_carrier=None,
    ):
        from ._adapter_http import make_response
        from .test_durable_bgp_capture import _CaptureHandler

        class EvidenceHandler(_CaptureHandler):
            def end_headers(handler):
                handler.send_header("X-Store-Incarnation", handler.server.evidence_incarnation)
                super().end_headers()

        self.server.RequestHandlerClass = EvidenceHandler
        self.server.evidence_incarnation = "00000000-0000-4000-8000-000000000004"
        from netbox_nso_plugin import adapter_client

        adapter_client.reset_session()
        original = self.adapter._handle
        self.evidence_requests = []
        feed_requests = 0

        def handle(method, url, **kwargs):
            nonlocal feed_requests
            self.evidence_requests.append((method, url))
            if method == "GET" and url.endswith("/jobs"):
                feed_requests += 1
                if epoch_change and feed_requests > 1:
                    self.server.evidence_incarnation = "00000000-0000-4000-8000-000000000003"
                job = {
                    "id": generation["carrier_job_id"],
                    "type": "apply",
                    "device_id": self.management.adapter_device_id,
                    "status": generation["carrier_job_status"],
                    "result": generation["carrier_job_result"],
                    "row_outcomes": generation["row_outcomes"],
                    "refusals": generation["refusals"],
                    "error": None,
                    "context": None,
                    "created_at": self.stamp.isoformat(),
                    "updated_at": generation["updated_at"],
                    "started_at": self.stamp.isoformat(),
                    "heartbeat_at": None,
                    "settle_seq": settle_seq,
                }
                after = int(kwargs["params"]["after_settle_seq"])
                return make_response(200, [] if pruned or job["settle_seq"] <= after else [job_wire(job)])
            if method == "GET" and url.endswith("/generations"):
                return make_response(
                    200,
                    [
                        generation_wire(row, self.management.adapter_device_id)
                        for row in [
                            *earlier_generations,
                            {
                                "generation_id": generation["generation_id"],
                                "seq": generation["seq"],
                                "status": generation["status"],
                                "job_id": None if pruned else generation["carrier_job_id"],
                                "carrier_job_id": generation["carrier_job_id"],
                                "carrier_job_status": generation["carrier_job_status"],
                                **(listed_carrier or {}),
                                "mode": "networked",
                                "settlement_cohort": 9,
                                "digest": "a" * 64,
                                "stream_revisions": generation["stream_revisions"],
                                "source_push_seq": generation["source_push_seq"],
                                "created_at": self.stamp.isoformat(),
                                "updated_at": generation["updated_at"],
                            },
                        ]
                    ],
                )
            if method == "POST" and url.endswith("/deployment-evidence"):
                head_row = dict(generation)
                head_row.update(
                    mode="networked", settlement_cohort=9, apply_attempt_id=None, created_at=self.stamp.isoformat()
                )
                return make_response(
                    200,
                    deployment_wire(
                        {
                            "device_id": self.management.adapter_device_id,
                            "head": head_row if head else None,
                            "blocked": generation["status"] == "failed",
                            "write_work_pending": False,
                            "held_jobs": [],
                            "pending_generations": 0,
                            "attempts": [
                                {
                                    "apply_attempt_id": str(self.attempt.pk),
                                    "admission_state": "admitted",
                                    "http_status": 202,
                                    "response": self.attempt.response,
                                    "generations": [generation],
                                }
                            ]
                            if manual and str(self.attempt.pk) in kwargs["json"]["apply_attempt_ids"]
                            else [],
                            "unknown_apply_attempt_ids": [],
                        }
                    ),
                )
            return original(method, url, **kwargs)

        self.adapter._handle = handle

    def evidence(self, refused=None, dispositions=None):
        import json
        from pathlib import Path

        catalog = json.loads((Path(__file__).parent / "fixtures/refusal_settlement.json").read_text(encoding="utf-8"))[
            "bgp"
        ]["refusal"]
        outcomes, attributed, revisions, sources = [], [], {}, {}
        for snapshot in self.attempt.overlay_snapshots.all():
            for contributor in snapshot.contributors["rows"]:
                stream = contributor["stream"]
                selector = contributor["source_selector"]
                source = selector.get("source_push_seq")
                revision = selector.get("selection_revision", 7)
                revisions[stream], sources[stream] = revision, source
                ref, parent = contributor["logical_ref"], contributor["carrier_ref"]
                carrier = {
                    "stream": stream,
                    "row": {key: parent[key] for key in ("subject", "key")},
                    "source_push_seq": source,
                    "frozen_revision": revision,
                    "accepted_at": self.stamp.isoformat(),
                    "generation_id": 81,
                    "settlement_cohort": 9,
                }
                identity = {key: value for key, value in carrier.items() if key not in {"row", "accepted_at"}}
                identity.update(
                    row={key: ref[key] for key in ("subject", "key")},
                    acceptance_identity=self.stamp.isoformat(),
                    carrier=carrier,
                )
                failed = refused is not None and (snapshot.model_label, snapshot.overlay_pk) == (
                    refused._meta.label_lower,
                    refused.pk,
                )
                disposition = (dispositions or {}).get(ref["subject"], "refused" if failed else "not_applied")
                causes = []
                if disposition == "refused":
                    cause = {**catalog, "family": ref["family"], "row": identity["row"]}
                    causes.append(cause)
                    attributed.append(
                        {
                            **cause,
                            "logical_rows": [copy.deepcopy(identity)],
                            "provenance": "current",
                            "sender_phase": "commit",
                        }
                    )
                outcomes.append({**identity, "disposition": disposition, "refusals": causes})
        priority = {"in_sync": 0, "not_applied": 1, "unavailable": 2, "refused": 3}
        carriers = {}
        for outcome in outcomes:
            carrier = outcome["carrier"]
            key = (carrier["stream"], carrier["row"]["subject"], carrier["row"]["key"])
            existing = carriers.get(key)
            if existing is None or priority[outcome["disposition"]] > priority[existing["disposition"]]:
                carriers[key] = {**carrier, "disposition": outcome["disposition"]}
        counts = {}
        for carrier in carriers.values():
            spec = next(spec for spec in OVERLAYS.values() if carrier["row"]["subject"] in spec.subjects)
            key = spec.counter_keys[0] if len(spec.counter_keys) == 1 else f"{carrier['stream']}_count_by_outcome"
            count = counts.setdefault(key, dict.fromkeys(("in_sync", "apply_failed", "not_applied", "unavailable"), 0))
            count["apply_failed" if carrier["disposition"] == "refused" else carrier["disposition"]] += 1
        result = {
            **counts,
            "row_outcomes": outcomes,
            "refusals": attributed,
            "settlement_owners": list(carriers.values()),
            "commit_acknowledged": not attributed,
        }
        return {
            "generation_id": 81,
            "seq": 4,
            "status": "failed" if attributed else "settled",
            "sections": sorted(revisions),
            "stream_revisions": revisions,
            "source_push_seq": sources,
            "carrier_job_id": 501,
            "carrier_job_status": "succeeded",
            "carrier_job_result": result,
            "carrier_job_error": None,
            "row_outcomes": outcomes,
            "refusals": attributed,
            "updated_at": self.stamp.isoformat(),
        }

    def _serve_bgp_observation(self, generation=None, *, drift_peer=None):
        from ._adapter_http import make_response
        from ._routing_observation_case import routing_observation
        from ._scope_observation_case import entry
        from .test_read_gate import _rs

        self.serve_terminal_evidence(self.terminal() if generation is None else generation)
        self.server.evidence_incarnation = self.management.adapter_incarnation
        original = self.adapter._handle
        observation = routing_observation(
            "bgp",
            document={
                "routers": [
                    entry(
                        asn="64512",
                        router_id="198.18.0.1",
                        scope=[
                            entry(
                                vrf="",
                                address_family=[entry(afi="ipv4-unicast")],
                                peer_group=[],
                                peer=[
                                    entry(
                                        peer_address=row.peer_address_str,
                                        remote_as="64513",
                                        enabled=row.pk != getattr(drift_peer, "pk", None),
                                        password_present=True,
                                        peer_address_family=[entry(afi="ipv4-unicast", enabled=True)],
                                    )
                                    for row in self.owners[1:]
                                ],
                            )
                        ],
                    )
                ],
                "present": ["routers"],
                "unprojectable": [],
            },
        )
        peers = [
            {
                "peer_address": row.peer_address_str,
                "remote_as": "64513",
                "enabled": row.pk != getattr(drift_peer, "pk", None),
                "password": "placeholder-secret-canary",
                "address_families": [{"af": "ipv4-unicast", "enabled": True}],
            }
            for row in self.owners[1:]
        ]
        document = {
            "routers": [
                {
                    "asn": "64512",
                    "router_id": "198.18.0.1",
                    "scopes": [{"vrf": "", "address_families": ["ipv4-unicast"], "peers": peers, "peer_groups": []}],
                }
            ],
            "read_state": _rs(),
            "observation": observation,
        }

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/bgp-config"):
                return make_response(200, document)
            if method == "GET" and url.endswith("/bfd"):
                return make_response(200, {"interfaces": []})
            return original(method, url, **kwargs)

        self.adapter._handle = handle

    def apply_owners(self, owners, *, prior_status="accepted", prior_error=None):
        self.owners = owners
        self.prior = {}
        for row in owners:
            self.bookkeeping(row, status=prior_status)
            if prior_error is not None:
                from ._outbox_case import mirror_update

                mirror_update(row, last_apply_error=prior_error)
            row.refresh_from_db()
            self.prior[(row._meta.label_lower, row.pk)] = (
                row.status,
                row.last_apply_at,
                copy.deepcopy(row.last_apply_error),
                row.apply_attempt_id,
            )
        from netbox_nso_plugin.delivery import delivery_keys

        streams = {
            delivery_keys()[key].section for row in owners for key in OVERLAYS[row._meta.label_lower].delivery_keys
        }
        self.adapter.apply_response = lambda selected: (202, promoted_streams(selected, streams))
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.attempt = NSOApplyAttempt.objects.get(pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"]))

    def settle(self, generation):
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        return settle_terminal_generation(
            self.management, generation, "00000000-0000-4000-8000-000000000004", attempt=self.attempt, settle_seq=1
        )
