# Copilot Instructions

## Project overview

This repository contains a NetBox 4.6 plugin that integrates NetBox with Cisco NSO through an external `nso-adapter` REST API. The plugin package is `netbox_nso_plugin`, the PyPI distribution is `netbox-nso-plugin`, and the plugin is exposed in NetBox under the `nso` base URL.

The adapter integration must degrade gracefully when the adapter is unavailable. Code that talks to the adapter should raise or handle `AdapterError` rather than crashing NetBox UI/API flows.

## Phase status (`../nso-adapter/docs/00-plan.md` §6.6)

- **Phase 1** — complete: scope + sync + read-only compliance.
- **Phase 2** — complete (M6): `NSOInterfaceState` model, `auto_apply` field
  on `NSODeviceManagement`, accept actions, intent push, apply button, full
  status taxonomy in the badge. Specifics in `docs/netbox-nso-plugin.md`
  §2, §4a, §8a.
- **Phase 3** — in-flight. **M7 ✅** device-matching (enriched NSO device list
  + already-claimed warning in the match form). **M8 ✅** derived intent —
  auto interface descriptions from cable topology via configurable sentinel
  markers (`derived_intent.py`); see `docs/m8-derived-intent.md`. **M9 in
  design** — LAG topology reconcile (a `_reconcile_lag_topology` sibling on
  the `template_content.py` reconcile path, fed by the adapter's new LAG
  endpoint); contract/plan in `../nso-adapter/docs/m9-lag-topology*.md`.

## Architecture

Follow the standard NetBox plugin pattern and keep responsibilities separated:

- **`models.py`**:
  - **Phase 1:** `AdapterConnection` (singleton, **no token field — ever**), `NSOInstance`, `NSODeviceManagement`.
  - **Phase 2 (M6):** add `auto_apply` to `NSODeviceManagement`; add `NSOInterfaceState` (per-interface, per-attribute status overlay — `(interface, attribute)` unique; status enum matches `api-contract.md`; **intent value lives on `dcim.Interface`**, not on this model — decision C + decision G activated).
- **`adapter_client.py`** — thin REST wrapper. Resolves URL and non-secret settings from the `AdapterConnection` singleton when present and `enabled`, otherwise from `PLUGINS_CONFIG["netbox_nso_plugin"]` (env). Reads `adapter_token` **only** from `PLUGINS_CONFIG`/env, never from the DB. Resolves per call with a short in-process cache (~30s) so UI edits to `AdapterConnection` take effect without a NetBox restart. Exposes:
  - Phase 1: `onboard_device()`, `set_scope()`, `get_device()`, `get_interfaces()`, `get_compliance()`, `trigger_sync()`, `trigger_check_compliance()`, `trigger_connect()`, `get_job()`, `patch_device()`, `delete_device()`, `notify_sync()`.
  - Phase 2 (M5+): `put_intent(device_id, payload)`, `get_intent(device_id)`, `trigger_apply(device_id, force=True)`.
- **`signals.py`** — `post_save` on `NSODeviceManagement` to onboard/update adapter scope (+ `auto_apply` once Phase 2) and `post_delete` to offboard the device. **Phase 2 (M6):** `post_save` on `NSOInterfaceState` (and on `dcim.Interface` when the interface is managed and `description`/`enabled` changed) pushes the device's full intent snapshot via `put_intent()` and calls `notify_sync()`.
- **`views.py`, `urls.py`, `tables.py`, `forms.py`, `filters.py`, `navigation.py`** — standard NetBox CRUD/UI plumbing for the two Phase 1 models.
- **`template_content.py`** — `PluginTemplateExtension` hooks for the device NSO tab and interface badge.
- **`derived_intent.py`** (M8) — computes auto interface descriptions from cable topology using configurable sentinel markers (`DERIVED_INTENT_TEMPLATES` in `PLUGINS_CONFIG`); `signals.py` recomputes on cable/interface change. **Only descriptions starting with a configured sentinel are ever overwritten** — manually entered descriptions are never touched.
- **`api/`** — DRF serializers/viewsets/router exposing plugin API endpoints, especially `/api/plugins/nso/device-management/`.
- **`migrations/0001_initial.py`** — initial Django migration for the Phase 1 models.

## Adapter API contract summary

Plugin settings live in `PLUGINS_CONFIG`:

```python
import os

PLUGINS_CONFIG = {
    "netbox_nso_plugin": {
        # URL: bootstrap default; AdapterConnection.url in the UI overrides.
        "adapter_url": os.environ.get("NSO_ADAPTER_URL", ""),
        # Token: env-only, by design (see "Hard guardrails" below). Required.
        "adapter_token": os.environ["NSO_ADAPTER_TOKEN"],
    }
}
```

Expected adapter endpoints (Phase 1 unless noted):

- `POST /api/v1/devices`
- `PATCH /api/v1/devices/{id}` — correct an existing mapping (re-key `nso_device_name` or `nso_instance`)
- `DELETE /api/v1/devices/{id}` — offboard
- `PUT /api/v1/devices/{id}/scope` — body now also carries `auto_apply` (Phase 2; optional, default false)
- `GET /api/v1/devices/{id}`
- `GET /api/v1/devices/{id}/interfaces` — `attrs` carries `intent_value`, `last_apply_at`, `last_apply_error` (null in Phase 1)
- `GET /api/v1/devices/{id}/compliance`
- `POST /api/v1/devices/{id}/sync-notify` — kicker after scope/intent changes
- `POST /api/v1/devices/{id}/actions/sync`
- `POST /api/v1/devices/{id}/actions/check-compliance`
- `POST /api/v1/devices/{id}/actions/connect`
- **Phase 2 (M5+):**
  - `PUT /api/v1/devices/{id}/intent` — push the device's full intent snapshot
  - `GET /api/v1/devices/{id}/intent`
  - `POST /api/v1/devices/{id}/actions/apply` — push intent to device (returns `501` until M4)
- `GET /api/v1/jobs/{id}`

Use `AdapterError` for transport/API failures and log warnings when signals cannot reach the adapter.

**Error shape returned by the adapter** (per `../nso-adapter/docs/api-contract.md`):

```json
{"error": {"code": "snake_case", "message": "...", "detail": {}}}
```

**Concurrency:** a second action for the same device while one is running returns `409` with the running job id in `error.detail.job_id` — surface that job to the user; don't queue a second one.

## Hard guardrails (do not violate)

- **`adapter_token` is env-only.** Never stored in any plugin model, never editable in the UI, never logged. The `AdapterConnection` model deliberately has **no token field** — the structural absence is the safeguard. Do not add one.
- **No Vault client in the plugin.** No `hvac` dependency. The token is env-injected into NetBox by deployment tooling (Ansible / ESO) from the same Vault entry the adapter reads — one secret, two consumers.
- **No direct NSO calls.** All NSO interaction goes through the adapter.
- **No writing to `dcim.Interface` fields in normal operation.** Synced values land there from the adapter via NetBox's REST API.
- **Push goes through the adapter's intent/apply contract only.** Phase 2 added the push direction: the plugin pushes a full intent snapshot via `put_intent()` on signal, then `trigger_apply()`. It does **not** write device config any other way, and never bypasses the adapter.
- **No file-level `omit` entries in `[tool.coverage.run]`.** The only acceptable file-level exclusions are `*/migrations/*` and `*/tests/*`. Do not add `urls.py`, `tables.py`, `navigation.py`, or any other "purely declarative" file to the omit list — declarative module-scope code already gets 100% line coverage for free at import time, and the exclusion silently swallows any future branching logic added to that file. For genuinely unreachable lines use `# pragma: no cover` at the line. Full rationale in `../nso-adapter/docs/testing-strategy.md` §5.1.

## Polyrepo layout & cross-cutting docs

This is the **`netbox-nso-plugin`** repo, an independent git repo, one of **three** in the polyrepo. The companion **`nso-adapter`** repo lives at `../nso-adapter/` (sibling under `/home/mzieba/workspace/nso/`, or `/workspaces/nso/nso-adapter/` inside the devcontainer) and owns the cross-cutting docs; **`nso-packages`** (`../nso-packages/`) holds the NSO-side YANG/Python packages (`interface-reconciler`, `vault-cred-manager`, and the Phase 3 `network-state-export` exporter). All NSO interaction still flows through the adapter — the plugin never talks to NSO or `nso-packages` directly.

- `../nso-adapter/docs/00-plan.md` — overall plan and decisions
- `../nso-adapter/docs/api-contract.md` — canonical REST API contract (build against this)
- `../nso-adapter/docs/nso-adapter.md` — adapter design

This repo's own design doc: `docs/netbox-nso-plugin.md`.

## Development environment

The development container is modeled on the reference INR plugin at `/home/mzieba/workspace/netbox-InterfaceNameRules-plugin/`. Both repositories live under `/home/mzieba/workspace/`, which is mounted inside the devcontainer as `/workspaces/`.

For this repo, the workspace root inside the container is expected to be `/workspaces/nso/netbox-nso-plugin`. The INR plugin is available alongside it at `/workspaces/netbox-InterfaceNameRules-plugin` for reference data, configuration examples, and sample-data loading.

## Common commands

Inside the devcontainer, use the provided helper commands:

```bash
netbox-run
netbox-manage migrate
netbox-test
dev-help
```

Other useful helpers include `netbox-run-bg`, `netbox-stop`, `netbox-restart`, `netbox-reload`, `ruff-check`, `ruff-format`, `ruff-fix`, and `diagnose`.

## Running tests (devcontainer)

**Use `netbox-test`. Do not run `pytest`, `python -m pytest`, or `python manage.py test` directly — and do not `pip install` anything to make them work.** Everything you need is already installed in the NetBox venv at `/opt/netbox/venv`.

What `netbox-test` actually does (`.devcontainer/scripts/load-aliases.sh`):

```bash
cd "$PLUGIN_DIR" && source /opt/netbox/venv/bin/activate && \
  TEST_DB_NAME="${TEST_DB_NAME:-test_netbox_nso_plugin}" \
  pytest netbox_nso_plugin/tests --no-cov -q --disable-warnings \
  -n "${NETBOX_TEST_WORKERS:-8}" --maxschedchunk=1
```

Why this is the only path you should take:

- It activates the NetBox venv, where `pytest`, `pytest-django`, `pytest-cov`, `pytest-xdist`, `ruff`, Django, and all plugin runtime deps are already present (`.devcontainer/scripts/setup.sh` does this on container build).
- It runs pytest from the plugin checkout so `pyproject.toml` and both conftest files are loaded. The session guard blocks every unmocked adapter request; Django's runner does not load that guard and can otherwise call the live adapter during tests.
- It reuses the isolated PostgreSQL databases and gives every xdist worker its own suffixed database. Eight workers are the default; set `NETBOX_TEST_WORKERS=1` for a serial run.

Common variants:

```bash
netbox-test                                                        # full suite, eight workers
netbox-test netbox_nso_plugin/tests/test_models.py                 # one module
netbox-test netbox_nso_plugin/tests/test_models.py::TestX          # one class
netbox-test netbox_nso_plugin/tests/test_models.py::TestX::test_y  # one test
NETBOX_TEST_WORKERS=1 netbox-test                                  # serial override
TEST_DB_NAME=test_nso_task netbox-test                             # separate DB family
```

If `netbox-test` is not found, you are not inside the devcontainer — `source ~/.zshrc` (or open a new terminal) to load the aliases, or fall back to the explicit form above. Do not invent a new command.

### Coverage reports

Coverage runs come from `netbox-test-coverage`; the pyproject `addopts` carries `--cov=netbox_nso_plugin --cov-report=term-missing`. `pytest-cov`, `pytest-xdist`, `requests-mock`, and `reuse` are installed by `.devcontainer/scripts/setup.sh`, so a clean devcontainer build has everything the suite needs. Rebuild the devcontainer after changing that dependency set rather than installing into an unrelated host environment.

```bash
netbox-test-coverage
```

For the **iterative edit-test loop, use `netbox-test`** (no coverage, parallel, warm DB reuse). Coverage is a periodic check, not a per-edit signal. `netbox-test-django` remains available for diagnosing Django-runner-specific behavior.

## Linting and quality gates

- Use **Ruff** for linting/formatting.
- Keep the repository **REUSE/SPDX compliant**.
- Use **Conventional Commits** (`feat:`, `fix:`, `docs:`, etc.).
- Preserve SPDX headers in Python and shell files:

```python
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
```

## Conventions

- Prefer NetBox base classes (`NetBoxModel`, `NetBoxModelForm`, `NetBoxModelSerializer`, `NetBoxModelViewSet`, `NetBoxTable`, `NetBoxModelFilterSet`) over raw Django/DRF primitives.
- Keep adapter interactions thin and centralized in `adapter_client.py`.
- Handle adapter outages gracefully: UI/API should remain usable even if NSO or the adapter is down.
- Signal handlers should log failures and avoid breaking model saves/deletes.
- Phase 2 (`NSOInterfaceState`, accept/apply, M8 derived intent) is complete; Phase 3 is the current work. Build M9 LAG reconcile against the adapter's published contract (`../nso-adapter/docs/m9-lag-topology.md`). Multi-NSO-at-scale and HA remain out of scope.
