"""Daily momentum run orchestration (spec §7) and the ``momentum run`` CLI glue.

:func:`run_momentum` is dependency-injected (a ``reader`` and a ``store``) so the
pipeline logic — as-of resolution, ordering guard, per-symbol error isolation,
counters, run finalization — is unit-testable without a database. Watchlist
submission is wired in Slice 5; here it is always off.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as dtime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from quant_momentum.config import Settings, get_settings, parse_run_at
from quant_momentum.lock import run_lock
from quant_momentum.momentum import compute_momentum, pct_change
from quant_momentum.persistence import DailyMomentumRow, DailyPriceChangeRow
from quant_momentum.signals import SubmitOutcome, build_payload

log = logging.getLogger("quant_momentum.runner")

# Shortest lookback needs lookback + 1 closes; below this we cannot compute any
# momentum interval, so the symbol is skipped.
_MIN_CLOSES_FOR_ANY_MOMENTUM = 6

# Wake up periodically rather than sleeping straight through to the target so a
# host clock adjustment (NTP step, DST transition) is picked up mid-wait.
_SCHEDULE_POLL_SECONDS = 60


def next_run_at(run_at: dtime, tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """Return the next occurrence of wall-clock ``run_at`` in ``tz`` strictly after ``now``."""
    current = (now or datetime.now(tz)).astimezone(tz)
    candidate = datetime.combine(current.date(), run_at, tzinfo=tz)
    if candidate <= current:
        candidate = datetime.combine(current.date() + timedelta(days=1), run_at, tzinfo=tz)
    return candidate


@dataclass
class RunSummary:
    run_id: int | None
    as_of: date | None
    adjustment_type: str
    momentum_rule: str
    status: str
    symbols_requested: int = 0
    symbols_computed: int = 0
    symbols_skipped: int = 0
    symbols_failed: int = 0
    momentum_flagged: int = 0
    signals_submitted: int = 0
    signals_accepted: int = 0
    signals_duplicate: int = 0
    signals_unresolved: int = 0
    signals_failed: int = 0
    error_message: str | None = None
    duration_seconds: float | None = None


def _build_daily_price_change_row(
    *,
    symbol_id: int,
    ticker: str,
    bar_date: date,
    adjustment_type: str,
    close: Decimal,
    prev_close: Decimal,
    high: Decimal,
    low: Decimal,
    run_id: int | None,
    computed_at: datetime,
) -> DailyPriceChangeRow | None:
    if low <= 0:
        return None

    close_change_amount = close - prev_close
    intraday_change_amount = high - low
    return DailyPriceChangeRow(
        symbol_id=symbol_id,
        ticker=ticker,
        bar_date=bar_date,
        adjustment_type=adjustment_type,
        close=close,
        prev_close=prev_close,
        close_change_amount=close_change_amount,
        close_change_percent=pct_change(close, prev_close),
        high=high,
        low=low,
        intraday_change_amount=intraday_change_amount,
        intraday_change_percent=pct_change(high, low),
        run_id=run_id,
        computed_at=computed_at,
    )


def run_momentum(
    *,
    reader,
    store,
    settings: Settings,
    as_of: date | None = None,
    tickers: list[str] | None = None,
    adjustment_type: str | None = None,
    rule: str | None = None,
    dry_run: bool = False,
    submit: bool = False,
    submitter=None,
) -> RunSummary:
    """Execute one momentum computation run and return a :class:`RunSummary`."""
    adjustment = adjustment_type or settings.momentum_adjustment_type
    resolved_rule = (rule or settings.momentum_rule).upper()
    thresholds = settings.thresholds

    latest = reader.latest_bar_date(adjustment)
    resolved_as_of = as_of or latest

    if resolved_as_of is None:
        log.warning("No bars available for adjustment_type=%s; nothing to do.", adjustment)
        return RunSummary(None, None, adjustment, resolved_rule, "skipped", error_message="no bars available")

    # Ordering guard: run only once the day's bars are present.
    if latest is None or latest < resolved_as_of:
        message = f"bars for {resolved_as_of} not yet available (latest={latest})"
        log.warning(message)
        return RunSummary(None, resolved_as_of, adjustment, resolved_rule, "skipped", error_message=message)

    started = time.perf_counter()
    symbols = reader.resolve_symbols(tickers)
    summary = RunSummary(
        run_id=None,
        as_of=resolved_as_of,
        adjustment_type=adjustment,
        momentum_rule=resolved_rule,
        status="running",
        symbols_requested=len(symbols),
    )

    if not dry_run:
        summary.run_id = store.create_run(
            run_date=date.today(),
            as_of_bar_date=resolved_as_of,
            adjustment_type=adjustment,
            momentum_rule=resolved_rule,
            thresholds=thresholds,
        )

    try:
        closes_by_id = reader.read_trailing_closes(
            [s.symbol_id for s in symbols], resolved_as_of, adjustment
        )
        daily_snapshots = reader.read_daily_snapshots(
            [s.symbol_id for s in symbols], resolved_as_of, adjustment
        )
        flagged: list[tuple[int, str, object]] = []
        for symbol in symbols:
            symbol_closes = closes_by_id.get(symbol.symbol_id)

            if symbol_closes is not None and symbol_closes.bars_available >= 2:
                snapshot = daily_snapshots.get(symbol.symbol_id)
                if snapshot is not None and not dry_run:
                    try:
                        row = _build_daily_price_change_row(
                            symbol_id=symbol.symbol_id,
                            ticker=snapshot.ticker or symbol.ticker,
                            bar_date=resolved_as_of,
                            adjustment_type=adjustment,
                            close=snapshot.close,
                            prev_close=symbol_closes.closes[1].close,
                            high=snapshot.high,
                            low=snapshot.low,
                            run_id=summary.run_id,
                            computed_at=datetime.now(UTC),
                        )
                        if row is not None:
                            store.upsert_daily_price_change(row)
                    except Exception:
                        log.exception("Daily price-change upsert failed for symbol_id=%s", symbol.symbol_id)

            if symbol_closes is None or symbol_closes.bars_available < _MIN_CLOSES_FOR_ANY_MOMENTUM:
                summary.symbols_skipped += 1
                continue
            try:
                result = compute_momentum(
                    [bar.close for bar in symbol_closes.closes],
                    thresholds=thresholds,
                    rule=resolved_rule,
                    segment_thresholds=settings.segment_thresholds,
                )
                ticker = symbol_closes.ticker or symbol.ticker
                if not dry_run:
                    store.upsert_daily_momentum(
                        DailyMomentumRow.from_result(
                            result,
                            symbol_id=symbol.symbol_id,
                            ticker=ticker,
                            bar_date=resolved_as_of,
                            adjustment_type=adjustment,
                            run_id=summary.run_id,
                            computed_at=datetime.now(UTC),
                        )
                    )
                summary.symbols_computed += 1
                if result.is_momentum:
                    summary.momentum_flagged += 1
                    flagged.append((symbol.symbol_id, ticker, result))
            except Exception:  # per-symbol isolation
                summary.symbols_failed += 1
                log.exception("Momentum computation failed for symbol_id=%s", symbol.symbol_id)

        if submit and submitter is not None and not dry_run and flagged:
            _submit_flagged(
                flagged,
                submitter=submitter,
                store=store,
                settings=settings,
                bar_date=resolved_as_of,
                adjustment_type=adjustment,
                summary=summary,
            )
        summary.status = "completed"
    except Exception as exc:  # fatal run-level failure
        summary.status = "failed"
        summary.error_message = str(exc)
        log.exception("Momentum run failed")
    finally:
        if not dry_run:
            try:
                cutoff = resolved_as_of - timedelta(days=settings.daily_change_retention_days - 1)
                store.prune_daily_price_changes(cutoff)
            except Exception:
                log.exception("Daily price-change retention prune failed")

        summary.duration_seconds = round(time.perf_counter() - started, 3)
        if summary.run_id is not None and not dry_run:
            store.finalize_run(
                summary.run_id,
                status=summary.status,
                symbols_requested=summary.symbols_requested,
                symbols_computed=summary.symbols_computed,
                symbols_skipped=summary.symbols_skipped,
                symbols_failed=summary.symbols_failed,
                momentum_flagged=summary.momentum_flagged,
                signals_submitted=summary.signals_submitted,
                signals_accepted=summary.signals_accepted,
                signals_duplicate=summary.signals_duplicate,
                signals_unresolved=summary.signals_unresolved,
                signals_failed=summary.signals_failed,
                error_message=summary.error_message,
                duration_seconds=summary.duration_seconds,
            )

    log.info(
        "Momentum run %s: as_of=%s status=%s requested=%d computed=%d "
        "skipped=%d failed=%d flagged=%d duration=%.3fs",
        summary.run_id,
        summary.as_of,
        summary.status,
        summary.symbols_requested,
        summary.symbols_computed,
        summary.symbols_skipped,
        summary.symbols_failed,
        summary.momentum_flagged,
        summary.duration_seconds or 0.0,
    )
    return summary


def _submit_flagged(
    flagged,
    *,
    submitter,
    store,
    settings: Settings,
    bar_date: date,
    adjustment_type: str,
    summary: RunSummary,
) -> None:
    """Submit flagged tickers to quant_signals and record each attempt."""
    for symbol_id, ticker, result in flagged:
        payload = build_payload(
            result,
            ticker=ticker,
            bar_date=bar_date,
            adjustment_type=adjustment_type,
            settings=settings,
        )
        summary.signals_submitted += 1
        try:
            outcome = submitter.submit(payload)
        except Exception as exc:  # never let submission fail the run
            outcome = SubmitOutcome("failed", None, None, str(exc))
            log.exception("Signal submission raised for %s", ticker)

        if outcome.status == "accepted":
            summary.signals_accepted += 1
        elif outcome.status == "duplicate":
            summary.signals_duplicate += 1
        elif outcome.status == "unresolved":
            summary.signals_unresolved += 1
        else:
            summary.signals_failed += 1

        try:
            store.record_submission(
                run_id=summary.run_id,
                symbol_id=symbol_id,
                ticker=ticker,
                bar_date=bar_date,
                idempotency_key=payload["idempotency_key"],
                source=payload["source"],
                direction=payload.get("direction"),
                score=payload.get("score"),
                status=outcome.status,
                signal_cache_id=outcome.signal_cache_id,
                http_status=outcome.http_status,
                error=outcome.error,
            )
        except Exception:  # audit failure is non-fatal
            log.exception("Failed to record submission audit for %s", ticker)


@dataclass
class BackfillSummary:
    from_date: date
    to_date: date
    adjustment_type: str
    dates_processed: int = 0
    symbols_computed: int = 0
    symbols_failed: int = 0
    momentum_flagged: int = 0


def backfill(
    *,
    reader,
    store,
    settings: Settings,
    from_date: date,
    to_date: date,
    tickers: list[str] | None = None,
    adjustment_type: str | None = None,
    rule: str | None = None,
) -> BackfillSummary:
    """Compute momentum for every trading date in ``[from_date, to_date]``.

    Historical / regime research only — never submits to the watchlist. Each
    date is upserted idempotently, so re-running overwrites cleanly.
    """
    adjustment = adjustment_type or settings.momentum_adjustment_type
    dates = reader.trading_dates(adjustment, from_date, to_date)
    summary = BackfillSummary(from_date=from_date, to_date=to_date, adjustment_type=adjustment)

    for as_of in dates:
        result = run_momentum(
            reader=reader,
            store=store,
            settings=settings,
            as_of=as_of,
            tickers=tickers,
            adjustment_type=adjustment,
            rule=rule,
            submit=False,
        )
        if result.status == "completed":
            summary.dates_processed += 1
            summary.symbols_computed += result.symbols_computed
            summary.symbols_failed += result.symbols_failed
            summary.momentum_flagged += result.momentum_flagged

    log.info(
        "Backfill %s..%s (%s): dates=%d computed=%d failed=%d flagged=%d",
        from_date,
        to_date,
        adjustment,
        summary.dates_processed,
        summary.symbols_computed,
        summary.symbols_failed,
        summary.momentum_flagged,
    )
    return summary


def _parse_as_of(raw: str | None) -> date | None:
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _parse_tickers(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


def run_momentum_with_engine(engine, settings: Settings, *, submit: bool = False, **kwargs) -> RunSummary:
    """Build the engine-backed reader/store (and signals client) and execute a run."""
    from quant_momentum.bars import BarsReader
    from quant_momentum.persistence import MomentumStore

    submitter = None
    if submit:
        from quant_momentum.signals import SignalsClient

        submitter = SignalsClient(
            settings.quant_signals_base_url,
            timeout=settings.quant_signals_timeout_seconds,
            retry_count=settings.quant_signals_retry_count,
            backoff_seconds=settings.quant_signals_backoff_seconds,
        )

    return run_momentum(
        reader=BarsReader(engine),
        store=MomentumStore(engine),
        settings=settings,
        submit=submit,
        submitter=submitter,
        **kwargs,
    )


def run_command(args) -> int:
    """CLI handler for ``momentum run`` (one-shot or ``--schedule``)."""
    from quant_momentum.db import get_engine

    settings = get_settings()
    engine = get_engine()
    submit = settings.momentum_submit_enabled and not args.no_submit

    def _once() -> RunSummary | None:
        with run_lock(redis_url=settings.quant_redis_url) as acquired:
            if not acquired:
                log.warning("Skipping run: another instance holds the run lock.")
                return None
            return run_momentum_with_engine(
                engine,
                settings,
                submit=submit,
                as_of=_parse_as_of(args.as_of),
                tickers=_parse_tickers(args.tickers),
                adjustment_type=args.adjustment_type,
                rule=args.rule,
                dry_run=args.dry_run,
            )

    raw_at = getattr(args, "at", None) or settings.momentum_run_at
    if raw_at:
        try:
            run_at = parse_run_at(raw_at)
            tz = ZoneInfo(getattr(args, "timezone", None) or settings.momentum_timezone)
        except Exception as exc:
            log.error("Invalid daily schedule (check MOMENTUM_RUN_AT/MOMENTUM_TIMEZONE): %s", exc)
            return 2

        log.info("Starting daily momentum runs at %s %s.", run_at.isoformat(), tz.key)
        try:
            while True:
                target = next_run_at(run_at, tz)
                log.info("Next momentum run scheduled for %s.", target.isoformat())
                while True:
                    remaining = (target - datetime.now(tz)).total_seconds()
                    if remaining <= 0:
                        break
                    time.sleep(min(remaining, _SCHEDULE_POLL_SECONDS))
                _once()
        except KeyboardInterrupt:  # pragma: no cover - operator interrupt
            log.info("Scheduled runs interrupted; exiting.")
            return 0

    if args.schedule is not None and args.schedule <= 0:
        log.error(
            "Invalid --schedule=%d: interval must be a positive number of seconds "
            "(check MOMENTUM_INTERVAL).",
            args.schedule,
        )
        return 2

    if args.schedule:
        log.info("Starting scheduled momentum runs every %d seconds.", args.schedule)
        try:
            while True:
                _once()
                time.sleep(args.schedule)
        except KeyboardInterrupt:  # pragma: no cover - operator interrupt
            log.info("Scheduled runs interrupted; exiting.")
            return 0

    summary = _once()
    if summary is None:
        return 0
    if summary.status == "completed":
        return 0
    if summary.status == "skipped":
        return 2
    return 1


def run_summary_command(args) -> int:
    """CLI handler for ``run-summary`` — print the latest run (or recent runs)."""
    import json

    from quant_momentum.api.queries import RunListParams, get_latest_run, list_runs
    from quant_momentum.db import get_engine

    engine = get_engine()
    if getattr(args, "latest", False):
        run = get_latest_run(engine)
        if run is None:
            log.info("No runs found.")
            return 0
        print(json.dumps(run, indent=2, default=str))
        return 0

    result = list_runs(engine, RunListParams(limit=10))
    print(json.dumps(result, indent=2, default=str))
    return 0


def backfill_with_engine(engine, settings: Settings, **kwargs) -> BackfillSummary:
    """Build the engine-backed reader/store and run a historical backfill."""
    from quant_momentum.bars import BarsReader
    from quant_momentum.persistence import MomentumStore

    return backfill(
        reader=BarsReader(engine),
        store=MomentumStore(engine),
        settings=settings,
        **kwargs,
    )


def backfill_command(args) -> int:
    """CLI handler for ``momentum backfill``."""
    from quant_momentum.db import get_engine

    from_date = _parse_as_of(args.from_date)
    to_date = _parse_as_of(args.to_date)
    if from_date is None or to_date is None or from_date > to_date:
        log.error("backfill requires --from-date <= --to-date (YYYY-MM-DD)")
        return 2

    settings = get_settings()
    backfill_with_engine(
        get_engine(),
        settings,
        from_date=from_date,
        to_date=to_date,
        tickers=_parse_tickers(args.tickers),
        adjustment_type=args.adjustment_type,
    )
    return 0
