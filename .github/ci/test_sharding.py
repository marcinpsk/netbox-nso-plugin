# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Exercise shard selection and aggregation through real pytest subprocesses."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


def create_shard_artifacts(temporary):
    """Produce reports and coverage from two real pytest shards."""
    helper = Path(__file__).with_name("sharding.py")
    root = Path(temporary)
    (root / "sample.py").write_text(
        "def classify(value):\n    if value > 0:\n        return 'positive'\n    return 'negative'\n"
    )
    (root / "test_sample.py").write_text(
        "from sample import classify\n"
        "def test_positive():\n    assert classify(1) == 'positive'\n"
        "def test_negative():\n    assert classify(-1) == 'negative'\n"
    )
    (root / "pyproject.toml").write_text(
        '[tool.coverage.run]\nbranch = true\nsource = ["sample"]\n[tool.coverage.report]\nfail_under = 100\n'
    )
    durations = root / "durations.json"
    durations.write_text(json.dumps({"test_sample.py::test_positive": 1, "test_sample.py::test_negative": 1}))
    environment = dict(os.environ, PYTHONPATH=str(helper.parent), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    for shard in (1, 2):
        output = root / f"shard-{shard}"
        output.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "xdist.plugin",
                "-p",
                "pytest_cov.plugin",
                "-p",
                "pytest_split.plugin",
                "-p",
                "sharding",
                "-n",
                "4",
                "--cov=sample",
                "--cov-fail-under=0",
                "--splits=2",
                f"--group={shard}",
                "--splitting-algorithm=least_duration",
                f"--durations-path={durations}",
                f"--ci-shard-report={output / 'report.json'}",
                "-q",
            ],
            cwd=root,
            env=dict(environment, COVERAGE_FILE=str(output / ".coverage")),
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)
    return root


class ShardingContractTests(unittest.TestCase):
    def test_complete_shards_pass_and_incomplete_or_duplicate_runs_fail(self):
        helper = Path(__file__).with_name("sharding.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = create_shard_artifacts(temporary)

            command = [sys.executable, str(helper), "aggregate", str(root), "--shards", "2"]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("100%", result.stdout)

            report_path = root / "shard-2" / "report.json"
            original = json.loads(report_path.read_text())
            first = json.loads((root / "shard-1" / "report.json").read_text())
            for corruption in (
                {"selected": first["selected"], "completed": first["completed"]},
                {"completed": []},
                {"exitstatus": 1},
                {"duration_input": "different-input"},
                {"shard": 1},
                {"full": original["full"][:1]},
            ):
                with self.subTest(corruption=corruption):
                    report_path.write_text(json.dumps(dict(original, **corruption)))
                    result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            report_path.write_text(json.dumps(original))
            report_path.unlink()
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_workflow_gate_step_runs_the_aggregator_for_its_own_environment(self):
        helper = Path(__file__).with_name("sharding.py")
        workflow = helper.parents[1] / "workflows" / "test.yaml"
        lines = iter(workflow.read_text().splitlines())
        for line in lines:
            if line.strip() == "- name: Verify complete execution and combined coverage":
                step_indentation = len(line) - len(line.lstrip())
                break
        else:
            self.fail("The workflow has no coverage verification step")
        for line in lines:
            if line.strip() and len(line) - len(line.lstrip()) <= step_indentation:
                self.fail("The coverage verification step has no run block")
            if line.strip() == "run: |":
                indentation = len(line) - len(line.lstrip())
                break
        else:
            self.fail("The coverage verification step has no run block")
        script_lines = []
        for line in lines:
            if line.strip() and len(line) - len(line.lstrip()) <= indentation:
                break
            script_lines.append(line)
        script = textwrap.dedent("\n".join(script_lines))
        self.assertTrue(script)
        with tempfile.TemporaryDirectory() as temporary:
            root = create_shard_artifacts(temporary)
            results = root / ".ci-results"
            results.mkdir()
            for shard in (1, 2):
                (root / f"shard-{shard}").rename(results / f"shard-{shard}")
            target = root / ".github" / "ci" / "sharding.py"
            target.parent.mkdir(parents=True)
            shutil.copyfile(helper, target)
            environment = dict(os.environ, CI_SHARDS="2", SHARD_RESULT="failure")
            environment.pop("PYTHONPATH", None)
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", script],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("100%", result.stdout)
            self.assertTrue((results / "durations.json").is_file())


if __name__ == "__main__":
    unittest.main()
