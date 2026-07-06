"""Pure momentum computation engine (spec §4 + issue follow-up).

No I/O: given a symbol's ordered closes (most-recent-first, ``closes[0]`` is the
as-of close, ``closes[n]`` is ``n`` trading days earlier), compute:

* N-day close-to-close momentum in **percentage points** for each lookback,
* a per-interval binary flag ``M_N >= threshold_N``,
* a combined flag governed by ``ALL`` / ``ANY`` / ``MAJORITY``,
* rolling 30-day daily-change statistics (avg / median / min / max) and the
  floor / ceiling close over the trailing 30 trading-day window.

Insufficient history for an interval yields ``None`` (stored as NULL) and a
``False`` flag. The rolling 30-day statistics require a full window of 31
closes (30 consecutive daily changes); otherwise they are ``None``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from statistics import mean, median

MOMENTUM_RULES = ("ALL", "ANY", "MAJORITY")
DEFAULT_LOOKBACKS = (5, 15, 30)

# 31 closes -> 30 consecutive daily changes over the trailing 30 trading days.
STAT_WINDOW_CLOSES = 31

_QUANT = Decimal("0.000001")  # NUMERIC(18, 6)


def _q(value: Decimal) -> Decimal:
    """Quantize to 6 decimal places (percentage-point storage precision)."""
    return value.quantize(_QUANT, rounding=ROUND_HALF_UP)


def pct_change(newer: Decimal, older: Decimal) -> Decimal | None:
    """Percentage-point change from ``older`` to ``newer`` (``None`` if older<=0)."""
    if older is None or newer is None or older <= 0:
        return None
    return _q((newer / older - 1) * 100)


def daily_pct_changes(closes: Sequence[Decimal]) -> list[Decimal]:
    """Consecutive daily % changes for ``closes`` (most-recent-first).

    ``changes[k]`` is the change realized moving from ``closes[k+1]`` (older) to
    ``closes[k]`` (newer). Pairs with a non-positive denominator are skipped.
    """
    changes: list[Decimal] = []
    for i in range(1, len(closes)):
        change = pct_change(closes[i - 1], closes[i])
        if change is not None:
            changes.append(change)
    return changes


def summarize_changes(changes: Sequence[Decimal]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return ``(avg, median, min, max)`` of ``changes`` (quantized)."""
    return (_q(mean(changes)), _q(median(changes)), min(changes), max(changes))


@dataclass(frozen=True)
class RollingStats:
    """Rolling 30-day daily-change statistics and price band."""

    avg_daily_change: Decimal | None
    median_daily_change: Decimal | None
    min_daily_change: Decimal | None
    max_daily_change: Decimal | None
    floor_price: Decimal | None
    ceiling_price: Decimal | None

    @classmethod
    def empty(cls) -> "RollingStats":
        return cls(None, None, None, None, None, None)


def rolling_30d_stats(closes: Sequence[Decimal]) -> RollingStats:
    """Compute rolling 30-day stats over the trailing 31-close window.

    Returns :meth:`RollingStats.empty` when fewer than 31 closes are available
    or any close in the window is non-positive.
    """
    window = list(closes[:STAT_WINDOW_CLOSES])
    if len(window) < STAT_WINDOW_CLOSES or min(window) <= 0:
        return RollingStats.empty()
    changes = daily_pct_changes(window)
    avg, med, lo, hi = summarize_changes(changes)
    return RollingStats(
        avg_daily_change=avg,
        median_daily_change=med,
        min_daily_change=lo,
        max_daily_change=hi,
        floor_price=min(window),
        ceiling_price=max(window),
    )


@dataclass(frozen=True)
class IntervalResult:
    lookback: int
    reference_close: Decimal | None
    momentum: Decimal | None
    is_momentum: bool
    threshold: Decimal


@dataclass(frozen=True)
class MomentumResult:
    close: Decimal
    bars_available: int
    momentum_rule: str
    intervals: dict[int, IntervalResult]
    is_momentum: bool
    stats: RollingStats

    def momentum(self, lookback: int) -> Decimal | None:
        interval = self.intervals.get(lookback)
        return interval.momentum if interval else None

    def reference_close(self, lookback: int) -> Decimal | None:
        interval = self.intervals.get(lookback)
        return interval.reference_close if interval else None

    def flag(self, lookback: int) -> bool:
        interval = self.intervals.get(lookback)
        return interval.is_momentum if interval else False

    def mean_momentum(self) -> Decimal | None:
        """Mean of the available interval momenta (used for scoring)."""
        values = [i.momentum for i in self.intervals.values() if i.momentum is not None]
        if not values:
            return None
        return _q(mean(values))


def _combine(flags: Sequence[bool], rule: str) -> bool:
    if rule == "ALL":
        return bool(flags) and all(flags)
    if rule == "ANY":
        return any(flags)
    if rule == "MAJORITY":
        return sum(1 for flag in flags if flag) * 2 > len(flags)
    raise ValueError(f"unknown momentum rule: {rule!r}")


def compute_momentum(
    closes: Sequence[Decimal],
    *,
    thresholds: Mapping[int, float | Decimal | str],
    rule: str,
    lookbacks: Sequence[int] = DEFAULT_LOOKBACKS,
) -> MomentumResult:
    """Compute momentum, per-interval flags, combined flag, and rolling stats."""
    rule = rule.upper()
    if rule not in MOMENTUM_RULES:
        raise ValueError(f"unknown momentum rule: {rule!r}")
    if not closes:
        raise ValueError("closes must be non-empty")

    close = closes[0]
    bars_available = len(closes)

    intervals: dict[int, IntervalResult] = {}
    for lookback in lookbacks:
        threshold = Decimal(str(thresholds.get(lookback, 0)))
        if bars_available >= lookback + 1:
            reference = closes[lookback]
            value = pct_change(close, reference)
        else:
            reference = None
            value = None
        flag = value is not None and value >= threshold
        intervals[lookback] = IntervalResult(
            lookback=lookback,
            reference_close=reference if value is not None else None,
            momentum=value,
            is_momentum=flag,
            threshold=threshold,
        )

    combined = _combine([intervals[n].is_momentum for n in lookbacks], rule)
    return MomentumResult(
        close=close,
        bars_available=bars_available,
        momentum_rule=rule,
        intervals=intervals,
        is_momentum=combined,
        stats=rolling_30d_stats(closes),
    )
