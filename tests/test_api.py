"""API route tests via webtest with injected fake handlers (no live DB)."""

from __future__ import annotations

import pytest
from webtest import TestApp

from quant_momentum.api.app import create_app
from quant_momentum.api.readiness import ReadinessStatus


def _client(**overrides) -> TestApp:
    base = dict(
        readiness_check=lambda: ReadinessStatus("ok", "0001", None),
        momentum_list=lambda p: {
            "status": "ok",
            "count": 1,
            "results": [{"ticker": "AAPL"}],
            "echo": {
                "ticker": p.ticker,
                "is_momentum": p.is_momentum,
                "adjustment_type": p.adjustment_type,
                "limit": p.limit,
            },
        },
        momentum_by_ticker=lambda t, limit: (
            {"status": "ok", "ticker": t.upper(), "count": 0, "results": []}
            if t.upper() == "AAPL"
            else None
        ),
        momentum_latest=lambda flag: {"status": "ok", "flag": flag, "results": []},
        momentum_date_range=lambda: {"status": "ok", "min_bar_date": "2026-01-01"},
        runs_list=lambda p: {"status": "ok", "count": 0, "results": [], "echo": {"status": p.status}},
        run_detail=lambda rid: {"id": rid, "status": "completed"} if rid == 1 else None,
        runs_latest=lambda: {"id": 9, "status": "completed"},
        stats=lambda: {"status": "ok", "totals": {"total_rows": 0}},
    )
    base.update(overrides)
    return TestApp(create_app(**base))


def test_health() -> None:
    assert _client().get("/health").json == {"status": "ok", "service": "quant-momentum-api"}


def test_ready_ok() -> None:
    resp = _client().get("/ready")
    assert resp.status_code == 200
    assert resp.json["status"] == "ready"
    assert resp.json["schema_version"] == "0001"


def test_ready_error_redacts_db_url(monkeypatch) -> None:
    url = "postgresql+psycopg://quant:supersecret@db:5432/quant"
    monkeypatch.setenv("DATABASE_URL", url)

    def _boom():
        raise RuntimeError(f"could not connect to {url}")

    resp = _client(readiness_check=_boom).get("/ready", status=503)
    assert resp.json["status"] == "not_ready"
    assert "supersecret" not in resp.text
    assert "<redacted>" in resp.text


def test_momentum_list_and_filter_echo() -> None:
    resp = _client().get("/momentum?ticker=aapl&is_momentum=true&adjustment_type=unadjusted&limit=50")
    assert resp.status_code == 200
    echo = resp.json["echo"]
    assert echo["ticker"] == "aapl"
    assert echo["is_momentum"] is True
    assert echo["adjustment_type"] == "unadjusted"
    assert echo["limit"] == 50


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=999",
        "offset=-1",
        "is_momentum=maybe",
        "adjustment_type=weird",
        "from_date=2026-13",
        "symbol_id=abc",
    ],
)
def test_momentum_list_validation_422(query: str) -> None:
    _client().get(f"/momentum?{query}", status=422)


def test_momentum_by_ticker_200_and_404() -> None:
    assert _client().get("/momentum/by-ticker/AAPL").status_code == 200
    _client().get("/momentum/by-ticker/ZZZ", status=404)


def test_momentum_latest_default_and_flag() -> None:
    assert _client().get("/momentum/latest").json["flag"] is True
    assert _client().get("/momentum/latest?is_momentum=false").json["flag"] is False


def test_momentum_date_range_200_and_404() -> None:
    assert _client().get("/momentum/date-range").status_code == 200
    _client(momentum_date_range=lambda: None).get("/momentum/date-range", status=404)


def test_runs_list_and_invalid_status_422() -> None:
    assert _client().get("/runs").status_code == 200
    _client().get("/runs?status=bogus", status=422)


def test_run_detail_200_404_422() -> None:
    assert _client().get("/runs/1").json["id"] == 1
    _client().get("/runs/2", status=404)
    _client().get("/runs/abc", status=422)


def test_runs_latest_200_and_404() -> None:
    assert _client().get("/runs/latest").json["latest"]["id"] == 9
    _client(runs_latest=lambda: None).get("/runs/latest", status=404)


def test_stats() -> None:
    assert _client().get("/stats").json["status"] == "ok"
