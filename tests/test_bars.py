"""Tests for the upstream bars reader (no live DB; fake connection)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from quant_momentum.bars import (
    BarsApiClient,
    DailyBarSnapshot,
    SymbolRef,
    build_trailing_closes,
    resolve_symbols,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return list(self._rows)

    def scalar(self):
        return self._rows

    def scalars(self):
        return list(self._rows)


class _FakeConn:
    """Minimal stand-in for a SQLAlchemy Connection."""

    def __init__(self, rows):
        self._rows = rows
        self.calls: list[tuple[str, dict | None]] = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return _FakeResult(self._rows)


def _row(symbol_id, ticker, bar_date, close, rn):
    return {"symbol_id": symbol_id, "ticker": ticker, "bar_date": bar_date, "close": close, "rn": rn}


def test_build_trailing_closes_orders_most_recent_first() -> None:
    rows = [
        _row(1, "AAPL", date(2026, 7, 2), Decimal("10"), 3),
        _row(1, "AAPL", date(2026, 7, 6), Decimal("12"), 1),
        _row(1, "AAPL", date(2026, 7, 3), Decimal("11"), 2),
    ]
    result = build_trailing_closes(rows)
    closes = result[1].closes
    assert result[1].ticker == "AAPL"
    assert [c.bar_date for c in closes] == [date(2026, 7, 6), date(2026, 7, 3), date(2026, 7, 2)]
    assert [c.close for c in closes] == [Decimal("12"), Decimal("11"), Decimal("10")]
    assert result[1].bars_available == 3


def test_build_trailing_closes_multiple_symbols_and_short_history() -> None:
    rows = [
        _row(1, "AAPL", date(2026, 7, 6), Decimal("12"), 1),
        _row(1, "AAPL", date(2026, 7, 3), Decimal("11"), 2),
        _row(2, "MSFT", date(2026, 7, 6), Decimal("50"), 1),  # only one bar
    ]
    result = build_trailing_closes(rows)
    assert set(result) == {1, 2}
    assert result[2].bars_available == 1
    assert result[2].closes[0].close == Decimal("50")


def test_build_trailing_closes_empty_returns_empty() -> None:
    assert build_trailing_closes([]) == {}


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class _FakeSession:
    """Serves ``GET /bars`` payloads from a params->payload handler."""

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return _FakeResponse(self._handler(params))


def _client(handler) -> BarsApiClient:
    return BarsApiClient(
        "http://bars.test",
        timeout=1.0,
        retry_count=0,
        backoff_seconds=0.0,
        session=_FakeSession(handler),
    )


def _item(symbol_id, ticker, bar_date, close, high=0.0, low=0.0):
    return {
        "symbol_id": symbol_id,
        "ticker": ticker,
        "bar_date": bar_date,
        "close": close,
        "high": high,
        "low": low,
    }


def test_latest_bar_date_reads_first_item() -> None:
    client = _client(lambda p: {"items": [_item(1, "AAPL", "2026-07-06", 12.0)]})
    assert client.latest_bar_date("unadjusted") == date(2026, 7, 6)
    assert client._session.calls[0]["adjustment_type"] == "unadjusted"
    assert client._session.calls[0]["limit"] == 1


def test_latest_bar_date_empty_returns_none() -> None:
    client = _client(lambda p: {"items": []})
    assert client.latest_bar_date("unadjusted") is None


def test_read_trailing_closes_orders_and_passes_params() -> None:
    items = [
        _item(1, "AAPL", "2026-07-06", 12.0),
        _item(1, "AAPL", "2026-07-03", 11.0),
    ]
    client = _client(lambda p: {"items": items})
    result = client.read_trailing_closes(
        [1], as_of=date(2026, 7, 6), adjustment_type="unadjusted", max_lookback=30
    )
    assert result[1].ticker == "AAPL"
    assert [c.bar_date for c in result[1].closes] == [date(2026, 7, 6), date(2026, 7, 3)]
    assert [c.close for c in result[1].closes] == [Decimal("12.0"), Decimal("11.0")]
    assert client._session.calls[0] == {
        "adjustment_type": "unadjusted",
        "symbol_id": 1,
        "to_date": "2026-07-06",
        "limit": 31,
    }


def test_read_trailing_closes_short_circuits_on_empty_ids() -> None:
    client = _client(lambda p: {"items": []})
    assert client.read_trailing_closes([], date(2026, 7, 6), "unadjusted") == {}
    assert client._session.calls == []


def test_read_daily_snapshots_returns_symbol_map() -> None:
    client = _client(
        lambda p: {"items": [_item(1, "AAPL", "2026-07-06", 12.0, high=13.0, low=11.0)]}
    )
    result = client.read_daily_snapshots([1], date(2026, 7, 6), "unadjusted")
    assert result[1] == DailyBarSnapshot(
        symbol_id=1,
        ticker="AAPL",
        bar_date=date(2026, 7, 6),
        close=Decimal("12.0"),
        high=Decimal("13.0"),
        low=Decimal("11.0"),
    )
    assert client._session.calls[0] == {
        "adjustment_type": "unadjusted",
        "symbol_id": 1,
        "from_date": "2026-07-06",
        "to_date": "2026-07-06",
        "limit": 1,
    }


def test_read_daily_snapshots_short_circuits_on_empty_ids() -> None:
    client = _client(lambda p: {"items": []})
    assert client.read_daily_snapshots([], date(2026, 7, 6), "unadjusted") == {}
    assert client._session.calls == []


def test_resolve_symbols_active_default() -> None:
    rows = [{"id": 1, "canonical_ticker": "AAPL"}, {"id": 2, "canonical_ticker": "MSFT"}]
    conn = _FakeConn(rows)
    refs = resolve_symbols(conn)
    assert refs == [SymbolRef(1, "AAPL"), SymbolRef(2, "MSFT")]
    assert "active = true" in conn.calls[0][0].lower()


def test_resolve_symbols_by_ticker_filter() -> None:
    rows = [{"id": 1, "canonical_ticker": "AAPL"}]
    conn = _FakeConn(rows)
    refs = resolve_symbols(conn, tickers=["AAPL"])
    assert refs == [SymbolRef(1, "AAPL")]
    sql, params = conn.calls[0]
    assert "canonical_ticker = any(:tickers)" in sql.lower()
    assert params == {"tickers": ["AAPL"]}


def test_trading_dates_collects_distinct_sorted_dates() -> None:
    items = [
        _item(1, "AAPL", "2026-07-06", 12.0),
        _item(2, "MSFT", "2026-07-06", 50.0),
        _item(1, "AAPL", "2026-07-01", 10.0),
        _item(1, "AAPL", "2026-07-02", 11.0),
    ]
    client = _client(lambda p: {"items": items})
    result = client.trading_dates("unadjusted", date(2026, 7, 1), date(2026, 7, 6))
    assert result == [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 6)]
    assert client._session.calls[0]["from_date"] == "2026-07-01"
    assert client._session.calls[0]["to_date"] == "2026-07-06"
