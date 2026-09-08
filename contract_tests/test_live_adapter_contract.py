# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Cross-repository contract checks against a disposable live nso-adapter.

This module intentionally lives outside the normal test tree: the fast suite's
session guard must never permit network access. CI invokes this module alone
after starting an isolated adapter and PostgreSQL store.
"""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from django.conf import settings
from django.utils.functional import empty

pytestmark = pytest.mark.skipif(
    os.environ.get("NSO_LIVE_ADAPTER_TEST") != "1",
    reason="requires the dedicated disposable live-adapter test environment",
)


_UNSET = object()


def test_generation_dispositions_match_the_committed_adapter_openapi_enum():
    """Pin every adapter generation status to one plugin disposition."""
    from netbox_nso_plugin.apply_settlement import GENERATION_DISPOSITIONS

    snapshot = Path(__file__).resolve().parents[2] / "nso-adapter" / "tests" / "api" / "openapi_snapshot.json"
    openapi = json.loads(snapshot.read_text(encoding="utf-8"))
    openapi_statuses = set(openapi["components"]["schemas"]["GenerationStatus"]["enum"])

    assert set(GENERATION_DISPOSITIONS) == openapi_statuses


@contextmanager
def _live_client():
    """Point the real plugin HTTP client at the live adapter for the block.

    ``PLUGINS_CONFIG`` is process-global and the auth arm below poisons the token in it, so the
    process state is put back on the way out — a later test must not inherit either. Three entry
    states, each with its own way back: unconfigured settings (this job runs with no settings
    module), configured without the key, and configured with a value to keep.
    """
    plugin_config = {
        "netbox_nso_plugin": {
            "adapter_url": os.environ["NSO_LIVE_ADAPTER_URL"],
            "adapter_token": os.environ["NSO_LIVE_ADAPTER_TOKEN"],
        }
    }
    configured_here = not settings.configured
    previous = _UNSET if configured_here else getattr(settings, "PLUGINS_CONFIG", _UNSET)
    if configured_here:
        settings.configure(PLUGINS_CONFIG=plugin_config)
    else:
        settings.PLUGINS_CONFIG = plugin_config

    import netbox_nso_plugin.adapter_client as client

    client.reset_config_cache()
    client.reset_session()
    try:
        yield client
    finally:
        if configured_here:
            settings._wrapped = empty  # settings.configure() has no public undo
        elif previous is _UNSET:
            delattr(settings, "PLUGINS_CONFIG")
        else:
            settings.PLUGINS_CONFIG = previous
        client.reset_config_cache()
        client.reset_session()


def test_the_live_client_restores_every_process_settings_state():
    """The one pin here that needs no adapter: the context owns process-global state.

    The auth arm points ``adapter_token`` at a deliberately wrong value, so a context that
    exits without restoring hands every later test a client that cannot authenticate. All
    three entry states have to come back to themselves, and the two absent ones are the trap:
    a context that only ever assigns leaves its replacement installed, which is then the
    "previous" value the next entry keeps.
    """

    def poison_and_fail():
        """Enter, replace the config, poison its token, and leave by an exception."""
        with pytest.raises(RuntimeError), _live_client():
            settings.PLUGINS_CONFIG["netbox_nso_plugin"]["adapter_token"] = "intentionally-wrong-test-token"
            raise RuntimeError("the block failed with the config replaced and its token poisoned")

    settings._wrapped = empty  # this job's own entry state: no settings module at all
    poison_and_fail()
    assert not settings.configured, "the live-client context left the process settings configured"

    settings._wrapped = empty
    settings.configure()  # configured, but holding no PLUGINS_CONFIG of its own
    poison_and_fail()
    assert not hasattr(settings, "PLUGINS_CONFIG"), "the live-client context leaked its PLUGINS_CONFIG"

    kept = {"netbox_nso_plugin": {"adapter_url": "https://kept.invalid", "adapter_token": "kept-token"}}
    settings.PLUGINS_CONFIG = kept
    poison_and_fail()
    assert settings.PLUGINS_CONFIG is kept, "the live-client context did not put the previous config back"
    assert kept["netbox_nso_plugin"]["adapter_token"] == "kept-token", "the poisoned token reached the kept config"

    settings._wrapped = empty  # and back to the unconfigured state this job starts in


def _entry(route_id, generation, **overrides):
    """One static-route intent entry in the plugin's own wire shape."""
    entry = {
        "route_id": route_id,
        "generation": generation,
        "vrf": "",
        "prefix": "10.9.0.0/16",
        "next_hop": "10.0.0.1",
        "permanent": False,
        "tag": None,
        "metric": 3,
    }
    entry.update(overrides)
    if entry["metric"] is None:
        # The plugin omits an unset metric, and an omitted leaf is a CLEAR to the adapter.
        del entry["metric"]
    return entry


def _put_static_routes(client, push_seq, device_id, routes):
    """Send one production-shaped static-route push with its durable sequence."""
    with client.push_seq(push_seq):
        return client.put_static_route_intent(device_id, routes)


def test_live_adapter_read_and_auth_contract():
    """Exercise the real plugin HTTP client, adapter auth, ORM reads, and response schemas."""
    with _live_client() as client:
        devices = client.list_devices()
        assert isinstance(devices, list)

        failover = client.get_failover_config()
        assert {
            "enabled",
            "deployment_enabled",
            "primary_probe_interval",
            "oob_probe_interval",
            "failure_threshold",
            "success_threshold",
            "probe_timeout",
            "active_probe_timeout",
            "probe_concurrency",
            "max_flips_per_tick",
            "sync_from_after_switch",
        } == set(failover)

        settings.PLUGINS_CONFIG["netbox_nso_plugin"]["adapter_token"] = "intentionally-wrong-test-token"
        client.reset_config_cache()
        with pytest.raises(client.AdapterError) as exc_info:
            client.list_devices()
        assert exc_info.value.code == "unauthorized"


@contextmanager
def _devices(client, count):
    """Onboard *count* throwaway adapter devices and offboard them afterwards."""
    tag = uuid4().hex[:8]
    base = int(uuid4().int % 1_000_000) + 1_000_000
    made = []
    try:
        for index in range(count):
            made.append(client.onboard_device("contract", f"contract-{tag}-{index}", base + index))
        yield [row["id"] for row in made]
    finally:
        for row in made:
            try:
                client.delete_device(row["id"])
            except client.AdapterError:  # best effort — the store is disposable
                pass


def _settle(client, device_id, *, previously=0):
    """Run one real device action to its terminal state and return the finished job.

    The instance the contract adapter is configured with points at a closed port, so the
    job fails — which is what this needs. A failure is a terminal write down the same
    ``terminalize`` path a success takes, so it allocates a real settlement sequence.
    """
    client.trigger_sync(device_id)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        jobs, _ = client.get_settlement_feed(device_id, after_settle_seq=previously, limit=10)
        if jobs:
            return jobs[0]
        time.sleep(0.5)
    raise AssertionError(f"no terminal job for adapter device {device_id} within 60s")


def test_a_timos_metric_edit_creates_no_pending_clear():
    """P1.3's owed adapter half, joined: the plugin's own client pushes, the real store answers.

    Nokia's default preference 5 used to be suppressed on the plugin's wire, so an operator
    edit 3 → 5 arrived as an *omission* — which the adapter reads NED-agnostically as "clear
    this leaf", recording a clear and owing the device a networked retract for a value it
    already had. The plugin now always sends the metric.

    The fingerprint is the evidence available over the contract, and it is the adapter's
    own: a SHA-256 over the exact wire entry the **stored** row renders. Equal to a device
    that was pushed 5 once means the edited device's store really ends holding 5. The
    ``pending_clear`` column itself has no wire form, so the carrier half of this pin is
    asserted where the column can be read —
    ``nso-adapter/tests/api/test_static_route_pending_clear.py::test_p1_3_a_timos_metric_edit_records_no_clear_and_no_removal``.
    """
    with _live_client() as client, _devices(client, 4) as ids:
        edited, cleared, control_five, control_three = ids

        _put_static_routes(client, 1, edited, [_entry(41, 101, metric=3)])
        _put_static_routes(client, 2, edited, [_entry(41, 102, metric=5)])  # the 3 → 5 edit

        _put_static_routes(client, 3, cleared, [_entry(41, 201, metric=3)])
        _put_static_routes(client, 4, cleared, [_entry(41, 202, metric=None)])  # the old wire

        _put_static_routes(client, 5, control_five, [_entry(41, 301, metric=5)])
        _put_static_routes(client, 6, control_three, [_entry(41, 401, metric=3)])

        def fingerprint(device_id):
            routes = client.get_static_route_intent(device_id)["routes"]
            assert [row["route_id"] for row in routes] == [41], routes
            return routes[0]["fingerprint"]

        five, three = fingerprint(control_five), fingerprint(control_three)
        assert five != three, "the fingerprint does not move with the metric, so it proves nothing"

        assert fingerprint(edited) == five, "the store did not end holding 5"
        assert fingerprint(cleared) not in (five, three), "an omitted metric was not read as a clear"

        # And the edit owes the device nothing: no removal job carries a retract for it.
        assert [job for job in client.list_jobs(edited) if job["type"] == "removal"] == []


def test_an_identity_edit_is_a_replacement_not_a_delete_plus_insert():
    """The adapter half of the identity edit: the pk decides, so A→B updates one row.

    The plugin names every route by its NetBox pk. That is what lets the store tell a
    *replacement* from an unrelated delete plus insert — and getting it wrong is not a
    cosmetic difference: a delete writes a tombstone and queues a networked removal that
    retracts the route from the device, moments before the insert puts it back.

    Only the pk half is expressible here. Whether the successful apply then advances the
    row's ``deployed_key`` needs a device write, which needs the RESTCONF boundary
    OQ-R3-4 left to the R5 live gate.
    """
    with _live_client() as client, _devices(client, 1) as (device_id,):
        _put_static_routes(client, 7, device_id, [_entry(41, 101, prefix="10.9.0.0/16")])
        _put_static_routes(client, 8, device_id, [_entry(41, 102, prefix="10.9.0.0/24")])  # A → B

        routes = client.get_static_route_intent(device_id)["routes"]
        assert [row["route_id"] for row in routes] == [41], f"the edit did not land on one row: {routes}"
        assert routes[0]["generation"] == 102
        assert [job for job in client.list_jobs(device_id) if job["type"] == "removal"] == [], (
            "the identity edit read as a delete plus an insert and queued a retract"
        )


def test_the_ordered_settlement_feed_contract():
    """S3's feed, over the real socket: the client's own parameters, validation and header.

    The in-suite settlement pins run against a double, so the shape of the request the
    plugin builds and the shape of the answer it parses are only ever checked against each
    other there. This is the one place both sides are real, and the rows are real terminal
    jobs the adapter sequenced itself.
    """
    with _live_client() as client, _devices(client, 1) as (device_id,):
        first = _settle(client, device_id)
        second = _settle(client, device_id, previously=first["settle_seq"])

        page, incarnation = client.get_settlement_feed(device_id, after_settle_seq=0, limit=100)
        assert incarnation, "the cursor epoch cannot be compared without X-Store-Incarnation"
        assert [job["id"] for job in page] == [first["id"], second["id"]], "the feed is not in commit order"
        assert all(isinstance(job["settle_seq"], int) for job in page), page
        assert first["settle_seq"] < second["settle_seq"]

        # The cursor is a cursor: past the first row, only the second is served.
        after_first, _ = client.get_settlement_feed(device_id, after_settle_seq=first["settle_seq"], limit=100)
        assert [job["id"] for job in after_first] == [second["id"]]

        # The header must be on an EMPTY page too — that is exactly the stale-cursor state.
        past_the_end, still_carried = client.get_settlement_feed(device_id, after_settle_seq=10**9, limit=1)
        assert past_the_end == []
        assert still_carried == incarnation

        with pytest.raises(client.AdapterError) as unscoped:
            client._request("GET", "/api/v1/jobs", params={"order": "asc"})
        assert unscoped.value.code == "validation_error"

        for bad_limit in (0, 5000):
            with pytest.raises(client.AdapterError) as rejected:
                client.get_settlement_feed(device_id, after_settle_seq=0, limit=bad_limit)
            assert rejected.value.code == "validation_error", f"limit={bad_limit} was clamped, not rejected"
