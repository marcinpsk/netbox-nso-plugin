# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Parsing/validation for fully-qualified Vault KV v2 references.

Canonical forms (always stored fully qualified, plugin and adapter alike):

* ``<mount>/<path...>#<key>`` — one secret field (SNMP communities)
* ``<mount>/<path...>`` — a secret path whose fields are fixed by convention
  (SNMP v3 users: fields ``auth``/``priv``)

Mirror of ``nso_adapter/secrets/refs.py`` — both test suites share the same
golden vectors; keep the two in sync.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

__all__ = ["VaultRef", "VaultRefError", "parse_vault_ref", "qualify_snmp_ref", "secret_fingerprint"]


def qualify_snmp_ref(raw: str, *, kind: str, kv_mount: str | None, base_path: str | None) -> str:
    """Qualify a pasted ref: short refs (no ``/``) land under the settings layout.

    A ref containing ``/`` is treated as fully qualified and never rewritten.
    Short community refs gain ``#community`` when no key is given; short v3 refs
    must not carry a key. Raises :class:`VaultRefError` when a short ref cannot
    be qualified (Vault settings absent/disabled).
    """
    raw = raw.strip()
    if "/" in raw:
        return raw
    if not kv_mount or not base_path:
        raise VaultRefError(
            f"short ref {raw!r} cannot be qualified — configure the Vault settings (mount + base path) "
            "or paste a fully-qualified 'mount/path#key' ref"
        )
    if kind == "community":
        if "#" not in raw:
            raw = f"{raw}#community"
        return f"{kv_mount}/{base_path}/community/{raw}"
    if kind == "v3":
        if "#" in raw:
            raise VaultRefError(f"v3 refs must not carry a '#key': {raw!r}")
        return f"{kv_mount}/{base_path}/v3/{raw}"
    raise VaultRefError(f"unknown ref kind {kind!r}")


class VaultRefError(ValueError):
    """Raised for a reference that cannot yield a (mount, path[, key]) triple."""


def secret_fingerprint(value: str) -> str:
    """Return the cross-repo secret fingerprint: first 16 hex chars of SHA-256.

    Matches network-state-export's ``_community_hash`` (the read mirror's
    community identity), so vault-vs-device comparison is string equality.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class VaultRef:
    """Parsed Vault reference containing mount, path, and optional key."""

    mount: str
    path: str
    key: str | None

    def __str__(self) -> str:
        base = f"{self.mount}/{self.path}"
        return f"{base}#{self.key}" if self.key is not None else base


def parse_vault_ref(reference: str, *, require_key: bool | None = None) -> VaultRef:
    """Parse a fully-qualified Vault reference into (mount, path, key).

    ``require_key=True`` rejects refs without ``#key`` (community-style),
    ``require_key=False`` rejects refs with one (v3 path-style), ``None``
    accepts both. Raises :class:`VaultRefError` on any malformed input; the
    message contains only the reference text (refs are non-secret).
    """
    if not isinstance(reference, str) or not reference:
        raise VaultRefError(f"empty vault_ref {reference!r}")
    if any(ch.isspace() for ch in reference):
        raise VaultRefError(f"vault_ref contains whitespace: {reference!r}")
    if reference.count("#") > 1:
        raise VaultRefError(f"vault_ref has more than one '#': {reference!r}")

    locator, sep, key = reference.partition("#")
    if sep and not key:
        raise VaultRefError(f"vault_ref has an empty key after '#': {reference!r}")
    if require_key is True and not sep:
        raise VaultRefError(f"vault_ref must end in '#<key>': {reference!r}")
    if require_key is False and sep:
        raise VaultRefError(f"vault_ref must not carry a '#<key>' here: {reference!r}")

    mount, slash, path = locator.partition("/")
    if not slash or not mount or not path:
        raise VaultRefError(f"vault_ref must be '<mount>/<path...>': {reference!r}")
    if "" in path.split("/"):
        raise VaultRefError(f"vault_ref has an empty path segment: {reference!r}")

    return VaultRef(mount=mount, path=path, key=key if sep else None)
