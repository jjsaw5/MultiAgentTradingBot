"""Risk / Reward component."""

from __future__ import annotations

from app.models.scoring import ScoreComponent, ScoreReason
from app.scoring.context import ScoringContext

NAME = "risk_reward"


def score(ctx: ScoringContext) -> ScoreComponent:
    rules = ctx.methodology.scoring.risk_reward
    limits = ctx.methodology.hard_rejections
    max_points = ctx.methodology.score_weights.risk_reward
    reasons: list[ScoreReason] = []
    missing: list[str] = []

    trade, rr = ctx.trade, ctx.risk_reward
    if trade is None or rr is None:
        return ScoreComponent(
            name=NAME,
            points=0.0,
            max_points=max_points,
            reasons=[],
            unscored_due_to_missing_data=["no priced trade structure available"],
        )

    # --- reward to risk ----------------------------------------------------
    if rr.reward_to_risk is None:
        missing.append("reward_to_risk (could not be modelled from available greeks)")
    else:
        awarded = 0.0
        band_note = "below the lowest configured band"
        for band in rules.rr_bands:
            if rr.reward_to_risk >= band.min:
                awarded, band_note = band.points, f">= {band.min}"
                break
        reasons.append(
            ScoreReason(
                rule="reward_to_risk",
                points=awarded,
                measurement=f"R:R={rr.reward_to_risk:.2f} ({band_note})",
                detail=(
                    f"Modelled value ${rr.expected_value_at_target:,.0f} at target "
                    f"{rr.target_underlying_price:.2f} against ${rr.max_loss:,.0f} at risk."
                )
                if rr.expected_value_at_target is not None
                else None,
            )
        )

    # --- absolute size -----------------------------------------------------
    budget = limits.max_premium_per_trade_usd
    within = rr.max_loss <= budget
    reasons.append(
        ScoreReason(
            rule="max_loss_within_budget",
            points=rules.max_loss_within_budget_points if within else 0.0,
            measurement=f"max loss ${rr.max_loss:,.0f} vs budget ${budget:,.0f}",
        )
    )

    # --- how much of the expected move is spent just getting to breakeven? --
    expected = ctx.candidate.expected_move.percent
    if expected <= 0:
        missing.append("breakeven_vs_expected_move (no expected move stated)")
    else:
        ratio = rr.breakeven_move_pct / expected
        if ratio <= rules.breakeven_vs_expected_move_full:
            pts, note = rules.breakeven_points, "comfortable"
        elif ratio <= rules.breakeven_vs_expected_move_partial:
            pts, note = rules.breakeven_points_partial, "tight"
        else:
            pts, note = 0.0, "breakeven consumes most or all of the expected move"
        reasons.append(
            ScoreReason(
                rule="breakeven_vs_expected_move",
                points=pts,
                measurement=(
                    f"breakeven needs {rr.breakeven_move_pct:.2f}% vs {expected:.2f}% expected "
                    f"(ratio {ratio:.2f})"
                ),
                detail=note,
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


__all__ = ["NAME", "score"]
