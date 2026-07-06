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


def test_dispatch_stubs_return_zero() -> None:
    assert main(["db", "upgrade"]) == 0
    assert main(["run-summary", "--latest"]) == 0
