"""Catalyst Strength component.

Question answered: *is there a real, timely, material reason this underlying
should move during the intended holding period?*
"""

from __future__ import annotations

from app.models.enums import EvidenceQuality, ValidationVerdict
from app.models.scoring import ScoreComponent, ScoreReason
from app.scoring.context import ScoringContext

NAME = "catalyst_strength"


def score(ctx: ScoringContext) -> ScoreComponent:
    rules = ctx.methodology.scoring.catalyst_strength
    max_points = ctx.methodology.score_weights.catalyst_strength
    reasons: list[ScoreReason] = []
    missing: list[str] = []

    cat = ctx.validation.catalyst
    primary = ctx.candidate.primary_catalyst

    # --- materiality / importance ----------------------------------------
    brief_catalysts = ctx.brief.catalysts_for(ctx.candidate.ticker)
    matched = next(
        (c for c in brief_catalysts if c.catalyst_type is primary.catalyst_type), None
    )
    if matched is not None:
        pts = round(rules.importance_points * matched.importance_score, 2)
        reasons.append(
            ScoreReason(
                rule="catalyst_importance",
                points=pts,
                measurement=f"importance_score={matched.importance_score:.2f}",
                detail=f"{matched.catalyst_type.value}: {matched.headline[:90]}",
            )
        )
    else:
        missing.append("catalyst_importance (no matching catalyst in the MarketBrief)")

    # --- evidence quality --------------------------------------------------
    quality = matched.evidence_quality if matched else EvidenceQuality.UNVERIFIED
    q_pts = rules.evidence_quality_points.get(quality.value, 0.0)
    reasons.append(
        ScoreReason(
            rule="evidence_quality",
            points=q_pts,
            measurement=f"evidence_quality={quality.value}",
        )
    )

    # --- recency -----------------------------------------------------------
    if cat.days_since_published is None:
        missing.append("catalyst_recency (no published_at timestamp available)")
    elif cat.days_since_published <= rules.recency_days_full:
        reasons.append(
            ScoreReason(
                rule="catalyst_recency",
                points=rules.recency_points_full,
                measurement=f"days_since_published={cat.days_since_published}",
            )
        )
    elif cat.days_since_published <= rules.recency_days_partial:
        reasons.append(
            ScoreReason(
                rule="catalyst_recency",
                points=rules.recency_points_partial,
                measurement=f"days_since_published={cat.days_since_published}",
            )
        )
    else:
        reasons.append(
            ScoreReason(
                rule="catalyst_recency",
                points=0.0,
                measurement=f"days_since_published={cat.days_since_published} (stale)",
            )
        )

    # --- forward timing relevance -----------------------------------------
    if cat.has_upcoming_timing_relevance and cat.days_until_catalyst is not None:
        if 0 <= cat.days_until_catalyst <= ctx.holding_days:
            reasons.append(
                ScoreReason(
                    rule="catalyst_timing",
                    points=rules.timing_points,
                    measurement=(
                        f"days_until_catalyst={cat.days_until_catalyst} within "
                        f"{ctx.holding_days}-day holding period"
                    ),
                )
            )
        else:
            reasons.append(
                ScoreReason(
                    rule="catalyst_timing",
                    points=0.0,
                    measurement=(
                        f"days_until_catalyst={cat.days_until_catalyst} outside the "
                        f"{ctx.holding_days}-day holding period"
                    ),
                )
            )
    elif cat.verdict is ValidationVerdict.CONFIRMED:
        # A confirmed but already-occurred catalyst still counts for something:
        # the move may still be developing. It just gets no timing credit.
        reasons.append(
            ScoreReason(
                rule="catalyst_timing",
                points=0.0,
                measurement="catalyst has already occurred; no forward timing credit",
            )
        )
    else:
        missing.append("catalyst_timing (no dated forward event)")

    # --- priced in ---------------------------------------------------------
    if cat.already_priced_in:
        reasons.append(
            ScoreReason(
                rule="already_priced_in",
                points=rules.priced_in_penalty,
                measurement="validator concluded the catalyst is largely priced in",
            )
        )

    # --- supporting catalysts ---------------------------------------------
    n_support = len(ctx.candidate.supporting_catalysts)
    if n_support:
        pts = min(
            rules.supporting_catalyst_points_cap,
            n_support * rules.supporting_catalyst_points_each,
        )
        reasons.append(
            ScoreReason(
                rule="supporting_catalysts",
                points=pts,
                measurement=f"supporting_catalyst_count={n_support}",
            )
        )

    # --- validator override -----------------------------------------------
    if cat.verdict is ValidationVerdict.CONTRADICTED:
        # A contradicted catalyst zeroes the component. The hard-rejection
        # engine handles removing the trade entirely; this keeps the score
        # honest in the meantime.
        reasons.append(
            ScoreReason(
                rule="catalyst_contradicted",
                points=-sum(r.points for r in reasons),
                measurement="validator verdict=CONTRADICTED; component zeroed",
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
