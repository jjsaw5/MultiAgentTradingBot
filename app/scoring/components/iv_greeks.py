"""IV / Greeks Structure component.

Long-premium trades are short volatility risk: buying a 90th-percentile IV rank
and being right on direction can still lose. This component prices that in,
using different IV-rank bands for single-leg longs and for debit verticals
(which are partly hedged against an IV contraction by the short leg).
"""

from __future__ import annotations

from app.config.methodology import IVRankBand
from app.models.scoring import ScoreComponent, ScoreReason
from app.scoring.context import ScoringContext
from app.services.pricing import implied_move_pct

NAME = "iv_greeks"


def _band_points(bands: list[IVRankBand], value: float) -> tuple[float, IVRankBand]:
    for band in bands:
        if value < band.max:
            return band.points, band
    return bands[-1].points, bands[-1]


def score(ctx: ScoringContext) -> ScoreComponent:
    rules = ctx.methodology.scoring.iv_greeks
    max_points = ctx.methodology.score_weights.iv_greeks
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

    long_c = trade.long_leg.contract
    is_spread = ctx.candidate.strategy_type.is_spread
    bands = rules.iv_rank_bands_spread if is_spread else rules.iv_rank_bands_long_premium

    # --- IV rank -----------------------------------------------------------
    iv_rank = long_c.iv_rank
    if iv_rank is None and ctx.flow is not None:
        iv_rank = ctx.flow.iv_rank
    if iv_rank is None:
        missing.append("iv_rank (neither the chain nor the flow provider supplied it)")
    else:
        pts, band = _band_points(bands, iv_rank)
        reasons.append(
            ScoreReason(
                rule="iv_rank",
                points=pts,
                measurement=f"iv_rank={iv_rank:.0f} (band <{band.max:.0f})",
                detail=(
                    "Debit vertical: partially hedged against IV contraction."
                    if is_spread
                    else "Long premium: exposed to IV contraction."
                ),
            )
        )

    # --- does the thesis move agree with what options imply? ---------------
    iv = long_c.implied_volatility
    dte = long_c.dte(ctx.trading_day)
    if iv is None:
        missing.append("iv_vs_expected_move (contract IV unavailable)")
    else:
        implied = implied_move_pct(iv=iv, days=min(dte, ctx.holding_days))
        if implied <= 0:
            missing.append("iv_vs_expected_move (degenerate implied move)")
        else:
            ratio = ctx.candidate.expected_move.percent / implied
            within = abs(ratio - 1.0) <= rules.iv_expected_move_tolerance
            reasons.append(
                ScoreReason(
                    rule="iv_vs_expected_move",
                    points=rules.iv_expected_move_points if within else 0.0,
                    measurement=(
                        f"thesis {ctx.candidate.expected_move.percent:.1f}% vs "
                        f"IV-implied {implied:.1f}% over {min(dte, ctx.holding_days)}d "
                        f"(ratio {ratio:.2f})"
                    ),
                    detail=(
                        "Thesis is in line with what the options market already prices."
                        if within
                        else "Thesis and the options market disagree on magnitude."
                    ),
                )
            )

    # --- theta burden ------------------------------------------------------
    net_theta = trade.net_theta
    premium = trade.net_debit_conservative * 100
    if net_theta is None or premium <= 0:
        missing.append("theta_burden (net theta or premium unavailable)")
    else:
        burn = abs(net_theta) * ctx.holding_days
        pct = burn / premium
        reasons.append(
            ScoreReason(
                rule="theta_burden",
                points=rules.theta_burden_points if pct <= rules.theta_burden_max_pct_of_premium else 0.0,
                measurement=(
                    f"theta {net_theta:.2f}/day x {ctx.holding_days}d = ${burn:.0f} "
                    f"= {pct:.0%} of ${premium:.0f} premium "
                    f"(limit {rules.theta_burden_max_pct_of_premium:.0%})"
                ),
            )
        )

    # --- vega exposure -----------------------------------------------------
    net_vega = trade.net_vega
    if net_vega is None or premium <= 0:
        missing.append("vega_exposure (net vega unavailable)")
    else:
        pct_per_pt = abs(net_vega) / premium
        reasons.append(
            ScoreReason(
                rule="vega_exposure",
                points=rules.vega_points if pct_per_pt <= rules.vega_max_pct_of_premium_per_iv_pt else 0.0,
                measurement=(
                    f"net vega {net_vega:.2f} = {pct_per_pt:.1%} of premium per IV point "
                    f"(limit {rules.vega_max_pct_of_premium_per_iv_pt:.0%})"
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


__all__ = ["NAME", "score"]
