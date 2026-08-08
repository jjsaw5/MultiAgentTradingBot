"""Data Agreement / Data Quality component.

A recommendation built on one provider's word, or on quotes from yesterday, is
worth less than the same recommendation corroborated by two live sources. This
component makes that explicit rather than leaving it as a footnote.
"""

from __future__ import annotations

from app.models.scoring import ScoreComponent, ScoreReason
from app.scoring.context import ScoringContext

NAME = "data_quality"


def score(ctx: ScoringContext) -> ScoreComponent:
    rules = ctx.methodology.scoring.data_quality
    max_points = ctx.methodology.score_weights.data_quality
    reasons: list[ScoreReason] = []
    missing: list[str] = []

    # --- provider coverage -------------------------------------------------
    expected = set(ctx.providers_expected)
    responded = set(ctx.providers_responded)
    if not expected:
        missing.append("provider_coverage (no provider expectations recorded)")
    else:
        gaps = sorted(expected - responded)
        reasons.append(
            ScoreReason(
                rule="provider_coverage",
                points=rules.all_providers_responded_points if not gaps else 0.0,
                measurement=(
                    f"{len(responded & expected)}/{len(expected)} expected providers responded"
                    + (f"; missing {', '.join(gaps)}" if gaps else "")
                ),
            )
        )

    # --- cross-provider price agreement ------------------------------------
    check = ctx.validation.price_check
    if check.max_disagreement_pct is None or len(check.prices_by_provider) < 2:
        missing.append("price_agreement (fewer than two independent price sources)")
    else:
        ok = check.max_disagreement_pct <= rules.price_agreement_tolerance_pct
        reasons.append(
            ScoreReason(
                rule="price_agreement",
                points=rules.price_agreement_points if ok else 0.0,
                measurement=(
                    f"max cross-provider disagreement={check.max_disagreement_pct:.3%} "
                    f"(tolerance {rules.price_agreement_tolerance_pct:.2%}) across "
                    + ", ".join(f"{k}={v:.2f}" for k, v in sorted(check.prices_by_provider.items()))
                ),
            )
        )

    # --- freshness ---------------------------------------------------------
    reasons.append(
        ScoreReason(
            rule="data_freshness",
            points=rules.freshness_points if not ctx.stale_inputs else 0.0,
            measurement=(
                "all inputs within the session freshness limit"
                if not ctx.stale_inputs
                else f"stale inputs: {', '.join(ctx.stale_inputs)}"
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
