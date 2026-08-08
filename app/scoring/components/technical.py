"""Technical Setup component -- the largest single weight in the model.

Every rule reads a measured indicator from :class:`TechnicalSnapshot`. Adding a
new indicator to the framework means adding a block here; nothing else changes.
"""

from __future__ import annotations

from app.models.scoring import ScoreComponent, ScoreReason
from app.scoring.context import ScoringContext

NAME = "technical_setup"


def score(ctx: ScoringContext) -> ScoreComponent:
    rules = ctx.methodology.scoring.technical_setup
    max_points = ctx.methodology.score_weights.technical_setup
    reasons: list[ScoreReason] = []
    missing: list[str] = []

    tech = ctx.technicals
    if tech is None:
        return ScoreComponent(
            name=NAME,
            points=0.0,
            max_points=max_points,
            reasons=[],
            unscored_due_to_missing_data=["technical_snapshot unavailable -- no credit given"],
        )

    bullish = ctx.candidate.direction.sign > 0
    price = tech.price

    # --- trend alignment ---------------------------------------------------
    if tech.sma20 is None or tech.sma50 is None:
        missing.append("trend_alignment (insufficient history for SMA20/SMA50)")
    else:
        stacked = (
            price > tech.sma20 > tech.sma50 if bullish else price < tech.sma20 < tech.sma50
        )
        partial = price > tech.sma20 if bullish else price < tech.sma20
        if stacked:
            reasons.append(
                ScoreReason(
                    rule="trend_alignment",
                    points=rules.trend_alignment_points,
                    measurement=f"price={price:.2f} sma20={tech.sma20:.2f} sma50={tech.sma50:.2f} (fully stacked)",
                )
            )
        elif partial:
            reasons.append(
                ScoreReason(
                    rule="trend_alignment",
                    points=rules.trend_partial_points,
                    measurement=f"price={price:.2f} vs sma20={tech.sma20:.2f} only (partial)",
                )
            )
        else:
            reasons.append(
                ScoreReason(
                    rule="trend_alignment",
                    points=0.0,
                    measurement=f"price={price:.2f} on the wrong side of sma20={tech.sma20:.2f}",
                )
            )

    # --- key level respected ----------------------------------------------
    level = tech.support if bullish else tech.resistance
    if level is None:
        missing.append("key_level (no swing level detected in lookback window)")
    else:
        dist_pct = abs(price - level) / price * 100
        respected = (price > level) if bullish else (price < level)
        reasons.append(
            ScoreReason(
                rule="key_level_respected",
                points=rules.key_level_points if respected else 0.0,
                measurement=(
                    f"{'support' if bullish else 'resistance'}={level:.2f}, "
                    f"price={price:.2f} ({dist_pct:.2f}% away, "
                    f"{'holding' if respected else 'broken'})"
                ),
            )
        )

    # --- relative volume ---------------------------------------------------
    rvol = tech.relative_volume
    if rvol is None:
        missing.append("relative_volume (volume or average volume unavailable)")
    elif rvol >= rules.relative_volume_strong:
        reasons.append(
            ScoreReason(
                rule="relative_volume",
                points=rules.relative_volume_points_strong,
                measurement=f"rvol={rvol:.2f}",
            )
        )
    elif rvol >= rules.relative_volume_ok:
        reasons.append(
            ScoreReason(
                rule="relative_volume",
                points=rules.relative_volume_points_ok,
                measurement=f"rvol={rvol:.2f}",
            )
        )
    elif rvol < rules.relative_volume_weak:
        reasons.append(
            ScoreReason(
                rule="relative_volume",
                points=rules.relative_volume_penalty_weak,
                measurement=f"rvol={rvol:.2f} (below {rules.relative_volume_weak})",
            )
        )
    else:
        reasons.append(
            ScoreReason(
                rule="relative_volume",
                points=0.0,
                measurement=f"rvol={rvol:.2f} (unremarkable)",
            )
        )

    # --- momentum ----------------------------------------------------------
    if tech.rsi14 is None:
        missing.append("momentum (RSI unavailable)")
    else:
        lo, hi = rules.rsi_bull_range if bullish else rules.rsi_bear_range
        in_zone = lo <= tech.rsi14 <= hi
        macd_ok = (
            tech.macd is not None
            and tech.macd_signal is not None
            and ((tech.macd > tech.macd_signal) if bullish else (tech.macd < tech.macd_signal))
        )
        if in_zone and macd_ok:
            pts = rules.momentum_points
            note = "RSI in zone and MACD confirming"
        elif in_zone or macd_ok:
            pts = rules.momentum_points / 2
            note = "RSI in zone" if in_zone else "MACD confirming"
        else:
            pts = 0.0
            note = "no momentum confirmation"
        reasons.append(
            ScoreReason(
                rule="momentum",
                points=pts,
                measurement=f"RSI14={tech.rsi14:.1f} zone=[{lo},{hi}], MACD={_fmt(tech.macd)}/{_fmt(tech.macd_signal)}",
                detail=note,
            )
        )

        overextended = (
            tech.rsi14 >= rules.rsi_overextended_bull
            if bullish
            else tech.rsi14 <= rules.rsi_overextended_bear
        )
        if overextended:
            reasons.append(
                ScoreReason(
                    rule="overextension_penalty",
                    points=rules.overextension_penalty,
                    measurement=f"RSI14={tech.rsi14:.1f} (chasing an extended move)",
                )
            )

    # --- blocking level proximity -----------------------------------------
    blocker = tech.resistance if bullish else tech.support
    if blocker is None:
        missing.append("blocking_level (no opposing swing level detected)")
    else:
        dist_pct = abs(blocker - price) / price * 100
        beyond = (blocker > price) if bullish else (blocker < price)
        if beyond and dist_pct <= rules.blocking_level_pct_severe:
            reasons.append(
                ScoreReason(
                    rule="blocking_level_proximity",
                    points=rules.blocking_level_penalty_severe,
                    measurement=(
                        f"{'resistance' if bullish else 'support'}={blocker:.2f} "
                        f"only {dist_pct:.2f}% away"
                    ),
                )
            )
        elif beyond and dist_pct <= rules.blocking_level_pct_moderate:
            reasons.append(
                ScoreReason(
                    rule="blocking_level_proximity",
                    points=rules.blocking_level_penalty_moderate,
                    measurement=(
                        f"{'resistance' if bullish else 'support'}={blocker:.2f} "
                        f"{dist_pct:.2f}% away"
                    ),
                )
            )
        else:
            reasons.append(
                ScoreReason(
                    rule="blocking_level_proximity",
                    points=0.0,
                    measurement=f"nearest opposing level {dist_pct:.2f}% away (clear runway)",
                )
            )

    # --- is the expected move physically plausible? ------------------------
    if tech.atr_pct is None:
        missing.append("atr_feasibility (ATR unavailable)")
    else:
        # Expected move is compared against ATR scaled by sqrt(time), which is
        # the standard way to project a daily range over a holding period.
        projected = tech.atr_pct * (ctx.holding_days**0.5)
        ratio = ctx.candidate.expected_move.percent / projected if projected else None
        if ratio is None:
            missing.append("atr_feasibility (zero projected range)")
        elif ratio <= rules.atr_feasibility_max_multiple:
            reasons.append(
                ScoreReason(
                    rule="expected_move_feasible",
                    points=rules.atr_feasibility_points,
                    measurement=(
                        f"expected {ctx.candidate.expected_move.percent:.1f}% vs "
                        f"{projected:.1f}% ATR-projected over {ctx.holding_days}d "
                        f"(ratio {ratio:.2f})"
                    ),
                )
            )
        else:
            reasons.append(
                ScoreReason(
                    rule="expected_move_feasible",
                    points=0.0,
                    measurement=(
                        f"expected {ctx.candidate.expected_move.percent:.1f}% is "
                        f"{ratio:.2f}x the ATR-projected {projected:.1f}% -- implausible"
                    ),
                )
            )

    total = min(max_points, max(0.0, sum(r.points for r in reasons)))
    return ScoreComponent(
        name=NAME,
        points=round(total, 2),
        max_points=max_points,
        reasons=reasons,
        unscored_due_to_missing_data=missing,
    )


def _fmt(v: float | None) -> str:
    return f"{v:.3f}" if v is not None else "n/a"


__all__ = ["NAME", "score"]
