"""Tests for the run orchestration (injected fake reader/store; no DB)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from quant_momentum.bars import BarClose, SymbolCloses, SymbolRef
from quant_momentum.config import Settings
from quant_momentum.runner import _parse_as_of, _parse_tickers, backfill, run_momentum

_AS_OF = date(2026, 7, 6)


def _closes(symbol_id: int, ticker: str, values: list[int | float]) -> SymbolCloses:
    return SymbolCloses(
        symbol_id=symbol_id,
        ticker=ticker,
        closes=tuple(BarClose(_AS_OF, Decimal(str(v))) for v in values),
    )


class FakeReader:
    def __init__(self, latest, symbols, closes_by_id, dates=None):
        self._latest = latest
        self._symbols = symbols
        self._closes = closes_by_id
        self._dates = dates or []
        self.read_args = None
        self.tickers_arg = "unset"

    def latest_bar_date(self, adjustment_type):
        return self._latest

    def resolve_symbols(self, tickers=None):
        self.tickers_arg = tickers
        return self._symbols

    def trading_dates(self, adjustment_type, from_date, to_date):
        return self._dates

    def read_trailing_closes(self, symbol_ids, as_of, adjustment_type, max_lookback=30):
        self.read_args = (list(symbol_ids), as_of, adjustment_type)
        return self._closes


class FakeStore:
    def __init__(self, fail_symbol_ids=()):
        self.created = None
        self.upserts = []
        self.finalized = None
        self.submissions = []
        self._fail = set(fail_symbol_ids)
        self._next_id = 101

    def create_run(self, **kwargs):
        self.created = kwargs
        run_id = self._next_id
        self._next_id += 1
        return run_id

    def upsert_daily_momentum(self, row):
        if row.symbol_id in self._fail:
            raise RuntimeError("boom")
        self.upserts.append(row)

    def record_submission(self, **kwargs):
        self.submissions.append(kwargs)

    def finalize_run(self, run_id, **kwargs):
        self.finalized = (run_id, kwargs)


class FakeSubmitter:
    def __init__(self, status="accepted"):
        self.calls = []
        self._status = status

    def submit(self, payload):
        from quant_momentum.signals import SubmitOutcome

        self.calls.append(payload)
        return SubmitOutcome(self._status, "cache-1", 200, None)


def _standard_fixture(fail_symbol_ids=()):
    symbols = [SymbolRef(1, "AAPL"), SymbolRef(2, "MSFT"), SymbolRef(3, "TSLA")]
    closes = {
        1: _closes(1, "AAPL", [100] * 31),  # flat -> all momentum 0 -> flagged (ALL, thr 0)
        2: _closes(2, "MSFT", [100 + i for i in range(31)]),  # recent lowest -> negative -> not flagged
        3: _closes(3, "TSLA", [100, 101, 102]),  # short history -> skipped
    }
    reader = FakeReader(_AS_OF, symbols, closes)
    store = FakeStore(fail_symbol_ids)
    return reader, store


def test_run_populates_and_finalizes() -> None:
    reader, store = _standard_fixture()
    summary = run_momentum(reader=reader, store=store, settings=Settings())

    assert summary.status == "completed"
    assert summary.symbols_requested == 3
    assert summary.symbols_computed == 2
    assert summary.symbols_skipped == 1
    assert summary.symbols_failed == 0
    assert summary.momentum_flagged == 1
    assert store.created is not None
    assert len(store.upserts) == 2
    assert store.finalized[0] == summary.run_id
    assert store.finalized[1]["status"] == "completed"
    assert store.finalized[1]["momentum_flagged"] == 1


def test_as_of_defaults_to_latest_bar_date() -> None:
    reader, store = _standard_fixture()
    summary = run_momentum(reader=reader, store=store, settings=Settings())
    assert summary.as_of == _AS_OF
    assert reader.read_args[1] == _AS_OF


def test_ordering_guard_skips_when_bars_missing() -> None:
    symbols = [SymbolRef(1, "AAPL")]
    reader = FakeReader(date(2026, 7, 1), symbols, {})
    store = FakeStore()
    summary = run_momentum(
        reader=reader, store=store, settings=Settings(), as_of=date(2026, 7, 6)
    )
    assert summary.status == "skipped"
    assert store.created is None
    assert store.upserts == []


def test_per_symbol_error_isolation() -> None:
    reader, store = _standard_fixture(fail_symbol_ids={2})
    summary = run_momentum(reader=reader, store=store, settings=Settings())
    assert summary.status == "completed"
    assert summary.symbols_computed == 1
    assert summary.symbols_failed == 1
    assert summary.symbols_skipped == 1
    assert summary.momentum_flagged == 1
    assert len(store.upserts) == 1
    assert store.finalized[1]["symbols_failed"] == 1


def test_dry_run_does_not_write() -> None:
    reader, store = _standard_fixture()
    summary = run_momentum(reader=reader, store=store, settings=Settings(), dry_run=True)
    assert summary.status == "completed"
    assert summary.symbols_computed == 2
    assert summary.run_id is None
    assert store.created is None
    assert store.upserts == []
    assert store.finalized is None


def test_no_bars_at_all_skips() -> None:
    reader = FakeReader(None, [], {})
    store = FakeStore()
    summary = run_momentum(reader=reader, store=store, settings=Settings())
    assert summary.status == "skipped"
    assert summary.as_of is None


def test_parse_helpers() -> None:
    assert _parse_as_of(None) is None
    assert _parse_as_of("2026-07-06") == date(2026, 7, 6)
    assert _parse_tickers("aapl, msft ,tsla") == ["AAPL", "MSFT", "TSLA"]
    assert _parse_tickers(None) is None


def test_submission_only_flagged_with_counters_and_audit() -> None:
    reader, store = _standard_fixture()
    submitter = FakeSubmitter("accepted")
    summary = run_momentum(
        reader=reader, store=store, settings=Settings(), submit=True, submitter=submitter
    )
    assert summary.momentum_flagged == 1
    assert summary.signals_submitted == 1
    assert summary.signals_accepted == 1
    assert len(submitter.calls) == 1  # only the flagged ticker
    assert submitter.calls[0]["ticker"] == "AAPL"
    assert len(store.submissions) == 1
    assert store.submissions[0]["status"] == "accepted"
    assert store.finalized[1]["signals_accepted"] == 1


def test_no_submission_when_disabled() -> None:
    reader, store = _standard_fixture()
    submitter = FakeSubmitter()
    summary = run_momentum(
        reader=reader, store=store, settings=Settings(), submit=False, submitter=submitter
    )
    assert summary.signals_submitted == 0
    assert submitter.calls == []
    assert store.submissions == []


def test_no_submission_in_dry_run() -> None:
    reader, store = _standard_fixture()
    submitter = FakeSubmitter()
    run_momentum(
        reader=reader,
        store=store,
        settings=Settings(),
        submit=True,
        submitter=submitter,
        dry_run=True,
    )
    assert submitter.calls == []


def test_backfill_iterates_trading_dates_idempotently() -> None:
    reader, store = _standard_fixture()
    reader._dates = [date(2026, 7, 4), date(2026, 7, 5), date(2026, 7, 6)]
    summary = backfill(
        reader=reader,
        store=store,
        settings=Settings(),
        from_date=date(2026, 7, 4),
        to_date=date(2026, 7, 6),
    )
    assert summary.dates_processed == 3
    assert summary.symbols_computed == 6  # 2 computable symbols x 3 dates
    assert summary.momentum_flagged == 3  # AAPL flagged each date
    assert len(store.upserts) == 6
    assert store.submissions == []  # backfill never submits
