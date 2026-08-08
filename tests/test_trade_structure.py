"""Defined-risk arithmetic.

These are the numbers a human sizes a position from, so they are pinned
exactly rather than approximately.
"""

from __future__ import annotations

import pytest

from app.models.enums import Direction, OptionRight, StrategyType
from app.models.trade_structure import CONTRACT_MULTIPLIER, Leg
from tests.conftest import make_candidate, make_contract, make_trade


def test_long_call_risk_is_the_premium_paid():
    cand = make_candidate(strategy=StrategyType.LONG_CALL)
    trade = make_trade(cand, legs=[Leg(action="BUY", quantity=1, contract=make_contract(ask=8.1))])

    assert trade.net_debit_conservative == 8.1
    assert trade.max_loss == 810.0
    assert trade.max_profit is None  # unbounded
    assert trade.breakeven == 128.1
    assert trade.breakeven_move_pct == pytest.approx(6.75, abs=0.01)


def test_long_put_breakeven_is_below_the_strike():
    cand = make_candidate(direction=Direction.BEARISH, strategy=StrategyType.LONG_PUT)
    trade = make_trade(
        cand,
        legs=[
            Leg(
                action="BUY",
                quantity=1,
                contract=make_contract(right=OptionRight.PUT, strike=120, bid=6.9, ask=7.1, delta=-0.5),
            )
        ],
    )
    assert trade.breakeven == 112.9
    assert trade.max_loss == 710.0


def test_bull_call_spread_is_capped_on_both_sides():
    cand = make_candidate(strategy=StrategyType.BULL_CALL_SPREAD)
    trade = make_trade(
        cand,
        legs=[
            Leg(action="BUY", quantity=1, contract=make_contract(strike=120, bid=7.9, ask=8.1)),
            Leg(
                action="SELL",
                quantity=1,
                contract=make_contract(strike=130, bid=3.4, ask=3.6, delta=0.3, theta=-5.0, vega=10.0),
            ),
        ],
    )
    # Conservative fill: pay the ask on the long, receive the bid on the short.
    assert trade.net_debit_conservative == pytest.approx(4.7)
    assert trade.spread_width == 10.0
    assert trade.max_loss == 470.0
    assert trade.max_profit == pytest.approx((10.0 - 4.7) * CONTRACT_MULTIPLIER)
    assert trade.breakeven == pytest.approx(124.7)
    # Max profit plus max loss must equal the full width of the spread.
    assert trade.max_profit + trade.max_loss == pytest.approx(10.0 * CONTRACT_MULTIPLIER)


def test_bear_put_spread_arithmetic():
    cand = make_candidate(direction=Direction.BEARISH, strategy=StrategyType.BEAR_PUT_SPREAD)
    trade = make_trade(
        cand,
        legs=[
            Leg(
                action="BUY",
                quantity=1,
                contract=make_contract(right=OptionRight.PUT, strike=120, bid=6.9, ask=7.1, delta=-0.5),
            ),
            Leg(
                action="SELL",
                quantity=1,
                contract=make_contract(right=OptionRight.PUT, strike=110, bid=2.9, ask=3.1, delta=-0.3),
            ),
        ],
    )
    assert trade.net_debit_conservative == pytest.approx(4.2)
    assert trade.breakeven == pytest.approx(115.8)
    assert trade.max_profit == pytest.approx((10.0 - 4.2) * CONTRACT_MULTIPLIER)


def test_net_greeks_account_for_the_short_leg():
    cand = make_candidate(strategy=StrategyType.BULL_CALL_SPREAD)
    trade = make_trade(
        cand,
        legs=[
            Leg(action="BUY", quantity=1, contract=make_contract(strike=120, delta=0.5, theta=-8.0, vega=15.0)),
            Leg(action="SELL", quantity=1, contract=make_contract(strike=130, delta=0.3, theta=-5.0, vega=10.0, bid=3.4, ask=3.6)),
        ],
    )
    assert trade.net_delta == pytest.approx(0.2)
    assert trade.net_theta == pytest.approx(-3.0)
    assert trade.net_vega == pytest.approx(5.0)


def test_worst_leg_metrics_pick_the_worst_not_the_average():
    cand = make_candidate(strategy=StrategyType.BULL_CALL_SPREAD)
    trade = make_trade(
        cand,
        legs=[
            Leg(action="BUY", quantity=1, contract=make_contract(strike=120, bid=7.99, ask=8.01, oi=9000, volume=4000)),
            Leg(action="SELL", quantity=1, contract=make_contract(strike=130, bid=1.0, ask=3.0, oi=40, volume=5, delta=0.3)),
        ],
    )
    assert trade.min_leg_open_interest == 40
    assert trade.min_leg_volume == 5
    assert trade.worst_leg_spread_pct == pytest.approx(1.0)  # 2.00 wide on a 2.00 mid


def test_missing_greeks_yield_none_rather_than_zero():
    cand = make_candidate()
    contract = make_contract()
    contract.theta = None
    trade = make_trade(cand, legs=[Leg(action="BUY", quantity=1, contract=contract)])
    assert trade.net_theta is None


def test_quantity_scales_risk_linearly():
    cand = make_candidate()
    trade = make_trade(cand, legs=[Leg(action="BUY", quantity=1, contract=make_contract(ask=8.1))])
    single = trade.max_loss
    trade.quantity = 3
    assert trade.max_loss == pytest.approx(single * 3)


def test_strategy_direction_mismatch_is_rejected_at_the_schema():
    with pytest.raises(ValueError, match="contradicts direction"):
        make_candidate(direction=Direction.BEARISH, strategy=StrategyType.LONG_CALL)
