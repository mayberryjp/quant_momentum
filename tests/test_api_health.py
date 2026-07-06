"""Tests for the API application skeleton."""

from __future__ import annotations

from webtest import TestApp

from quant_momentum.api.app import create_app


def test_health_ok() -> None:
    client = TestApp(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json == {"status": "ok", "service": "quant-momentum-api"}
