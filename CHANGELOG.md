# Changelog

<!-- version list -->

## v0.2.0 (2026-08-10)

### Bug Fixes

- **apply**: Promote static routes only on an acknowledged stored count
  ([`4e80f37`](https://github.com/marcinpsk/netbox-nso-plugin/commit/4e80f372962840409d0fa6bc03c1228c253c100d))

- **reconcile**: Read nso_management defensively in the settle step
  ([`509fa10`](https://github.com/marcinpsk/netbox-nso-plugin/commit/509fa106bf7aef56c96e66d4c16f5fc48641dbe1))

- **static-route**: Accept only a real route count as a stored acknowledgement
  ([`4990b6d`](https://github.com/marcinpsk/netbox-nso-plugin/commit/4990b6d2baca70dad65dffc9f0c14e29bd260762))

### Chores

- **deps**: Watch the JS test harness with dependabot
  ([`166f7af`](https://github.com/marcinpsk/netbox-nso-plugin/commit/166f7afa7350bffb141a2e33e2189bc22c9a4f69))

### Continuous Integration

- **test**: Pin the matrix diagonally and honor DB_NAME in the CI configuration
  ([`8041a05`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8041a057ed220536beab0cf5faf353ed2a847199))

### Features

- **jobs**: Report the settlement sweep's elapsed time in the tick summary
  ([`29b4714`](https://github.com/marcinpsk/netbox-nso-plugin/commit/29b4714c0252f3b0c515f422f8182450d1d7a89e))

### Performance Improvements

- **settlement**: Drop the redundant feed request and the unbounded reads
  ([`ac7b8c4`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ac7b8c4b4b50d52b72de393307fd59f3663b68f3))

### Refactoring

- **signals**: Attribute a static-route rejection with the push filter
  ([`ab6014f`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ab6014f419e673d8eb03c3ee648860355b83dffb))

### Testing

- **apply**: Stop each patch on its own cleanup, and pin the stuck-row message
  ([`2e9f58e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2e9f58ea3d9b3daa8e43f4aa99ffb3ed1aacf365))

- **contract**: Restore all three settings entry states, not just the value
  ([`7cda4b5`](https://github.com/marcinpsk/netbox-nso-plugin/commit/7cda4b5a2a559788552839688d5c2a5c65bc8b7e))

- **contract**: Restore the process plugin config the live client replaces
  ([`72aa7c8`](https://github.com/marcinpsk/netbox-nso-plugin/commit/72aa7c89be5cbca1b4af71f991b87ee7bdfa7001))

- **migrations**: Report the pending migration instead of a raw SystemExit
  ([`f1ecdf3`](https://github.com/marcinpsk/netbox-nso-plugin/commit/f1ecdf3e7182e569c21b2bb64c43f81ea06ef212))

- **settlement**: 422 a jobs request with no device_id on either order
  ([`7a7dcc4`](https://github.com/marcinpsk/netbox-nso-plugin/commit/7a7dcc49d2a3de5fd471903242f0cca54d6a0905))

- **settlement**: Define the generation-clock helper once, in the shared base
  ([`1d72354`](https://github.com/marcinpsk/netbox-nso-plugin/commit/1d7235436bc3f0941fdc2008cb1d3211ed993661))

- **settlement**: Derive the protected settlement columns, and locate manage.py
  ([`fc35978`](https://github.com/marcinpsk/netbox-nso-plugin/commit/fc35978664552f07f2fd96f1634908d839594353))

- **settlement**: Fail a bounded thread join on its own terms
  ([`6006505`](https://github.com/marcinpsk/netbox-nso-plugin/commit/60065050194060735a76c1ffa7733bc5cc26ace5))

- **settlement**: Judge the adapter's callback on the test thread
  ([`c43c9bd`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c43c9bd9f175a5441bfef6ad676f028a32aee790))

- **settlement**: Prove the mirror pass reached the failed-settlement row
  ([`45e4cd1`](https://github.com/marcinpsk/netbox-nso-plugin/commit/45e4cd1ad561262fce4f1bda867adea97c2c2648))


## [Unreleased]

### Added — read paths (NSO → NetBox)

- Core models: `NSOInstance`, `NSODeviceManagement` (managed scopes per device),
  plus `NSO*State` status overlays for every synced family, all driven by a
  unified status state machine (`unknown → imported → accepted → deploying →
  in_sync / apply_failed`, drift → `changed`, faults → `error`).
- Device **NSO tab**: counts-first category summaries with lazy row expansion,
  pagination + search per category, rendered from last-synced state for fast
  loads; one merged per-interface table (enabled / description / IPs / MTU /
  switchport) with per-cell Accept.
- Synced families: interface attributes + IPs + MTU (`mtu`/`ip-mtu`/`mpls-mtu`),
  VLAN database, L2 switchport, SVI/IRB, dot1q subinterfaces, L2 services
  (Nokia epipe/vpls SAPs), LACP/LAG bundles, IS-IS (instances, interfaces,
  levels, segment-routing, Flex-Algo), OSPF, BGP (peers, AFs, peer-group
  templates), route-policy objects, redistribution, static routes, BFD, SNMP
  (secret-safe: hash + vault refs only), logging hosts.
- Reconcilers materialise native NetBox/netbox-routing objects (interfaces,
  IPs, VLANs, ISIS/OSPF/BGP graph, route policies, static routes) and 3-way
  merge device vs NetBox edits without clobbering operator-owned rows.

### Added — write path (NetBox → device, via nso-adapter)

- **Accept → Apply** flow: Accept promotes an imported value to NetBox-owned
  intent (pushed to the adapter intent mirror); a single device **Apply**
  commits all pending scopes through the NSO `*-reconciler` services, with a
  two-panel preview (intent summary + native device diff per scope).
- Greenfield writes for operator-created objects: static routes, VLANs
  (shared-VLAN attach + rename propagation), route policies, OSPF/IS-IS
  interface enablement, IS-IS Flex-Algo, SVIs, subinterfaces, Nokia routed
  sub-interfaces (port binding emitted for never-imported interfaces).
- Inline operator edit on the tab (description, enabled, MTU) with
  clobber-safe value overlays.
- Intent **split-brain detection + one-click re-sync**: per-scope comparison
  of the adapter intent mirror vs NetBox-owned overlays (orphaned and
  partial/count-based), surfaced as a device-tab banner; re-sync re-pushes
  the owned snapshot and never touches the device.
- Drift banner + re-sync for orphaned adapter intent; value-aware drift
  display comparing live NetBox values against the device.
- **Intent-push rejections are recorded and shown.** A push the adapter refuses
  is still swallowed — an unreachable adapter must not raise into the operator's
  save — but the reason is now persisted per (device, scope) on
  `NSODeviceManagement` and rendered as a category banner, instead of living only
  in a log line under a green row. Static-route rows additionally show the apply's
  own per-route error or its `unproven` advisory, and an owned `apply_failed`
  static route no longer renders as "pending apply". Recording only: durable
  retry over the record is tracked separately.
- **Generation-correlated settlement for static routes.** A static-route overlay
  no longer settles on a scope-wide apply counter. Each push stamps the route with
  a plugin-global `intent_generation` and records the fingerprint the adapter
  echoes for it, and the overlay settles only when a per-route apply result names
  **both**. A result naming a generation the overlay has already moved past is
  simply not this row's result: it is skipped and the cursor advances. A result
  naming the **current** generation but a fingerprint this device is not waiting
  for is a disagreement about content, so it does not settle and it records why.
  `unproven` is neither a settle nor a failure: it is kept as an advisory on the row.
  - Results arrive over the adapter's **ordered settlement feed**
    (`GET /api/v1/jobs?order=asc&after_settle_seq=…`), walked under a durable
    per-device cursor on `NSODeviceManagement`. The cursor is keyed on
    *(store incarnation, adapter device id)* and both halves are compared on every
    read — the incarnation against the feed response's `X-Store-Incarnation` header,
    never against a cached mirror — so an adapter store rebuild or a device remap
    resets the cursor instead of silently skipping every settlement below it.
  - A result that cannot be decided (a lost PUT response whose expectation the
    adapter's read-back also fails to re-serve) stalls the device rather than being
    burned. The stall is bounded at **five** attempts, counted per stuck sequence
    and persisted on the row, so the count survives a worker restart; on the fifth
    the cursor advances past it with an error-level log.
  - Two independent clocks consume the feed: the device reconcile (the carrier,
    running ahead of the stuck-`deploying` backstop in the same invocation) and the
    five-minute `RefreshDeviceSyncCacheJob` maintenance tick. The tick runs
    plugin → adapter, so consumption survives a dead adapter → NetBox callback
    channel. A consumer failure stands the static-route backstop down for that
    invocation only, leaving the other scopes' settlement untouched.
  - `manage.py nso_consume_static_route_settlements` walks the feed by hand — the
    operator's drain tool, not the production path.
  - Rows owned before generations existed carry the sentinel `0` and can correlate
    with nothing. `manage.py nso_resync_static_route_intent` arms them in the same
    pass that backfills `route_id` into the adapter's store, and demotes a
    pre-existing `deploying` row to `accepted` so no result is owed for a
    generation that was never sent.
  - **Not included: deletion semantics.** The removed-device arm — a push that lists
    the dropped route in `deleted_route_ids` and an adapter tombstone marked
    `delete_origin` — ships with the intent-outbox work and is still open. Until it
    lands, a combined identity-plus-membership edit settles only the **retained**
    device.

### Added — operations

- Device onboarding NetBox → NSO (create node, fetch host keys, unlock,
  sync-from) with NED picker and quick-manage from the NSO Devices dashboard.
- Adapter job orchestration from the tab: Sync, Detect drift, Test
  connection, Apply — with client-side job polling and status strip.
- REST API at `/api/plugins/nso/device-management/` consumed by the
  adapter's reconcile loop.

### Changed

- Per-interface scope cards consolidated into the single merged Interface
  table; category templates deduplicated into shared partials.

### Fixed (highlights)

- Owned overlays no longer stuck in `pending`/`deploying` (settle to
  `in_sync` after Apply; `apply_failed` wired).
- Reconcile no longer clobbers owned OSPF/IS-IS interface intent; stale
  overlay rows pruned across all FK reconcilers instead of raising false
  drift.
- Reconcile no longer restores a static route's superseded generation state.
  Its mirror refresh saves an explicit field allow-list, and it writes the
  overlay status only as a compare-and-set against the status it observed, so a
  reconcile that began before a concurrent writer's lock can no longer put back
  the generation, the expectation or the status that writer had just replaced.
- Route-policy Accept crash, VLAN rename surfacing as drift, switchport
  3-way merge seeding, per-scope apply-failure surfacing.
