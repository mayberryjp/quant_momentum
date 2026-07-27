"""add daily_price_changes table with retention-friendly indexes

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CREATE_DAILY_PRICE_CHANGES = """
CREATE TABLE momentum.daily_price_changes (
    id                        BIGSERIAL PRIMARY KEY,
    symbol_id                 INT NOT NULL,
    ticker                    TEXT NOT NULL,
    bar_date                  DATE NOT NULL,
    adjustment_type           TEXT NOT NULL DEFAULT 'unadjusted',
    close                     NUMERIC(18, 6) NOT NULL,
    prev_close                NUMERIC(18, 6) NOT NULL,
    close_change_amount       NUMERIC(18, 6) NOT NULL,
    close_change_percent      NUMERIC(18, 6),
    high                      NUMERIC(18, 6) NOT NULL,
    low                       NUMERIC(18, 6) NOT NULL,
    intraday_change_amount    NUMERIC(18, 6) NOT NULL,
    intraday_change_percent   NUMERIC(18, 6),
    run_id                    BIGINT REFERENCES momentum.momentum_runs (id),
    computed_at               TIMESTAMPTZ NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_daily_price_changes_symbol_date_adj
        UNIQUE (symbol_id, bar_date, adjustment_type)
)
"""

_INDEXES = (
    "CREATE INDEX ix_daily_price_changes_ticker_bar_date "
    "ON momentum.daily_price_changes (ticker, bar_date)",
    "CREATE INDEX ix_daily_price_changes_bar_date "
    "ON momentum.daily_price_changes (bar_date)",
)


def upgrade() -> None:
    op.execute(_CREATE_DAILY_PRICE_CHANGES)
    for statement in _INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS momentum.daily_price_changes")
