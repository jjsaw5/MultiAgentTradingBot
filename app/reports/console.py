"""Rich console rendering of a trade report.

The score breakdown is printed in full by default. Being able to see every
point and the measurement behind it is the point of the exercise -- a ranked
list you cannot audit is not useful for evaluating a methodology.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app.models.enums import Classification, WorkflowStage
from app.models.report import RankedTrade, RejectedTrade, TradeReport

_CLASS_STYLE = {
    Classification.EXCEPTIONAL: "bold green",
    Classification.HIGH_CONVICTION: "green",
    Classification.GOOD_CANDIDATE: "cyan",
    Classification.WATCHLIST: "yellow",
    Classification.REJECTED: "red",
}


def render(report: TradeReport, console: Console | None = None, *, show_audit: bool = True) -> None:
    c = console or Console()
    _header(c, report)
    _market_summary(c, report)

    if report.top_trades:
        c.rule("[bold]Top Ranked Trades")
        for trade in report.top_trades:
            _ranked_trade(c, trade, show_audit=show_audit)
    else:
        c.print(
            Panel(
                "No trade met the criteria for presentation in this run.\n"
                + "\n".join(f"- {n}" for n in report.notes),
                title="No actionable trades",
                border_style="yellow",
            )
        )

    if report.rejected:
        c.rule("[bold]Rejected Candidates")
        for rej in report.rejected:
            _rejected(c, rej)

    _footer(c, report)


def _header(c: Console, report: TradeReport) -> None:
    stage_style = "green" if report.stage is WorkflowStage.MARKET_OPEN else "yellow"
    c.print(
        Panel(
            Text.assemble(
                ("Multi-Agent Options Research\n", "bold"),
                (f"run {report.run_id}   trading day {report.trading_day}\n", "dim"),
                ("stage ", "dim"),
                (report.stage.value, stage_style),
                (
                    f"   methodology {report.methodology_version} "
                    f"({report.methodology_fingerprint})",
                    "dim",
                ),
            ),
            border_style="blue",
        )
    )
    for flag in report.data_quality_flags:
        if flag.code == "MOCK_DATA":
            c.print(
                Panel(
                    "[bold red]SYNTHETIC DATA[/bold red] -- every provider is running its mock "
                    "backend. Nothing below reflects a real market.",
                    border_style="red",
                )
            )
            break


def _market_summary(c: Console, report: TradeReport) -> None:
    s = report.market_summary
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim", width=22)
    table.add_column()
    table.add_row("Market regime", s.market_regime)
    table.add_row("Volatility regime", s.volatility_regime)
    table.add_row("SPY bias", f"{s.spy_bias}  [dim]{s.spy_note or ''}[/dim]")
    table.add_row("QQQ bias", f"{s.qqq_bias}  [dim]{s.qqq_note or ''}[/dim]")
    if s.vix_level is not None:
        table.add_row("VIX", f"{s.vix_level}  [dim]{s.vix_note or ''}[/dim]")
    elif s.vix_note:
        table.add_row("Volatility note", s.vix_note)
    if s.regime_rationale:
        table.add_row("Rationale", s.regime_rationale)

    if s.major_event_risks_today:
        table.add_row(
            "Event risk",
            "\n".join(f"- {r.description} ({r.importance.value})" for r in s.major_event_risks_today),
        )
    if s.upcoming_economic_events:
        table.add_row(
            "Upcoming macro",
            "\n".join(
                f"- {e.scheduled_date} {e.name} ({e.importance.value})"
                + (f"  consensus {e.consensus}" if e.consensus else "")
                for e in s.upcoming_economic_events[:6]
            ),
        )
    c.print(Panel(table, title="Market Summary", border_style="blue"))


def _ranked_trade(c: Console, t: RankedTrade, *, show_audit: bool) -> None:
    style = _CLASS_STYLE.get(t.classification, "white")
    long_c = t.trade.long_leg.contract
    short = t.trade.short_leg

    title = (
        f"#{t.rank}  {t.candidate.ticker}  {t.candidate.strategy_type.value.replace('_', ' ')}"
        f"   [{style}]{t.breakdown.total:.0f}/100 -- {t.classification_label}[/{style}]"
    )

    spec = Table(show_header=False, box=None, pad_edge=False)
    spec.add_column(style="dim", width=22)
    spec.add_column()
    spec.add_row("Underlying", f"{t.trade.underlying_price:.2f}")
    spec.add_row("Direction", t.candidate.direction.value)
    spec.add_row("Expiration", f"{t.trade.expiration}  ({long_c.dte()} DTE)")
    spec.add_row("Long strike", f"{long_c.strike:.2f} {long_c.right.value}")
    if short is not None:
        spec.add_row("Short strike", f"{short.contract.strike:.2f} {short.contract.right.value}")
        spec.add_row("Width", f"{t.trade.spread_width:.2f}")
    spec.add_row(
        "Debit",
        f"{t.trade.net_debit_conservative:.2f} conservative / {t.trade.net_debit_mid:.2f} mid",
    )
    spec.add_row("Max loss", f"${t.risk_reward.max_loss:,.0f} (full debit)")
    if t.risk_reward.risk_to_invalidation is not None:
        spec.add_row(
            "Risk to invalidation",
            f"${t.risk_reward.risk_to_invalidation:,.0f} at "
            f"{t.risk_reward.invalidation_underlying_price:.2f}",
        )
    spec.add_row(
        "Max profit",
        f"${t.trade.max_profit:,.0f}" if t.trade.max_profit is not None else "undefined (long option)",
    )
    spec.add_row("Breakeven", f"{t.trade.breakeven:.2f}  ({t.trade.breakeven_move_pct:.2f}% move)")
    spec.add_row(
        "Reward / risk",
        f"{t.risk_reward.reward_to_risk:.2f}  [dim]to invalidation[/dim]"
        if t.risk_reward.reward_to_risk is not None
        else "n/a",
    )
    spec.add_row(
        "Return on premium",
        f"{t.risk_reward.return_on_premium_at_target:+.0%} at target"
        if t.risk_reward.return_on_premium_at_target is not None
        else "n/a",
    )
    spec.add_row("Net delta", _num(t.trade.net_delta))
    spec.add_row("IV (long leg)", _pct(long_c.implied_volatility))
    spec.add_row("IV rank", _num(long_c.iv_rank, "{:.0f}"))
    spec.add_row(
        "Bid / ask",
        f"{_num(long_c.bid)} / {_num(long_c.ask)}"
        + (f"  ({long_c.spread_pct:.1%} wide)" if long_c.spread_pct is not None else ""),
    )
    spec.add_row("Volume / OI", f"{_num(long_c.volume, '{:,.0f}')} / {_num(long_c.open_interest, '{:,.0f}')}")

    c.print(Panel(spec, title=title, border_style=style))

    _bullets(c, "Catalyst", [f"{t.candidate.primary_catalyst.catalyst_type.value}: "
                             f"{t.candidate.primary_catalyst.headline}"])
    _bullets(c, "Technical thesis", [t.technical_thesis, t.candidate.technical_context.notes or ""])
    _bullets(c, "Options flow", [t.flow_confirmation])
    _bullets(c, "Entry conditions", t.entry_conditions)
    _bullets(c, "Profit targets", t.profit_targets)
    _bullets(c, "Invalidation", [t.invalidation])
    _bullets(c, "Risks", t.risks)

    if show_audit:
        _score_table(c, t)


def _score_table(c: Console, t: RankedTrade) -> None:
    table = Table(box=None, pad_edge=False, show_edge=False)
    table.add_column("Component", style="dim", width=22)
    table.add_column("Score", justify="right", width=10)
    table.add_column("Rules that fired")
    for comp in t.breakdown.components:
        detail = "\n".join(
            f"{r.points:+5.1f}  {r.rule}  [dim]{r.measurement}[/dim]" for r in comp.reasons
        )
        for miss in comp.unscored_due_to_missing_data:
            detail += f"\n  0.0  [yellow]{miss}[/yellow]"
        table.add_row(
            comp.name,
            f"{comp.points:.1f}/{comp.max_points:.0f}",
            detail or "[dim]no rules fired[/dim]",
        )
    table.add_row("", "", "")
    table.add_row(
        "[bold]TOTAL", f"[bold]{t.breakdown.total:.1f}/{t.breakdown.max_total:.0f}", ""
    )
    c.print(Panel(table, title="Score breakdown", border_style="dim"))


def _rejected(c: Console, rej: RejectedTrade) -> None:
    lines = [f"- {r}" for r in rej.reasons]
    if rej.breakdown:
        weakest = sorted(rej.breakdown.components, key=lambda x: x.pct)[:3]
        lines.append("")
        lines.append("Weakest components:")
        lines.extend(
            f"  {comp.name}: {comp.points:.1f}/{comp.max_points:.0f} ({comp.pct:.0f}%)"
            for comp in weakest
        )
    c.print(
        Panel(
            "\n".join(lines),
            title=(
                f"{rej.candidate.ticker}  {rej.candidate.strategy_type.value.replace('_', ' ')}"
                + (f"   {rej.score:.0f}/100" if rej.score is not None else "")
            ),
            border_style="red",
        )
    )


def _footer(c: Console, report: TradeReport) -> None:
    lines = [f"Candidates considered: {report.candidates_considered}"]
    lines.extend(f"- {n}" for n in report.notes)
    if report.data_quality_flags:
        lines.append("")
        lines.append("Data quality flags:")
        lines.extend(
            f"  [{f.severity.value}] {f.code}: {f.message}" for f in report.data_quality_flags
        )
    lines.append("")
    lines.append(
        "This system does not place orders. Every recommendation requires human review."
    )
    c.print(Panel("\n".join(lines), title="Run notes", border_style="dim"))


def _bullets(c: Console, title: str, items: list[str]) -> None:
    items = [i for i in items if i]
    if not items:
        return
    c.print(f"  [bold dim]{title}[/bold dim]")
    for item in items:
        c.print(f"    • {item}")


def _num(v: float | None, fmt: str = "{:.2f}") -> str:
    return fmt.format(v) if v is not None else "n/a"


def _pct(v: float | None) -> str:
    return f"{v:.1%}" if v is not None else "n/a"


__all__ = ["render"]
