# Changelog

<!-- version list -->

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
- Route-policy Accept crash, VLAN rename surfacing as drift, switchport
  3-way merge seeding, per-scope apply-failure surfacing.
