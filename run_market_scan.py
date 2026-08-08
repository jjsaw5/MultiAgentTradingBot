#!/usr/bin/env python3
"""Run one full market scan and print the ranked report.

    python run_market_scan.py
    python run_market_scan.py --stage PREMARKET
    python run_market_scan.py --no-save --markdown reports_out/scan.md

This is the thinnest possible entry point: it exists so the pipeline can be
exercised end to end without learning the CLI. `matb scan` does the same thing
with more options.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from rich.console import Console

from app.database.session import init_db
from app.logging_config import configure_logging
from app.models.enums import WorkflowStage
from app.reports import console as console_report
from app.reports import markdown as markdown_report
from app.services.orchestrator import Orchestrator
from app.services.persistence import persist_scan


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a multi-agent options market scan.")
    parser.add_argument("--stage", choices=[s.value for s in WorkflowStage], default=None)
    parser.add_argument("--trading-day", default=None, help="ISO date; defaults to today.")
    parser.add_argument("--universe", default=None, help="Comma-separated tickers.")
    parser.add_argument("--no-save", action="store_true", help="Do not persist the run.")
    parser.add_argument("--no-audit", action="store_true", help="Hide the score breakdown.")
    parser.add_argument("--markdown", default=None, help="Write a markdown report here.")
    args = parser.parse_args()

    configure_logging()
    console = Console()

    orchestrator = Orchestrator(
        trading_day=date.fromisoformat(args.trading_day) if args.trading_day else None,
        universe=[t.strip().upper() for t in args.universe.split(",")] if args.universe else None,
    )
    result = orchestrator.run(stage=WorkflowStage(args.stage) if args.stage else None)

    console_report.render(result.report, console, show_audit=not args.no_audit)

    if args.markdown:
        path = Path(args.markdown)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown_report.render(result.report))
        console.print(f"[dim]Markdown report written to {path}[/dim]")

    if not args.no_save:
        init_db()
        persist_scan(result)
        console.print(f"[dim]Run {result.run_id} persisted.[/dim]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
