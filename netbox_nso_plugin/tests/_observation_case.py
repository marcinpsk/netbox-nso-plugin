# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Observation fixtures and the adapter HTTP boundary for publication tests."""

import hashlib
import json
from urllib.parse import urlsplit

from requests.adapters import BaseAdapter
from requests.sessions import Session

from ._adapter_http import make_response
from .test_read_gate import _rs


def observation(family, *, revision=1, source_epoch=1, interfaces=None, unprojectable=None):
    document = {"interfaces": interfaces or [], "unprojectable": unprojectable or []}
    attributes = {
        "interface_attributes": ["description", "enabled"],
        "interface_ip": ["address", "prefix_length", "secondary", "vrf"],
    }
    return {
        "family": family,
        "revision": revision,
        "source_epoch": source_epoch,
        "digest": hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "observed_at": "2026-10-01T12:00:00+00:00",
        "coverage": {"attributes": attributes[family]},
        "document": document,
    }


def interface_observation(name="Ethernet1", description="device description", enabled=True):
    return {
        "name": name,
        "description": description,
        "enabled": enabled,
        "kind": None,
        "parent_binding": None,
        "encap_tag": None,
        "vrf": None,
        "service": None,
    }


def ip_observation(interface="Ethernet1", address="198.18.0.1/24", *, bound_port=None, vrf=None, prefix_length=24):
    return {
        "interface": interface,
        "bound_port": bound_port,
        "addresses": [
            {"address": address, "prefix_length": prefix_length, "family": "ipv4", "secondary": False, "vrf": vrf}
        ],
    }


class ObservationTransport(BaseAdapter):
    def __init__(self, device_id, interface_snapshot, ip_snapshot, *, legacy=False):
        self.documents = {
            "interfaces-doc": {
                "interfaces": [],
                "read_state": _rs(payload_revision=interface_snapshot["revision"]),
                "observation": interface_snapshot,
            },
            "interface-ips": {
                "interfaces": [],
                "read_state": _rs(payload_revision=ip_snapshot["revision"]),
                "observation": ip_snapshot,
            },
            "interfaces": [],
            "state": [],
            "svi": {"interfaces": []},
            "subinterface": {"interfaces": []},
        }
        self.device_id = device_id
        self.legacy = legacy
        self.onboard_result = None
        self.patch_result = None
        self.requests = []

    def session(self):
        session = Session()
        session.trust_env = False
        session.mount("http://", self)
        session.mount("https://", self)
        return session

    def send(self, request, **kwargs):
        self.requests.append(request)
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        if request.method == "POST" and endpoint == "devices" and self.onboard_result is not None:
            response = make_response(200, self.onboard_result)
            response.request = request
            response.url = request.url
            return response
        if request.method == "PATCH" and endpoint == str(self.device_id) and self.patch_result is not None:
            response = make_response(200, self.patch_result)
            response.request = request
            response.url = request.url
            return response
        assert request.method == "GET", request.method
        assert f"/devices/{self.device_id}/" in request.url, request.url
        assert endpoint in self.documents, endpoint
        status = 404 if endpoint == "interfaces-doc" and self.legacy else 200
        response = make_response(status, self.documents[endpoint])
        response.request = request
        response.url = request.url
        return response

    def close(self):
        pass
