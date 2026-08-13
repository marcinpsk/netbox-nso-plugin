# Changelog

<!-- version list -->

## v0.3.0 (2026-08-18)

### Bug Fixes

- **adapter-client**: Log the transport exception type, never its text
  ([`23b396f`](https://github.com/marcinpsk/netbox-nso-plugin/commit/23b396f2813cb521d1b35c5b3e2ca81a183a0585))

- **devcontainer**: Pin netbox-test-django at a non-resolving adapter
  ([`97a298a`](https://github.com/marcinpsk/netbox-nso-plugin/commit/97a298a234ecdf76a921b112a1ed07a263295b44))

- **logging**: Survive a concurrent delete of the levels singleton
  ([`1cb8290`](https://github.com/marcinpsk/netbox-nso-plugin/commit/1cb829093c0ef787b09a6e102e516e91f6c04ad5))

- **reconcile**: Re-read the apply state after the settlement consumes a repair
  ([`c155b09`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c155b0942ee5955a62740a30980472cdb196ae65))

- **review**: Harden test and timestamp boundaries
  ([`0b76e4a`](https://github.com/marcinpsk/netbox-nso-plugin/commit/0b76e4afe3a67cc586de98cd45573972dd7258bb))

- **review**: Preserve valid sync timestamps
  ([`cad4a34`](https://github.com/marcinpsk/netbox-nso-plugin/commit/cad4a34ce9287294aff812940ba2658218013654))

- **security**: Escape the unknown-category key in the 400 body
  ([`b7cab70`](https://github.com/marcinpsk/netbox-nso-plugin/commit/b7cab70ce0263191b68b750623d98ef5d1103bdd))

- **security**: Report adapter failures by exception type, not text
  ([`226f0c2`](https://github.com/marcinpsk/netbox-nso-plugin/commit/226f0c217aae40aeb81a909282dacfa3433d8eab))

- **settlement**: Bind the reused apply probe to the device the consumer locked
  ([`aa53804`](https://github.com/marcinpsk/netbox-nso-plugin/commit/aa53804ae6567adfe80a2f39ac7b91dcf016a219))

- **settlement**: Bound a feed row with no sequence, and read back once per pass
  ([`8374d82`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8374d827f7968a93cce080ab84815c0c0256e1b0))

- **settlement**: Skip a feed row with no sequence instead of stalling on it
  ([`7431567`](https://github.com/marcinpsk/netbox-nso-plugin/commit/743156749b5938e7fc68d1aaea2b3501e0eb1e0b))

- **sync-cache**: Degrade non-string and offset-less adapter timestamps
  ([`c9b7007`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c9b7007ec6cac883c2590a3452af0cb5addef845))

- **test**: Require a private database name
  ([`658d3a6`](https://github.com/marcinpsk/netbox-nso-plugin/commit/658d3a605bb4c63f24429011e3293a089174a4c2))

- **test**: Spawn the restart consumer under the standard settings
  ([`340c878`](https://github.com/marcinpsk/netbox-nso-plugin/commit/340c878e4f3ed3e221ef3e2d2ed150943acce428))

### Chores

- **ci**: Bump astral-sh/setup-uv in the actions group
  ([`f7a3ed2`](https://github.com/marcinpsk/netbox-nso-plugin/commit/f7a3ed2eaaaedb75b8b163ad6f40ea1173141f9b))

- **deps**: Bump jsdom from 26.1.0 to 30.0.1
  ([`3b712fd`](https://github.com/marcinpsk/netbox-nso-plugin/commit/3b712fdb8e51bbfd859f40bfccf285c9b2f77f00))

- **deps**: Bump vitest from 3.2.7 to 4.1.10
  ([`74a5d8a`](https://github.com/marcinpsk/netbox-nso-plugin/commit/74a5d8aba0a8c34336e1c599633c43b150a4dc61))

- **deps**: Update django requirement from <7.0,>=5.1 to >=6.1,<7.0
  ([`28e0866`](https://github.com/marcinpsk/netbox-nso-plugin/commit/28e0866c6a9299449fdaf5b13184fb1de447d2a9))

- **deps**: Update mkdocs requirement from <2,>=1 to >=1.6.1,<2
  ([`f3b26f9`](https://github.com/marcinpsk/netbox-nso-plugin/commit/f3b26f9f29d0bac30d623765c0c69249b83424d4))

- **deps**: Update pytest-cov requirement from >=6.0 to >=7.1.0
  ([`2addffb`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2addffb569b4863d40b4c387733be64a585d3c41))

- **deps**: Update requests requirement from >=2.32 to >=2.34.2
  ([`0a619ae`](https://github.com/marcinpsk/netbox-nso-plugin/commit/0a619ae2f32b2165f0f59f34e50a824567228d59))

- **test**: Run pytest on capped xdist workers
  ([`5ed9927`](https://github.com/marcinpsk/netbox-nso-plugin/commit/5ed9927956f50882bb9342b7d5c2711057f1bbc8))

### Code Style

- Drop em-dashes from the text this branch added
  ([`9f4d5d4`](https://github.com/marcinpsk/netbox-nso-plugin/commit/9f4d5d42e2a52bde62c2efdd2ad7fe6976a997c6))

### Continuous Integration

- Bound the quick workflow jobs with a job timeout
  ([`69b7fc9`](https://github.com/marcinpsk/netbox-nso-plugin/commit/69b7fc9b236848945263f153ac52482c48e138f4))

- Give lint-format and js-test a read-only GITHUB_TOKEN
  ([`c87c629`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c87c6296d29bfb1d0b317cc81c8a0361e068b4df))

- **release**: Guard refs with expected tip lease
  ([`cceca5f`](https://github.com/marcinpsk/netbox-nso-plugin/commit/cceca5f92f753e21dbcfe8cff6ec13f2bd68ea3b))

- **release**: Skip a stale release trigger instead of resetting to it
  ([`542de23`](https://github.com/marcinpsk/netbox-nso-plugin/commit/542de23892b919f3d32398e23c84ac089ddd3d7f))

### Documentation

- **settlement**: State the stall bound in terms of feed entries
  ([`65dd1c8`](https://github.com/marcinpsk/netbox-nso-plugin/commit/65dd1c8a702d198fa8b3a539ea26bf59c113134e))

### Features

- **resync**: Report the arming a rejected push rolled back
  ([`65d5070`](https://github.com/marcinpsk/netbox-nso-plugin/commit/65d5070f8a86a3a12a51e6eba09b13861d447a55))

### Performance Improvements

- **reconcile**: Reuse Step 4's apply-job state in the static-route escalation
  ([`02dedcd`](https://github.com/marcinpsk/netbox-nso-plugin/commit/02dedcdc4290a17f964671495bff23ea717f7592))

- **views**: Join nso_instance on the onboarding dashboard's managed rows
  ([`e0eeac0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/e0eeac03f6b3535f424d1d73ba407d45fcab5bb0))

### Testing

- **config**: Scope the concurrent-editor injections to the row under test
  ([`8308a31`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8308a31f81249518c446003f2b8aa08e3f2132ec))

- **migrations**: Re-apply every leaf, not just the first
  ([`c2709c0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c2709c0a101c17841d90bbbd64e8b955fab9c660))

- **reconcile**: Exercise the settlement window on a real transaction boundary
  ([`ff49962`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ff499621cdfa69008723ae0df4579263ecab85ab))

- **reconcile**: Patch the forced SNMP, logging and L2 SAP pushes in the apply pin
  ([`081beeb`](https://github.com/marcinpsk/netbox-nso-plugin/commit/081beeba1c702273326696393a6f1d20f8894ff3))

- **release**: Pin bare remote branch
  ([`3570b5d`](https://github.com/marcinpsk/netbox-nso-plugin/commit/3570b5dc247e042dc8249fc19b4b69fce0ca45d2))

- **review**: Close isolated settings coverage gaps
  ([`ca7a95e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ca7a95e277a94d9396115382b8ea76948bafff29))

- **settlement**: Anchor the carrier barrier on the consumer entry point
  ([`3c33993`](https://github.com/marcinpsk/netbox-nso-plugin/commit/3c3399367864303946d2d3819cefbbcf73d49091))

- **signals**: Pin that a fail-closed rekey never reaches sync_notify
  ([`2f06b35`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2f06b35bfdbcf0575b76dba619be832a56e8f99f))

- **static-route**: Inject the reclassification on the accept update
  ([`6cb5dce`](https://github.com/marcinpsk/netbox-nso-plugin/commit/6cb5dcec3b2fc180cdfe2f8738fbcb35e318fbb1))

- **static-route**: Share the P2/P6 fixtures and reset the coalescer in a finally
  ([`789e058`](https://github.com/marcinpsk/netbox-nso-plugin/commit/789e0581dc15b9f27b2e8505f4ee80d4109c6bbd))


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
  - **Per-object static-route deletion authority is active end to end.** The intent
    outbox retains each removed route and its acknowledged identity until delivery.
    Static-route pushes carry the authority in `deleted_routes`, the adapter records a
    `delete_origin` tombstone, and its removal worker executes the networked retraction.
    A combined identity and membership edit settles only the retained device.

### Added — operations

- Device onboarding NetBox → NSO (create node, fetch host keys, unlock,
  sync-from) with NED picker and quick-manage from the NSO Devices dashboard.
- Adapter job orchestration from the tab: Sync, Detect drift, Test
  connection, Apply — with client-side job polling and status strip.
- REST API at `/api/plugins/nso/device-management/` consumed by the
  adapter's reconcile loop.
- Deployment-window tooling for adapter store restores:
  `nso_intent_deployment_gate` (`--prepare`/`--verify`/`--abort`) quiesces
  plugin-side writes behind a durable gate while a restore runs, with mutating
  HTTP requests answering 503 until the gate lifts, and `nso_intent_restore`
  rebuilds the outbox from the adapter's replayed receipts: it advances the
  push-seq and static-route pk namespaces past everything the store
  acknowledged, clears delivery lineage, and resolves open claims.

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
