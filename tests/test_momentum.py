"""Thorough unit tests for the pure momentum engine (no DB)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from quant_momentum.momentum import (
    RollingStats,
    compute_momentum,
    daily_pct_changes,
    pct_change,
    rolling_30d_stats,
    summarize_changes,
)


def _series(**overrides: Decimal) -> list[Decimal]:
    """31 closes defaulting to 100; override specific offsets for momentum tests."""
    closes = [Decimal("100")] * 31
    for offset, value in overrides.items():
        closes[int(offset[1:])] = value  # keys like "i0", "i5"
    return closes


# --------------------------------------------------------------------------
# pct_change / daily changes / summary
# --------------------------------------------------------------------------
def test_pct_change_basic_and_guards() -> None:
    assert pct_change(Decimal("110"), Decimal("100")) == Decimal("10.000000")
    assert pct_change(Decimal("90"), Decimal("100")) == Decimal("-10.000000")
    assert pct_change(Decimal("100"), Decimal("100")) == Decimal("0.000000")
    assert pct_change(Decimal("100"), Decimal("0")) is None


def test_daily_pct_changes_most_recent_first() -> None:
    closes = [Decimal("104"), Decimal("100"), Decimal("100"), Decimal("125")]
    assert daily_pct_changes(closes) == [
        Decimal("4.000000"),
        Decimal("0.000000"),
        Decimal("-20.000000"),
    ]


def test_summarize_changes() -> None:
    avg, med, lo, hi = summarize_changes(
        [Decimal("4.000000"), Decimal("0.000000"), Decimal("-20.000000")]
    )
    assert avg == Decimal("-5.333333")
    assert med == Decimal("0.000000")
    assert lo == Decimal("-20.000000")
    assert hi == Decimal("4.000000")


# --------------------------------------------------------------------------
# rolling 30-day stats
# --------------------------------------------------------------------------
def test_rolling_stats_requires_full_window() -> None:
    assert rolling_30d_stats([Decimal("100")] * 30) == RollingStats.empty()


def test_rolling_stats_constant_one_percent_series() -> None:
    # Most-recent-first, prices rising exactly 1% per trading day.
    closes = [Decimal("100") * (Decimal("1.01") ** (30 - i)) for i in range(31)]
    stats = rolling_30d_stats(closes)
    assert stats.avg_daily_change == Decimal("1.000000")
    assert stats.median_daily_change == Decimal("1.000000")
    assert stats.min_daily_change == Decimal("1.000000")
    assert stats.max_daily_change == Decimal("1.000000")
    assert stats.floor_price == Decimal("100")  # oldest close (offset 30)
    assert stats.ceiling_price == closes[0]  # newest close


# --------------------------------------------------------------------------
# compute_momentum: values, thresholds, rules, history
# --------------------------------------------------------------------------
def _thresholds(t5=0, t15=0, t30=0) -> dict[int, float]:
    return {5: t5, 15: t15, 30: t30}


def _segment_thresholds(t515=0, t1530=0) -> dict[tuple[int, int], float]:
    return {(5, 15): t515, (15, 30): t1530}


def test_momentum_values_positive_negative_zero() -> None:
    closes = _series(i0=Decimal("110"), i5=Decimal("100"), i15=Decimal("121"), i30=Decimal("110"))
    result = compute_momentum(closes, thresholds=_thresholds(), rule="ANY")
    assert result.momentum(5) == Decimal("10.000000")
    assert result.momentum(15) == Decimal("-9.090909")
    assert result.momentum(30) == Decimal("0.000000")
    assert result.reference_close(5) == Decimal("100")
    assert result.bars_available == 31


def test_threshold_boundary_is_inclusive() -> None:
    closes = _series(i0=Decimal("110"), i5=Decimal("100"))
    # exactly at threshold -> flagged
    assert compute_momentum(closes, thresholds=_thresholds(t5=10), rule="ANY").flag(5) is True
    # just above threshold -> not flagged
    assert (
        compute_momentum(closes, thresholds={5: Decimal("10.000001"), 15: 0, 30: 0}, rule="ANY").flag(5)
        is False
    )


@pytest.mark.parametrize(
    "rule,expected",
    [("ALL", False), ("ANY", True), ("MAJORITY", True)],
)
def test_combined_rules(rule: str, expected: bool) -> None:
    # Combined flag is over {5d, 5-15d, 15-30d}: 5d True (+10%),
    # 5-15d False (100/121 -> -17%), 15-30d True (121/110 -> +10%).
    closes = _series(i0=Decimal("110"), i5=Decimal("100"), i15=Decimal("121"), i30=Decimal("110"))
    result = compute_momentum(closes, thresholds=_thresholds(), rule=rule)
    assert result.flag(5) is True
    assert result.segment_flag(5, 15) is False
    assert result.segment_flag(15, 30) is True
    assert result.is_momentum is expected


def test_insufficient_history_yields_nulls_and_false() -> None:
    result = compute_momentum([Decimal("110")], thresholds=_thresholds(), rule="ALL")
    assert result.momentum(5) is None
    assert result.momentum(15) is None
    assert result.momentum(30) is None
    assert result.flag(5) is False
    assert result.is_momentum is False
    assert result.stats == RollingStats.empty()
    assert result.mean_momentum() is None


def test_exactly_enough_for_5d_only() -> None:
    closes = [Decimal("110")] + [Decimal("100")] * 5  # length 6 -> offset 5 available
    result = compute_momentum(closes, thresholds=_thresholds(), rule="MAJORITY")
    assert result.momentum(5) == Decimal("10.000000")
    assert result.momentum(15) is None
    assert result.momentum(30) is None
    assert result.stats == RollingStats.empty()  # < 31 closes
    assert result.is_momentum is False  # only 1 of 3 flags


def test_mean_momentum_over_available_intervals() -> None:
    closes = _series(i0=Decimal("110"), i5=Decimal("100"), i15=Decimal("121"), i30=Decimal("110"))
    result = compute_momentum(closes, thresholds=_thresholds(), rule="ALL")
    assert result.mean_momentum() == Decimal("0.303030")


def test_unknown_rule_rejected() -> None:
    with pytest.raises(ValueError):
        compute_momentum([Decimal("100")] * 31, thresholds=_thresholds(), rule="SOMETIMES")


# --------------------------------------------------------------------------
# segment momentum: 5-15d and 15-30d isolate a past window
# --------------------------------------------------------------------------
def test_segment_momentum_isolates_past_window() -> None:
    # A large recent move (i0) does not touch the segment returns, which are
    # computed purely from the closes 5 / 15 / 30 days ago.
    closes = _series(i0=Decimal("500"), i5=Decimal("110"), i15=Decimal("100"), i30=Decimal("80"))
    result = compute_momentum(closes, thresholds=_thresholds(), rule="ANY")
    assert result.segment_momentum(5, 15) == Decimal("10.000000")   # 110/100 - 1
    assert result.segment_momentum(15, 30) == Decimal("25.000000")  # 100/80 - 1
    assert result.segment_flag(5, 15) is True
    assert result.segment_flag(15, 30) is True


def test_segment_history_requirements() -> None:
    # 16 closes: 5-15d is computable (needs offset 15); 15-30d is not (needs 30).
    closes = [Decimal("110")] + [Decimal("100")] * 15
    result = compute_momentum(closes, thresholds=_thresholds(), rule="ANY")
    assert result.segment_momentum(5, 15) == Decimal("0.000000")
    assert result.segment_flag(5, 15) is True
    assert result.segment_momentum(15, 30) is None
    assert result.segment_flag(15, 30) is False


def test_segment_threshold_boundary_is_inclusive() -> None:
    closes = _series(i5=Decimal("110"), i15=Decimal("100"))  # 5-15d = +10%
    at = compute_momentum(
        closes,
        thresholds=_thresholds(),
        rule="ANY",
        segment_thresholds=_segment_thresholds(t515=10),
    )
    assert at.segment_flag(5, 15) is True
    above = compute_momentum(
        closes,
        thresholds=_thresholds(),
        rule="ANY",
        segment_thresholds={(5, 15): Decimal("10.000001"), (15, 30): 0},
    )
    assert above.segment_flag(5, 15) is False


def test_combined_excludes_full_window_15_30d_flags() -> None:
    # 15d / 30d full-window momentum are positive, but the 5-15d segment is
    # negative, so under ALL the combined flag is driven False -- the old
    # {5, 15, 30} rule would instead have flagged it.
    closes = _series(i0=Decimal("110"), i5=Decimal("95"), i15=Decimal("100"), i30=Decimal("90"))
    result = compute_momentum(closes, thresholds=_thresholds(), rule="ALL")
    assert result.flag(15) is True   # 110/100 - 1 = +10%
    assert result.flag(30) is True   # 110/90 - 1 = +22%
    assert result.segment_flag(5, 15) is False  # 95/100 - 1 = -5%
    assert result.is_momentum is False
