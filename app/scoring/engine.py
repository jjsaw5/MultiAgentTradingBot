"""The deterministic scoring engine.

No LLM is involved past this point. Given a :class:`ScoringContext` -- which is
entirely composed of measured data plus the agents' *structured* findings --
this produces the same score every time.
"""

from __future__ import annotations

from collections.abc import Callable

from app.models.enums import Classification
from app.models.scoring import ScoreBreakdown, ScoreComponent, ScoredCandidate
from app.rules import hard_rejections
from app.scoring.components import (
    alignment,
    catalyst,
    data_quality,
    flow,
    iv_greeks,
    liquidity,
    risk_reward,
    technical,
)
from app.scoring.context import ScoringContext

ComponentFn = Callable[[ScoringContext], ScoreComponent]

#: Order matters only for presentation; scores are independent of it.
COMPONENTS: list[ComponentFn] = [
    catalyst.score,
    alignment.score,
    technical.score,
    flow.score,
    iv_greeks.score,
    liquidity.score,
    risk_reward.score,
    data_quality.score,
]


def score_candidate(
    ctx: ScoringContext,
    *,
    components: list[ComponentFn] | None = None,
    run_hard_rules: bool = True,
) -> ScoreBreakdown:
    breakdown = ScoreBreakdown(
        run_id=ctx.candidate.run_id,
        candidate_id=ctx.candidate.candidate_id,
        structure_id=ctx.trade.structure_id if ctx.trade else None,
        methodology_version=ctx.methodology.version,
        methodology_fingerprint=ctx.methodology.fingerprint(),
        components=[fn(ctx) for fn in (components or COMPONENTS)],
        hard_rejections=hard_rejections.evaluate(ctx) if run_hard_rules else [],
    )
    return breakdown


def classify(breakdown: ScoreBreakdown, ctx: ScoringContext) -> ScoredCandidate:
    """Turn a raw breakdown into a ranked, classified candidate.

    A hard rejection forces ``REJECTED`` regardless of the numeric total. The
    total is still reported, because "82 points but untradable spread" is more
    useful feedback than a bare rejection.
    """
    m = ctx.methodology
    band = m.classify(breakdown.total)
    classification = Classification(band.name)
    label = band.label
    reasons: list[str] = []

    if breakdown.hard_rejected:
        classification = Classification.REJECTED
        label = "Reject (hard rule)"
        reasons.extend(hr.message for hr in breakdown.hard_rejections)
    elif breakdown.total < m.min_presentable_score:
        classification = Classification.REJECTED
        reasons.append(
            f"Score {breakdown.total:.1f} is below the {m.min_presentable_score:.0f} "
            "minimum presentable score."
        )

    # A weak component is worth naming even on trades that pass, so the human
    # reviewer knows where the score came from.
    for comp in breakdown.components:
        if comp.pct < 40 and comp.max_points >= 10:
            reasons.append(
                f"Weak {comp.name}: {comp.points:.1f}/{comp.max_points:.0f} ({comp.pct:.0f}%)"
            )

    return ScoredCandidate(
        run_id=breakdown.run_id,
        candidate_id=breakdown.candidate_id,
        ticker=ctx.candidate.ticker,
        breakdown=breakdown,
        classification=classification,
        classification_label=label,
        presentable=classification is not Classification.REJECTED,
        rejection_summary=reasons,
    )


def rank(scored: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """Rank presentable candidates by score, then by ticker for stability."""
    presentable = [s for s in scored if s.presentable]
    presentable.sort(key=lambda s: (-s.breakdown.total, s.ticker))
    for i, s in enumerate(presentable, start=1):
        s.rank = i
    return presentable


__all__ = ["COMPONENTS", "classify", "rank", "score_candidate"]
