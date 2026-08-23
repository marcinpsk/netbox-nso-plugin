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

## Installation

```bash
pip install netbox-nso-plugin
```

Add to `PLUGINS` in `configuration.py`:

```python
PLUGINS = ["netbox_nso_plugin"]
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

Programmatic writers must wrap each create, save, or delete of a managed intent model in
`transaction.atomic()`. The model write and its intent outbox entry must commit together.
`QuerySet.update()`, `bulk_update()`, and `bulk_create()` bypass the save signals that schedule
the outbox. Programmatic intent writes must not use them unless an outbox-aware service runs
inside `transaction.atomic()`. Reconcile and drain bookkeeping writes are exempt when they do
not change operator intent.

## Development

See `.devcontainer/` for the Docker-based development environment.
