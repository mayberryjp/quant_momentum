"""Tests for the CLI parser and dispatch skeleton."""

from __future__ import annotations

import pytest

from quant_momentum._cli_impl import build_parser, main


def test_help_exits_zero() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--help"])
    assert excinfo.value.code == 0


def test_group_required() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parse_momentum_run() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["momentum", "run", "--as-of", "2026-07-05", "--rule", "ANY", "--no-submit"]
    )
    assert args.group == "momentum"
    assert args.command == "run"
    assert args.as_of == "2026-07-05"
    assert args.rule == "ANY"
    assert args.no_submit is True
    assert args.dry_run is False


def test_parse_backfill_requires_dates() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["momentum", "backfill"])
    args = parser.parse_args(
        ["momentum", "backfill", "--from-date", "2026-01-01", "--to-date", "2026-02-01"]
    )
    assert args.from_date == "2026-01-01"
    assert args.to_date == "2026-02-01"


def test_backfill_rejects_reversed_dates() -> None:
    # from-date > to-date is rejected before touching the database.
    assert main(["momentum", "backfill", "--from-date", "2026-07-10", "--to-date", "2026-07-01"]) == 2


def test_dispatch_run_summary_dispatches(monkeypatch) -> None:
    import quant_momentum.runner as runner

    captured: list = []
    monkeypatch.setattr(runner, "run_summary_command", lambda args: captured.append(args) or 0)
    assert main(["run-summary", "--latest"]) == 0
    assert captured and captured[0].latest is True


def test_db_upgrade_dispatches_to_db_module(monkeypatch) -> None:
    import quant_momentum.db as dbmod

    calls: list[str] = []
    monkeypatch.setattr(dbmod, "upgrade", lambda: calls.append("upgrade") or 0)
    monkeypatch.setattr(dbmod, "verify", lambda: calls.append("verify") or 0)
    monkeypatch.setattr(dbmod, "downgrade_base", lambda: calls.append("downgrade") or 0)

    assert main(["db", "upgrade"]) == 0
    assert main(["db", "verify"]) == 0
    assert main(["db", "downgrade-base"]) == 0
    assert calls == ["upgrade", "verify", "downgrade"]
