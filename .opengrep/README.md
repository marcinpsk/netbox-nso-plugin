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
or the runner changes. The directory scan uses the root `.semgrepignore` instead
of OpenGrep defaults and disables Git ignore handling. It checks every Python
file under `netbox_nso_plugin` except files in `/netbox_nso_plugin/migrations/`.
The leading slash anchors this path at the repository root. The scan applies no
file-size limit. Rule changes also run the annotated fixtures. Missing OpenGrep,
invalid rules, and findings fail the hook. An explicit target is also supported:
`scripts/check-review-patterns scan path/to/file.py`. Arguments after `scan` are
paths, not scanner options. Fixtures contain deliberate defects, so Ruff excludes
only `.opengrep/tests`.

Every rule ID and every positive pattern alternative must match at least one
`# ruleid:` fixture line. The fixture hook fails when a rule or alternative has
no matching defect. The fixture hook also checks that a push builder definition
in `delivery.py` is reported. The tree scan checks the remaining path filters.

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
| Push scheduled while suppression is active | `nso-push-inside-suppression` | Direct or module-qualified calls (`signals.suppress_intent_push()`) in a suppression context. It does not follow helper calls. |
| Assertion compares a value with itself | `nso-tautological-assertion` | Literal assertion shapes, not proof that every assertion reaches the intended path. |
| Global monotonic clock patched through a module | `nso-global-monotonic-patch` | Literal `patch` targets that name `time.monotonic`. |
| Race test hides a broken barrier | `nso-swallowed-barrier-failure` | Exception handlers that contain only `pass` or a bare `return`. |
| Test thread join has no bounded timeout | `nso-unbounded-thread-join` | Enforces bounded `.join()` calls in test modules that contain a `threading` import.<br>Limit: An import inside a function, class, `if` or `else` branch, `try`, `except`, or `finally` block, loop, or `with` block does not enable detection of joins outside that block, although the module-level Python walk caught these cases.<br>Limit: Relative imports, such as `from .threading import Thread`, aliased `from ..threading import ...`, and relative wildcard imports, do not enable this rule, although they enabled the module-level Python walk.<br>Limit: The shared fixture file always imports from `threading`. The module-without-threading negative case remains covered only by the unit contract in the commit history. |
| Direct push builder used outside the delivery registry | `nso-retired-push-builder` | Covers calls, references, and imports in production Python modules. The owner file `delivery.py` is excluded. The rule also reports dotted imports, wildcard imports from a matching module, and matching names used as `case` captures.<br>New limit: Unlike the old AST check, this rule does not match Unicode-normalized identifier spellings.<br>Existing limit: A name built with `getattr` and a string is not matched. |
| Push builder defined outside its owner module | `nso-push-builder-definition-outside-signals` | Reports matching definitions outside `signals.py`. `test_delivery_registry.py` checks that every definition in `signals.py` is registered with delivery. |
| Retired in-memory coalescer state named in production | `nso-retired-coalescer-state` | Covers calls, references, and imports of `_pending_pushes` and `_last_pushed_hashes` in production Python modules. The rule also reports dotted imports, wildcard imports from a matching module, and matching names used as `case` captures.<br>New limit: Unlike the old AST check, this rule does not match Unicode-normalized identifier spellings.<br>Existing limit: A name built with `getattr` and a string is not matched. |
| Retired renderer-writer symbol named in production | `nso-retired-renderer-writer-symbol` | Covers calls, references, attributes, definitions, assignment targets, and imports of the retired renderer-writer names in production Python modules.<br>Limit: Unlike the old AST checks, this rule does not match Unicode-normalized identifier spellings. Python normalizes `_ＩＭＰＬＩＣＩＴ_PERMITS` to `_IMPLICIT_PERMITS`, but the text rule does not. |
| Renderer writer reference resolver has one owner | `nso-renderer-writer-single-resolver` | Requires exactly one direct `_resolve_reference` declaration in `RendererWriter`. Inherited, nested, missing, and duplicate declarations fail.<br>Limit: An `async def _resolve_reference` counts as a declaration for this rule. The retired Python guard counted only `def`.<br>Limit: Python normalizes fullwidth identifier letters before the retired AST guard runs. A class name such as `ＲendererWriter` fails that guard, and this rule does not report it. A method name such as `_ｒesolve_reference` satisfies that guard, but this rule reports the class.<br>Limit: The rule cannot follow an aliased class binding such as `RendererWriter = Writer`. Aliased classes with a missing or duplicate resolver fail the retired guard but escape this rule. An aliased class with exactly one resolver passes both checks.<br>Limit: A resolver added through class decoration or assignment (`_resolve_reference = other`) is not seen as a declaration by either implementation. |
| Retired interface receipt literal used outside the delivery registry | `nso-retired-interface-config-literal` | Covers ordinary single-quoted or double-quoted `"interface_config"` literals in production Python modules. The same literal pattern also reports `b"interface_config"` and `"interface_" + "config"`.<br>Limit: Escaped, adjacent, and f-string spellings are not covered.<br>Limit: The `delivery.py` owner is excluded by path, so the fixture does not test that exclusion. |
| Mutable display name used as VLAN group identity | `nso-vlan-group-mutable-lookup` | Imported `VLANGroup` manager calls with both `name` and `slug` lookup keys. |
| Exact write-set assertion loses duplicate writes | `nso-write-set-cardinality-assertion` | Direct `assertEqual` set comprehensions over a frozen `write_set`. Membership and subset checks stay valid. |
| Text file read or written without an explicit encoding | `nso-implicit-text-encoding` | `read_text`, `write_text`, `Path.open`, and builtin `open` calls that have no `encoding=` keyword. Any attribute call named `open` is reported, including `os.open` and `tokenize.open`, which have no encoding parameter. The rule also reports a positional encoding (`p.read_text("utf-8")`). An encoding that arrives through `**kwargs` or through a differently named keyword is not seen. The rule detects binary mode from a literal mode string only. |
| Duplicate adapter entries silently kept | `nso-silent-duplicate-adapter-entry` | Rejects `continue` on a seen key while iterating a redistribution `payload["entries"]` list or normalized VLAN entries. Other payload shapes still need behavior tests. |
| Model signal receiver connected without `dispatch_uid` | `nso-signal-connect-without-dispatch-uid` | `connect` calls on the `django.db.models.signals` objects, including module-qualified and aliased imports. A signal object stored in a variable is not followed. Test modules are excluded. |
| Deletion receiver connected without deletion origin | `nso-delete-signal-without-delete-origin` | Direct `pre_delete` and `post_delete` connects on `django.db.models.signals`, including module-qualified and aliased imports, when the receiver name starts with `_on_` (or a dotted receiver ends with such a name). Other receiver names and signal objects stored in variables are not covered. Test modules are excluded. |
| Request body read before validation | `nso-unchecked-request-body` | Reports every `request.data` or `self.request.data` read in production Python modules, including aliases. The `_request_body` helper and a call's `data=` keyword value are allowed. A request object bound under another name, such as `req`, is not seen. |
| Deployment gate resume() failure leaves intent work quiesced without guidance | `.opengrep/check-resume-failure-guidance.py` | The AST guard resolves absolute and relative imports, aliases, rebinding, and shadowing. It accepts a resume call only when that exact call is the sole statement in a try block. The single `except BaseException` handler must report both the quiesced state and the abort recovery command inside `contextlib.suppress(Exception)`, then use a bare raise. A `finally` body is allowed, but each resume call in it is checked separately. The abort exemption applies only to calls in the `if options["abort"]` body of `handle(self, *args, **options)`. Calls in `elif` and `else` branches remain guarded. |
| Validation raised after the loop already skipped the entry | `.opengrep/check-adapter-error-continue.py` | The AST guard reports each `AdapterError` raise in a `for` or `async for` body only when a `continue` targets that same nearest loop. Nested loops do not leak their `continue` statements into an outer loop. The guard scans `netbox_nso_plugin/*_reconciler.py` from the existing pre-commit review-pattern hook. It recognizes fully qualified names, package aliases, module aliases, bare imported symbols, and aliased symbols through absolute or relative imports. Parameter shadowing suppresses that binding for the complete function.<br>Limit: An imported name that a later statement rebinds or deletes still counts as `AdapterError`.<br>Limit: An import must be visited before the function or loop that uses it. |
| Registered renderer inputs written outside the writer | `test_renderer_writer_structure.py` | Uses the live model registry and reviewed call sites. Dynamic model targets need review. |
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
