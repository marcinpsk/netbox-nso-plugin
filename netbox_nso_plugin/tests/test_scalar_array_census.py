# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Every observed scalar array has an explicit coverage classification."""

import json
import os
from pathlib import Path

import pytest

from netbox_nso_plugin import collection_coverage


def scalar_array_paths():
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "observations.json").read_text(encoding="utf-8")
    )
    definitions = schema["components"]["schemas"]
    paths = set()

    def variants(node):
        if "$ref" in node:
            return variants(definitions[node["$ref"].rsplit("/", 1)[-1]])
        if "anyOf" in node:
            return [item for variant in node["anyOf"] for item in variants(variant)]
        return [node]

    def walk(family, node, prefix):
        for variant in variants(node):
            if variant.get("type") == "array":
                items = variants(variant["items"])
                if all(item.get("type") in {"string", "integer", "number", "boolean", "null"} for item in items):
                    paths.add((family, prefix))
                else:
                    walk(family, variant["items"], prefix + "[]")
            else:
                for name, field in variant.get("properties", {}).items():
                    if name not in {"present", "unprojectable"}:
                        walk(family, field, f"{prefix}.{name}" if prefix else name)

    for family, component in schema["families"].items():
        walk(family, definitions[component]["properties"]["document"], "")
    return paths


def test_every_scalar_array_has_exactly_one_classification():
    collections = collection_coverage.SCALAR_ARRAY_COLLECTIONS
    values = collection_coverage.SCALAR_ARRAY_VALUES
    assert not collections & values
    assert scalar_array_paths() == collections | values


def test_scalar_array_collections_match_joined_manifest_leaf_lists():
    adapter_root = Path(
        os.environ.get("NSO_JOINED_ADAPTER_ROOT", Path(__file__).resolve().parents[2].parent / ".o3c-adapter")
    )
    manifest_path = adapter_root / "nso_adapter" / "nso" / "collection_manifest.json"
    if not manifest_path.is_file():
        pytest.skip("Joined adapter collection manifest is unavailable; set NSO_JOINED_ADAPTER_ROOT.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    leaf_lists = set()

    def walk(family, collections, prefix):
        for name, node in collections.items():
            field = "route_maps" if name == "route-map" else name.replace("-", "_")
            path = f"{prefix}.{field}" if prefix else field
            if node["kind"] == "leaf-list":
                leaf_lists.add((family.replace("-", "_"), path))
            else:
                walk(family, node["collections"], path + "[]")

    for family, node in manifest["families"].items():
        walk(family, node["collections"], "")
    assert leaf_lists == collection_coverage.SCALAR_ARRAY_COLLECTIONS


def test_access_port_tagged_vlan_value_is_not_a_collection_gap():
    document = {
        "interfaces": [
            {"interface_name": "Ethernet1", "mode": "access", "untagged_vlan": 10, "present": ["mode", "untagged_vlan"]}
        ],
        "present": ["interfaces"],
        "unprojectable": [],
    }
    assert collection_coverage.collection_gaps("switchport", document) == ()
