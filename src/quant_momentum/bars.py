"""Upstream data access: active symbols and trailing closes (spec §7).

Reads only the shared ``symbol_master`` and ``market_data`` schemas (owned by
``quant_symbols`` / ``quant_daily_bars``). All SQL is parameterized. The pure
row-shaping logic (:func:`build_trailing_closes`) is separated from execution
so it can be unit-tested without a live database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import Integer, Text, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import Connection, Engine

# Longest lookback we need history for; also covers the rolling 30-day stats,
# which require 31 closes (30 consecutive daily changes).
DEFAULT_MAX_LOOKBACK = 30


@dataclass(frozen=True)
class SymbolRef:
    """An active symbol resolved from ``symbol_master.symbols``."""

    symbol_id: int
    ticker: str


@dataclass(frozen=True)
class BarClose:
    """A single trailing close for a symbol."""

    bar_date: date
    close: Decimal


@dataclass(frozen=True)
class SymbolCloses:
    """Ordered trailing closes for one symbol (most-recent-first).

    ``closes[0]`` is the as-of close, ``closes[n]`` is the close ``n`` trading
    days earlier.
    """

    symbol_id: int
    ticker: str
    closes: tuple[BarClose, ...]

    @property
    def bars_available(self) -> int:
        return len(self.closes)


_RESOLVE_ACTIVE_SQL = text(
    "SELECT id, canonical_ticker FROM symbol_master.symbols "
    "WHERE active = true ORDER BY id"
)

_RESOLVE_BY_TICKER_SQL = text(
    "SELECT id, canonical_ticker FROM symbol_master.symbols "
    "WHERE canonical_ticker = ANY(:tickers) ORDER BY id"
).bindparams(bindparam("tickers", type_=ARRAY(Text)))

_LATEST_BAR_DATE_SQL = text(
    "SELECT MAX(bar_date) FROM market_data.daily_bars WHERE adjustment_type = :adj"
)

_TRAILING_CLOSES_SQL = text(
    """
    SELECT symbol_id, ticker, bar_date, close, rn
    FROM (
        SELECT symbol_id, ticker, bar_date, close,
               ROW_NUMBER() OVER (PARTITION BY symbol_id ORDER BY bar_date DESC) AS rn
        FROM market_data.daily_bars
        WHERE adjustment_type = :adj
          AND bar_date <= :as_of
          AND symbol_id = ANY(:symbol_ids)
    ) ranked
    WHERE rn <= :max_rows
    ORDER BY symbol_id, rn
    """
).bindparams(bindparam("symbol_ids", type_=ARRAY(Integer)))


def resolve_symbols(conn: Connection, tickers: Sequence[str] | None = None) -> list[SymbolRef]:
    """Resolve target symbols.

    With ``tickers`` given, resolves exactly those canonical tickers; otherwise
    returns all active symbols.
    """
    if tickers:
        rows = conn.execute(_RESOLVE_BY_TICKER_SQL, {"tickers": list(tickers)}).mappings()
    else:
        rows = conn.execute(_RESOLVE_ACTIVE_SQL).mappings()
    return [SymbolRef(symbol_id=row["id"], ticker=row["canonical_ticker"]) for row in rows]


def latest_bar_date(conn: Connection, adjustment_type: str) -> date | None:
    """Return ``MAX(bar_date)`` for the adjustment series, or ``None``."""
    return conn.execute(_LATEST_BAR_DATE_SQL, {"adj": adjustment_type}).scalar()


def build_trailing_closes(rows: Iterable[Mapping]) -> dict[int, SymbolCloses]:
    """Group ranked bar rows into per-symbol, most-recent-first closes.

    Pure function: ``rows`` are mappings with ``symbol_id, ticker, bar_date,
    close, rn``. Symbols absent from ``rows`` are simply absent from the result;
    symbols with short history yield fewer closes.
    """
    grouped: dict[int, list[Mapping]] = {}
    for row in rows:
        grouped.setdefault(row["symbol_id"], []).append(row)

    result: dict[int, SymbolCloses] = {}
    for symbol_id, symbol_rows in grouped.items():
        ordered = sorted(symbol_rows, key=lambda r: r["rn"])
        closes = tuple(
            BarClose(bar_date=r["bar_date"], close=Decimal(str(r["close"]))) for r in ordered
        )
        result[symbol_id] = SymbolCloses(
            symbol_id=symbol_id,
            ticker=ordered[0]["ticker"],
            closes=closes,
        )
    return result


def read_trailing_closes(
    conn: Connection,
    symbol_ids: Sequence[int],
    as_of: date,
    adjustment_type: str,
    max_lookback: int = DEFAULT_MAX_LOOKBACK,
) -> dict[int, SymbolCloses]:
    """Bulk-read trailing closes for ``symbol_ids`` as of ``as_of``.

    Returns up to ``max_lookback + 1`` closes per symbol (most-recent-first).
    """
    if not symbol_ids:
        return {}
    rows = conn.execute(
        _TRAILING_CLOSES_SQL,
        {
            "adj": adjustment_type,
            "as_of": as_of,
            "symbol_ids": list(symbol_ids),
            "max_rows": max_lookback + 1,
        },
    ).mappings()
    return build_trailing_closes(rows)


class BarsReader:
    """Engine-backed facade over the reader functions (one connection per call)."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def latest_bar_date(self, adjustment_type: str) -> date | None:
        with self._engine.connect() as conn:
            return latest_bar_date(conn, adjustment_type)

    def resolve_symbols(self, tickers: Sequence[str] | None = None) -> list[SymbolRef]:
        with self._engine.connect() as conn:
            return resolve_symbols(conn, tickers)

    def read_trailing_closes(
        self,
        symbol_ids: Sequence[int],
        as_of: date,
        adjustment_type: str,
        max_lookback: int = DEFAULT_MAX_LOOKBACK,
    ) -> dict[int, SymbolCloses]:
        with self._engine.connect() as conn:
            return read_trailing_closes(conn, symbol_ids, as_of, adjustment_type, max_lookback)
