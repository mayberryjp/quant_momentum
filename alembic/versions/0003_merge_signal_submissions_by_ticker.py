"""merge signal submissions by ticker/day with source chaining

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-04
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE momentum.signal_submissions "
        "ADD COLUMN submission_count INT NOT NULL DEFAULT 1"
    )

    # Collapse any existing duplicates before adding uniqueness.
    op.execute(
        """
        WITH keepers AS (
            SELECT DISTINCT ON (ticker, bar_date)
                id,
                ticker,
                bar_date
            FROM momentum.signal_submissions
            ORDER BY ticker, bar_date, submitted_at DESC, id DESC
        ),
        aggregates AS (
            SELECT
                s.ticker,
                s.bar_date,
                COUNT(*)::INT AS submission_count,
                array_to_string(array_agg(DISTINCT src ORDER BY src), ',') AS merged_sources
            FROM momentum.signal_submissions AS s
            CROSS JOIN LATERAL unnest(regexp_split_to_array(s.source, '\\s*,\\s*')) AS src
            GROUP BY s.ticker, s.bar_date
        )
        UPDATE momentum.signal_submissions AS t
        SET submission_count = a.submission_count,
            source = a.merged_sources
        FROM keepers AS k
        JOIN aggregates AS a
          ON a.ticker = k.ticker
         AND a.bar_date = k.bar_date
        WHERE t.id = k.id
        """
    )

    op.execute(
        """
        WITH keepers AS (
            SELECT DISTINCT ON (ticker, bar_date)
                id,
                ticker,
                bar_date
            FROM momentum.signal_submissions
            ORDER BY ticker, bar_date, submitted_at DESC, id DESC
        )
        DELETE FROM momentum.signal_submissions AS s
        USING keepers AS k
        WHERE s.ticker = k.ticker
          AND s.bar_date = k.bar_date
          AND s.id <> k.id
        """
    )

    op.execute("DROP INDEX IF EXISTS momentum.ix_signal_submissions_ticker_bar_date")
    op.execute(
        "ALTER TABLE momentum.signal_submissions "
        "ADD CONSTRAINT uq_signal_submissions_ticker_bar_date UNIQUE (ticker, bar_date)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE momentum.signal_submissions "
        "DROP CONSTRAINT IF EXISTS uq_signal_submissions_ticker_bar_date"
    )
    op.execute(
        "CREATE INDEX ix_signal_submissions_ticker_bar_date "
        "ON momentum.signal_submissions (ticker, bar_date)"
    )
    op.execute(
        "ALTER TABLE momentum.signal_submissions "
        "DROP COLUMN IF EXISTS submission_count"
    )
