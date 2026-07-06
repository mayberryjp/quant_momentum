"""CLI argument parsing and dispatch (spec §10).

The concrete implementation lives here so it can be imported and unit-tested
without the ``__main__`` side effects of :mod:`quant_momentum.cli`. Command
handlers are stubs in the scaffold slice and are filled in by later slices.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from quant_momentum.logging_config import configure_logging

log = logging.getLogger("quant_momentum.cli")

ADJUSTMENT_TYPES = ("unadjusted", "split_adjusted")
RULES = ("ALL", "ANY", "MAJORITY")


def build_parser() -> argparse.ArgumentParser:
    """Build the full ``quant_momentum`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="quant_momentum.cli",
        description="quant_momentum — daily 5/15/30-day momentum service.",
    )
    groups = parser.add_subparsers(dest="group", required=True, metavar="<group>")

    # ----- db ---------------------------------------------------------
    db = groups.add_parser("db", help="Database migration commands.")
    db_cmds = db.add_subparsers(dest="command", required=True, metavar="<command>")
    db_cmds.add_parser("upgrade", help="Apply Alembic migrations up to head.")
    db_cmds.add_parser("verify", help="Verify the momentum schema is at head.")
    db_cmds.add_parser(
        "downgrade-base",
        help="Downgrade the momentum schema to base (drops momentum objects).",
    )

    # ----- momentum ---------------------------------------------------
    momentum = groups.add_parser("momentum", help="Momentum computation commands.")
    momentum_cmds = momentum.add_subparsers(dest="command", required=True, metavar="<command>")

    run_p = momentum_cmds.add_parser("run", help="Compute momentum for an as-of date.")
    run_p.add_argument("--as-of", metavar="YYYY-MM-DD", help="As-of date (default: latest bar date).")
    run_p.add_argument("--tickers", help="Comma-separated ticker subset (default: all active symbols).")
    run_p.add_argument("--adjustment-type", choices=ADJUSTMENT_TYPES, help="Price series to use.")
    run_p.add_argument("--rule", choices=RULES, help="Combined-indicator rule override.")
    run_p.add_argument("--no-submit", action="store_true", help="Skip watchlist submission.")
    run_p.add_argument("--dry-run", action="store_true", help="Compute without persisting or submitting.")
    run_p.add_argument("--schedule", type=int, metavar="SECONDS", help="Run continuously every N seconds.")

    backfill_p = momentum_cmds.add_parser(
        "backfill",
        help="Compute momentum across a historical date range (no submission).",
    )
    backfill_p.add_argument("--from-date", required=True, metavar="YYYY-MM-DD")
    backfill_p.add_argument("--to-date", required=True, metavar="YYYY-MM-DD")
    backfill_p.add_argument("--tickers", help="Comma-separated ticker subset.")
    backfill_p.add_argument("--adjustment-type", choices=ADJUSTMENT_TYPES)

    # ----- run-summary ------------------------------------------------
    summary_p = groups.add_parser("run-summary", help="Show a run summary.")
    summary_p.add_argument("--latest", action="store_true", help="Show the latest run.")

    return parser


def _not_implemented(feature: str, slice_no: int) -> int:
    log.warning("%s is not implemented yet (arriving in slice %d).", feature, slice_no)
    return 0


def _dispatch_db(command: str) -> int:
    from quant_momentum import db

    if command == "upgrade":
        return db.upgrade()
    if command == "verify":
        return db.verify()
    if command == "downgrade-base":
        return db.downgrade_base()
    return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.group == "db":
        return _dispatch_db(args.command)
    if args.group == "momentum":
        if args.command == "run":
            from quant_momentum import runner

            return runner.run_command(args)
        if args.command == "backfill":
            return _not_implemented("momentum backfill", 8)
    if args.group == "run-summary":
        from quant_momentum import runner

        return runner.run_summary_command(args)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the matching command handler."""
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    return _dispatch(args)
