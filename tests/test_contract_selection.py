"""Contract selection rules."""

from __future__ import annotations

from datetime import timedelta

from app.models.common import Provenance
from app.models.enums import DataProvider, Direction, OptionRight, StrategyType
from app.models.market_data import OptionChain
from app.services.contract_selection import select_contracts
from tests.conftest import TODAY, make_candidate, make_contract


def build_chain(
    *,
    dtes: list[int] | None = None,
    strikes: list[float] | None = None,
    spot: float = 120.0,
    oi: int = 5000,
    volume: int = 1500,
) -> OptionChain:
    """A chain whose deltas fall away sensibly from the money."""
    dtes = dtes or [10, 30, 45, 75, 100]
    strikes = strikes or [100, 105, 110, 115, 120, 125, 130, 135, 140]
    contracts = []
    for dte in dtes:
        for strike in strikes:
            moneyness = (spot - strike) / spot
            call_delta = min(0.95, max(0.05, 0.5 + moneyness * 3))
            extrinsic = 4.0 * (dte / 45) ** 0.5 * (
                1 - abs(moneyness) * 2 if abs(moneyness) < 0.5 else 0.1
            )
            # Each right gets its own intrinsic value; sharing one would make
            # put verticals price as credits and quietly break the fixture.
            for right, delta in ((OptionRight.CALL, call_delta), (OptionRight.PUT, call_delta - 1)):
                intrinsic = (
                    max(spot - strike, 0.0)
                    if right is OptionRight.CALL
                    else max(strike - spot, 0.0)
                )
                price = max(0.2, intrinsic + max(extrinsic, 0.2))
                contracts.append(
                    make_contract(
                        strike=strike,
                        right=right,
                        dte=dte,
                        bid=round(price * 0.99, 2),
                        ask=round(price * 1.01, 2),
                        delta=round(delta, 3),
                        oi=oi,
                        volume=volume,
                    )
                )
    return OptionChain(
        underlying="NVDA",
        underlying_price=spot,
        contracts=contracts,
        provenance=Provenance(provider=DataProvider.ROBINHOOD, endpoint="test"),
    )


def test_long_call_targets_the_configured_delta(methodology):
    cand = make_candidate(strategy=StrategyType.LONG_CALL)
    outcome = select_contracts(
        cand, build_chain(), methodology.contract_selection, today=TODAY, underlying_price=120.0
    )
    assert outcome.trade is not None
    delta = abs(outcome.trade.long_leg.contract.delta)
    rules = methodology.contract_selection
    assert rules.long_option_delta_min <= delta <= rules.long_option_delta_max


def test_expiration_leaves_extrinsic_value_at_the_planned_exit(methodology):
    cand = make_candidate(strategy=StrategyType.LONG_CALL)  # 21-day holding period
    outcome = select_contracts(
        cand, build_chain(), methodology.contract_selection, today=TODAY, underlying_price=120.0
    )
    assert outcome.trade is not None
    dte = (outcome.trade.expiration - TODAY).days
    rules = methodology.contract_selection
    assert dte >= cand.expected_holding_period.approx_days + rules.min_days_beyond_holding_period


def test_expiration_clears_the_catalyst_plus_buffer(methodology):
    catalyst_date = TODAY + timedelta(days=40)
    cand = make_candidate(strategy=StrategyType.LONG_CALL, catalyst_date=catalyst_date)
    outcome = select_contracts(
        cand, build_chain(), methodology.contract_selection, today=TODAY, underlying_price=120.0
    )
    assert outcome.trade is not None
    assert (outcome.trade.expiration - catalyst_date).days >= (
        methodology.contract_selection.min_days_past_catalyst
    )


def test_vertical_short_strike_sits_beyond_the_long_strike(methodology):
    cand = make_candidate(strategy=StrategyType.BULL_CALL_SPREAD)
    outcome = select_contracts(
        cand, build_chain(), methodology.contract_selection, today=TODAY, underlying_price=120.0
    )
    assert outcome.trade is not None
    short = outcome.trade.short_leg
    assert short is not None
    assert short.contract.strike > outcome.trade.long_leg.contract.strike
    width = outcome.trade.spread_width
    rules = methodology.contract_selection
    assert rules.spread_min_width <= width <= rules.spread_max_width


def test_bear_put_spread_sells_the_lower_strike(methodology):
    cand = make_candidate(
        direction=Direction.BEARISH, strategy=StrategyType.BEAR_PUT_SPREAD
    )
    outcome = select_contracts(
        cand, build_chain(), methodology.contract_selection, today=TODAY, underlying_price=120.0
    )
    assert outcome.trade is not None
    assert outcome.trade.short_leg.contract.strike < outcome.trade.long_leg.contract.strike
    assert outcome.trade.long_leg.contract.right is OptionRight.PUT


def test_a_spread_costing_too_much_of_its_width_is_refused(methodology):
    tight = methodology.contract_selection.model_copy(
        update={"spread_max_debit_pct_of_width": 0.01}
    )
    cand = make_candidate(strategy=StrategyType.BULL_CALL_SPREAD)
    outcome = select_contracts(cand, build_chain(), tight, today=TODAY, underlying_price=120.0)
    assert outcome.trade is None
    assert any("debit above" in r for r in outcome.reasons)


def test_no_eligible_expiration_returns_a_reason_not_a_guess(methodology):
    cand = make_candidate(strategy=StrategyType.LONG_CALL)
    outcome = select_contracts(
        cand,
        build_chain(dtes=[3, 5]),
        methodology.contract_selection,
        today=TODAY,
        underlying_price=120.0,
    )
    assert outcome.trade is None
    assert outcome.reasons


def test_contracts_without_a_two_sided_market_are_skipped(methodology):
    chain = build_chain(dtes=[45])
    for c in chain.contracts:
        c.bid = None
    cand = make_candidate(strategy=StrategyType.LONG_CALL)
    outcome = select_contracts(
        cand, chain, methodology.contract_selection, today=TODAY, underlying_price=120.0
    )
    assert outcome.trade is None


def test_alternatives_are_ranked_by_tradability_not_cheapness(methodology):
    cand = make_candidate(strategy=StrategyType.LONG_CALL)
    outcome = select_contracts(
        cand, build_chain(), methodology.contract_selection, today=TODAY, underlying_price=120.0
    )
    assert outcome.trade is not None
    for alt in outcome.alternatives:
        chosen_spread = outcome.trade.worst_leg_spread_pct or 1.0
        assert (alt.worst_leg_spread_pct or 1.0) >= chosen_spread - 1e-9


# ------------------------------------------------------------------- budget
def test_over_budget_structures_are_dropped_when_affordable_ones_exist(methodology):
    cand = make_candidate(strategy=StrategyType.LONG_CALL)
    outcome = select_contracts(
        cand,
        build_chain(),
        methodology.contract_selection,
        today=TODAY,
        underlying_price=120.0,
        max_premium_usd=500.0,
    )
    assert outcome.trade is not None
    assert outcome.trade.max_loss <= 500.0
    for alt in outcome.alternatives:
        assert alt.max_loss <= 500.0


def test_budget_filters_but_does_not_prefer_the_cheapest(methodology):
    """Affordability is a gate; tradability still decides among survivors."""
    cand = make_candidate(strategy=StrategyType.LONG_CALL)
    chain = build_chain()
    outcome = select_contracts(
        cand, chain, methodology.contract_selection,
        today=TODAY, underlying_price=120.0, max_premium_usd=100_000.0,
    )
    unconstrained = outcome.trade
    assert unconstrained is not None
    # With an effectively unlimited budget the choice is purely tradability,
    # so it is not simply the cheapest contract available.
    cheapest = min(
        (c for c in chain.contracts if c.right is OptionRight.CALL and c.ask),
        key=lambda c: c.ask,
    )
    assert unconstrained.long_leg.contract.ask > cheapest.ask


def test_when_nothing_fits_the_cheapest_is_carried_forward_to_be_rejected(methodology):
    """The report should say "costs $X, budget is $Y", not "no contract found"."""
    cand = make_candidate(strategy=StrategyType.LONG_CALL)
    outcome = select_contracts(
        cand,
        build_chain(),
        methodology.contract_selection,
        today=TODAY,
        underlying_price=120.0,
        max_premium_usd=1.0,
    )
    assert outcome.trade is not None
    assert outcome.trade.max_loss > 1.0
    assert any("budget" in r for r in outcome.reasons)
