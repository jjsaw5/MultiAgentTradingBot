"""Hard rejection rules.

The central property under test: a hard failure cannot be outvoted. A trade may
score in the nineties and still be rejected if it is untradable.
"""

from __future__ import annotations

from datetime import timedelta

from app.models.enums import (
    CatalystType,
    Classification,
    RejectionReasonCode,
    StrategyType,
    ValidationVerdict,
    WorkflowStage,
)
from app.models.trade_structure import Leg
from app.rules import hard_rejections
from app.scoring import engine
from tests.conftest import (
    TODAY,
    make_candidate,
    make_context,
    make_contract,
    make_trade,
    make_validation,
)


def codes(ctx) -> set[RejectionReasonCode]:
    return {hr.code for hr in hard_rejections.evaluate(ctx)}


def test_a_clean_trade_triggers_no_hard_rule(methodology):
    assert codes(make_context(methodology)) == set()


def test_wide_spread_is_rejected(methodology):
    wide = make_trade(
        make_candidate(),
        legs=[Leg(action="BUY", quantity=1, contract=make_contract(bid=6.0, ask=10.0))],
    )
    assert RejectionReasonCode.SPREAD_TOO_WIDE in codes(make_context(methodology, trade=wide))


def test_thin_volume_and_open_interest_are_rejected(methodology):
    thin = make_trade(
        make_candidate(),
        legs=[Leg(action="BUY", quantity=1, contract=make_contract(volume=5, oi=12))],
    )
    found = codes(make_context(methodology, trade=thin))
    assert RejectionReasonCode.INSUFFICIENT_VOLUME in found
    assert RejectionReasonCode.INSUFFICIENT_OPEN_INTEREST in found


def test_missing_quote_is_rejected_not_estimated(methodology):
    no_quote = make_trade(
        make_candidate(),
        legs=[Leg(action="BUY", quantity=1, contract=make_contract())],
    )
    no_quote.legs[0].contract.bid = None
    assert RejectionReasonCode.MISSING_CRITICAL_DATA in codes(
        make_context(methodology, trade=no_quote)
    )


def test_no_structure_is_rejected(methodology):
    found = codes(make_context(methodology, trade=None, risk_reward=None))
    assert RejectionReasonCode.NO_TRADABLE_CONTRACT in found


def test_premium_over_budget_is_rejected(methodology):
    expensive = make_trade(
        make_candidate(),
        legs=[Leg(action="BUY", quantity=1, contract=make_contract(bid=19.0, ask=20.0))],
    )
    assert RejectionReasonCode.PREMIUM_EXCEEDS_LIMIT in codes(
        make_context(methodology, trade=expensive)
    )


def test_reward_to_risk_below_minimum_is_rejected(methodology):
    ctx = make_context(methodology)
    ctx.risk_reward.reward_to_risk = 0.4
    assert RejectionReasonCode.REWARD_RISK_TOO_LOW in codes(ctx)


def test_excessive_theta_is_rejected(methodology):
    ctx = make_context(methodology)
    ctx.risk_reward.theta_burn_pct_of_premium = 0.85
    assert RejectionReasonCode.EXCESSIVE_THETA in codes(ctx)


def test_unvalidated_catalyst_is_rejected(methodology):
    cand = make_candidate()
    v = make_validation(cand, catalyst_verdict=ValidationVerdict.CONTRADICTED)
    assert RejectionReasonCode.CATALYST_NOT_VALIDATED in codes(
        make_context(methodology, candidate=cand, validation=v)
    )


def test_earnings_inside_the_holding_period_is_rejected(methodology):
    cand = make_candidate(earnings=TODAY + timedelta(days=5))
    assert RejectionReasonCode.EARNINGS_BLACKOUT in codes(
        make_context(methodology, candidate=cand)
    )


def test_earnings_is_allowed_when_it_is_the_thesis(methodology):
    cand = make_candidate(
        earnings=TODAY + timedelta(days=5), catalyst_type=CatalystType.EARNINGS
    )
    assert RejectionReasonCode.EARNINGS_BLACKOUT not in codes(
        make_context(methodology, candidate=cand)
    )


def test_earnings_beyond_the_window_is_not_rejected(methodology):
    cand = make_candidate(earnings=TODAY + timedelta(days=120))
    assert RejectionReasonCode.EARNINGS_BLACKOUT not in codes(
        make_context(methodology, candidate=cand)
    )


def test_irreconcilable_provider_disagreement_is_rejected(methodology):
    cand = make_candidate()
    v = make_validation(cand, prices={"fmp": 120.0, "robinhood": 130.0}, reconciled=False)
    assert RejectionReasonCode.PROVIDER_DISAGREEMENT in codes(
        make_context(methodology, candidate=cand, validation=v)
    )


def test_premarket_quotes_cannot_produce_an_entry(methodology):
    found = codes(make_context(methodology, stage=WorkflowStage.PREMARKET))
    assert RejectionReasonCode.STALE_QUOTES in found


def test_market_open_quotes_are_actionable(methodology):
    found = codes(make_context(methodology, stage=WorkflowStage.MARKET_OPEN))
    assert RejectionReasonCode.STALE_QUOTES not in found


def test_disallowed_strategy_is_rejected(methodology):
    restricted = methodology.model_copy(
        update={"strategies": methodology.strategies.model_copy(update={"allowed": ["LONG_PUT"]})}
    )
    ctx = make_context(restricted)
    assert RejectionReasonCode.STRATEGY_NOT_ALLOWED in codes(ctx)


def test_every_failing_rule_is_reported_not_just_the_first(methodology):
    awful = make_trade(
        make_candidate(),
        legs=[Leg(action="BUY", quantity=1, contract=make_contract(bid=1.0, ask=9.0, volume=2, oi=3))],
    )
    found = codes(make_context(methodology, trade=awful))
    assert len(found) >= 3


def test_a_high_score_cannot_override_a_hard_rejection(methodology):
    """The whole point of a separate rules engine."""
    untradable = make_trade(
        make_candidate(),
        legs=[Leg(action="BUY", quantity=1, contract=make_contract(bid=6.0, ask=10.0, volume=1, oi=1))],
    )
    ctx = make_context(methodology, trade=untradable)
    breakdown = engine.score_candidate(ctx)
    scored = engine.classify(breakdown, ctx)

    assert breakdown.hard_rejected
    assert scored.classification is Classification.REJECTED
    assert scored.presentable is False
    # The numeric score is still reported: "good thesis, untradable market" is
    # more useful feedback than a bare rejection.
    assert breakdown.total > 0


def test_rejected_candidates_keep_their_full_breakdown(methodology):
    ctx = make_context(methodology, stage=WorkflowStage.PREMARKET)
    breakdown = engine.score_candidate(ctx)
    scored = engine.classify(breakdown, ctx)
    assert scored.presentable is False
    assert len(breakdown.components) == 8
    assert scored.rejection_summary


def test_spread_strategy_is_evaluated_on_the_worst_leg(methodology):
    cand = make_candidate(strategy=StrategyType.BULL_CALL_SPREAD)
    trade = make_trade(
        cand,
        legs=[
            Leg(action="BUY", quantity=1, contract=make_contract(strike=120)),
            Leg(
                action="SELL",
                quantity=1,
                contract=make_contract(strike=130, bid=0.5, ask=2.5, volume=3, oi=5, delta=0.25),
            ),
        ],
    )
    found = codes(make_context(methodology, candidate=cand, trade=trade))
    assert RejectionReasonCode.SPREAD_TOO_WIDE in found
    assert RejectionReasonCode.INSUFFICIENT_VOLUME in found
