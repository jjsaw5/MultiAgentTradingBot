"""Options Flow Confirmation component.

Deliberate stance: flow is *corroboration*, never proof. A large print is not
inherently bullish or bearish -- it may be a hedge, a roll, or one leg of a
spread. Three guards encode that:

* credit scales with the *share* of directionally-attributed premium above a
  configured floor, so a coin-flip 50/50 tape earns nothing;
* flow that clearly opposes the thesis draws an explicit penalty;
* heavy multi-leg flow adds a caveat and suppresses the sweep credit, because
  sweep counts in multi-leg-dominated tape do not imply direction.

Missing flow scores zero. It never scores "neutral credit".
"""

from __future__ import annotations

from app.models.scoring import ScoreComponent, ScoreReason
from app.scoring.context import ScoringContext

NAME = "options_flow"


def score(ctx: ScoringContext) -> ScoreComponent:
    rules = ctx.methodology.scoring.options_flow
    max_points = ctx.methodology.score_weights.options_flow
    reasons: list[ScoreReason] = []
    missing: list[str] = []

    flow = ctx.flow
    if flow is None:
        return ScoreComponent(
            name=NAME,
            points=rules.insufficient_data_points,
            max_points=max_points,
            reasons=[],
            unscored_due_to_missing_data=[
                "options flow provider unavailable -- no confirmation credit given"
            ],
        )

    bullish = ctx.candidate.direction.sign > 0
    noisy = (flow.multileg_share or 0.0) > 0.45

    # --- directional premium share ----------------------------------------
    share = flow.directional_premium_share
    if share is None:
        missing.append("directional_premium (provider returned no bullish/bearish split)")
    else:
        aligned_share = share if bullish else (1.0 - share)
        floor = rules.directional_premium_floor
        if aligned_share <= rules.contradiction_threshold:
            reasons.append(
                ScoreReason(
                    rule="flow_contradicts_thesis",
                    points=rules.contradiction_penalty,
                    measurement=(
                        f"aligned directional premium share={aligned_share:.2f} "
                        f"(threshold {rules.contradiction_threshold})"
                    ),
                    detail="Tape is positioned against the thesis.",
                )
            )
        elif aligned_share <= floor:
            reasons.append(
                ScoreReason(
                    rule="directional_premium",
                    points=0.0,
                    measurement=f"aligned share={aligned_share:.2f} at or below the {floor} floor",
                )
            )
        else:
            scaled = (aligned_share - floor) / (1.0 - floor)
            reasons.append(
                ScoreReason(
                    rule="directional_premium",
                    points=round(rules.directional_premium_points * scaled, 2),
                    measurement=(
                        f"aligned share={aligned_share:.2f} "
                        f"(bullish={_m(flow.bullish_premium)}, bearish={_m(flow.bearish_premium)})"
                    ),
                )
            )

    # --- side of market ----------------------------------------------------
    ask_share = flow.ask_side_share
    if ask_share is None:
        missing.append("ask_side_share (provider returned no bid/ask attribution)")
    else:
        # Ask-side buying supports a bullish read; for a bearish thesis the
        # supportive signal is premium hitting the bid.
        supportive = ask_share if bullish else (1.0 - ask_share)
        if supportive > rules.ask_side_floor:
            scaled = (supportive - rules.ask_side_floor) / (1.0 - rules.ask_side_floor)
            reasons.append(
                ScoreReason(
                    rule="side_of_market",
                    points=round(rules.ask_side_points * scaled, 2),
                    measurement=(
                        f"{'ask' if bullish else 'bid'}-side share={supportive:.2f}"
                    ),
                )
            )
        else:
            reasons.append(
                ScoreReason(
                    rule="side_of_market",
                    points=0.0,
                    measurement=f"{'ask' if bullish else 'bid'}-side share={supportive:.2f} (not supportive)",
                )
            )

    # --- sweeps ------------------------------------------------------------
    if flow.sweep_count is None:
        missing.append("sweeps (provider returned no sweep count)")
    elif noisy:
        reasons.append(
            ScoreReason(
                rule="sweeps",
                points=0.0,
                measurement=(
                    f"sweep_count={flow.sweep_count} but multileg_share="
                    f"{flow.multileg_share:.2f} -- direction not inferable"
                ),
            )
        )
    elif flow.sweep_count >= rules.sweep_min_count:
        reasons.append(
            ScoreReason(
                rule="sweeps",
                points=rules.sweep_points,
                measurement=f"sweep_count={flow.sweep_count} (>= {rules.sweep_min_count})",
            )
        )
    else:
        reasons.append(
            ScoreReason(
                rule="sweeps",
                points=0.0,
                measurement=f"sweep_count={flow.sweep_count} (below {rules.sweep_min_count})",
            )
        )

    # --- new positioning ---------------------------------------------------
    ratio = flow.volume_oi_ratio
    if ratio is None:
        missing.append("volume_oi_ratio (volume or open interest unavailable)")
    elif ratio >= rules.volume_oi_ratio_new_position:
        reasons.append(
            ScoreReason(
                rule="new_positioning",
                points=rules.new_position_points,
                measurement=f"volume/OI={ratio:.2f} (>= {rules.volume_oi_ratio_new_position})",
                detail="Day's volume exceeds open interest: consistent with new positions.",
            )
        )
    else:
        reasons.append(
            ScoreReason(
                rule="new_positioning",
                points=0.0,
                measurement=f"volume/OI={ratio:.2f} (likely existing positions)",
            )
        )

    # --- net delta flow ----------------------------------------------------
    if flow.net_delta_flow is None:
        missing.append("net_delta_flow (provider returned no greek flow)")
    else:
        consistent = (flow.net_delta_flow > 0) if bullish else (flow.net_delta_flow < 0)
        reasons.append(
            ScoreReason(
                rule="delta_flow_consistency",
                points=rules.delta_flow_points if consistent else 0.0,
                measurement=(
                    f"net_delta_flow={flow.net_delta_flow:,.0f} "
                    f"({'consistent' if consistent else 'inconsistent'} with thesis)"
                ),
            )
        )

    if noisy:
        reasons.append(
            ScoreReason(
                rule="multileg_caveat",
                points=0.0,
                measurement=f"multileg_share={flow.multileg_share:.2f}",
                detail=(
                    "A large share of flow is multi-leg; single-leg directional "
                    "inference is unreliable here."
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


def _m(v: float | None) -> str:
    return f"${v:,.0f}" if v is not None else "n/a"


__all__ = ["NAME", "score"]
