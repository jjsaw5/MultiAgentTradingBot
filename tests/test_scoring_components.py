"""Per-component scoring tests.

Each test isolates one rule so a failure names the rule that broke.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.enums import (
    Bias,
    CatalystType,
    Direction,
    EvidenceQuality,
    OptionRight,
    StrategyType,
    ValidationVerdict,
)
from app.models.trade_structure import Leg
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
from tests.conftest import (
    TODAY,
    make_brief,
    make_candidate,
    make_catalyst,
    make_context,
    make_contract,
    make_flow,
    make_technicals,
    make_trade,
    make_validation,
)


def reason(component, rule: str):
    return next((r for r in component.reasons if r.rule == rule), None)


# ---------------------------------------------------------------- catalyst
def test_catalyst_full_credit_for_confirmed_recent_timely(methodology):
    ctx = make_context(methodology)
    c = catalyst.score(ctx)
    assert reason(c, "evidence_quality").points == 4.0
    assert reason(c, "catalyst_recency").points == 2.0
    assert reason(c, "catalyst_timing").points == 3.0
    assert c.points == pytest.approx(min(15.0, 4.8 + 4.0 + 2.0 + 3.0))


def test_catalyst_rumor_earns_no_evidence_credit(methodology):
    cand = make_candidate()
    brief = make_brief(
        catalysts=[make_catalyst(ticker=cand.ticker, quality=EvidenceQuality.RUMOR)]
    )
    c = catalyst.score(make_context(methodology, candidate=cand, brief=brief))
    assert reason(c, "evidence_quality").points == 0.0


def test_catalyst_priced_in_is_penalised(methodology):
    cand = make_candidate()
    brief = make_brief(catalysts=[make_catalyst(ticker=cand.ticker)])
    validation = make_validation(cand, priced_in=True)
    c = catalyst.score(
        make_context(methodology, candidate=cand, brief=brief, validation=validation)
    )
    assert reason(c, "already_priced_in").points == -3.0


def test_catalyst_timing_outside_holding_period_earns_nothing(methodology):
    cand = make_candidate()
    validation = make_validation(cand, days_until=90)
    c = catalyst.score(make_context(methodology, candidate=cand, validation=validation))
    assert reason(c, "catalyst_timing").points == 0.0


def test_catalyst_missing_from_brief_is_recorded_not_guessed(methodology):
    cand = make_candidate(catalyst_type=CatalystType.FDA_DECISION)
    brief = make_brief(catalysts=[make_catalyst(catalyst_type=CatalystType.MAJOR_CONTRACT)])
    c = catalyst.score(make_context(methodology, candidate=cand, brief=brief))
    assert any("catalyst_importance" in m for m in c.unscored_due_to_missing_data)
    assert reason(c, "evidence_quality").points == 0.0


def test_catalyst_contradicted_zeroes_the_component(methodology):
    cand = make_candidate()
    validation = make_validation(cand, catalyst_verdict=ValidationVerdict.CONTRADICTED)
    c = catalyst.score(make_context(methodology, candidate=cand, validation=validation))
    assert c.points == 0.0


# --------------------------------------------------------------- alignment
def test_alignment_rewards_agreement_with_the_tape(methodology):
    a = alignment.score(make_context(methodology))
    assert reason(a, "spy_alignment").points == 4.0
    assert reason(a, "qqq_alignment").points == 2.0
    assert reason(a, "sector_alignment").points == 2.0
    assert reason(a, "relative_strength_20d").points == 2.0
    assert a.points == 10.0


def test_alignment_penalises_fighting_the_sector(methodology):
    cand = make_candidate()
    brief = make_brief(
        sector_bias=Bias.BEARISH, catalysts=[make_catalyst(ticker=cand.ticker)]
    )
    a = alignment.score(make_context(methodology, candidate=cand, brief=brief))
    assert reason(a, "sector_alignment").points == -1.0


def test_alignment_bearish_trade_wants_relative_weakness(methodology):
    cand = make_candidate(direction=Direction.BEARISH, strategy=StrategyType.LONG_PUT)
    brief = make_brief(
        spy=Bias.BEARISH,
        qqq=Bias.BEARISH,
        sector_bias=Bias.BEARISH,
        catalysts=[make_catalyst(ticker=cand.ticker)],
    )
    strong = make_technicals(rs=5.0)
    weak = make_technicals(rs=-5.0)
    assert (
        reason(
            alignment.score(
                make_context(methodology, candidate=cand, brief=brief, technicals=strong)
            ),
            "relative_strength_20d",
        ).points
        == 0.0
    )
    assert (
        reason(
            alignment.score(
                make_context(methodology, candidate=cand, brief=brief, technicals=weak)
            ),
            "relative_strength_20d",
        ).points
        == 2.0
    )


# --------------------------------------------------------------- technical
def test_technical_stacked_trend_gets_full_trend_credit(methodology):
    t = technical.score(make_context(methodology))
    assert reason(t, "trend_alignment").points == 6.0
    assert reason(t, "key_level_respected").points == 4.0
    assert reason(t, "relative_volume").points == 3.0
    assert reason(t, "momentum").points == 4.0


def test_technical_partial_trend_gets_partial_credit(methodology):
    tech = make_technicals(price=120.0, sma20=115.0, sma50=125.0)
    t = technical.score(make_context(methodology, technicals=tech))
    assert reason(t, "trend_alignment").points == 3.0


def test_technical_penalises_immediate_overhead_resistance(methodology):
    tech = make_technicals(price=120.0, resistance=120.5)
    t = technical.score(make_context(methodology, technicals=tech))
    assert reason(t, "blocking_level_proximity").points == -3.0


def test_technical_penalises_chasing_an_overextended_move(methodology):
    tech = make_technicals(rsi=82.0)
    t = technical.score(make_context(methodology, technicals=tech))
    assert reason(t, "overextension_penalty").points == -2.0


def test_technical_rejects_an_implausible_expected_move(methodology):
    cand = make_candidate(expected_move=60.0)
    tech = make_technicals(atr_pct=1.0)
    t = technical.score(make_context(methodology, candidate=cand, technicals=tech))
    assert reason(t, "expected_move_feasible").points == 0.0


def test_technical_scores_zero_without_data_rather_than_guessing(methodology):
    t = technical.score(make_context(methodology, technicals=None))
    assert t.points == 0.0
    assert t.unscored_due_to_missing_data


# -------------------------------------------------------------------- flow
def test_flow_scales_credit_with_directional_share(methodology):
    strong = flow.score(make_context(methodology, flow=make_flow(bullish=9_000_000, bearish=1_000_000)))
    weak = flow.score(make_context(methodology, flow=make_flow(bullish=5_500_000, bearish=4_500_000)))
    assert reason(strong, "directional_premium").points > reason(weak, "directional_premium").points


def test_flow_at_the_floor_earns_nothing(methodology):
    f = flow.score(make_context(methodology, flow=make_flow(bullish=5_000_000, bearish=5_000_000)))
    assert reason(f, "directional_premium").points == 0.0


def test_flow_opposing_the_thesis_is_penalised(methodology):
    f = flow.score(make_context(methodology, flow=make_flow(bullish=1_000_000, bearish=9_000_000)))
    assert reason(f, "flow_contradicts_thesis").points == -6.0


def test_flow_suppresses_sweep_credit_when_multileg_dominates(methodology):
    f = flow.score(make_context(methodology, flow=make_flow(multileg=0.6)))
    assert reason(f, "sweeps").points == 0.0
    assert reason(f, "multileg_caveat") is not None


def test_missing_flow_scores_zero_and_says_so(methodology):
    f = flow.score(make_context(methodology, flow=None))
    assert f.points == 0.0
    assert f.unscored_due_to_missing_data


def test_bearish_thesis_is_confirmed_by_bid_side_premium(methodology):
    cand = make_candidate(direction=Direction.BEARISH, strategy=StrategyType.LONG_PUT)
    brief = make_brief(
        spy=Bias.BEARISH, qqq=Bias.BEARISH, catalysts=[make_catalyst(ticker=cand.ticker)]
    )
    bid_heavy = make_flow(bullish=2_000_000, bearish=8_000_000, ask=2_000_000, bid=8_000_000,
                          net_delta=-120_000)
    f = flow.score(
        make_context(
            methodology,
            candidate=cand,
            brief=brief,
            flow=bid_heavy,
            trade=make_trade(
                cand,
                legs=[Leg(action="BUY", quantity=1, contract=make_contract(right=OptionRight.PUT, delta=-0.5))],
            ),
        )
    )
    assert reason(f, "side_of_market").points > 0
    assert reason(f, "delta_flow_consistency").points == 2.0


# --------------------------------------------------------------- iv_greeks
def test_low_iv_rank_favours_long_premium(methodology):
    low = iv_greeks.score(
        make_context(methodology, trade=make_trade(make_candidate(), legs=[
            Leg(action="BUY", quantity=1, contract=make_contract(iv_rank=20.0))
        ]))
    )
    high = iv_greeks.score(
        make_context(methodology, trade=make_trade(make_candidate(), legs=[
            Leg(action="BUY", quantity=1, contract=make_contract(iv_rank=85.0))
        ]))
    )
    assert reason(low, "iv_rank").points == 4.0
    assert reason(high, "iv_rank").points == 0.0


def test_spreads_tolerate_high_iv_better_than_long_premium(methodology):
    cand = make_candidate(strategy=StrategyType.BULL_CALL_SPREAD)
    spread_trade = make_trade(
        cand,
        legs=[
            Leg(action="BUY", quantity=1, contract=make_contract(strike=120, iv_rank=85.0)),
            Leg(action="SELL", quantity=1, contract=make_contract(strike=130, iv_rank=85.0,
                                                                 bid=3.9, ask=4.1, delta=0.30)),
        ],
    )
    s = iv_greeks.score(make_context(methodology, candidate=cand, trade=spread_trade))
    assert reason(s, "iv_rank").points == 2.0


def test_excessive_theta_burden_earns_no_credit(methodology):
    heavy = make_trade(
        make_candidate(), legs=[Leg(action="BUY", quantity=1, contract=make_contract(theta=-30.0))]
    )
    s = iv_greeks.score(make_context(methodology, trade=heavy))
    assert reason(s, "theta_burden").points == 0.0


# --------------------------------------------------------------- liquidity
def test_liquidity_rewards_a_tight_market(methodology):
    tight = make_trade(
        make_candidate(),
        legs=[Leg(action="BUY", quantity=1, contract=make_contract(bid=7.95, ask=8.05, oi=5000, volume=1500))],
    )
    lq = liquidity.score(make_context(methodology, trade=tight))
    assert lq.points == 10.0


def test_liquidity_scores_the_worst_leg_not_the_average(methodology):
    cand = make_candidate(strategy=StrategyType.BULL_CALL_SPREAD)
    mixed = make_trade(
        cand,
        legs=[
            Leg(action="BUY", quantity=1, contract=make_contract(strike=120, bid=7.99, ask=8.01, oi=9000, volume=4000)),
            Leg(action="SELL", quantity=1, contract=make_contract(strike=130, bid=1.0, ask=3.0, oi=40, volume=5, delta=0.25)),
        ],
    )
    lq = liquidity.score(make_context(methodology, candidate=cand, trade=mixed))
    assert reason(lq, "bid_ask_spread").points == 0.0
    assert reason(lq, "open_interest").points == 0.0
    assert reason(lq, "contract_volume").points == 0.0


# -------------------------------------------------------------- risk/reward
def test_risk_reward_awards_by_configured_band(methodology):
    ctx = make_context(methodology)
    ctx.risk_reward.reward_to_risk = 3.5
    r = risk_reward.score(ctx)
    assert reason(r, "reward_to_risk").points == 8.0

    ctx.risk_reward.reward_to_risk = 1.1
    assert reason(risk_reward.score(ctx), "reward_to_risk").points == 2.0


def test_risk_reward_penalises_a_breakeven_that_eats_the_move(methodology):
    cand = make_candidate(expected_move=2.0)
    r = risk_reward.score(make_context(methodology, candidate=cand))
    assert reason(r, "breakeven_vs_expected_move").points == 0.0


def test_risk_reward_flags_a_position_over_budget(methodology):
    big = make_trade(
        make_candidate(), legs=[Leg(action="BUY", quantity=1, contract=make_contract(bid=24.0, ask=25.0))]
    )
    r = risk_reward.score(make_context(methodology, trade=big))
    assert reason(r, "max_loss_within_budget").points == 0.0


# ------------------------------------------------------------- data quality
def test_data_quality_requires_every_expected_provider(methodology):
    full = data_quality.score(make_context(methodology))
    assert reason(full, "provider_coverage").points == 2.0

    partial = data_quality.score(
        make_context(methodology, providers_responded=["fmp"])
    )
    assert reason(partial, "provider_coverage").points == 0.0


def test_data_quality_penalises_provider_price_disagreement(methodology):
    cand = make_candidate()
    v = make_validation(cand, prices={"fmp": 120.0, "robinhood": 126.0})
    d = data_quality.score(make_context(methodology, candidate=cand, validation=v))
    assert reason(d, "price_agreement").points == 0.0


def test_data_quality_penalises_stale_inputs(methodology):
    d = data_quality.score(make_context(methodology, stale_inputs=["option_chain"]))
    assert reason(d, "data_freshness").points == 0.0


# ------------------------------------------------------------------ shared
@pytest.mark.parametrize(
    "module,weight_attr",
    [
        (catalyst, "catalyst_strength"),
        (alignment, "market_alignment"),
        (technical, "technical_setup"),
        (flow, "options_flow"),
        (iv_greeks, "iv_greeks"),
        (liquidity, "contract_liquidity"),
        (risk_reward, "risk_reward"),
        (data_quality, "data_quality"),
    ],
)
def test_components_never_exceed_or_undercut_their_weight(methodology, module, weight_attr):
    comp = module.score(make_context(methodology))
    weight = getattr(methodology.score_weights, weight_attr)
    assert comp.max_points == weight
    assert 0.0 <= comp.points <= weight


def test_every_awarded_point_carries_a_measurement(methodology):
    from app.scoring.engine import score_candidate

    breakdown = score_candidate(make_context(methodology))
    for comp in breakdown.components:
        for r in comp.reasons:
            assert r.measurement, f"{comp.name}/{r.rule} awarded points with no measurement"


def test_catalyst_recency_boundary_uses_configured_days(methodology):
    cand = make_candidate()
    brief = make_brief(catalysts=[make_catalyst(ticker=cand.ticker)])
    partial = make_validation(cand, days_since=methodology.scoring.catalyst_strength.recency_days_partial)
    stale = make_validation(cand, days_since=methodology.scoring.catalyst_strength.recency_days_partial + 1)
    assert reason(
        catalyst.score(make_context(methodology, candidate=cand, brief=brief, validation=partial)),
        "catalyst_recency",
    ).points == 1.0
    assert reason(
        catalyst.score(make_context(methodology, candidate=cand, brief=brief, validation=stale)),
        "catalyst_recency",
    ).points == 0.0


def test_scheduled_catalyst_inside_holding_period_is_timed(methodology):
    cand = make_candidate(catalyst_date=TODAY + timedelta(days=10))
    c = catalyst.score(make_context(methodology, candidate=cand))
    assert reason(c, "catalyst_timing").points == 3.0
