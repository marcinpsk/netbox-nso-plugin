# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Record pytest shard evidence and enforce the combined coverage gate."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import pytest


def require(condition, message):
    if not condition:
        raise ValueError(message)


def pytest_addoption(parser):
    parser.addoption("--ci-shard-report", help="Write collection, completion, and timing evidence to this JSON file.")


def pytest_configure(config):
    if config.getoption("ci_shard_report"):
        config.pluginmanager.register(ShardReport(config), "ci-shard-report")


class ShardReport:
    def __init__(self, config):
        self.config = config
        self.started = time.perf_counter()
        self.collection = {}
        self.workers = []
        self.completed = []
        self.durations = defaultdict(float)
        self.database_seconds = 0.0
        self.pending_database_seconds = 0.0
        self.worker = hasattr(config, "workerinput")

    @pytest.hookimpl(wrapper=True)
    def pytest_collection_modifyitems(self, items):
        self.collection["full"] = [item.nodeid for item in items]
        yield
        self.collection["selected"] = [item.nodeid for item in items]
        self.collection["collection_seconds"] = time.perf_counter() - self.started

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef):
        started = time.perf_counter()
        result = yield
        if fixturedef.argname == "django_db_setup":
            elapsed = time.perf_counter() - started
            self.database_seconds += elapsed
            self.pending_database_seconds += elapsed
        return result

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, call):
        report = yield
        duration = call.duration
        if report.when == "setup":
            duration = max(0.0, duration - self.pending_database_seconds)
            self.pending_database_seconds = 0.0
        report.user_properties.append(("ci_execution_seconds", duration))
        return report

    def pytest_runtest_logreport(self, report):
        if not self.worker and not hasattr(report, "context"):
            self.durations[report.nodeid] += dict(report.user_properties)["ci_execution_seconds"]

    def pytest_runtest_logfinish(self, nodeid):
        if not self.worker:
            self.completed.append(nodeid)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        require(error is None, f"Worker failed: {error}")
        self.workers.append(node.workeroutput["ci_collection"])

    def pytest_sessionfinish(self, session, exitstatus):
        self.collection["database_seconds"] = self.database_seconds
        if self.worker:
            self.config.workeroutput["ci_collection"] = self.collection
            return
        collections = self.workers or [self.collection]
        first = collections[0]
        for collection in collections:
            require(collection["full"] == first["full"], "Workers collected different full suites")
            require(collection["selected"] == first["selected"], "Workers selected different shard suites")
        durations_path = Path(self.config.getoption("durations_path"))
        splits = self.config.getoption("splits")
        require(not splits or durations_path.is_file(), "Sharded runs require a fixed duration input")
        report = {
            "shard": self.config.getoption("group") or 1,
            "shards": splits or 1,
            "revision": os.environ.get("GITHUB_SHA", "local"),
            "lane": os.environ.get("CI_TEST_LANE", "local"),
            "duration_input": hashlib.sha256(durations_path.read_bytes()).hexdigest() if splits else "calibration",
            "full": first["full"],
            "selected": first["selected"],
            "completed": self.completed,
            "exitstatus": int(exitstatus),
            "durations": dict(self.durations),
            "workers": [{key: value for key, value in row.items() if key.endswith("_seconds")} for row in collections],
            "elapsed_seconds": time.perf_counter() - self.started,
        }
        output = Path(self.config.getoption("ci_shard_report"))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def aggregate(directory, shards):
    paths = sorted(directory.glob("*/report.json"))
    require(len(paths) == shards, f"Expected {shards} shard reports, found {len(paths)}")
    reports = [json.loads(path.read_text()) for path in paths]
    first = reports[0]
    require({row["shard"] for row in reports} == set(range(1, shards + 1)), "Missing or duplicate shard numbers")
    selected = []
    durations = {}
    coverage_files = []
    for path, report in zip(paths, reports, strict=True):
        for key in ("full", "revision", "lane", "duration_input"):
            require(report[key] == first[key], f"Shard {key} differs")
        require(report["shards"] == shards, "Shard count differs")
        require(report["exitstatus"] == 0, "A shard did not pass")
        require(
            Counter(report["completed"]) == Counter(report["selected"]), "A selected test did not complete exactly once"
        )
        selected.extend(report["selected"])
        durations.update(report["durations"])
        coverage_file = path.parent / ".coverage"
        require(coverage_file.is_file(), f"Missing coverage: {coverage_file}")
        coverage_files.append(str(coverage_file.resolve()))
    require(bool(first["full"]), "Empty full collection")
    require(len(set(first["full"])) == len(first["full"]), "Duplicate node IDs in full collection")
    require(Counter(selected) == Counter(first["full"]), "Shard union omits or duplicates tests")
    (directory / "durations.json").write_text(json.dumps(durations, indent=2, sort_keys=True) + "\n")
    subprocess.run([sys.executable, "-m", "coverage", "combine", "--keep", *coverage_files], check=True)
    subprocess.run([sys.executable, "-m", "coverage", "report"], check=True)
    print(f"Verified {len(selected)} tests across {shards} shards")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["aggregate"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--shards", type=int, required=True)
    arguments = parser.parse_args()
    aggregate(arguments.directory, arguments.shards)
