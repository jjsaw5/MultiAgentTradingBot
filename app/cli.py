"""Command line interface.

    matb scan                      run the pipeline and print a ranked report
    matb scan --stage PREMARKET    force the premarket workflow
    matb report <run_id>           re-render a stored run
    matb decide <reco_id> APPROVED record a human decision
    matb config                    show the active methodology and backends
    matb init-db                   create tables
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from app.config.methodology import get_methodology
from app.config.settings import get_settings
from app.database.session import init_db, session_scope
from app.logging_config import configure_logging
from app.models.enums import HumanDecision, WorkflowStage
from app.reports import console as console_report
from app.reports import markdown as markdown_report

app = typer.Typer(help="Multi-agent options trading research system (research only).", no_args_is_help=True)
console = Console()


@app.command()
def scan(
    stage: str = typer.Option(
        None, help="PREMARKET | MARKET_OPEN | POSTMARKET. Defaults to the live session."
    ),
    trading_day: str = typer.Option(None, help="ISO date to scan. Defaults to today."),
    save: bool = typer.Option(True, help="Persist the run to the database."),
    audit: bool = typer.Option(True, help="Print the full score breakdown."),
    markdown_out: Path = typer.Option(None, help="Also write a markdown report to this path."),
    universe: str = typer.Option(None, help="Comma-separated ticker list to override the default."),
) -> None:
    """Run the full pipeline and print a ranked trade report."""
    from app.services.orchestrator import Orchestrator
    from app.services.persistence import persist_scan

    configure_logging()
    stage_enum = WorkflowStage(stage.upper()) if stage else None
    day = date.fromisoformat(trading_day) if trading_day else None
    tickers = [t.strip().upper() for t in universe.split(",")] if universe else None

    orchestrator = Orchestrator(trading_day=day, universe=tickers)
    result = orchestrator.run(stage=stage_enum)

    console_report.render(result.report, console, show_audit=audit)

    if markdown_out:
        markdown_out.parent.mkdir(parents=True, exist_ok=True)
        markdown_out.write_text(markdown_report.render(result.report))
        console.print(f"[dim]Markdown report written to {markdown_out}[/dim]")

    if save:
        init_db()
        persist_scan(result)
        console.print(f"[dim]Run {result.run_id} persisted.[/dim]")


@app.command("init-db")
def init_database() -> None:
    """Create database tables."""
    engine = init_db()
    console.print(f"Schema created on [bold]{engine.url.render_as_string(hide_password=True)}[/bold]")


@app.command()
def report(run_id: str, audit: bool = typer.Option(True)) -> None:
    """Re-render a stored run from the database."""
    from app.database import models as m

    with session_scope() as s:
        run = s.get(m.MarketRun, run_id)
        if run is None:
            console.print(f"[red]No run {run_id}[/red]")
            raise typer.Exit(code=1)
        recos = (
            s.query(m.TradeRecommendation)
            .filter(m.TradeRecommendation.run_id == run_id)
            .order_by(m.TradeRecommendation.total_score.desc())
            .all()
        )

        table = Table(title=f"Run {run_id} — {run.trading_day} ({run.stage})")
        table.add_column("Rank")
        table.add_column("Ticker")
        table.add_column("Strategy")
        table.add_column("Score", justify="right")
        table.add_column("Classification")
        table.add_column("R:R", justify="right")
        for r in recos:
            table.add_row(
                str(r.rank or "—"),
                r.ticker,
                r.strategy_type,
                f"{r.total_score:.0f}",
                r.classification_label,
                f"{r.reward_to_risk:.2f}" if r.reward_to_risk is not None else "n/a",
            )
        console.print(table)

        if audit:
            for r in recos:
                comps = (
                    s.query(m.ScoreComponentRow)
                    .filter(m.ScoreComponentRow.score_id == r.score_id)
                    .all()
                )
                console.rule(f"{r.ticker} — {r.total_score:.0f}/100")
                for comp in comps:
                    console.print(f"[dim]{comp.name}[/dim] {comp.points:.1f}/{comp.max_points:.0f}")
                    for reason in comp.reasons:
                        console.print(
                            f"    {reason['points']:+5.1f}  {reason['rule']}  "
                            f"[dim]{reason['measurement']}[/dim]"
                        )
                for hr in r.hard_rejections:
                    console.print(f"  [red]HARD REJECT {hr['code']}: {hr['message']}[/red]")


@app.command()
def decide(
    recommendation_id: str,
    decision: str = typer.Argument(..., help="APPROVED | REJECTED | WATCHED | ENTERED | SKIPPED"),
    notes: str = typer.Option(None),
    by: str = typer.Option(None, help="Who made the call."),
) -> None:
    """Record a human decision against a recommendation."""
    from app.services.decisions import record_decision

    with session_scope() as s:
        row = record_decision(
            s,
            recommendation_id,
            HumanDecision(decision.upper()),
            decided_by=by,
            notes=notes,
        )
        console.print(f"Recorded [bold]{row.decision}[/bold] as {row.decision_id}")


@app.command()
def config() -> None:
    """Show the active configuration. Secrets are shown only as set/unset."""
    s = get_settings()
    m = get_methodology()

    table = Table(title="Runtime settings", show_header=False)
    table.add_column(style="dim")
    table.add_column()
    for k, v in s.safe_dict().items():
        table.add_row(k, str(v))
    console.print(table)

    console.print(
        f"\n[bold]Methodology[/bold] {m.version} fingerprint [bold]{m.fingerprint()}[/bold]"
    )
    weights = Table(show_header=True)
    weights.add_column("Component")
    weights.add_column("Weight", justify="right")
    for k, v in m.score_weights.as_dict().items():
        weights.add_row(k, f"{v:.0f}")
    weights.add_row("[bold]TOTAL", f"[bold]{sum(m.score_weights.as_dict().values()):.0f}")
    console.print(weights)

    console.print("\n[bold]Classification bands[/bold]")
    for band in m.classification_bands:
        console.print(f"  >= {band.min:>3.0f}  {band.label}")

    console.print("\n[bold]Hard rejection thresholds[/bold]")
    console.print(json.dumps(m.hard_rejections.model_dump(), indent=2))


if __name__ == "__main__":
    app()
