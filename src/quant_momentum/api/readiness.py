"""Readiness checks and error redaction for the API (spec §9)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

_CREDENTIALS_RE = re.compile(r"://[^@/\s]+@")


def _redact(value: str) -> str:
    """Mask ``user:password@`` credentials in any connection-string-like text."""
    return _CREDENTIALS_RE.sub("://<redacted>@", value)


def sanitize_readiness_error(exc: Exception, database_url: str | None) -> str:
    """Return an error string with DB credentials redacted."""
    message = str(exc)
    if database_url:
        message = message.replace(database_url, _redact(database_url))
    return _redact(message)


@dataclass(frozen=True)
class ReadinessStatus:
    database: str
    schema_version: str | None
    latest_run: dict[str, Any] | None

    def as_json(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "database": self.database,
            "schema_version": self.schema_version,
            "latest_run": self.latest_run,
        }


def check_database_readiness(engine: Engine) -> ReadinessStatus:
    """Verify DB connectivity, read the schema version and the latest run."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        schema_version = conn.execute(
            text("SELECT version_num FROM momentum.alembic_version_momentum")
        ).scalar()
        latest = conn.execute(
            text(
                "SELECT id, run_date, status, finished_at "
                "FROM momentum.momentum_runs ORDER BY id DESC LIMIT 1"
            )
        ).mappings().first()

    latest_run = None
    if latest is not None:
        latest_run = {
            "id": latest["id"],
            "run_date": latest["run_date"].isoformat() if latest["run_date"] else None,
            "status": latest["status"],
            "finished_at": latest["finished_at"].isoformat() if latest["finished_at"] else None,
        }
    return ReadinessStatus(database="ok", schema_version=schema_version, latest_run=latest_run)
