# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Keep the settlement adapter double's diagnostics for unexpected requests."""

import pytest

from ._settlement_adapter import FakeAdapter, LoopbackOnlySession


@pytest.fixture
def adapter():
    double = FakeAdapter()
    yield double
    double.stop()


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_an_unexpected_non_json_request_gets_the_404_diagnostic(adapter, method):
    with LoopbackOnlySession() as session:
        response = session.request(method, f"{adapter.url}/api/v1/unexpected", data=b"not json", timeout=5)
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "unexpected request /api/v1/unexpected"
        # The drained body leaves the kept-alive connection usable.
        again = session.request(method, f"{adapter.url}/api/v1/unexpected", data=b"{}", timeout=5)
        assert again.status_code == 404
