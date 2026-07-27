"""Bottle API application for quant_momentum (served by waitress; spec §9).

Handlers are dependency-injected into :func:`create_app` so the routes can be
driven by ``webtest`` with fakes (no live DB). Errors redact the DB URL; invalid
query parameters return 422; missing resources return 404.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from bottle import Bottle, request, response

from quant_momentum.api.queries import (
    DailyChangeListParams,
    VALID_ADJUSTMENT_TYPES,
    VALID_RUN_STATUSES,
    MomentumListParams,
    RunListParams,
)
from quant_momentum.api.readiness import sanitize_readiness_error
from quant_momentum.logging_config import configure_logging

SERVICE_NAME = "quant-momentum-api"

log = logging.getLogger(SERVICE_NAME)


# ---------------------------------------------------------------------------
# Query-parameter helpers
# ---------------------------------------------------------------------------
class _ValidationError(Exception):
    pass


def _int_param(raw: str | None, *, default, ge: int | None = None, le: int | None = None):
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (ValueError, TypeError):
        raise _ValidationError("invalid integer parameter")
    if ge is not None and value < ge:
        raise _ValidationError(f"value must be >= {ge}")
    if le is not None and value > le:
        raise _ValidationError(f"value must be <= {le}")
    return value


def _bool_param(raw: str | None) -> bool | None:
    if raw is None or raw == "":
        return None
    lowered = raw.lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise _ValidationError("invalid boolean parameter (use true/false)")


def _adjustment_param(raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    if raw not in VALID_ADJUSTMENT_TYPES:
        raise _ValidationError(f"adjustment_type must be one of {sorted(VALID_ADJUSTMENT_TYPES)}")
    return raw


def _status_param(raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    if raw not in VALID_RUN_STATUSES:
        raise _ValidationError(f"status must be one of {sorted(VALID_RUN_STATUSES)}")
    return raw


def _date_param(raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    parts = raw.split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise _ValidationError("date must be YYYY-MM-DD")
    return raw


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
def _not_found(error: str = "not found") -> dict:
    response.status = 404
    return {"status": "not_found", "error": error}


def _validation_error_response(detail: str = "validation error") -> dict:
    response.status = 422
    return {"detail": detail}


def _server_error(exc: Exception) -> dict:
    log.exception("handler_error: %s", exc)
    response.status = 500
    return {"status": "error", "error": sanitize_readiness_error(exc, os.environ.get("DATABASE_URL"))}


# ---------------------------------------------------------------------------
# Default (DB-backed) handlers
# ---------------------------------------------------------------------------
def _default_handlers() -> dict[str, Callable]:
    from quant_momentum.api import queries
    from quant_momentum.api.readiness import check_database_readiness
    from quant_momentum.db import get_engine

    holder: dict[str, Any] = {}

    def engine():
        if "engine" not in holder:
            holder["engine"] = get_engine()
        return holder["engine"]

    return {
        "readiness_check": lambda: check_database_readiness(engine()),
        "momentum_list": lambda p: queries.list_momentum(engine(), p),
        "momentum_by_ticker": lambda t, limit: queries.get_momentum_by_ticker(engine(), t, limit),
        "momentum_latest": lambda flag: queries.get_latest_momentum(engine(), flag),
        "momentum_date_range": lambda: queries.get_momentum_date_range(engine()),
        "daily_changes_list": lambda p: queries.list_daily_changes(engine(), p),
        "runs_list": lambda p: queries.list_runs(engine(), p),
        "run_detail": lambda rid: queries.get_run(engine(), rid),
        "runs_latest": lambda: queries.get_latest_run(engine()),
        "stats": lambda: queries.get_stats(engine()),
    }


def create_app(**overrides: Callable) -> Bottle:
    """Construct the Bottle application with injectable handlers."""
    handlers = _default_handlers()
    handlers.update(overrides)

    readiness_check = handlers["readiness_check"]
    momentum_list = handlers["momentum_list"]
    momentum_by_ticker = handlers["momentum_by_ticker"]
    momentum_latest = handlers["momentum_latest"]
    momentum_date_range = handlers["momentum_date_range"]
    daily_changes_list = handlers["daily_changes_list"]
    runs_list = handlers["runs_list"]
    run_detail = handlers["run_detail"]
    runs_latest = handlers["runs_latest"]
    stats = handlers["stats"]

    api = Bottle()

    # -- health / readiness -------------------------------------------------
    @api.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": SERVICE_NAME}

    @api.get("/ready")
    def ready() -> dict:
        try:
            status = readiness_check()
            return status.as_json() if hasattr(status, "as_json") else status
        except Exception as exc:
            response.status = 503
            return {
                "status": "not_ready",
                "database": "error",
                "error": sanitize_readiness_error(exc, os.environ.get("DATABASE_URL")),
            }

    # -- momentum data ------------------------------------------------------
    @api.get("/momentum")
    def momentum_route() -> dict:
        try:
            symbol_id_raw = request.query.get("symbol_id")
            params = MomentumListParams(
                ticker=request.query.get("ticker") or None,
                symbol_id=_int_param(symbol_id_raw, default=None) if symbol_id_raw else None,
                from_date=_date_param(request.query.get("from_date")),
                to_date=_date_param(request.query.get("to_date")),
                is_momentum=_bool_param(request.query.get("is_momentum")),
                adjustment_type=_adjustment_param(request.query.get("adjustment_type")),
                limit=_int_param(request.query.get("limit"), default=100, ge=1, le=500),
                offset=_int_param(request.query.get("offset"), default=0, ge=0),
            )
        except _ValidationError as exc:
            return _validation_error_response(str(exc))
        try:
            return momentum_list(params)
        except Exception as exc:
            return _server_error(exc)

    @api.get("/momentum/by-ticker/<ticker>")
    def momentum_by_ticker_route(ticker: str) -> dict:
        try:
            limit = _int_param(request.query.get("limit"), default=100, ge=1, le=500)
        except _ValidationError as exc:
            return _validation_error_response(str(exc))
        try:
            result = momentum_by_ticker(ticker, limit)
        except Exception as exc:
            return _server_error(exc)
        if result is None:
            return _not_found(f"no momentum for ticker {ticker}")
        return result

    @api.get("/momentum/latest")
    def momentum_latest_route() -> dict:
        try:
            flag = _bool_param(request.query.get("is_momentum"))
        except _ValidationError as exc:
            return _validation_error_response(str(exc))
        try:
            return momentum_latest(True if flag is None else flag)
        except Exception as exc:
            return _server_error(exc)

    @api.get("/momentum/date-range")
    def momentum_date_range_route() -> dict:
        try:
            result = momentum_date_range()
        except Exception as exc:
            return _server_error(exc)
        if result is None:
            return _not_found("no momentum rows")
        return result

    @api.get("/daily-changes")
    def daily_changes_route() -> dict:
        try:
            symbol_id_raw = request.query.get("symbol_id")
            params = DailyChangeListParams(
                ticker=request.query.get("ticker") or None,
                symbol_id=_int_param(symbol_id_raw, default=None) if symbol_id_raw else None,
                from_date=_date_param(request.query.get("from_date")),
                to_date=_date_param(request.query.get("to_date")),
                adjustment_type=_adjustment_param(request.query.get("adjustment_type")),
                limit=_int_param(request.query.get("limit"), default=100, ge=1, le=500),
                offset=_int_param(request.query.get("offset"), default=0, ge=0),
            )
        except _ValidationError as exc:
            return _validation_error_response(str(exc))
        try:
            return daily_changes_list(params)
        except Exception as exc:
            return _server_error(exc)

    # -- runs ---------------------------------------------------------------
    @api.get("/runs")
    def runs_route() -> dict:
        try:
            params = RunListParams(
                status=_status_param(request.query.get("status")),
                limit=_int_param(request.query.get("limit"), default=20, ge=1, le=100),
                offset=_int_param(request.query.get("offset"), default=0, ge=0),
            )
        except _ValidationError as exc:
            return _validation_error_response(str(exc))
        try:
            return runs_list(params)
        except Exception as exc:
            return _server_error(exc)

    @api.get("/runs/latest")
    def runs_latest_route() -> dict:
        try:
            result = runs_latest()
        except Exception as exc:
            return _server_error(exc)
        if result is None:
            return _not_found("no runs found")
        return {"status": "ok", "latest": result}

    @api.get("/runs/<run_id>")
    def run_detail_route(run_id: str) -> dict:
        try:
            rid = int(run_id)
        except (ValueError, TypeError):
            return _validation_error_response("run_id must be an integer")
        try:
            result = run_detail(rid)
        except Exception as exc:
            return _server_error(exc)
        if result is None:
            return _not_found("run not found")
        return result

    # -- stats --------------------------------------------------------------
    @api.get("/stats")
    def stats_route() -> dict:
        try:
            return stats()
        except Exception as exc:
            return _server_error(exc)

    return api


configure_logging()

print(
    f"[{SERVICE_NAME}] module={__file__} python={sys.executable} "
    f"version={sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    file=sys.stderr,
    flush=True,
)

app = create_app()


if __name__ == "__main__":
    from waitress import serve

    host = os.environ.get("API_LISTEN_ADDRESS", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8020"))
    log.info("Starting %s on %s:%d ...", SERVICE_NAME, host, port)
    serve(app, host=host, port=port, threads=20)
