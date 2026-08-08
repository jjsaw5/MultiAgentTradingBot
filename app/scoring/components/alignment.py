"""Market / Sector Alignment component.

Question answered: *is this trade swimming with the tide or against it?*
"""

from __future__ import annotations

from app.models.scoring import ScoreComponent, ScoreReason
from app.scoring.context import ScoringContext

NAME = "market_alignment"


def score(ctx: ScoringContext) -> ScoreComponent:
    rules = ctx.methodology.scoring.market_alignment
    max_points = ctx.methodology.score_weights.market_alignment
    reasons: list[ScoreReason] = []
    missing: list[str] = []

    direction = ctx.candidate.direction.sign
    align = ctx.validation.alignment

    # --- SPY ---------------------------------------------------------------
    spy_sign = ctx.brief.spy.bias.sign
    if spy_sign == 0:
        pts, note = rules.spy_neutral_points, "neutral"
    elif spy_sign == direction:
        pts, note = rules.spy_aligned_points, "aligned"
    else:
        pts, note = rules.spy_fighting_points, "fighting"
    reasons.append(
        ScoreReason(
            rule="spy_alignment",
            points=pts,
            measurement=f"SPY bias={ctx.brief.spy.bias.value}, trade={ctx.candidate.direction.value} ({note})",
        )
    )

    # --- QQQ ---------------------------------------------------------------
    qqq_sign = ctx.brief.qqq.bias.sign
    if qqq_sign == 0:
        pts, note = rules.qqq_neutral_points, "neutral"
    elif qqq_sign == direction:
        pts, note = rules.qqq_aligned_points, "aligned"
    else:
        pts, note = rules.qqq_fighting_points, "fighting"
    reasons.append(
        ScoreReason(
            rule="qqq_alignment",
            points=pts,
            measurement=f"QQQ bias={ctx.brief.qqq.bias.value} ({note})",
        )
    )

    # --- sector ------------------------------------------------------------
    sector_bias = ctx.brief.sector_bias(ctx.candidate.sector)
    if sector_bias is None:
        missing.append(f"sector_alignment (no MarketBrief observation for {ctx.candidate.sector})")
    else:
        s_sign = sector_bias.sign
        if s_sign == direction and s_sign != 0:
            reasons.append(
                ScoreReason(
                    rule="sector_alignment",
                    points=rules.sector_aligned_points,
                    measurement=f"{ctx.candidate.sector} bias={sector_bias.value} (aligned)",
                )
            )
        elif s_sign != 0 and s_sign != direction:
            reasons.append(
                ScoreReason(
                    rule="sector_alignment",
                    points=rules.sector_fighting_penalty,
                    measurement=f"{ctx.candidate.sector} bias={sector_bias.value} (fighting)",
                )
            )
        else:
            reasons.append(
                ScoreReason(
                    rule="sector_alignment",
                    points=0.0,
                    measurement=f"{ctx.candidate.sector} bias=NEUTRAL",
                )
            )

    # --- relative strength -------------------------------------------------
    rs = align.relative_strength_20d
    if rs is None and ctx.technicals is not None:
        rs = ctx.technicals.relative_strength_20d_vs_spy
    if rs is None:
        missing.append("relative_strength (no benchmark series available)")
    else:
        # Bullish trades want positive relative strength; bearish trades want
        # relative weakness. Same threshold, mirrored.
        favourable = (rs > rules.relative_strength_threshold) if direction > 0 else (
            rs < -rules.relative_strength_threshold
        )
        reasons.append(
            ScoreReason(
                rule="relative_strength_20d",
                points=rules.relative_strength_points if favourable else 0.0,
                measurement=f"RS20 vs SPY={rs:+.2f}pp ({'favourable' if favourable else 'unfavourable'})",
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
