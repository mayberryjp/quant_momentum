"""Tests for the Alembic migration (offline SQL rendering; no live DB)."""

from __future__ import annotations

import contextlib
import io

from alembic import command
from alembic.script import ScriptDirectory

from quant_momentum.db import make_alembic_config


def _render_sql(target: str) -> str:
    """Render offline (``--sql``) migration output for a target/range."""
    cfg = make_alembic_config()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        if ":" in target:
            command.downgrade(cfg, target, sql=True)
        else:
            command.upgrade(cfg, target, sql=True)
    return buffer.getvalue().lower()


def test_single_head_revision_0003() -> None:
    script = ScriptDirectory.from_config(make_alembic_config())
    assert script.get_heads() == ["0003"]
    rev = script.get_revision("0003")
    assert rev.down_revision == "0002"


def test_upgrade_creates_schema_tables_and_indexes() -> None:
    sql = _render_sql("head")
    assert "create schema if not exists momentum" in sql
    assert "create table momentum.momentum_runs" in sql
    assert "create table momentum.daily_momentum" in sql
    assert "create table momentum.daily_price_changes" in sql
    assert "create table momentum.signal_submissions" in sql
    assert "unique (symbol_id, bar_date, adjustment_type)" in sql
    assert "submission_count" in sql
    assert "uq_signal_submissions_ticker_bar_date" in sql
    for index in (
        "ix_daily_momentum_ticker_bar_date",
        "ix_daily_momentum_bar_date",
        "ix_daily_momentum_bar_date_is_momentum",
        "ix_daily_momentum_is_momentum_bar_date",
        "ix_daily_price_changes_ticker_bar_date",
        "ix_daily_price_changes_bar_date",
        "ix_signal_submissions_run_id",
    ):
        assert index in sql


def test_upgrade_includes_rolling_30d_stat_columns() -> None:
    sql = _render_sql("head")
    for column in (
        "avg_daily_change_30d",
        "median_daily_change_30d",
        "min_daily_change_30d",
        "max_daily_change_30d",
        "floor_price_30d",
        "ceiling_price_30d",
    ):
        assert column in sql


def test_downgrade_only_touches_momentum_schema() -> None:
    sql = _render_sql("0003:base")
    assert "drop table if exists momentum.daily_price_changes" in sql
    assert "drop table if exists momentum.daily_momentum" in sql
    assert "drop table if exists momentum.momentum_runs" in sql
    assert "drop table if exists momentum.signal_submissions" in sql
    # Never drops shared schemas / objects.
    assert "market_data" not in sql
    assert "symbol_master" not in sql
    assert "drop schema" not in sql
