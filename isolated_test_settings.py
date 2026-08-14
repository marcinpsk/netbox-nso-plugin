# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Settings for an isolated NetBox plugin test database.

Run inside the devcontainer with an isolated database family:

    TEST_DB_NAME=test_<yours> PYTHONPATH=<this repo's checkout> \
        python /opt/netbox/netbox/manage.py test netbox_nso_plugin --settings=isolated_test_settings \
        --keepdb --noinput

Several sessions share one PostgreSQL instance. Each suite run must use its own test
database. This file lives in the repository because a container rebuild deletes files
placed under ``/opt/netbox/netbox``.
"""

import os
import re

from netbox.settings import *  # noqa: F403


def _test_database_name() -> str:
    name = os.environ.get("TEST_DB_NAME")
    if not name:
        raise RuntimeError("TEST_DB_NAME is required. Run with TEST_DB_NAME=test_nso_<tag>.")
    live_name = str(DATABASES["default"].get("NAME") or "")  # noqa: F405
    if name == live_name or re.fullmatch(r"test_nso_[a-z0-9][a-z0-9_]{0,53}", name) is None:
        raise RuntimeError("TEST_DB_NAME must name a private test_nso_<tag> database.")
    return name


TEST_DATABASE_NAME = _test_database_name()

DATABASES["default"].setdefault("TEST", {})["NAME"] = TEST_DATABASE_NAME  # noqa: F405

# Django's runner does not load the pytest network guard. Use a reserved name and a
# placeholder token so no inherited adapter configuration or credential reaches a request.
_PLUGIN_CONFIG = dict(PLUGINS_CONFIG.get("netbox_nso_plugin", {}))  # noqa: F405
_PLUGIN_CONFIG.update(adapter_url="http://adapter.mock.invalid", adapter_token="test-token")
PLUGINS_CONFIG = {**PLUGINS_CONFIG, "netbox_nso_plugin": _PLUGIN_CONFIG}  # noqa: F405
