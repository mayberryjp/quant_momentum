"""Database access helpers and migration commands (spec §10, §11).

Provides a pooled SQLAlchemy engine (``pool_pre_ping=True``) and thin wrappers
around Alembic used by the ``db`` CLI group. Alembic is imported lazily so the
CLI ``--help`` path stays fast and DB-free.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine, create_engine

from quant_momentum.config import get_settings

log = logging.getLogger("quant_momentum.db")

# Repo root = parents[2] of src/quant_momentum/db.py
_REPO_ROOT = Path(__file__).resolve().parents[2]

VERSION_TABLE = "alembic_version_momentum"
VERSION_TABLE_SCHEMA = "momentum"


def get_database_url() -> str:
    return get_settings().database_url


def get_engine(url: str | None = None) -> Engine:
    """Return a SQLAlchemy engine with liveness pre-ping enabled."""
    return create_engine(url or get_database_url(), pool_pre_ping=True, future=True)


def make_alembic_config(url: str | None = None):
    """Build an Alembic ``Config`` pointing at the packaged migration tree."""
    from alembic.config import Config

    ini_path = _REPO_ROOT / "alembic.ini"
    cfg = Config(str(ini_path)) if ini_path.exists() else Config()
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url or get_database_url())
    return cfg


def upgrade(url: str | None = None) -> int:
    """Apply migrations up to head."""
    from alembic import command

    command.upgrade(make_alembic_config(url), "head")
    log.info("Database upgraded to head.")
    return 0


def downgrade_base(url: str | None = None) -> int:
    """Downgrade the momentum objects to base (shared schemas untouched)."""
    from alembic import command

    command.downgrade(make_alembic_config(url), "base")
    log.info("Database downgraded to base (momentum objects dropped).")
    return 0


def verify(url: str | None = None) -> int:
    """Verify the database is migrated to the latest revision.

    Returns ``0`` when the current revision matches head, ``1`` otherwise.
    """
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    cfg = make_alembic_config(url)
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()

    engine = get_engine(url)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={
                "version_table": VERSION_TABLE,
                "version_table_schema": VERSION_TABLE_SCHEMA,
            },
        )
        current = context.get_current_revision()

    if current == head:
        log.info("Schema verification OK (revision=%s).", current)
        return 0

    log.error("Schema verification FAILED: current=%s head=%s.", current, head)
    return 1
