"""Pure momentum computation engine (spec §4 + issue follow-up).

No I/O: given a symbol's ordered closes (most-recent-first, ``closes[0]`` is the
as-of close, ``closes[n]`` is ``n`` trading days earlier), compute:

* N-day close-to-close momentum in **percentage points** for each lookback,
* segment (interval) momentum for the 5->15 and 15->30 day windows, which
  isolate a past window so recent short-term momentum cannot leak in,
* a per-interval binary flag ``M_N >= threshold_N`` (per lookback and segment),
* a combined flag over the 5-day lookback plus the two segments, governed by
  ``ALL`` / ``ANY`` / ``MAJORITY``,
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

# Segment (interval) momentum windows as (near, far) close offsets. Each isolates
# the return between two past closes so a recent move cannot leak in: 5->15 uses
# the closes 5 and 15 days ago; 15->30 uses the closes 15 and 30 days ago.
DEFAULT_SEGMENTS = ((5, 15), (15, 30))

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
class SegmentResult:
    """Momentum over a past window bounded by two close offsets.

    ``near`` is the more recent offset and ``far`` the older one; the momentum is
    the return from ``far_close`` to ``near_close`` so a move inside the most
    recent ``near`` days cannot influence it.
    """

    near: int
    far: int
    near_close: Decimal | None
    far_close: Decimal | None
    momentum: Decimal | None
    is_momentum: bool
    threshold: Decimal


@dataclass(frozen=True)
class MomentumResult:
    close: Decimal
    bars_available: int
    momentum_rule: str
    intervals: dict[int, IntervalResult]
    segments: dict[tuple[int, int], SegmentResult]
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

    def segment_momentum(self, near: int, far: int) -> Decimal | None:
        segment = self.segments.get((near, far))
        return segment.momentum if segment else None

    def segment_flag(self, near: int, far: int) -> bool:
        segment = self.segments.get((near, far))
        return segment.is_momentum if segment else False

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
    segments: Sequence[tuple[int, int]] = DEFAULT_SEGMENTS,
    segment_thresholds: Mapping[tuple[int, int], float | Decimal | str] | None = None,
) -> MomentumResult:
    """Compute momentum, per-interval/segment flags, combined flag, and rolling stats."""
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

    segment_thresholds = segment_thresholds or {}
    segment_results: dict[tuple[int, int], SegmentResult] = {}
    for near, far in segments:
        seg_threshold = Decimal(str(segment_thresholds.get((near, far), 0)))
        if bars_available >= far + 1:
            near_close = closes[near]
            far_close = closes[far]
            seg_value = pct_change(near_close, far_close)
        else:
            near_close = far_close = None
            seg_value = None
        seg_flag = seg_value is not None and seg_value >= seg_threshold
        segment_results[(near, far)] = SegmentResult(
            near=near,
            far=far,
            near_close=near_close if seg_value is not None else None,
            far_close=far_close if seg_value is not None else None,
            momentum=seg_value,
            is_momentum=seg_flag,
            threshold=seg_threshold,
        )

    # Combined indicator: the 5-day lookback plus the two segment flags. The
    # full-window 15d/30d flags are still computed and stored, but excluded here
    # so a strong recent move cannot single-handedly flag the longer windows.
    combined_flags = [intervals[n].is_momentum for n in lookbacks if n == 5]
    combined_flags += [segment_results[key].is_momentum for key in segments]
    combined = _combine(combined_flags, rule)
    return MomentumResult(
        close=close,
        bars_available=bars_available,
        momentum_rule=rule,
        intervals=intervals,
        segments=segment_results,
        is_momentum=combined,
        stats=rolling_30d_stats(closes),
    )
