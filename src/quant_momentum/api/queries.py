"""Read-side database queries for the API (spec §9).

All SQL is parameterized; filter *clauses* are built from fixed string
fragments (never user input). Results are serialized to JSON-friendly types
(``Decimal`` -> float, dates -> ISO strings).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

VALID_RUN_STATUSES = frozenset(("running", "completed", "failed"))
VALID_ADJUSTMENT_TYPES = frozenset(("unadjusted", "split_adjusted"))


def _ser(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _row(mapping) -> dict[str, Any]:
    return {key: _ser(value) for key, value in mapping.items()}


@dataclass(frozen=True)
class MomentumListParams:
    ticker: str | None = None
    symbol_id: int | None = None
    from_date: str | None = None
    to_date: str | None = None
    is_momentum: bool | None = None
    adjustment_type: str | None = None
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True)
class RunListParams:
    status: str | None = None
    limit: int = 20
    offset: int = 0


@dataclass(frozen=True)
class DailyChangeListParams:
    ticker: str | None = None
    symbol_id: int | None = None
    from_date: str | None = None
    to_date: str | None = None
    adjustment_type: str | None = None
    limit: int = 100
    offset: int = 0


def list_momentum(engine: Engine, params: MomentumListParams) -> dict[str, Any]:
    clauses: list[str] = []
    values: dict[str, Any] = {}
    if params.ticker:
        clauses.append("ticker = :ticker")
        values["ticker"] = params.ticker.upper()
    if params.symbol_id is not None:
        clauses.append("symbol_id = :symbol_id")
        values["symbol_id"] = params.symbol_id
    if params.from_date:
        clauses.append("bar_date >= :from_date")
        values["from_date"] = params.from_date
    if params.to_date:
        clauses.append("bar_date <= :to_date")
        values["to_date"] = params.to_date
    if params.is_momentum is not None:
        clauses.append("is_momentum = :is_momentum")
        values["is_momentum"] = params.is_momentum
    if params.adjustment_type:
        clauses.append("adjustment_type = :adjustment_type")
        values["adjustment_type"] = params.adjustment_type

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values["limit"] = params.limit
    values["offset"] = params.offset
    sql = text(
        f"SELECT * FROM momentum.daily_momentum {where} "
        "ORDER BY bar_date DESC, ticker ASC LIMIT :limit OFFSET :offset"
    )
    with engine.connect() as conn:
        rows = [_row(m) for m in conn.execute(sql, values).mappings()]
    return {"status": "ok", "count": len(rows), "results": rows}


def get_momentum_by_ticker(engine: Engine, ticker: str, limit: int = 100) -> dict[str, Any] | None:
    sql = text(
        "SELECT * FROM momentum.daily_momentum WHERE ticker = :ticker "
        "ORDER BY bar_date DESC LIMIT :limit"
    )
    with engine.connect() as conn:
        rows = [_row(m) for m in conn.execute(sql, {"ticker": ticker.upper(), "limit": limit}).mappings()]
    if not rows:
        return None
    return {"status": "ok", "ticker": ticker.upper(), "count": len(rows), "results": rows}


def get_latest_momentum(engine: Engine, is_momentum: bool = True) -> dict[str, Any]:
    extra = "AND is_momentum = true" if is_momentum else ""
    sql = text(
        f"""
        SELECT * FROM momentum.daily_momentum
        WHERE bar_date = (SELECT MAX(bar_date) FROM momentum.daily_momentum)
        {extra}
        ORDER BY ticker ASC
        """
    )
    with engine.connect() as conn:
        rows = [_row(m) for m in conn.execute(sql).mappings()]
    return {"status": "ok", "count": len(rows), "results": rows}


def get_momentum_date_range(engine: Engine) -> dict[str, Any] | None:
    sql = text(
        """
        SELECT MIN(bar_date) AS min_bar_date,
               MAX(bar_date) AS max_bar_date,
               COUNT(*) AS total_rows,
               COUNT(*) FILTER (WHERE is_momentum) AS flagged_rows
        FROM momentum.daily_momentum
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql).mappings().first()
    if row is None or row["min_bar_date"] is None:
        return None
    return {"status": "ok", **_row(row)}


def list_daily_changes(engine: Engine, params: DailyChangeListParams) -> dict[str, Any]:
    clauses: list[str] = []
    values: dict[str, Any] = {}
    if params.ticker:
        clauses.append("ticker = :ticker")
        values["ticker"] = params.ticker.upper()
    if params.symbol_id is not None:
        clauses.append("symbol_id = :symbol_id")
        values["symbol_id"] = params.symbol_id
    if params.from_date:
        clauses.append("bar_date >= :from_date")
        values["from_date"] = params.from_date
    if params.to_date:
        clauses.append("bar_date <= :to_date")
        values["to_date"] = params.to_date
    if params.adjustment_type:
        clauses.append("adjustment_type = :adjustment_type")
        values["adjustment_type"] = params.adjustment_type

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values["limit"] = params.limit
    values["offset"] = params.offset
    sql = text(
        f"""
        SELECT symbol_id, ticker, bar_date,
               close_change_amount, close_change_percent,
               intraday_change_amount, intraday_change_percent
        FROM momentum.daily_price_changes
        {where}
        ORDER BY bar_date DESC, ticker ASC
        LIMIT :limit OFFSET :offset
        """
    )
    with engine.connect() as conn:
        rows = [_row(m) for m in conn.execute(sql, values).mappings()]
    return {"status": "ok", "count": len(rows), "results": rows}


def list_runs(engine: Engine, params: RunListParams) -> dict[str, Any]:
    clauses: list[str] = []
    values: dict[str, Any] = {"limit": params.limit, "offset": params.offset}
    if params.status:
        clauses.append("status = :status")
        values["status"] = params.status
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = text(
        f"SELECT * FROM momentum.momentum_runs {where} "
        "ORDER BY id DESC LIMIT :limit OFFSET :offset"
    )
    with engine.connect() as conn:
        rows = [_row(m) for m in conn.execute(sql, values).mappings()]
    return {"status": "ok", "count": len(rows), "results": rows}


def get_run(engine: Engine, run_id: int) -> dict[str, Any] | None:
    sql = text("SELECT * FROM momentum.momentum_runs WHERE id = :id")
    with engine.connect() as conn:
        row = conn.execute(sql, {"id": run_id}).mappings().first()
    return _row(row) if row is not None else None


def get_latest_run(engine: Engine) -> dict[str, Any] | None:
    sql = text("SELECT * FROM momentum.momentum_runs ORDER BY id DESC LIMIT 1")
    with engine.connect() as conn:
        row = conn.execute(sql).mappings().first()
    return _row(row) if row is not None else None


def get_stats(engine: Engine) -> dict[str, Any]:
    totals_sql = text(
        """
        SELECT COUNT(*) AS total_rows,
               COUNT(DISTINCT ticker) AS distinct_tickers,
               MAX(bar_date) AS latest_bar_date
        FROM momentum.daily_momentum
        """
    )
    with engine.connect() as conn:
        totals = conn.execute(totals_sql).mappings().first()
        latest = conn.execute(
            text("SELECT * FROM momentum.momentum_runs ORDER BY id DESC LIMIT 1")
        ).mappings().first()
    return {
        "status": "ok",
        "totals": _row(totals) if totals is not None else {},
        "latest_run": _row(latest) if latest is not None else None,
    }
