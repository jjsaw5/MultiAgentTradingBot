"""Markdown rendering of a trade report, for saving or sharing a scan."""

from __future__ import annotations

from app.models.report import TradeReport


def render(report: TradeReport) -> str:
    out: list[str] = []
    a = out.append

    a(f"# Trade Report — {report.trading_day}")
    a("")
    a(f"- Run: `{report.run_id}`")
    a(f"- Stage: **{report.stage.value}**")
    a(f"- Methodology: {report.methodology_version} (`{report.methodology_fingerprint}`)")
    if any(f.code == "MOCK_DATA" for f in report.data_quality_flags):
        a("")
        a("> **SYNTHETIC DATA.** Every provider is running its mock backend. "
          "Nothing below reflects a real market.")
    a("")

    s = report.market_summary
    a("## Market summary")
    a("")
    a(f"- Regime: **{s.market_regime}**, volatility **{s.volatility_regime}**")
    a(f"- SPY bias: **{s.spy_bias}**{f' — {s.spy_note}' if s.spy_note else ''}")
    a(f"- QQQ bias: **{s.qqq_bias}**{f' — {s.qqq_note}' if s.qqq_note else ''}")
    if s.vix_note:
        a(f"- Volatility: {s.vix_note}")
    if s.regime_rationale:
        a(f"- Rationale: {s.regime_rationale}")
    if s.major_event_risks_today:
        a("")
        a("**Event risk**")
        a("")
        for r in s.major_event_risks_today:
            a(f"- {r.description} ({r.importance.value})")
    if s.upcoming_economic_events:
        a("")
        a("**Upcoming economic events**")
        a("")
        for e in s.upcoming_economic_events[:8]:
            a(f"- `{e.scheduled_date}` {e.name} ({e.importance.value})"
              + (f" — consensus {e.consensus}" if e.consensus else ""))
    a("")

    a("## Top trades")
    a("")
    if not report.top_trades:
        a("_No trade met the criteria for presentation in this run._")
    for t in report.top_trades:
        long_c = t.trade.long_leg.contract
        short = t.trade.short_leg
        a(f"### #{t.rank} {t.candidate.ticker} "
          f"{t.candidate.strategy_type.value.replace('_', ' ').title()}")
        a("")
        a(f"**Score: {t.breakdown.total:.0f}/100 — {t.classification_label}**")
        a("")
        a("| Field | Value |")
        a("| --- | --- |")
        a(f"| Underlying | {t.trade.underlying_price:.2f} |")
        a(f"| Direction | {t.candidate.direction.value} |")
        a(f"| Expiration | {t.trade.expiration} ({long_c.dte()} DTE) |")
        a(f"| Long strike | {long_c.strike:.2f} {long_c.right.value} |")
        if short is not None:
            a(f"| Short strike | {short.contract.strike:.2f} {short.contract.right.value} |")
            a(f"| Width | {t.trade.spread_width:.2f} |")
        a(f"| Debit | {t.trade.net_debit_conservative:.2f} (mid {t.trade.net_debit_mid:.2f}) |")
        a(f"| Max loss | ${t.risk_reward.max_loss:,.0f} |")
        a(f"| Max profit | {f'${t.trade.max_profit:,.0f}' if t.trade.max_profit is not None else 'undefined'} |")
        a(f"| Breakeven | {t.trade.breakeven:.2f} ({t.trade.breakeven_move_pct:.2f}% move) |")
        a(f"| Reward/risk | {t.risk_reward.reward_to_risk if t.risk_reward.reward_to_risk is not None else 'n/a'} |")
        a(f"| Net delta | {t.trade.net_delta} |")
        a(f"| IV (long leg) | {long_c.implied_volatility} |")
        a(f"| Bid/ask | {long_c.bid} / {long_c.ask} |")
        a(f"| Volume / OI | {long_c.volume} / {long_c.open_interest} |")
        a("")
        a(f"**Catalyst.** {t.candidate.primary_catalyst.catalyst_type.value}: "
          f"{t.candidate.primary_catalyst.headline}")
        a("")
        a(f"**Technical thesis.** {t.technical_thesis}")
        a("")
        a(f"**Options flow.** {t.flow_confirmation}")
        a("")
        _list(a, "Entry conditions", t.entry_conditions)
        _list(a, "Profit targets", t.profit_targets)
        a(f"**Invalidation.** {t.invalidation}")
        a("")
        _list(a, "Risks", t.risks)
        a("<details><summary>Score breakdown</summary>")
        a("")
        a("| Component | Score | Rules |")
        a("| --- | --- | --- |")
        for comp in t.breakdown.components:
            rules = "<br>".join(
                f"{r.points:+.1f} {r.rule} ({r.measurement})" for r in comp.reasons
            ) or "—"
            a(f"| {comp.name} | {comp.points:.1f}/{comp.max_points:.0f} | {rules} |")
        a("")
        a("</details>")
        a("")

    a("## Rejected candidates")
    a("")
    if not report.rejected:
        a("_None._")
    for r in report.rejected:
        a(f"### {r.candidate.ticker} — "
          + (f"{r.score:.0f}/100" if r.score is not None else "no score"))
        a("")
        for reason in r.reasons:
            a(f"- {reason}")
        a("")

    a("## Run notes")
    a("")
    a(f"- Candidates considered: {report.candidates_considered}")
    for n in report.notes:
        a(f"- {n}")
    for f in report.data_quality_flags:
        a(f"- `{f.severity.value}` **{f.code}**: {f.message}")
    a("")
    a("_This system does not place orders. Every recommendation requires human review._")
    return "\n".join(out)


def _list(a, title: str, items: list[str]) -> None:
    items = [i for i in items if i]
    if not items:
        return
    a(f"**{title}**")
    a("")
    for i in items:
        a(f"- {i}")
    a("")


__all__ = ["render"]
