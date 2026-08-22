"""add 5-15d and 15-30d segment momentum columns

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-22

Adds segment (interval) momentum to ``momentum.daily_momentum``: the 5->15 and
15->30 day windows isolate a past return so a recent short-term move cannot leak
into it. Each carries a value, a binary flag, and the threshold applied (for
reproducibility). New columns use defaults so the ALTERs are safe on populated
tables; the combined ``is_momentum`` is recomputed on the next run.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ADD_COLUMNS = (
    "ALTER TABLE momentum.daily_momentum "
    "ADD COLUMN momentum_5_15d NUMERIC(18, 6)",
    "ALTER TABLE momentum.daily_momentum "
    "ADD COLUMN momentum_15_30d NUMERIC(18, 6)",
    "ALTER TABLE momentum.daily_momentum "
    "ADD COLUMN is_momentum_5_15d BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE momentum.daily_momentum "
    "ADD COLUMN is_momentum_15_30d BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE momentum.daily_momentum "
    "ADD COLUMN threshold_5_15d NUMERIC(9, 6) NOT NULL DEFAULT 0",
    "ALTER TABLE momentum.daily_momentum "
    "ADD COLUMN threshold_15_30d NUMERIC(9, 6) NOT NULL DEFAULT 0",
)

_DROP_COLUMNS = (
    "ALTER TABLE momentum.daily_momentum DROP COLUMN IF EXISTS momentum_5_15d",
    "ALTER TABLE momentum.daily_momentum DROP COLUMN IF EXISTS momentum_15_30d",
    "ALTER TABLE momentum.daily_momentum DROP COLUMN IF EXISTS is_momentum_5_15d",
    "ALTER TABLE momentum.daily_momentum DROP COLUMN IF EXISTS is_momentum_15_30d",
    "ALTER TABLE momentum.daily_momentum DROP COLUMN IF EXISTS threshold_5_15d",
    "ALTER TABLE momentum.daily_momentum DROP COLUMN IF EXISTS threshold_15_30d",
)


def upgrade() -> None:
    for statement in _ADD_COLUMNS:
        op.execute(statement)


def downgrade() -> None:
    for statement in _DROP_COLUMNS:
        op.execute(statement)
