"""quant_signals watchlist producer (spec §8).

Builds the ``POST /signals`` payload for a flagged momentum result and submits it
with timeout, exponential-backoff retry on 5xx / network errors, and response
classification into ``accepted`` / ``duplicate`` / ``unresolved`` / ``failed``.
The HTTP session and sleep function are injectable for DB/network-free tests.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import requests

from quant_momentum.config import Settings
from quant_momentum.momentum import MomentumResult

log = logging.getLogger("quant_momentum.signals")

_TERMINAL_STATUSES = ("accepted", "duplicate", "unresolved")


@dataclass(frozen=True)
class SubmitOutcome:
    status: str  # accepted | duplicate | unresolved | failed
    signal_cache_id: str | None
    http_status: int | None
    error: str | None


def _clamp_score(result: MomentumResult, scale: float) -> Decimal | None:
    mean_momentum = result.mean_momentum()
    if mean_momentum is None or scale == 0:
        return None
    raw = mean_momentum / Decimal(str(scale))
    return max(Decimal("0"), min(Decimal("1"), raw)).quantize(Decimal("0.000001"))


def _fmt_pct(value: Decimal | None) -> str:
    return f"{value:+.1f}%" if value is not None else "n/a"


def _num(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def build_payload(
    result: MomentumResult,
    *,
    ticker: str,
    bar_date: date,
    adjustment_type: str,
    settings: Settings,
) -> dict:
    """Build the ``POST /signals`` JSON body for a flagged momentum result."""
    ticker = ticker.upper()
    m5, m15, m30 = result.momentum(5), result.momentum(15), result.momentum(30)
    thresholds = result.intervals
    thr5 = _num(thresholds[5].threshold) if 5 in thresholds else 0
    thr15 = _num(thresholds[15].threshold) if 15 in thresholds else 0
    thr30 = _num(thresholds[30].threshold) if 30 in thresholds else 0
    score = _clamp_score(result, settings.momentum_score_scale)

    return {
        "source": settings.momentum_source,
        "idempotency_key": f"{settings.momentum_source}:{bar_date.isoformat()}:{ticker}",
        "ticker": ticker,
        "market": "stocks",
        "locale": "us",
        "signal_type": "watchlist_candidate",
        "direction": "long",
        "score": _num(score),
        "horizon": settings.momentum_horizon,
        "reason": (
            f"Momentum across 5/15/30d: {_fmt_pct(m5)}/{_fmt_pct(m15)}/{_fmt_pct(m30)} "
            f"(rule={result.momentum_rule}, thresholds {thr5}/{thr15}/{thr30})"
        ),
        "tags": ["momentum", "5d", "15d", "30d"],
        "metadata": {
            "strategy_version": settings.momentum_source,
            "adjustment_type": adjustment_type,
            "bar_date": bar_date.isoformat(),
            "close": _num(result.close),
            "momentum_5d": _num(m5),
            "momentum_15d": _num(m15),
            "momentum_30d": _num(m30),
            "is_momentum_5d": result.flag(5),
            "is_momentum_15d": result.flag(15),
            "is_momentum_30d": result.flag(30),
            "thresholds": {"5d": thr5, "15d": thr15, "30d": thr30},
            "rule": result.momentum_rule,
        },
    }


def _classify(response: requests.Response) -> SubmitOutcome:
    http_status = response.status_code
    try:
        body = response.json() or {}
    except ValueError:
        body = {}
    status = str(body.get("status", "")).lower()
    cache_id = body.get("signal_cache_id") or body.get("id")
    if status in _TERMINAL_STATUSES:
        return SubmitOutcome(status, cache_id, http_status, None)
    if 200 <= http_status < 300:
        return SubmitOutcome("accepted", cache_id, http_status, None)
    return SubmitOutcome("failed", cache_id, http_status, body.get("error") or f"http {http_status}")


class SignalsClient:
    """HTTP client for the quant_signals ``POST /signals`` endpoint."""

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
        self._url = base_url.rstrip("/") + "/signals"
        self._timeout = timeout
        self._retries = retry_count
        self._backoff = backoff_seconds
        self._session = session or requests.Session()
        self._sleep = sleep

    def submit(self, payload: dict) -> SubmitOutcome:
        last_error: str | None = None
        last_http: int | None = None
        for attempt in range(self._retries + 1):
            try:
                response = self._session.post(self._url, json=payload, timeout=self._timeout)
            except requests.RequestException as exc:
                last_error = str(exc)
                last_http = None
            else:
                if response.status_code < 500:
                    return _classify(response)
                last_error = f"server error {response.status_code}"
                last_http = response.status_code

            if attempt < self._retries:
                self._sleep(self._backoff * (2 ** attempt))

        return SubmitOutcome("failed", None, last_http, last_error)
