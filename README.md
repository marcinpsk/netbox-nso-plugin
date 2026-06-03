# netbox-nso-plugin

NetBox 4.6 plugin that integrates NetBox with Cisco NSO via the **nso-adapter** REST API.

## Features

- Mark NetBox devices as NSO-managed and choose which attributes to sync (`description`, `enabled`).
- Trigger adapter actions: **Sync**, **Check compliance**, **Test connection**.
- Display per-interface compliance status in the Device detail "NSO" tab.
- Exposes `NSODeviceManagement` at `/api/plugins/netbox-nso-plugin/device-management/` for the adapter's self-healing reconcile path.

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

## Development

See `.devcontainer/` for the Docker-based development environment.
