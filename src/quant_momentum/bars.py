"""Upstream data access: active symbols and trailing closes (spec §7).

Active symbols are fetched from the ``quant_symbols`` service over its
``GET /symbols`` HTTP API, and daily bars from the ``quant_daily_bars`` service
over its ``GET /bars`` API, rather than by reading either service's tables
directly. The pure row-shaping logic (:func:`build_trailing_closes`) is
separated from execution so it can be unit-tested without a live network.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import requests

from quant_momentum.config import Settings

# Longest lookback we need history for; also covers the rolling 30-day stats,
# which require 31 closes (30 consecutive daily changes).
DEFAULT_MAX_LOOKBACK = 30


@dataclass(frozen=True)
class SymbolRef:
    """An active symbol resolved from the ``quant_symbols`` API."""

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


@dataclass(frozen=True)
class DailyBarSnapshot:
    """Single-day OHLC snapshot for one symbol."""

    symbol_id: int
    ticker: str
    bar_date: date
    close: Decimal
    high: Decimal
    low: Decimal


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


class BarsApiClient:
    """HTTP client for the ``quant_daily_bars`` read API (spec §7).

    ``quant_daily_bars`` owns the ``daily_bars`` schema, so all bar data is
    fetched over ``GET /bars`` instead of by reading its table directly. Requests
    use a timeout and exponential-backoff retry on 5xx / network errors; the
    session and sleep function are injectable for network-free tests.
    """

    # Page size for bulk ``GET /bars`` scans (mirrors the symbols API cap).
    _PAGE_SIZE = 500

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float,
        retry_count: int,
        backoff_seconds: float,
        session: requests.Session | None = None,
        sleep=time.sleep,
    ):
        self._url = base_url.rstrip("/") + "/bars"
        self._timeout = timeout
        self._retries = retry_count
        self._backoff = backoff_seconds
        self._session = session or requests.Session()
        self._sleep = sleep

    @classmethod
    def from_settings(cls, settings: Settings) -> "BarsApiClient":
        return cls(
            settings.quant_daily_bars_base_url,
            timeout=settings.quant_daily_bars_timeout_seconds,
            retry_count=settings.quant_daily_bars_retry_count,
            backoff_seconds=settings.quant_daily_bars_backoff_seconds,
        )

    def _get_bars(self, params: dict) -> list[dict]:
        """Return the ``items`` list from ``GET /bars`` for ``params``."""
        last_error: str | None = None
        for attempt in range(self._retries + 1):
            try:
                response = self._session.get(self._url, params=params, timeout=self._timeout)
            except requests.RequestException as exc:
                last_error = str(exc)
            else:
                if response.status_code < 500:
                    if response.status_code >= 400:
                        raise RuntimeError(
                            f"quant_daily_bars GET /bars failed: http {response.status_code}"
                        )
                    return (response.json() or {}).get("items", [])
                last_error = f"server error {response.status_code}"

            if attempt < self._retries:
                self._sleep(self._backoff * (2 ** attempt))

        raise RuntimeError(f"quant_daily_bars GET /bars failed: {last_error}")

    def _iter_bars(self, params: dict) -> Iterator[dict]:
        """Yield every ``GET /bars`` item across all pages for ``params``."""
        offset = 0
        while True:
            page = self._get_bars({**params, "limit": self._PAGE_SIZE, "offset": offset})
            yield from page
            if len(page) < self._PAGE_SIZE:
                break
            offset += self._PAGE_SIZE

    def latest_bar_date(self, adjustment_type: str) -> date | None:
        """Return ``MAX(bar_date)`` for the adjustment series, or ``None``."""
        items = self._get_bars({"adjustment_type": adjustment_type, "limit": 1})
        if not items:
            return None
        return date.fromisoformat(items[0]["bar_date"])

    def trading_dates(
        self, adjustment_type: str, from_date: date, to_date: date
    ) -> list[date]:
        """Distinct bar dates present in the range (the effective trading calendar)."""
        seen: set[date] = set()
        offset = 0
        page = 500
        while True:
            items = self._get_bars(
                {
                    "adjustment_type": adjustment_type,
                    "from_date": from_date.isoformat(),
                    "to_date": to_date.isoformat(),
                    "limit": page,
                    "offset": offset,
                }
            )
            for item in items:
                seen.add(date.fromisoformat(item["bar_date"]))
            if len(items) < page:
                break
            offset += page
        return sorted(seen)

    def read_trailing_closes(
        self,
        symbol_ids: Sequence[int],
        as_of: date,
        adjustment_type: str,
        max_lookback: int = DEFAULT_MAX_LOOKBACK,
    ) -> dict[int, SymbolCloses]:
        """Fetch up to ``max_lookback + 1`` trailing closes per symbol (most-recent-first).

        One paged scan over ``GET /bars`` for the whole universe across a trailing
        date window, instead of a request per symbol. The window is padded for
        weekends/holidays so it comfortably spans ``max_lookback + 1`` trading days.
        """
        wanted = set(symbol_ids)
        if not wanted:
            return {}
        need = max_lookback + 1
        from_date = as_of - timedelta(days=need * 7 // 5 + 10)
        grouped: dict[int, list[dict]] = {}
        for item in self._iter_bars(
            {
                "adjustment_type": adjustment_type,
                "from_date": from_date.isoformat(),
                "to_date": as_of.isoformat(),
            }
        ):
            symbol_id = item["symbol_id"]
            if symbol_id in wanted:
                grouped.setdefault(symbol_id, []).append(item)

        rows: list[dict] = []
        for symbol_id, items in grouped.items():
            # Newest-first, then keep only the most recent ``need`` closes.
            items.sort(key=lambda r: r["bar_date"], reverse=True)
            for rank, item in enumerate(items[:need], start=1):
                rows.append(
                    {
                        "symbol_id": symbol_id,
                        "ticker": item["ticker"],
                        "bar_date": date.fromisoformat(item["bar_date"]),
                        "close": item["close"],
                        "rn": rank,
                    }
                )
        return build_trailing_closes(rows)

    def read_daily_snapshots(
        self,
        symbol_ids: Sequence[int],
        as_of: date,
        adjustment_type: str,
    ) -> dict[int, DailyBarSnapshot]:
        """Fetch the single ``as_of`` OHLC snapshot per symbol in one paged scan."""
        wanted = set(symbol_ids)
        if not wanted:
            return {}
        snapshots: dict[int, DailyBarSnapshot] = {}
        for item in self._iter_bars(
            {
                "adjustment_type": adjustment_type,
                "from_date": as_of.isoformat(),
                "to_date": as_of.isoformat(),
            }
        ):
            symbol_id = item["symbol_id"]
            if symbol_id not in wanted or symbol_id in snapshots:
                continue
            snapshots[symbol_id] = DailyBarSnapshot(
                symbol_id=symbol_id,
                ticker=item["ticker"],
                bar_date=date.fromisoformat(item["bar_date"]),
                close=Decimal(str(item["close"])),
                high=Decimal(str(item["high"])),
                low=Decimal(str(item["low"])),
            )
        return snapshots


class SymbolsApiClient:
    """HTTP client for the ``quant_symbols`` read API (spec §7).

    ``quant_symbols`` owns the ``symbol_master`` schema, so active symbols are
    resolved over ``GET /symbols`` instead of by reading its table directly.
    Requests use a timeout and exponential-backoff retry on 5xx / network
    errors; the session and sleep function are injectable for network-free tests.
    """

    # ``GET /symbols`` caps the page size at 500.
    _PAGE_SIZE = 500

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float,
        retry_count: int,
        backoff_seconds: float,
        session: requests.Session | None = None,
        sleep=time.sleep,
    ):
        self._url = base_url.rstrip("/") + "/symbols"
        self._timeout = timeout
        self._retries = retry_count
        self._backoff = backoff_seconds
        self._session = session or requests.Session()
        self._sleep = sleep

    @classmethod
    def from_settings(cls, settings: Settings) -> "SymbolsApiClient":
        return cls(
            settings.quant_symbols_base_url,
            timeout=settings.quant_symbols_timeout_seconds,
            retry_count=settings.quant_symbols_retry_count,
            backoff_seconds=settings.quant_symbols_backoff_seconds,
        )

    def _get_symbols(self, params: dict) -> list[dict]:
        """Return the ``items`` list from ``GET /symbols`` for ``params``."""
        last_error: str | None = None
        for attempt in range(self._retries + 1):
            try:
                response = self._session.get(self._url, params=params, timeout=self._timeout)
            except requests.RequestException as exc:
                last_error = str(exc)
            else:
                if response.status_code < 500:
                    if response.status_code >= 400:
                        raise RuntimeError(
                            f"quant_symbols GET /symbols failed: http {response.status_code}"
                        )
                    return (response.json() or {}).get("items", [])
                last_error = f"server error {response.status_code}"

            if attempt < self._retries:
                self._sleep(self._backoff * (2 ** attempt))

        raise RuntimeError(f"quant_symbols GET /symbols failed: {last_error}")

    def _iter_symbols(self, params: dict) -> Iterator[dict]:
        """Yield every item across all pages for ``params``."""
        offset = 0
        while True:
            page = self._get_symbols({**params, "limit": self._PAGE_SIZE, "offset": offset})
            yield from page
            if len(page) < self._PAGE_SIZE:
                break
            offset += self._PAGE_SIZE

    def resolve(self, tickers: Sequence[str] | None = None) -> list[SymbolRef]:
        """Resolve target symbols, ordered by ``symbol_id``.

        With ``tickers`` given, resolves exactly those canonical tickers;
        otherwise returns all active symbols.
        """
        if tickers:
            by_id: dict[int, SymbolRef] = {}
            for ticker in tickers:
                wanted = ticker.upper()
                for item in self._iter_symbols({"q": ticker}):
                    if item["canonical_ticker"].upper() == wanted:
                        by_id[item["id"]] = SymbolRef(
                            symbol_id=item["id"], ticker=item["canonical_ticker"]
                        )
            resolved: Iterable[SymbolRef] = by_id.values()
        else:
            resolved = [
                SymbolRef(symbol_id=item["id"], ticker=item["canonical_ticker"])
                for item in self._iter_symbols({"active": "true"})
            ]
        return sorted(resolved, key=lambda ref: ref.symbol_id)


class BarsReader:
    """Reader facade over the ``quant_symbols`` and ``quant_daily_bars`` APIs.

    Symbol resolution is delegated to :class:`SymbolsApiClient` and all
    ``daily_bars`` access to :class:`BarsApiClient`; neither reads another
    service's tables directly.
    """

    def __init__(self, api: BarsApiClient, symbols_api: SymbolsApiClient):
        self._api = api
        self._symbols = symbols_api

    def latest_bar_date(self, adjustment_type: str) -> date | None:
        return self._api.latest_bar_date(adjustment_type)

    def resolve_symbols(self, tickers: Sequence[str] | None = None) -> list[SymbolRef]:
        return self._symbols.resolve(tickers)

    def trading_dates(self, adjustment_type: str, from_date: date, to_date: date) -> list[date]:
        return self._api.trading_dates(adjustment_type, from_date, to_date)

    def read_trailing_closes(
        self,
        symbol_ids: Sequence[int],
        as_of: date,
        adjustment_type: str,
        max_lookback: int = DEFAULT_MAX_LOOKBACK,
    ) -> dict[int, SymbolCloses]:
        return self._api.read_trailing_closes(symbol_ids, as_of, adjustment_type, max_lookback)

    def read_daily_snapshots(
        self,
        symbol_ids: Sequence[int],
        as_of: date,
        adjustment_type: str,
    ) -> dict[int, DailyBarSnapshot]:
        return self._api.read_daily_snapshots(symbol_ids, as_of, adjustment_type)
