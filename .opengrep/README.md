<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright (C) 2026 Marcin Zieba -->

# Local review checks

Run these checks before a commit:

```sh
scripts/check-review-patterns test
scripts/check-review-patterns scan
```

Install OpenGrep from its official release, or set `OPENGREP_BIN` to an installed
executable. The rules and fixtures are tested with OpenGrep 1.30.0. Install the
repository hooks with `pre-commit install --install-hooks`.

The pre-commit hooks scan the package and its test files when Python code, rules,
or the runner changes. Rule changes also run the annotated fixtures. Missing
OpenGrep, invalid rules, and findings fail the hook. An explicit target is also
supported: `scripts/check-review-patterns scan path/to/file.py`. Arguments after
`scan` are paths, not scanner options. Fixtures contain deliberate defects, so
Ruff excludes only `.opengrep/tests`.

## Preserve CodeRabbit's default scan

The rules live in `.opengrep/nso-rules.yaml`. This custom filename does not match
CodeRabbit's documented OpenGrep configuration names. Do not rename it to
`opengrep.yml`, `opengrep.yaml`, `opengrep.config.yml`, `opengrep.config.yaml`, or
the corresponding `semgrep` names. Those files replace its fallback rule packs.

Run these custom rules only in pre-commit, not in a GitHub Actions workflow.
CodeRabbit documents that it skips OpenGrep when OpenGrep already runs in GitHub
workflows. The existing CI test and lint gates continue to run.

References checked on 2026-09-12:

- [CodeRabbit OpenGrep configuration and skip conditions](https://docs.coderabbit.ai/tools/opengrep).
- [LibreNMS integration](https://github.com/marcinpsk/netbox-librenms-plugin/pull/163).
  Its custom filename is useful here. Its workflow is not copied. The latest
  detailed reviews list other tools but no OpenGrep execution; they do not state
  an explicit skip reason.

## Coverage of recurring findings

| Issue class | Mechanical check | Limit |
| --- | --- | --- |
| Push scheduled while suppression is active | `nso-push-inside-suppression` | Direct calls in a suppression context. It does not follow helper calls. |
| Assertion compares a value with itself | `nso-tautological-assertion` | Literal assertion shapes, not proof that every assertion reaches the intended path. |
| Global monotonic clock patched through a module | `nso-global-monotonic-patch` | Literal `patch` targets that name `time.monotonic`. |
| Race test hides a broken barrier | `nso-swallowed-barrier-failure` | Exception handlers that contain only `pass` or a bare `return`. |
| Mutable display name used as VLAN group identity | `nso-vlan-group-mutable-lookup` | Imported `VLANGroup` manager calls with both `name` and `slug` lookup keys. |
| Registered renderer inputs written outside the writer | `test_renderer_writer_structure.py` | Uses the live model registry and reviewed call sites. Dynamic model targets need review. |
| Retired SQL guard restored in production | `test_renderer_writer_structure.py` | Structural guard over production modules. |
| Spec-less object mocks | Existing `mock-discipline` hook | Tests beyond the recorded baseline. |
| Duplicate definitions, dead imports, naive datetimes | Existing Ruff rules | Python source checked by the repository lint gate. |
| Raw SQL or ORM bypass leaves stale rendered intent | `test_renderer_audit.py` and `test_intent_outbox_claim.py` | Real database assertions cover drift, repair, and claim interleavings. |
| Frozen identity, timestamp, or pre-image lost during replay | `test_gated_reconcile.py`, `test_renderer_writer.py`, and reconcile tests | Real state changes prove refusal and replay behaviour. |
| Lock order, lifecycle transitions, ownership, permissions, malformed responses, and retry boundaries | Existing integration suites | Requires behavioural assertions. A syntax rule cannot establish these contracts. |

No finite ruleset catches every review finding. These rules reject concrete,
repeated source patterns. Keep integration tests for stateful contracts and add
a fixture before adding a new rule. Include both a defect and a valid nearby
shape. First confirm that the defect is missed, then implement the rule and run
both commands above. Do not add a broad suppression to make the tree pass.
