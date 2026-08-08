"""Contract Liquidity component.

Scored on the *worst* leg, not an average: a vertical is only as tradable as
its least liquid side.
"""

from __future__ import annotations

from app.models.scoring import ScoreComponent, ScoreReason
from app.scoring.context import ScoringContext

NAME = "contract_liquidity"


def score(ctx: ScoringContext) -> ScoreComponent:
    rules = ctx.methodology.scoring.contract_liquidity
    max_points = ctx.methodology.score_weights.contract_liquidity
    reasons: list[ScoreReason] = []
    missing: list[str] = []

    trade = ctx.trade
    if trade is None:
        return ScoreComponent(
            name=NAME,
            points=0.0,
            max_points=max_points,
            reasons=[],
            unscored_due_to_missing_data=["no priced trade structure available"],
        )

    # --- bid/ask spread ----------------------------------------------------
    spread = trade.worst_leg_spread_pct
    if spread is None:
        missing.append("bid_ask_spread (a leg is missing a two-sided quote)")
    elif spread <= rules.spread_pct_excellent:
        reasons.append(
            ScoreReason(
                rule="bid_ask_spread",
                points=rules.spread_points_excellent,
                measurement=f"worst-leg spread={spread:.2%} (<= {rules.spread_pct_excellent:.0%})",
            )
        )
    elif spread <= rules.spread_pct_good:
        reasons.append(
            ScoreReason(
                rule="bid_ask_spread",
                points=rules.spread_points_good,
                measurement=f"worst-leg spread={spread:.2%}",
            )
        )
    elif spread <= rules.spread_pct_acceptable:
        reasons.append(
            ScoreReason(
                rule="bid_ask_spread",
                points=rules.spread_points_acceptable,
                measurement=f"worst-leg spread={spread:.2%}",
            )
        )
    else:
        reasons.append(
            ScoreReason(
                rule="bid_ask_spread",
                points=0.0,
                measurement=(
                    f"worst-leg spread={spread:.2%} exceeds the "
                    f"{rules.spread_pct_acceptable:.0%} acceptable ceiling"
                ),
            )
        )

    # --- open interest -----------------------------------------------------
    oi = trade.min_leg_open_interest
    if oi is None:
        missing.append("open_interest (a leg is missing OI)")
    elif oi >= rules.open_interest_strong:
        reasons.append(
            ScoreReason(
                rule="open_interest",
                points=rules.open_interest_points_strong,
                measurement=f"worst-leg OI={oi:,} (>= {rules.open_interest_strong:,})",
            )
        )
    elif oi >= rules.open_interest_ok:
        reasons.append(
            ScoreReason(
                rule="open_interest",
                points=rules.open_interest_points_ok,
                measurement=f"worst-leg OI={oi:,}",
            )
        )
    else:
        reasons.append(
            ScoreReason(
                rule="open_interest",
                points=0.0,
                measurement=f"worst-leg OI={oi:,} (below {rules.open_interest_ok:,})",
            )
        )

    # --- volume ------------------------------------------------------------
    vol = trade.min_leg_volume
    if vol is None:
        missing.append("volume (a leg is missing volume)")
    elif vol >= rules.volume_strong:
        reasons.append(
            ScoreReason(
                rule="contract_volume",
                points=rules.volume_points_strong,
                measurement=f"worst-leg volume={vol:,} (>= {rules.volume_strong:,})",
            )
        )
    elif vol >= rules.volume_ok:
        reasons.append(
            ScoreReason(
                rule="contract_volume",
                points=rules.volume_points_ok,
                measurement=f"worst-leg volume={vol:,}",
            )
        )
    else:
        reasons.append(
            ScoreReason(
                rule="contract_volume",
                points=0.0,
                measurement=f"worst-leg volume={vol:,} (below {rules.volume_ok:,})",
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
