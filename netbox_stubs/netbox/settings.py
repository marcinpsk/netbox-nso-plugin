# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Minimal Django settings stub for running plugin tests outside the devcontainer."""

SECRET_KEY = "test-secret-key-stub-not-for-production"
DEBUG = True
USE_TZ = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

PLUGINS_CONFIG = {
    "netbox_nso_plugin": {
        "adapter_url": "http://adapter",
        "adapter_token": "test-token",
    }
}
