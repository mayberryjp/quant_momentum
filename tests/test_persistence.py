"""Tests for persistence mapping and SQL (no live DB)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from quant_momentum.momentum import compute_momentum
from quant_momentum.persistence import (
    _UPSERT_DAILY_PRICE_CHANGE_SQL,
    _UPSERT_DAILY_MOMENTUM_SQL,
    DailyMomentumRow,
)

_THRESHOLDS = {5: 0, 15: 0, 30: 0}
_NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def _series() -> list[Decimal]:
    closes = [Decimal("100")] * 31
    closes[0] = Decimal("110")
    closes[5] = Decimal("100")
    closes[15] = Decimal("105")
    closes[30] = Decimal("100")
    return closes


def test_from_result_maps_all_columns() -> None:
    result = compute_momentum(_series(), thresholds=_THRESHOLDS, rule="ALL")
    row = DailyMomentumRow.from_result(
        result,
        symbol_id=7,
        ticker="AAPL",
        bar_date=date(2026, 7, 6),
        adjustment_type="unadjusted",
        run_id=5,
        computed_at=_NOW,
    )
    assert row.symbol_id == 7
    assert row.ticker == "AAPL"
    assert row.close == Decimal("110")
    assert row.close_5d_ago == Decimal("100")
    assert row.close_15d_ago == Decimal("105")
    assert row.close_30d_ago == Decimal("100")
    assert row.momentum_5d == Decimal("10.000000")
    assert row.momentum_15d == Decimal("4.761905")
    assert row.momentum_30d == Decimal("10.000000")
    assert row.is_momentum_5d is True
    assert row.is_momentum is True
    assert row.threshold_5d == Decimal("0")
    assert row.bars_available == 31
    assert row.run_id == 5
    assert row.computed_at == _NOW
    # rolling stats populated for a full 31-close window
    assert row.avg_daily_change_30d is not None
    assert row.floor_price_30d == Decimal("100")
    assert row.ceiling_price_30d == Decimal("110")


def test_from_result_insufficient_history_nulls() -> None:
    result = compute_momentum([Decimal("100")], thresholds=_THRESHOLDS, rule="ALL")
    row = DailyMomentumRow.from_result(
        result,
        symbol_id=1,
        ticker="AAA",
        bar_date=date(2026, 7, 6),
        adjustment_type="unadjusted",
        run_id=None,
        computed_at=_NOW,
    )
    assert row.momentum_5d is None
    assert row.close_5d_ago is None
    assert row.is_momentum is False
    assert row.avg_daily_change_30d is None
    assert row.floor_price_30d is None
    assert row.bars_available == 1
    assert row.run_id is None


def test_upsert_sql_is_idempotent() -> None:
    sql = str(_UPSERT_DAILY_MOMENTUM_SQL).lower()
    assert "on conflict (symbol_id, bar_date, adjustment_type) do update" in sql
    assert "updated_at = now()" in sql


def test_daily_price_change_upsert_sql_is_idempotent() -> None:
    sql = str(_UPSERT_DAILY_PRICE_CHANGE_SQL).lower()
    assert "on conflict (symbol_id, bar_date, adjustment_type) do update" in sql
    assert "updated_at = now()" in sql
