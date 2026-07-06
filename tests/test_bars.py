"""Tests for the upstream bars reader (no live DB; fake connection)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from quant_momentum.bars import (
    SymbolRef,
    build_trailing_closes,
    latest_bar_date,
    read_trailing_closes,
    resolve_symbols,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return list(self._rows)

    def scalar(self):
        return self._rows


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


def test_read_trailing_closes_passes_expected_params() -> None:
    rows = [_row(1, "AAPL", date(2026, 7, 6), Decimal("12"), 1)]
    conn = _FakeConn(rows)
    result = read_trailing_closes(
        conn, symbol_ids=[1, 2, 3], as_of=date(2026, 7, 6), adjustment_type="unadjusted", max_lookback=30
    )
    assert result[1].ticker == "AAPL"
    sql, params = conn.calls[0]
    assert params == {
        "adj": "unadjusted",
        "as_of": date(2026, 7, 6),
        "symbol_ids": [1, 2, 3],
        "max_rows": 31,
    }
    assert "row_number() over" in sql.lower()


def test_read_trailing_closes_short_circuits_on_empty_ids() -> None:
    conn = _FakeConn([])
    assert read_trailing_closes(conn, [], date(2026, 7, 6), "unadjusted") == {}
    assert conn.calls == []


def test_latest_bar_date_returns_scalar() -> None:
    conn = _FakeConn(date(2026, 7, 6))
    assert latest_bar_date(conn, "unadjusted") == date(2026, 7, 6)
    assert conn.calls[0][1] == {"adj": "unadjusted"}


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
