"""Indicator maths and reward/risk modelling."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.common import Provenance
from app.models.enums import DataProvider, Direction, OptionRight, StrategyType
from app.models.market_data import PriceBar, PriceHistory
from app.models.trade_structure import Leg
from app.services import technicals as t
from app.services.pricing import black_scholes, implied_move_pct
from app.services.risk import compute_risk_reward
from tests.conftest import TODAY, make_candidate, make_contract, make_trade


def bars(closes: list[float], start: date = date(2024, 1, 1)) -> list[PriceBar]:
    return [
        PriceBar(
            day=start + timedelta(days=i),
            open=c,
            high=c * 1.01,
            low=c * 0.99,
            close=c,
            volume=1_000_000,
        )
        for i, c in enumerate(closes)
    ]


# ------------------------------------------------------------------ indicators
def test_sma_and_ema_basics():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert t.sma(values, 5) == 3.0
    assert t.sma(values, 2) == 4.5
    assert t.sma(values, 9) is None
    assert t.ema(values, 5) is not None
    assert t.ema(values, 9) is None


def test_rsi_saturates_on_a_pure_uptrend():
    assert t.rsi([float(i) for i in range(1, 40)]) == 100.0


def test_rsi_is_low_on_a_pure_downtrend():
    assert t.rsi([float(i) for i in range(40, 1, -1)]) == pytest.approx(0.0, abs=0.001)


def test_rsi_needs_enough_history():
    assert t.rsi([1.0, 2.0, 3.0]) is None


def test_atr_measures_the_true_range():
    series = bars([100.0] * 20)
    # Each bar spans 99..101, so the true range is ~2.
    assert t.atr(series) == pytest.approx(2.0, abs=0.05)


def test_swing_levels_bracket_the_current_price():
    closes = [100, 104, 98, 106, 95, 108, 101, 110, 97, 105] * 5
    series = bars([float(c) for c in closes])
    support, resistance = t.swing_levels(series)
    assert support is not None and resistance is not None
    assert support < series[-1].close < resistance


def test_higher_highs_and_lower_lows_detect_direction():
    up = bars([float(i) for i in range(1, 61)])
    down = bars([float(i) for i in range(60, 0, -1)])
    assert t.higher_highs(up) is True
    assert t.lower_lows(up) is False
    assert t.lower_lows(down) is True


def test_snapshot_reports_none_for_indicators_it_cannot_compute():
    history = PriceHistory(
        symbol="X",
        bars=bars([100.0, 101.0, 102.0]),
        provenance=Provenance(provider=DataProvider.FMP, endpoint="test"),
    )
    snap = t.compute_snapshot(history)
    assert snap.sma50 is None
    assert snap.sma200 is None
    assert snap.price == 102.0


def test_relative_strength_is_measured_against_the_benchmark():
    prov = Provenance(provider=DataProvider.FMP, endpoint="test")
    stock = PriceHistory(symbol="A", bars=bars([100.0 + i for i in range(60)]), provenance=prov)
    bench = PriceHistory(symbol="SPY", bars=bars([100.0] * 60), provenance=prov)
    snap = t.compute_snapshot(stock, benchmark=bench)
    assert snap.relative_strength_20d_vs_spy > 0


# --------------------------------------------------------------------- pricing
def test_black_scholes_call_put_parity():
    spot, strike, years, vol, rate = 100.0, 100.0, 0.5, 0.3, 0.045
    call = black_scholes(spot=spot, strike=strike, years_to_expiry=years, volatility=vol, rate=rate, is_call=True)
    put = black_scholes(spot=spot, strike=strike, years_to_expiry=years, volatility=vol, rate=rate, is_call=False)
    import math

    assert call.price - put.price == pytest.approx(
        spot - strike * math.exp(-rate * years), abs=0.01
    )


def test_greeks_have_the_expected_signs():
    call = black_scholes(spot=100, strike=100, years_to_expiry=0.25, volatility=0.3, is_call=True)
    put = black_scholes(spot=100, strike=100, years_to_expiry=0.25, volatility=0.3, is_call=False)
    assert 0 < call.delta < 1
    assert -1 < put.delta < 0
    assert call.gamma > 0 and put.gamma > 0
    assert call.theta < 0  # long options decay
    assert call.vega > 0


def test_deep_itm_call_approaches_intrinsic_value():
    g = black_scholes(spot=200, strike=100, years_to_expiry=0.02, volatility=0.2, is_call=True)
    assert g.price == pytest.approx(100.0, abs=1.0)
    assert g.delta == pytest.approx(1.0, abs=0.01)


def test_implied_move_scales_with_the_square_root_of_time():
    one = implied_move_pct(iv=0.4, days=30)
    four = implied_move_pct(iv=0.4, days=120)
    assert four == pytest.approx(one * 2, rel=0.01)


# ----------------------------------------------------------------- risk/reward
def test_reward_improves_with_a_larger_expected_move():
    cand = make_candidate()
    trade = make_trade(cand)
    small = compute_risk_reward(
        trade, expected_move_pct=5.0, direction_sign=1, holding_days=21, reference_day=TODAY
    )
    large = compute_risk_reward(
        trade, expected_move_pct=20.0, direction_sign=1, holding_days=21, reference_day=TODAY
    )
    assert large.reward_to_risk > small.reward_to_risk


def test_vertical_profit_is_capped_at_the_spread_width():
    cand = make_candidate(strategy=StrategyType.BULL_CALL_SPREAD)
    trade = make_trade(
        cand,
        legs=[
            Leg(action="BUY", quantity=1, contract=make_contract(strike=120, bid=7.9, ask=8.1)),
            Leg(
                action="SELL",
                quantity=1,
                contract=make_contract(strike=130, bid=3.4, ask=3.6, delta=0.3),
            ),
        ],
    )
    rr = compute_risk_reward(
        trade, expected_move_pct=50.0, direction_sign=1, holding_days=21, reference_day=TODAY
    )
    # Even on an absurd move the structure cannot pay more than its width.
    assert rr.expected_value_at_target <= trade.max_profit + 1e-6


def test_theta_burn_is_reported_over_the_holding_period():
    cand = make_candidate()
    trade = make_trade(
        cand, legs=[Leg(action="BUY", quantity=1, contract=make_contract(theta=-6.0))]
    )
    rr = compute_risk_reward(
        trade, expected_move_pct=10.0, direction_sign=1, holding_days=20, reference_day=TODAY
    )
    assert rr.theta_burn_over_holding_period == pytest.approx(120.0)
    # The stored value is rounded to four decimals for readability.
    assert rr.theta_burn_pct_of_premium == pytest.approx(120.0 / trade.max_loss, abs=1e-4)


def test_iv_contraction_hurts_a_long_premium_position():
    cand = make_candidate()
    rr = compute_risk_reward(
        make_trade(cand),
        expected_move_pct=10.0,
        direction_sign=1,
        holding_days=21,
        reference_day=TODAY,
    )
    assert rr.iv_contraction_sensitivity < 0


def test_bearish_target_is_below_the_current_price():
    cand = make_candidate(direction=Direction.BEARISH, strategy=StrategyType.LONG_PUT)
    trade = make_trade(
        cand,
        legs=[
            Leg(
                action="BUY",
                quantity=1,
                contract=make_contract(right=OptionRight.PUT, delta=-0.5),
            )
        ],
    )
    rr = compute_risk_reward(
        trade, expected_move_pct=10.0, direction_sign=-1, holding_days=21, reference_day=TODAY
    )
    assert rr.target_underlying_price < trade.underlying_price


def test_assumptions_are_recorded_rather_than_left_implicit():
    rr = compute_risk_reward(
        make_trade(make_candidate()),
        expected_move_pct=10.0,
        direction_sign=1,
        holding_days=21,
        reference_day=TODAY,
    )
    assert any("Black-Scholes" in note for note in rr.method_notes)
    assert any("ask" in note for note in rr.method_notes)


def test_reward_is_unavailable_rather_than_guessed_without_iv():
    cand = make_candidate()
    contract = make_contract()
    contract.implied_volatility = None
    trade = make_trade(cand, legs=[Leg(action="BUY", quantity=1, contract=contract)])
    rr = compute_risk_reward(
        trade, expected_move_pct=10.0, direction_sign=1, holding_days=21, reference_day=TODAY
    )
    assert rr.reward_to_risk is None
    assert any("unavailable" in n for n in rr.method_notes)
