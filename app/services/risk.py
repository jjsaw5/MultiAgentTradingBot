"""Deterministic reward/risk modelling for a proposed trade.

The projection deliberately does *not* assume you hold to expiration. It
re-prices every leg with Black-Scholes at the end of the intended holding
period, with the underlying at the thesis target and implied volatility
unchanged. That captures the two things that most often turn a directionally
correct option trade into a loss: time decay, and paying for a move that was
already priced in.

Assumptions are recorded on the result in ``method_notes`` rather than left
implicit, because they materially affect the reward/risk figure the score
depends on.
"""

from __future__ import annotations

from app.models.trade_structure import CONTRACT_MULTIPLIER, ProposedTrade, RiskReward
from app.services.pricing import black_scholes

IV_CONTRACTION_POINTS = 0.05  # 5 IV points, the sensitivity we report


def _value_at(
    trade: ProposedTrade,
    *,
    spot: float,
    days_forward: int,
    iv_shift: float = 0.0,
    reference_day=None,
) -> float | None:
    """Model value of the whole structure, in dollars, ``days_forward`` from now."""
    total = 0.0
    for leg in trade.legs:
        c = leg.contract
        if c.implied_volatility is None:
            return None
        dte = c.dte(reference_day)
        remaining = max(dte - days_forward, 0)
        if remaining == 0:
            # At expiration the only value is intrinsic.
            intrinsic = (
                max(spot - c.strike, 0.0)
                if c.right.value == "CALL"
                else max(c.strike - spot, 0.0)
            )
            price = intrinsic
        else:
            price = black_scholes(
                spot=spot,
                strike=c.strike,
                years_to_expiry=remaining / 365.0,
                volatility=max(c.implied_volatility + iv_shift, 0.01),
                is_call=c.right.value == "CALL",
            ).price
        sign = 1 if leg.action == "BUY" else -1
        total += sign * price * leg.quantity
    return total * CONTRACT_MULTIPLIER * trade.quantity


def compute_risk_reward(
    trade: ProposedTrade,
    *,
    expected_move_pct: float,
    direction_sign: int,
    holding_days: int,
    reference_day=None,
) -> RiskReward:
    target = trade.underlying_price * (1 + direction_sign * expected_move_pct / 100.0)
    cost = trade.max_loss
    notes = [
        "Legs re-priced with Black-Scholes at the end of the holding period, "
        "implied volatility held constant.",
        "Entry cost assumes paying the ask and selling the bid; a mid fill would "
        "improve every figure below.",
    ]

    value = _value_at(
        trade, spot=target, days_forward=holding_days, reference_day=reference_day
    )
    expected_value = None
    reward_to_risk = None
    if value is not None:
        # A defined-risk vertical cannot be worth more than its width.
        width = trade.spread_width
        if width is not None:
            value = min(value, width * CONTRACT_MULTIPLIER * trade.quantity)
        expected_value = round(value - cost, 2)
        if cost > 0:
            reward_to_risk = round(expected_value / cost, 3)
    else:
        notes.append("Reward/risk unavailable: a leg is missing implied volatility.")

    theta_burn = None
    theta_pct = None
    if trade.net_theta is not None:
        theta_burn = round(abs(trade.net_theta) * holding_days, 2)
        if cost > 0:
            theta_pct = round(theta_burn / cost, 4)

    iv_sensitivity = None
    down = _value_at(
        trade,
        spot=trade.underlying_price,
        days_forward=holding_days,
        iv_shift=-IV_CONTRACTION_POINTS,
        reference_day=reference_day,
    )
    flat = _value_at(
        trade, spot=trade.underlying_price, days_forward=holding_days, reference_day=reference_day
    )
    if down is not None and flat is not None:
        iv_sensitivity = round(down - flat, 2)
        notes.append(
            f"A {IV_CONTRACTION_POINTS:.0%} IV contraction with the underlying unchanged "
            f"is modelled at ${iv_sensitivity:,.0f}."
        )

    return RiskReward(
        structure_id=trade.structure_id,
        max_loss=cost,
        max_profit=trade.max_profit,
        breakeven=trade.breakeven,
        breakeven_move_pct=trade.breakeven_move_pct,
        target_underlying_price=round(target, 4),
        expected_value_at_target=expected_value,
        reward_to_risk=reward_to_risk,
        theta_burn_over_holding_period=theta_burn,
        theta_burn_pct_of_premium=theta_pct,
        iv_contraction_sensitivity=iv_sensitivity,
        method_notes=notes,
    )


__all__ = ["compute_risk_reward"]
