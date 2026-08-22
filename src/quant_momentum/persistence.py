"""Persistence for the ``momentum`` schema: run tracking + idempotent upserts.

``DailyMomentumRow.from_result`` maps a pure :class:`MomentumResult` onto the
``daily_momentum`` columns (pure, unit-testable). :class:`MomentumStore` is
engine-backed and runs each write in its own short transaction so a single
symbol's failure is isolated (spec §7 per-symbol error isolation).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine

from quant_momentum.momentum import MomentumResult


@dataclass(frozen=True)
class DailyMomentumRow:
    symbol_id: int
    ticker: str
    bar_date: date
    adjustment_type: str
    close: Decimal
    close_5d_ago: Decimal | None
    close_15d_ago: Decimal | None
    close_30d_ago: Decimal | None
    momentum_5d: Decimal | None
    momentum_15d: Decimal | None
    momentum_30d: Decimal | None
    momentum_5_15d: Decimal | None
    momentum_15_30d: Decimal | None
    is_momentum_5d: bool
    is_momentum_15d: bool
    is_momentum_30d: bool
    is_momentum_5_15d: bool
    is_momentum_15_30d: bool
    is_momentum: bool
    momentum_rule: str
    threshold_5d: Decimal
    threshold_15d: Decimal
    threshold_30d: Decimal
    threshold_5_15d: Decimal
    threshold_15_30d: Decimal
    avg_daily_change_30d: Decimal | None
    median_daily_change_30d: Decimal | None
    min_daily_change_30d: Decimal | None
    max_daily_change_30d: Decimal | None
    floor_price_30d: Decimal | None
    ceiling_price_30d: Decimal | None
    bars_available: int
    run_id: int | None
    computed_at: datetime

    @classmethod
    def from_result(
        cls,
        result: MomentumResult,
        *,
        symbol_id: int,
        ticker: str,
        bar_date: date,
        adjustment_type: str,
        run_id: int | None,
        computed_at: datetime,
    ) -> "DailyMomentumRow":
        stats = result.stats

        def threshold(lookback: int) -> Decimal:
            interval = result.intervals.get(lookback)
            return interval.threshold if interval else Decimal("0")

        def segment_threshold(near: int, far: int) -> Decimal:
            segment = result.segments.get((near, far))
            return segment.threshold if segment else Decimal("0")

        return cls(
            symbol_id=symbol_id,
            ticker=ticker,
            bar_date=bar_date,
            adjustment_type=adjustment_type,
            close=result.close,
            close_5d_ago=result.reference_close(5),
            close_15d_ago=result.reference_close(15),
            close_30d_ago=result.reference_close(30),
            momentum_5d=result.momentum(5),
            momentum_15d=result.momentum(15),
            momentum_30d=result.momentum(30),
            momentum_5_15d=result.segment_momentum(5, 15),
            momentum_15_30d=result.segment_momentum(15, 30),
            is_momentum_5d=result.flag(5),
            is_momentum_15d=result.flag(15),
            is_momentum_30d=result.flag(30),
            is_momentum_5_15d=result.segment_flag(5, 15),
            is_momentum_15_30d=result.segment_flag(15, 30),
            is_momentum=result.is_momentum,
            momentum_rule=result.momentum_rule,
            threshold_5d=threshold(5),
            threshold_15d=threshold(15),
            threshold_30d=threshold(30),
            threshold_5_15d=segment_threshold(5, 15),
            threshold_15_30d=segment_threshold(15, 30),
            avg_daily_change_30d=stats.avg_daily_change,
            median_daily_change_30d=stats.median_daily_change,
            min_daily_change_30d=stats.min_daily_change,
            max_daily_change_30d=stats.max_daily_change,
            floor_price_30d=stats.floor_price,
            ceiling_price_30d=stats.ceiling_price,
            bars_available=result.bars_available,
            run_id=run_id,
            computed_at=computed_at,
        )


@dataclass(frozen=True)
class DailyPriceChangeRow:
    symbol_id: int
    ticker: str
    bar_date: date
    adjustment_type: str
    close: Decimal
    prev_close: Decimal
    close_change_amount: Decimal
    close_change_percent: Decimal | None
    high: Decimal
    low: Decimal
    intraday_change_amount: Decimal
    intraday_change_percent: Decimal | None
    run_id: int | None
    computed_at: datetime


_INSERT_RUN_SQL = text(
    """
    INSERT INTO momentum.momentum_runs
        (run_date, as_of_bar_date, adjustment_type, momentum_rule,
         threshold_5d, threshold_15d, threshold_30d, status, started_at)
    VALUES
        (:run_date, :as_of_bar_date, :adjustment_type, :momentum_rule,
         :threshold_5d, :threshold_15d, :threshold_30d, 'running', now())
    RETURNING id
    """
)

_INSERT_SUBMISSION_SQL = text(
    """
    INSERT INTO momentum.signal_submissions
        (run_id, symbol_id, ticker, bar_date, idempotency_key, source,
         submission_count, direction, score, status, signal_cache_id, http_status, error)
    VALUES
        (:run_id, :symbol_id, :ticker, :bar_date, :idempotency_key, :source,
         1, :direction, :score, :status, :signal_cache_id, :http_status, :error)
    ON CONFLICT (ticker, bar_date) DO UPDATE SET
         run_id = EXCLUDED.run_id,
         symbol_id = EXCLUDED.symbol_id,
         idempotency_key = EXCLUDED.idempotency_key,
         source = CASE
             WHEN EXCLUDED.source = ANY(regexp_split_to_array(momentum.signal_submissions.source, '\\s*,\\s*'))
                 THEN momentum.signal_submissions.source
             ELSE momentum.signal_submissions.source || ',' || EXCLUDED.source
         END,
         submission_count = momentum.signal_submissions.submission_count + 1,
         direction = EXCLUDED.direction,
         score = EXCLUDED.score,
         status = EXCLUDED.status,
         signal_cache_id = EXCLUDED.signal_cache_id,
         http_status = EXCLUDED.http_status,
         error = EXCLUDED.error,
         submitted_at = now()
    """
)

_FINALIZE_RUN_SQL = text(
    """
    UPDATE momentum.momentum_runs
       SET status = :status,
           symbols_requested = :symbols_requested,
           symbols_computed = :symbols_computed,
           symbols_skipped = :symbols_skipped,
           symbols_failed = :symbols_failed,
           momentum_flagged = :momentum_flagged,
           signals_submitted = :signals_submitted,
           signals_accepted = :signals_accepted,
           signals_duplicate = :signals_duplicate,
           signals_unresolved = :signals_unresolved,
           signals_failed = :signals_failed,
           error_message = :error_message,
           duration_seconds = :duration_seconds,
           finished_at = now()
     WHERE id = :run_id
    """
)

_UPSERT_DAILY_PRICE_CHANGE_SQL = text(
    """
    INSERT INTO momentum.daily_price_changes
        (symbol_id, ticker, bar_date, adjustment_type,
         close, prev_close, close_change_amount, close_change_percent,
         high, low, intraday_change_amount, intraday_change_percent,
         run_id, computed_at)
    VALUES
        (:symbol_id, :ticker, :bar_date, :adjustment_type,
         :close, :prev_close, :close_change_amount, :close_change_percent,
         :high, :low, :intraday_change_amount, :intraday_change_percent,
         :run_id, :computed_at)
    ON CONFLICT (symbol_id, bar_date, adjustment_type) DO UPDATE SET
         ticker = EXCLUDED.ticker,
         close = EXCLUDED.close,
         prev_close = EXCLUDED.prev_close,
         close_change_amount = EXCLUDED.close_change_amount,
         close_change_percent = EXCLUDED.close_change_percent,
         high = EXCLUDED.high,
         low = EXCLUDED.low,
         intraday_change_amount = EXCLUDED.intraday_change_amount,
         intraday_change_percent = EXCLUDED.intraday_change_percent,
         run_id = EXCLUDED.run_id,
         computed_at = EXCLUDED.computed_at,
         updated_at = now()
    """
)

_PRUNE_DAILY_PRICE_CHANGES_SQL = text(
    "DELETE FROM momentum.daily_price_changes WHERE bar_date < :cutoff_date"
)

_UPSERT_DAILY_MOMENTUM_SQL = text(
    """
    INSERT INTO momentum.daily_momentum
        (symbol_id, ticker, bar_date, adjustment_type, close,
         close_5d_ago, close_15d_ago, close_30d_ago,
         momentum_5d, momentum_15d, momentum_30d,
         momentum_5_15d, momentum_15_30d,
         is_momentum_5d, is_momentum_15d, is_momentum_30d,
         is_momentum_5_15d, is_momentum_15_30d, is_momentum,
         momentum_rule, threshold_5d, threshold_15d, threshold_30d,
         threshold_5_15d, threshold_15_30d,
         avg_daily_change_30d, median_daily_change_30d,
         min_daily_change_30d, max_daily_change_30d,
         floor_price_30d, ceiling_price_30d,
         bars_available, run_id, computed_at)
    VALUES
        (:symbol_id, :ticker, :bar_date, :adjustment_type, :close,
         :close_5d_ago, :close_15d_ago, :close_30d_ago,
         :momentum_5d, :momentum_15d, :momentum_30d,
         :momentum_5_15d, :momentum_15_30d,
         :is_momentum_5d, :is_momentum_15d, :is_momentum_30d,
         :is_momentum_5_15d, :is_momentum_15_30d, :is_momentum,
         :momentum_rule, :threshold_5d, :threshold_15d, :threshold_30d,
         :threshold_5_15d, :threshold_15_30d,
         :avg_daily_change_30d, :median_daily_change_30d,
         :min_daily_change_30d, :max_daily_change_30d,
         :floor_price_30d, :ceiling_price_30d,
         :bars_available, :run_id, :computed_at)
    ON CONFLICT (symbol_id, bar_date, adjustment_type) DO UPDATE SET
         ticker = EXCLUDED.ticker,
         close = EXCLUDED.close,
         close_5d_ago = EXCLUDED.close_5d_ago,
         close_15d_ago = EXCLUDED.close_15d_ago,
         close_30d_ago = EXCLUDED.close_30d_ago,
         momentum_5d = EXCLUDED.momentum_5d,
         momentum_15d = EXCLUDED.momentum_15d,
         momentum_30d = EXCLUDED.momentum_30d,
         momentum_5_15d = EXCLUDED.momentum_5_15d,
         momentum_15_30d = EXCLUDED.momentum_15_30d,
         is_momentum_5d = EXCLUDED.is_momentum_5d,
         is_momentum_15d = EXCLUDED.is_momentum_15d,
         is_momentum_30d = EXCLUDED.is_momentum_30d,
         is_momentum_5_15d = EXCLUDED.is_momentum_5_15d,
         is_momentum_15_30d = EXCLUDED.is_momentum_15_30d,
         is_momentum = EXCLUDED.is_momentum,
         momentum_rule = EXCLUDED.momentum_rule,
         threshold_5d = EXCLUDED.threshold_5d,
         threshold_15d = EXCLUDED.threshold_15d,
         threshold_30d = EXCLUDED.threshold_30d,
         threshold_5_15d = EXCLUDED.threshold_5_15d,
         threshold_15_30d = EXCLUDED.threshold_15_30d,
         avg_daily_change_30d = EXCLUDED.avg_daily_change_30d,
         median_daily_change_30d = EXCLUDED.median_daily_change_30d,
         min_daily_change_30d = EXCLUDED.min_daily_change_30d,
         max_daily_change_30d = EXCLUDED.max_daily_change_30d,
         floor_price_30d = EXCLUDED.floor_price_30d,
         ceiling_price_30d = EXCLUDED.ceiling_price_30d,
         bars_available = EXCLUDED.bars_available,
         run_id = EXCLUDED.run_id,
         computed_at = EXCLUDED.computed_at,
         updated_at = now()
    """
)


class MomentumStore:
    """Engine-backed writer for the ``momentum`` schema."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def create_run(
        self,
        *,
        run_date: date,
        as_of_bar_date: date,
        adjustment_type: str,
        momentum_rule: str,
        thresholds: dict[int, float | Decimal],
    ) -> int:
        params = {
            "run_date": run_date,
            "as_of_bar_date": as_of_bar_date,
            "adjustment_type": adjustment_type,
            "momentum_rule": momentum_rule,
            "threshold_5d": thresholds.get(5, 0),
            "threshold_15d": thresholds.get(15, 0),
            "threshold_30d": thresholds.get(30, 0),
        }
        with self._engine.begin() as conn:
            return int(conn.execute(_INSERT_RUN_SQL, params).scalar_one())

    def upsert_daily_momentum(self, row: DailyMomentumRow) -> None:
        with self._engine.begin() as conn:
            conn.execute(_UPSERT_DAILY_MOMENTUM_SQL, asdict(row))

    def upsert_daily_price_change(self, row: DailyPriceChangeRow) -> None:
        with self._engine.begin() as conn:
            conn.execute(_UPSERT_DAILY_PRICE_CHANGE_SQL, asdict(row))

    def prune_daily_price_changes(self, cutoff_date: date) -> None:
        with self._engine.begin() as conn:
            conn.execute(_PRUNE_DAILY_PRICE_CHANGES_SQL, {"cutoff_date": cutoff_date})

    def record_submission(
        self,
        *,
        run_id: int | None,
        symbol_id: int | None,
        ticker: str,
        bar_date: date,
        idempotency_key: str,
        source: str,
        direction: str | None,
        score: float | Decimal | None,
        status: str,
        signal_cache_id: str | None,
        http_status: int | None,
        error: str | None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                _INSERT_SUBMISSION_SQL,
                {
                    "run_id": run_id,
                    "symbol_id": symbol_id,
                    "ticker": ticker,
                    "bar_date": bar_date,
                    "idempotency_key": idempotency_key,
                    "source": source,
                    "direction": direction,
                    "score": score,
                    "status": status,
                    "signal_cache_id": signal_cache_id,
                    "http_status": http_status,
                    "error": error,
                },
            )

    def finalize_run(
        self,
        run_id: int,
        *,
        status: str,
        symbols_requested: int = 0,
        symbols_computed: int = 0,
        symbols_skipped: int = 0,
        symbols_failed: int = 0,
        momentum_flagged: int = 0,
        signals_submitted: int = 0,
        signals_accepted: int = 0,
        signals_duplicate: int = 0,
        signals_unresolved: int = 0,
        signals_failed: int = 0,
        error_message: str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                _FINALIZE_RUN_SQL,
                {
                    "run_id": run_id,
                    "status": status,
                    "symbols_requested": symbols_requested,
                    "symbols_computed": symbols_computed,
                    "symbols_skipped": symbols_skipped,
                    "symbols_failed": symbols_failed,
                    "momentum_flagged": momentum_flagged,
                    "signals_submitted": signals_submitted,
                    "signals_accepted": signals_accepted,
                    "signals_duplicate": signals_duplicate,
                    "signals_unresolved": signals_unresolved,
                    "signals_failed": signals_failed,
                    "error_message": error_message,
                    "duration_seconds": duration_seconds,
                },
            )
