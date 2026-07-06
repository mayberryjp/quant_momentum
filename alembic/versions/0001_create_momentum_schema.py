"""create momentum schema, daily_momentum, momentum_runs, signal_submissions

Revision ID: 0001
Revises:
Create Date: 2026-07-06

Creates the ``momentum`` schema owned by this service. Only reads
``market_data`` / ``symbol_master`` elsewhere; this migration never touches
those shared schemas. ``daily_momentum`` additionally carries rolling 30-day
daily-change statistics (avg / median / min / max) and floor/ceiling prices.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_MOMENTUM_RUNS = """
CREATE TABLE momentum.momentum_runs (
    id                  BIGSERIAL PRIMARY KEY,
    run_date            DATE NOT NULL,
    as_of_bar_date      DATE NOT NULL,
    adjustment_type     TEXT NOT NULL,
    momentum_rule       TEXT NOT NULL,
    threshold_5d        NUMERIC(9, 6),
    threshold_15d       NUMERIC(9, 6),
    threshold_30d       NUMERIC(9, 6),
    status              TEXT NOT NULL DEFAULT 'running',
    symbols_requested   INT DEFAULT 0,
    symbols_computed    INT DEFAULT 0,
    symbols_skipped     INT DEFAULT 0,
    symbols_failed      INT DEFAULT 0,
    momentum_flagged    INT DEFAULT 0,
    signals_submitted   INT DEFAULT 0,
    signals_accepted    INT DEFAULT 0,
    signals_duplicate   INT DEFAULT 0,
    signals_unresolved  INT DEFAULT 0,
    signals_failed      INT DEFAULT 0,
    error_message       TEXT,
    duration_seconds    FLOAT,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
)
"""

_DAILY_MOMENTUM = """
CREATE TABLE momentum.daily_momentum (
    id                       BIGSERIAL PRIMARY KEY,
    symbol_id                INT NOT NULL,
    ticker                   TEXT NOT NULL,
    bar_date                 DATE NOT NULL,
    adjustment_type          TEXT NOT NULL DEFAULT 'unadjusted',
    close                    NUMERIC(18, 6) NOT NULL,
    close_5d_ago             NUMERIC(18, 6),
    close_15d_ago            NUMERIC(18, 6),
    close_30d_ago            NUMERIC(18, 6),
    momentum_5d              NUMERIC(18, 6),
    momentum_15d             NUMERIC(18, 6),
    momentum_30d             NUMERIC(18, 6),
    is_momentum_5d           BOOLEAN NOT NULL DEFAULT false,
    is_momentum_15d          BOOLEAN NOT NULL DEFAULT false,
    is_momentum_30d          BOOLEAN NOT NULL DEFAULT false,
    is_momentum              BOOLEAN NOT NULL DEFAULT false,
    momentum_rule            TEXT NOT NULL,
    threshold_5d             NUMERIC(9, 6) NOT NULL,
    threshold_15d            NUMERIC(9, 6) NOT NULL,
    threshold_30d            NUMERIC(9, 6) NOT NULL,
    -- rolling 30-day daily-change statistics (issue follow-up)
    avg_daily_change_30d     NUMERIC(18, 6),
    median_daily_change_30d  NUMERIC(18, 6),
    min_daily_change_30d     NUMERIC(18, 6),
    max_daily_change_30d     NUMERIC(18, 6),
    floor_price_30d          NUMERIC(18, 6),
    ceiling_price_30d        NUMERIC(18, 6),
    bars_available           INT NOT NULL,
    run_id                   BIGINT REFERENCES momentum.momentum_runs (id),
    computed_at              TIMESTAMPTZ NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_daily_momentum_symbol_date_adj
        UNIQUE (symbol_id, bar_date, adjustment_type)
)
"""

_SIGNAL_SUBMISSIONS = """
CREATE TABLE momentum.signal_submissions (
    id               BIGSERIAL PRIMARY KEY,
    run_id           BIGINT REFERENCES momentum.momentum_runs (id),
    symbol_id        INT,
    ticker           TEXT NOT NULL,
    bar_date         DATE NOT NULL,
    idempotency_key  TEXT NOT NULL,
    source           TEXT NOT NULL,
    direction        TEXT,
    score            NUMERIC(9, 6),
    status           TEXT NOT NULL,
    signal_cache_id  TEXT,
    http_status      INT,
    error            TEXT,
    submitted_at     TIMESTAMPTZ DEFAULT now()
)
"""

_INDEXES = (
    "CREATE INDEX ix_daily_momentum_ticker_bar_date "
    "ON momentum.daily_momentum (ticker, bar_date)",
    "CREATE INDEX ix_daily_momentum_bar_date "
    "ON momentum.daily_momentum (bar_date)",
    "CREATE INDEX ix_daily_momentum_bar_date_is_momentum "
    "ON momentum.daily_momentum (bar_date, is_momentum)",
    "CREATE INDEX ix_daily_momentum_is_momentum_bar_date "
    "ON momentum.daily_momentum (is_momentum, bar_date)",
    "CREATE INDEX ix_signal_submissions_run_id "
    "ON momentum.signal_submissions (run_id)",
    "CREATE INDEX ix_signal_submissions_ticker_bar_date "
    "ON momentum.signal_submissions (ticker, bar_date)",
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS momentum")
    op.execute(_MOMENTUM_RUNS)
    op.execute(_DAILY_MOMENTUM)
    op.execute(_SIGNAL_SUBMISSIONS)
    for statement in _INDEXES:
        op.execute(statement)


def downgrade() -> None:
    # Drop only this service's objects; the momentum schema (and its Alembic
    # version table) is left in place so shared schemas remain untouched.
    op.execute("DROP TABLE IF EXISTS momentum.signal_submissions")
    op.execute("DROP TABLE IF EXISTS momentum.daily_momentum")
    op.execute("DROP TABLE IF EXISTS momentum.momentum_runs")
