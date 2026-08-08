"""Hard rejection rules engine.

Separate from scoring by design. A hard failure is a veto: no amount of
catalyst strength, flow confirmation, or technical beauty can outvote an
untradable spread or a missing price. The engine returns *every* failure it
finds rather than short-circuiting, so the report can explain the full picture.

Each rule is a small function of the :class:`ScoringContext`. To add one,
write the function and append it to :data:`RULES`.
"""

from __future__ import annotations

from collections.abc import Callable

from app.models.enums import CatalystType, RejectionReasonCode, ValidationVerdict
from app.models.scoring import HardRejection
from app.scoring.context import ScoringContext

Rule = Callable[[ScoringContext], HardRejection | None]


def rule_strategy_allowed(ctx: ScoringContext) -> HardRejection | None:
    allowed = ctx.methodology.strategies.allowed
    if ctx.candidate.strategy_type.value not in allowed:
        return HardRejection(
            code=RejectionReasonCode.STRATEGY_NOT_ALLOWED,
            rule="strategy_allowed",
            message=f"{ctx.candidate.strategy_type.value} is not in the allowed strategy set.",
            measurement=ctx.candidate.strategy_type.value,
            threshold=", ".join(allowed),
        )
    return None


def rule_tradable_contract(ctx: ScoringContext) -> HardRejection | None:
    if ctx.trade is None:
        return HardRejection(
            code=RejectionReasonCode.NO_TRADABLE_CONTRACT,
            rule="tradable_contract",
            message="No contract satisfying the selection rules could be assembled.",
        )
    return None


def rule_required_fields(ctx: ScoringContext) -> HardRejection | None:
    required = ctx.methodology.hard_rejections.required_fields
    absent: list[str] = []

    if "underlying_price" in required and (ctx.quote is None or ctx.quote.price is None):
        absent.append("underlying_price")
    if ctx.trade is not None:
        for leg in ctx.trade.legs:
            c = leg.contract
            if "option_bid" in required and c.bid is None:
                absent.append(f"option_bid[{c.symbol}]")
            if "option_ask" in required and c.ask is None:
                absent.append(f"option_ask[{c.symbol}]")
            if "expiration" in required and c.expiration is None:
                absent.append(f"expiration[{c.symbol}]")
            if "strike" in required and c.strike is None:
                absent.append(f"strike[{c.symbol}]")

    if absent:
        return HardRejection(
            code=RejectionReasonCode.MISSING_CRITICAL_DATA,
            rule="required_fields",
            message=f"Critical market data is missing: {', '.join(sorted(set(absent)))}.",
            measurement=", ".join(sorted(set(absent))),
            threshold=", ".join(required),
        )
    return None


def rule_spread_width(ctx: ScoringContext) -> HardRejection | None:
    if ctx.trade is None:
        return None
    limit = ctx.methodology.hard_rejections.max_bid_ask_spread_pct
    worst = ctx.trade.worst_leg_spread_pct
    if worst is None:
        return None  # covered by rule_required_fields
    if worst > limit:
        return HardRejection(
            code=RejectionReasonCode.SPREAD_TOO_WIDE,
            rule="max_bid_ask_spread_pct",
            message=f"Worst-leg bid/ask spread of {worst:.2%} exceeds the {limit:.0%} limit.",
            measurement=f"{worst:.4f}",
            threshold=f"{limit:.4f}",
        )
    return None


def rule_min_volume(ctx: ScoringContext) -> HardRejection | None:
    if ctx.trade is None:
        return None
    limit = ctx.methodology.hard_rejections.min_option_volume
    vol = ctx.trade.min_leg_volume
    if vol is not None and vol < limit:
        return HardRejection(
            code=RejectionReasonCode.INSUFFICIENT_VOLUME,
            rule="min_option_volume",
            message=f"Worst-leg volume of {vol:,} is below the {limit:,} minimum.",
            measurement=str(vol),
            threshold=str(limit),
        )
    return None


def rule_min_open_interest(ctx: ScoringContext) -> HardRejection | None:
    if ctx.trade is None:
        return None
    limit = ctx.methodology.hard_rejections.min_open_interest
    oi = ctx.trade.min_leg_open_interest
    if oi is not None and oi < limit:
        return HardRejection(
            code=RejectionReasonCode.INSUFFICIENT_OPEN_INTEREST,
            rule="min_open_interest",
            message=f"Worst-leg open interest of {oi:,} is below the {limit:,} minimum.",
            measurement=str(oi),
            threshold=str(limit),
        )
    return None


def rule_reward_to_risk(ctx: ScoringContext) -> HardRejection | None:
    if ctx.risk_reward is None:
        return None
    limit = ctx.methodology.hard_rejections.min_reward_to_risk
    rr = ctx.risk_reward.reward_to_risk
    if rr is None:
        return None
    if rr < limit:
        return HardRejection(
            code=RejectionReasonCode.REWARD_RISK_TOO_LOW,
            rule="min_reward_to_risk",
            message=f"Modelled reward/risk of {rr:.2f} is below the {limit:.2f} minimum.",
            measurement=f"{rr:.3f}",
            threshold=f"{limit:.2f}",
        )
    return None


def rule_premium_budget(ctx: ScoringContext) -> HardRejection | None:
    if ctx.trade is None:
        return None
    limit = ctx.methodology.hard_rejections.max_premium_per_trade_usd
    cost = ctx.trade.max_loss
    if cost > limit:
        return HardRejection(
            code=RejectionReasonCode.PREMIUM_EXCEEDS_LIMIT,
            rule="max_premium_per_trade_usd",
            message=f"Trade cost of ${cost:,.0f} exceeds the ${limit:,.0f} per-trade limit.",
            measurement=f"{cost:.2f}",
            threshold=f"{limit:.2f}",
        )
    return None


def rule_theta_burn(ctx: ScoringContext) -> HardRejection | None:
    if ctx.risk_reward is None or ctx.risk_reward.theta_burn_pct_of_premium is None:
        return None
    limit = ctx.methodology.hard_rejections.max_theta_burn_pct_of_premium
    pct = ctx.risk_reward.theta_burn_pct_of_premium
    if pct > limit:
        return HardRejection(
            code=RejectionReasonCode.EXCESSIVE_THETA,
            rule="max_theta_burn_pct_of_premium",
            message=(
                f"Theta would consume {pct:.0%} of the premium over the "
                f"{ctx.holding_days}-day holding period (limit {limit:.0%})."
            ),
            measurement=f"{pct:.4f}",
            threshold=f"{limit:.2f}",
        )
    return None


def rule_catalyst_validated(ctx: ScoringContext) -> HardRejection | None:
    if not ctx.methodology.hard_rejections.require_validated_catalyst:
        return None
    verdict = ctx.validation.catalyst.verdict
    if verdict in (ValidationVerdict.CONTRADICTED, ValidationVerdict.DATA_UNAVAILABLE) or (
        ctx.validation.catalyst.exists is False
    ):
        return HardRejection(
            code=RejectionReasonCode.CATALYST_NOT_VALIDATED,
            rule="require_validated_catalyst",
            message=f"The primary catalyst could not be validated (verdict={verdict.value}).",
            measurement=verdict.value,
        )
    return None


def rule_earnings_blackout(ctx: ScoringContext) -> HardRejection | None:
    """Reject when an earnings print lands inside the trade's life.

    Exception: if the trade *is* the earnings trade and the config permits it,
    the event is the thesis rather than an uncontrolled risk.
    """
    rules = ctx.methodology.hard_rejections
    earnings_date = ctx.earnings.event_date if ctx.earnings else ctx.candidate.earnings_date
    if earnings_date is None:
        return None

    days_until = (earnings_date - ctx.trading_day).days
    window_start = -rules.earnings_blackout_days_after
    window_end = ctx.holding_days + rules.earnings_blackout_days_before
    inside = window_start <= days_until <= window_end
    if not inside:
        return None

    is_earnings_thesis = (
        ctx.candidate.primary_catalyst.catalyst_type is CatalystType.EARNINGS
    )
    if is_earnings_thesis and rules.allow_earnings_when_catalyst_is_earnings:
        return None

    return HardRejection(
        code=RejectionReasonCode.EARNINGS_BLACKOUT,
        rule="earnings_blackout",
        message=(
            f"Earnings on {earnings_date} falls {days_until} days out, inside the "
            f"{ctx.holding_days}-day holding period plus the configured buffer, and the "
            "thesis is not an earnings trade."
        ),
        measurement=f"days_until_earnings={days_until}",
        threshold=(
            f"blackout window [-{rules.earnings_blackout_days_after}, "
            f"{ctx.holding_days}+{rules.earnings_blackout_days_before}]"
        ),
    )


def rule_provider_agreement(ctx: ScoringContext) -> HardRejection | None:
    limit = ctx.methodology.hard_rejections.max_provider_price_disagreement_pct
    check = ctx.validation.price_check
    if check.max_disagreement_pct is None:
        return None
    if check.max_disagreement_pct > limit and not check.reconciled:
        return HardRejection(
            code=RejectionReasonCode.PROVIDER_DISAGREEMENT,
            rule="max_provider_price_disagreement_pct",
            message=(
                f"Providers disagree on the underlying price by "
                f"{check.max_disagreement_pct:.2%} and could not be reconciled."
            ),
            measurement=f"{check.max_disagreement_pct:.4f}",
            threshold=f"{limit:.4f}",
        )
    return None


def rule_stale_quotes(ctx: ScoringContext) -> HardRejection | None:
    """Contracts may not be finalised on quotes the session says are not actionable."""
    from app.models.enums import WorkflowStage

    if ctx.stage is WorkflowStage.MARKET_OPEN:
        return None
    if ctx.trade is None:
        return None
    return HardRejection(
        code=RejectionReasonCode.STALE_QUOTES,
        rule="stale_quotes",
        message=(
            f"Stage is {ctx.stage.value}: option quotes are not actionable, so this "
            "structure is provisional and is not presented as an entry."
        ),
        measurement=ctx.stage.value,
        threshold=WorkflowStage.MARKET_OPEN.value,
    )


RULES: list[Rule] = [
    rule_strategy_allowed,
    rule_tradable_contract,
    rule_required_fields,
    rule_spread_width,
    rule_min_volume,
    rule_min_open_interest,
    rule_reward_to_risk,
    rule_premium_budget,
    rule_theta_burn,
    rule_catalyst_validated,
    rule_earnings_blackout,
    rule_provider_agreement,
    rule_stale_quotes,
]


def evaluate(ctx: ScoringContext, rules: list[Rule] | None = None) -> list[HardRejection]:
    """Run every rule and return all failures, in declaration order."""
    out: list[HardRejection] = []
    for rule in rules or RULES:
        result = rule(ctx)
        if result is not None:
            out.append(result)
    return out


__all__ = ["RULES", "HardRejection", "Rule", "evaluate"]
