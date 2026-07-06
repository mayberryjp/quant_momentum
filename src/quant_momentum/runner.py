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
from datetime import UTC, date, datetime

from quant_momentum.config import Settings, get_settings
from quant_momentum.momentum import compute_momentum
from quant_momentum.persistence import DailyMomentumRow

log = logging.getLogger("quant_momentum.runner")

# Shortest lookback needs lookback + 1 closes; below this we cannot compute any
# momentum interval, so the symbol is skipped.
_MIN_CLOSES_FOR_ANY_MOMENTUM = 6


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
        for symbol in symbols:
            symbol_closes = closes_by_id.get(symbol.symbol_id)
            if symbol_closes is None or symbol_closes.bars_available < _MIN_CLOSES_FOR_ANY_MOMENTUM:
                summary.symbols_skipped += 1
                continue
            try:
                result = compute_momentum(
                    [bar.close for bar in symbol_closes.closes],
                    thresholds=thresholds,
                    rule=resolved_rule,
                )
                if not dry_run:
                    store.upsert_daily_momentum(
                        DailyMomentumRow.from_result(
                            result,
                            symbol_id=symbol.symbol_id,
                            ticker=symbol_closes.ticker or symbol.ticker,
                            bar_date=resolved_as_of,
                            adjustment_type=adjustment,
                            run_id=summary.run_id,
                            computed_at=datetime.now(UTC),
                        )
                    )
                summary.symbols_computed += 1
                if result.is_momentum:
                    summary.momentum_flagged += 1
            except Exception:  # per-symbol isolation
                summary.symbols_failed += 1
                log.exception("Momentum computation failed for symbol_id=%s", symbol.symbol_id)
        summary.status = "completed"
    except Exception as exc:  # fatal run-level failure
        summary.status = "failed"
        summary.error_message = str(exc)
        log.exception("Momentum run failed")
    finally:
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


def _parse_as_of(raw: str | None) -> date | None:
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _parse_tickers(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


def run_momentum_with_engine(engine, settings: Settings, **kwargs) -> RunSummary:
    """Build the engine-backed reader/store and execute a run."""
    from quant_momentum.bars import BarsReader
    from quant_momentum.persistence import MomentumStore

    return run_momentum(
        reader=BarsReader(engine),
        store=MomentumStore(engine),
        settings=settings,
        **kwargs,
    )


def run_command(args) -> int:
    """CLI handler for ``momentum run`` (one-shot or ``--schedule``)."""
    from quant_momentum.db import get_engine

    settings = get_settings()
    engine = get_engine()

    def _once() -> RunSummary:
        return run_momentum_with_engine(
            engine,
            settings,
            as_of=_parse_as_of(args.as_of),
            tickers=_parse_tickers(args.tickers),
            adjustment_type=args.adjustment_type,
            rule=args.rule,
            dry_run=args.dry_run,
        )

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
    if summary.status == "completed":
        return 0
    if summary.status == "skipped":
        return 2
    return 1
