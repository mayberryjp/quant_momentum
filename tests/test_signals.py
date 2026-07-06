"""Tests for the quant_signals producer (mocked HTTP; no network)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import requests

from quant_momentum.config import Settings
from quant_momentum.momentum import compute_momentum
from quant_momentum.signals import SignalsClient, build_payload

_AS_OF = date(2026, 7, 5)


def _flagged_result() -> "object":
    closes = [Decimal("100")] * 31
    closes[0] = Decimal("120")
    closes[5] = Decimal("110")
    closes[15] = Decimal("105")
    closes[30] = Decimal("100")
    return compute_momentum(closes, thresholds={5: 0, 15: 0, 30: 0}, rule="ALL")


class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class _Session:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(responses, **kwargs):
    slept: list[float] = []
    client = SignalsClient(
        "http://signals:8016",
        timeout=5,
        retry_count=kwargs.get("retry_count", 3),
        backoff_seconds=0.5,
        session=_Session(responses),
        sleep=slept.append,
    )
    return client, slept


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------
def test_build_payload_shape_and_idempotency_key() -> None:
    payload = build_payload(
        _flagged_result(),
        ticker="aapl",
        bar_date=_AS_OF,
        adjustment_type="unadjusted",
        settings=Settings(),
    )
    assert payload["idempotency_key"] == "momentum-v1:2026-07-05:AAPL"
    assert payload["ticker"] == "AAPL"
    assert payload["source"] == "momentum-v1"
    assert payload["direction"] == "long"
    assert payload["signal_type"] == "watchlist_candidate"
    assert payload["tags"] == ["momentum", "5d", "15d", "30d"]
    assert 0.0 <= payload["score"] <= 1.0
    assert payload["metadata"]["rule"] == "ALL"
    assert payload["metadata"]["bar_date"] == "2026-07-05"


def test_score_is_clamped_to_one() -> None:
    closes = [Decimal("100")] * 31
    closes[0] = Decimal("1000")  # huge move -> mean/scale > 1 -> clamp
    payload = build_payload(
        compute_momentum(closes, thresholds={5: 0, 15: 0, 30: 0}, rule="ANY"),
        ticker="X",
        bar_date=_AS_OF,
        adjustment_type="unadjusted",
        settings=Settings(),
    )
    assert payload["score"] == 1.0


# --------------------------------------------------------------------------
# client classification & retry
# --------------------------------------------------------------------------
def test_accepted() -> None:
    client, _ = _client([_Resp(200, {"status": "accepted", "signal_cache_id": "c1"})])
    outcome = client.submit({"idempotency_key": "k"})
    assert outcome.status == "accepted"
    assert outcome.signal_cache_id == "c1"
    assert outcome.http_status == 200


def test_duplicate() -> None:
    client, _ = _client([_Resp(200, {"status": "duplicate"})])
    assert client.submit({}).status == "duplicate"


def test_unresolved() -> None:
    client, _ = _client([_Resp(200, {"status": "unresolved"})])
    assert client.submit({}).status == "unresolved"


def test_2xx_without_status_is_accepted() -> None:
    client, _ = _client([_Resp(201, {})])
    assert client.submit({}).status == "accepted"


def test_5xx_retries_then_succeeds() -> None:
    client, slept = _client([_Resp(503), _Resp(503), _Resp(200, {"status": "accepted"})])
    outcome = client.submit({})
    assert outcome.status == "accepted"
    assert len(slept) == 2  # two backoffs before success
    assert slept == [0.5, 1.0]  # exponential


def test_network_error_then_success() -> None:
    client, slept = _client([requests.ConnectionError("boom"), _Resp(200, {"status": "accepted"})])
    assert client.submit({}).status == "accepted"
    assert len(slept) == 1


def test_persistent_5xx_fails() -> None:
    client, _ = _client([_Resp(500), _Resp(500), _Resp(500), _Resp(500)], retry_count=3)
    outcome = client.submit({})
    assert outcome.status == "failed"
    assert outcome.http_status == 500


def test_4xx_is_failed_not_retried() -> None:
    client, slept = _client([_Resp(400, {"error": "bad"})])
    outcome = client.submit({})
    assert outcome.status == "failed"
    assert outcome.http_status == 400
    assert slept == []
