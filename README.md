# netbox-nso-plugin

NetBox 4.6 plugin that integrates NetBox with Cisco NSO via the **nso-adapter** REST API —
a bidirectional sync: device state is imported into NetBox, and operator-accepted values
become intent that is applied back to the device through NSO (reconcile-mode commits).

## Features

- Mark NetBox devices as NSO-managed and choose the managed scopes (interfaces, routing,
  VLANs/L2, SNMP, logging, …) per device.
- Device **NSO tab**: counts-first category summaries with lazy row expansion, a merged
  per-interface table (enabled / description / IPs / MTU / switchport), pagination +
  search, and inline editing of operator-owned values.
- **Read mirrors** for: interface attributes/IPs/MTU, VLAN database, switchport, SVI/IRB,
  dot1q subinterfaces, L2 services (epipe/vpls SAPs), LACP/LAG, IS-IS (incl. levels,
  segment-routing, Flex-Algo), OSPF, BGP (peers/AFs/templates), route policies,
  redistribution, static routes, BFD, SNMP (secret-safe), logging hosts — materialised
  into native NetBox / netbox-routing objects with `NSO*State` status overlays and
  clobber-safe 3-way merge.
- **Accept → Apply** write path: Accept promotes a value to NetBox-owned intent; one
  device Apply commits all pending scopes via the NSO `*-reconciler` services, with a
  two-panel preview (intent summary + native device diff). Greenfield writes for
  operator-created routes, VLANs, policies, SVIs, subinterfaces and more.
- Drift handling: value-aware drift display, plus intent **split-brain detection**
  (orphaned and partial) with one-click re-sync of the adapter intent mirror — never
  touches the device.
- Adapter actions from the tab: **Sync**, **Detect drift**, **Test connection**,
  **Apply**, with job polling.
- Device onboarding NetBox → NSO (create node, fetch host keys, sync-from) from the
  NSO Devices dashboard.
- Exposes `NSODeviceManagement` at `/api/plugins/nso/device-management/` for the
  adapter's self-healing reconcile path.

## Requirements

- NetBox ≥ 4.6.0
- Python ≥ 3.12

## Development status

This plugin, the NSO adapter, and the NSO packages have no deployed installations.
The three repositories move together during development. Migrations 0024 and
0025 replace the development ownership manifest. They do not preserve rows from
migration 0022 because no deployed data uses that schema.

## Installation

The plugin has hard foreign keys to the netbox-routing fork. Install the fork commit that CI tests
(migration `0040_case_sensitive_policy_names` or later; plugin migrations refuse an older fork):

```bash
pip install "netbox-routing @ git+https://github.com/marcinpsk/netbox-routing.git@f8c0626b45a38f7f13daf9f224a810fb7364d800"
pip install netbox-nso-plugin
```

Add both to `PLUGINS` in `configuration.py`:

```python
PLUGINS = ["netbox_routing", "netbox_nso_plugin"]
PLUGINS_CONFIG = {
    "netbox_nso_plugin": {
        "adapter_url": "https://nso-adapter.example.net",
        "adapter_token": "<bearer token>",
    }
}
```

Run migrations:

```bash
python manage.py migrate netbox_nso_plugin
```

Give the adapter's NetBox callback token to a service user with two `ObjectPermission`
rows:

- the `change` action on **NSO Provision Tombstone**, for
  `POST /api/plugins/nso/provision-complete/`;
- the custom action `sync` on **NSO Device Management** (enter it under additional
  actions), for `POST /api/plugins/nso/sync-complete/`.

An authenticated token without the permission receives HTTP 403. A callback for a row
outside the permission's constraints receives HTTP 404. The periodic tombstone sweep
still checks jobs when a callback cannot be delivered.

The `adapter_token` setting above authenticates requests from NetBox to the adapter.
The callback token authenticates requests from the adapter to NetBox.

Programmatic writers must wrap each create, save, or delete of a managed intent model in
`intent_transaction()`. The renderer write, revision bump, and intent outbox entry must
commit together. The mutation footprint must be complete before the first write.
`QuerySet.update()`, `bulk_update()`, and `bulk_create()` bypass the save signals that schedule
the outbox. Programmatic intent writes must not use them unless an outbox-aware service runs
inside `transaction.atomic()`. Reconcile and drain bookkeeping writes are exempt when they do
not change operator intent.

## Development

See `.devcontainer/` for the Docker-based development environment.

## Paired intent deployment

Keep plugin operations and adapter Apply, removal, cadence, sweeper, and reclaimer
admissions stopped throughout the cutover. The plugin gate does not stop the adapter.
The code that installs the paired cutover also adds `--paired-activation` and its migration.
Use this order:

1. On the old plugin code, run `nso_intent_deployment_gate --prepare`. Keep the adapter
   admissions stopped.
2. Install the paired plugin code and run its migrations. The gate stays in place.
3. Run `nso_intent_deployment_gate --prepare --paired-activation`. This marks the retained
   gate for paired activation. The gate keeps its original `quiesced_at`.

The gate keeps the paired mark until paired verification releases it. A later `--prepare`
without the flag does not remove it.
A failed paired preparation, maintenance, or verification action leaves the plugin quiesced.
The maintenance and probe actions below require a paired-activation gate.

Use `--import-routers --device ID` to import one validated BGP observation and print
router overlay IDs, router IDs, and complete scope and address-family membership.
The inventory contains identities and content. It does not grant ownership.
Review it, then use `--accept-routers ID [ID ...] --device ID` with the exact selection.
Only these router overlays receive an explicit accept grant.

Use `--prepare-store --device ID --scope SCOPE` for each active stream, including
store-only/manual streams and direct switching. This action obtains durable selection
provenance and a verified receipt or direct preparation. It does not enqueue device Apply.
Normal callbacks, drains, accepts, and reconciles remain blocked.
Resolve old-schema work with the old installed code before installing this cutover.
Do not manufacture historical snapshots or infer router ownership from peers.

For a controlled placeholder validation device, prepare its BGP store selection first.
Use `--start-probe ATTEMPT_UUID --device ID` with a fresh operator-selected UUID.
This bounded action captures the current selection and registers its durable permit before
sending the one explicit manual Apply. It does not re-send device intent.
Allow only this controlled adapter admission. Keep all other adapter admissions stopped.
Use the same UUID to recover an interrupted start without creating a second request.
Preparation and admission require the adapter store to match the imported source identity.
A definite rejection restores the snapshots and closes the attempt. Use a fresh UUID after
correcting that rejection.
Use `--register-probe ATTEMPT_UUID` when its exact terminal generation and carrier are
available. Registration validates the selection, execution, and full owner footprint.
A callback received before these bindings exist remains blocked. After registration,
await the adapter notification or repeat the authenticated sync-complete callback.
An authenticated sync-complete callback can then enqueue only this attempt.
The RQ worker rechecks the durable permit and evidence, reads only the captured BGP
owners, and runs production settlement. Completion or failure revokes the permit.
A process restart keeps the same durable authority or leaves the gate unresolved.
Use `--revoke-probe` to revoke a parked permit without resuming work.
Probe capture, admission, and terminal execution must belong to the current gate interval.

Run `--verify --device ID --settled-probe UUID --settled-probe UUID --settled-probe UUID`.
The completed probes must cover success, a peer refusal, and a router refusal, each
with the complete router and two-peer membership. Runtime verification checks the
installed migration leaf, pending migrations, preview shape, settlement evidence,
held work, and retained selection and execution provenance before resuming the plugin.
CI proves shared fixture byte equality and codec/corpus equality with the package
generator at pinned refs. Record those results and the exact deploy refs in the cutover
hand-off record.
On a paired-activation gate, `--resume` and `--abort` use the same checks. Neither bypasses
failed verification.
A gate prepared without `--paired-activation`, or created by `nso_intent_restore` or
`nso_renderer_baseline_cutover`, does not use paired verification. Plain `--abort` releases it,
and `--verify --device ID` sends one no-deletion static push before resume. A failed plain
`--prepare` releases only a gate that the same invocation created.
Resume the paired adapter admissions only after both components pass their cutover gates.
Local HTTP-double tests do not establish live NSO acceptance.

Terminal Apply history has a 30-day minimum retention period.
`nso_prune_apply_ledger --batch-size 100` is a dry run.
Add `--execute` to delete eligible history. A daily NetBox system job uses the same
bounded pruning function. The job retains a scan cursor in its job data.
Use the reported cursor with `--after CURSOR` to continue a command scan.
A reported `next_cursor=none` has no cursor: omit `--after`.
Active or repeatable selections, unresolved executions, current badges, fences, and
referenced prior snapshots retain their evidence.
Listed adapter generations retain all consumed carrier certificates for historical recovery.
The pruning scan requires a reachable adapter with a stable store identity.
