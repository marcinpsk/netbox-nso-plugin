# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Require collection coverage from the vendored observation schema before writes."""

from .comparison_values import MISSING, observed_collection
from .observations import OBSERVATION_SCHEMAS

SCALAR_ARRAY_COLLECTIONS = frozenset(
    {
        ("route_policy", "route_maps[].entry[].match_prefix_lists"),
        ("route_policy", "route_maps[].entry[].match_community_lists"),
        ("route_policy", "route_maps[].entry[].match_as_paths"),
    }
)
SCALAR_ARRAY_VALUES = frozenset({("switchport", "interfaces[].tagged_vlans")})


def segment_routing_absent(item):
    """Recognize the documented empty IS-IS segment-routing container."""
    return (
        {"segment_routing_reported", "segment_routing_configured"} <= set(item.get("present", ()))
        and item.get("segment_routing_reported") is True
        and item.get("segment_routing_configured") is False
    )


def collection_gaps(family, document, *, schema_component=None):
    """Return unavailable collection paths without interpreting them as empty."""
    schema = OBSERVATION_SCHEMAS[family].schema
    definitions = schema["components"]["schemas"]

    def resolve(node):
        return definitions[node["$ref"].rsplit("/", 1)[-1]] if "$ref" in node else node

    gaps = []

    def walk(parent, node, prefix, schema_prefix):
        for name, field in resolve(node).get("properties", {}).items():
            if name in {"present", "unprojectable"}:
                continue
            path = f"{prefix}.{name}" if prefix else name
            schema_path = f"{schema_prefix}.{name}" if schema_prefix else name
            variants = field.get("anyOf", [field])
            array = next((variant for variant in variants if variant.get("type") == "array"), None)
            if array is not None:
                items = resolve(array["items"])
                item_variants = items.get("anyOf", [items])
                if not any(resolve(variant).get("type") == "object" for variant in item_variants):
                    scalar_path = (family, schema_path)
                    if scalar_path in SCALAR_ARRAY_VALUES:
                        continue
                    if scalar_path not in SCALAR_ARRAY_COLLECTIONS:
                        raise ValueError(f"Unclassified scalar array: {family}.{schema_path}")
                collection = observed_collection(parent, name)
                if not isinstance(collection, list):
                    gaps.append(path)
                    continue
                for index, item in enumerate(collection):
                    if isinstance(item, dict):
                        walk(item, array["items"], f"{path}[{index}]", schema_path + "[]")
            elif name == "segment_routing":
                if not segment_routing_absent(parent) and observed_collection(parent, name) is MISSING:
                    gaps.append(path)

    root = definitions[schema_component] if schema_component else resolve(schema)["properties"]["document"]
    walk(document, root, "", "")
    return tuple(gaps)


def require_collection_coverage(family, observation):
    """Refuse an observation whose collections cannot authorize mirror writes."""
    from .adapter_client import AdapterError

    document = observation.get("document") if isinstance(observation, dict) else None
    if not isinstance(document, dict):
        raise AdapterError("Collection coverage is unavailable: missing observation document.", code="invalid_response")
    gaps = collection_gaps(family, document)
    if gaps:
        raise AdapterError("Collection coverage is unavailable: " + ", ".join(gaps), code="invalid_response")
