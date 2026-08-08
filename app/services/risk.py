"""Deterministic reward/risk modelling for a proposed trade.

Two modelling choices matter more than anything else here.

**Exit, not expiration.** Legs are re-priced with Black-Scholes at the end of
the intended holding period, with remaining DTE = original DTE minus holding
days and implied volatility unchanged. Valuing at expiry intrinsic would
systematically flatter long premium by ignoring the time value you actually
sell back.

**Risk is measured to the invalidation level, not to zero.** You do not lose the
whole premium on a losing trade -- you exit when the thesis breaks. So the
denominator of reward/risk is the loss incurred *at the invalidation price*, not
the full debit:

    reward_to_risk = (value at target − cost) / (cost − value at invalidation)

That is the ratio a trader actually reasons about: risk to the stop, reward to
the target. Measuring reward-to-target against risk-to-zero compares two
different things and penalises long premium for a loss that would never be
taken. When no invalidation level is available the model falls back to the full
premium and says so in ``method_notes``.

Every assumption is recorded on the result rather than left implicit, because
each one moves the number the scoring engine consumes.
"""

from __future__ import annotations

from app.models.trade_structure import CONTRACT_MULTIPLIER, ProposedTrade, RiskReward
from app.services.pricing import black_scholes

IV_CONTRACTION_POINTS = 0.05  # 5 IV points, the sensitivity we report

#: Fallbacks used only when no methodology config is supplied (tests, ad-hoc
#: analysis). Production callers pass `risk_model` from config/methodology.yaml.
DEFAULT_EXIT_FRACTION = 1.0 / 3.0
DEFAULT_MIN_STOP_ATR = 1.0


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


def _capped(value: float, trade: ProposedTrade) -> float:
    """A defined-risk vertical cannot be worth more than its width."""
    width = trade.spread_width
    if width is None:
        return value
    return min(value, width * CONTRACT_MULTIPLIER * trade.quantity)


def _effective_stop(
    *,
    stated: float,
    underlying_price: float,
    direction_sign: int,
    atr: float | None,
    min_stop_atr: float,
) -> tuple[float, str | None]:
    """Widen a stop that sits inside ordinary daily noise.

    A level closer than ``min_stop_atr`` average true ranges would be taken out
    by routine movement rather than by the thesis failing. Left alone it also
    flatters reward/risk, because the modelled loss at a stop one tick away is
    almost nothing. So it is widened for modelling purposes and the adjustment
    is reported.
    """
    if not atr or atr <= 0 or min_stop_atr <= 0:
        return stated, None
    distance = abs(underlying_price - stated)
    required = atr * min_stop_atr
    if distance >= required:
        return stated, None
    widened = underlying_price - direction_sign * required
    return (
        round(widened, 4),
        f"The stated invalidation level {stated:.2f} sits {distance:.2f} from price, "
        f"inside {min_stop_atr:g} ATR ({required:.2f}). Risk is modelled at "
        f"{widened:.2f} instead, because a stop within daily noise would be taken "
        f"out by ordinary movement and would understate the risk.",
    )


def compute_risk_reward(
    trade: ProposedTrade,
    *,
    expected_move_pct: float,
    direction_sign: int,
    holding_days: int,
    invalidation_price: float | None = None,
    atr: float | None = None,
    risk_model=None,
    reference_day=None,
) -> RiskReward:
    exit_fraction = (
        risk_model.invalidation_exit_fraction if risk_model else DEFAULT_EXIT_FRACTION
    )
    min_stop_atr = risk_model.min_stop_distance_atr if risk_model else DEFAULT_MIN_STOP_ATR

    target = trade.underlying_price * (1 + direction_sign * expected_move_pct / 100.0)
    cost = trade.max_loss
    notes = [
        "Legs re-priced with Black-Scholes at the end of the holding period, "
        "implied volatility held constant.",
        "Entry cost assumes paying the ask and selling the bid; a mid fill would "
        "improve every figure below.",
    ]

    # --- upside ------------------------------------------------------------
    value = _value_at(
        trade, spot=target, days_forward=holding_days, reference_day=reference_day
    )
    expected_value = None
    return_on_premium = None
    if value is not None:
        value = _capped(value, trade)
        expected_value = round(value - cost, 2)
        if cost > 0:
            return_on_premium = round(expected_value / cost, 3)
    else:
        notes.append("Reward/risk unavailable: a leg is missing implied volatility.")

    # --- downside ----------------------------------------------------------
    # The loss actually taken is the loss at the invalidation level, not the
    # whole debit. Only when no level is available do we fall back to the debit.
    value_at_invalidation = None
    risk_to_invalidation = None
    if invalidation_price is not None and invalidation_price > 0:
        invalidation_price, widened_note = _effective_stop(
            stated=invalidation_price,
            underlying_price=trade.underlying_price,
            direction_sign=direction_sign,
            atr=atr,
            min_stop_atr=min_stop_atr,
        )
        if widened_note:
            notes.append(widened_note)
        stop_days = max(1, int(round(holding_days * exit_fraction)))
        value_at_invalidation = _value_at(
            trade,
            spot=invalidation_price,
            days_forward=stop_days,
            reference_day=reference_day,
        )
        if value_at_invalidation is not None:
            value_at_invalidation = round(max(0.0, _capped(value_at_invalidation, trade)), 2)
            # Never model a stop as recovering more than the position cost.
            risk_to_invalidation = round(min(cost, max(0.0, cost - value_at_invalidation)), 2)
            notes.append(
                f"Risk measured to the invalidation level {invalidation_price:.2f}, modelled "
                f"{stop_days} days out: the position is worth about "
                f"${value_at_invalidation:,.0f}, so ${risk_to_invalidation:,.0f} of the "
                f"${cost:,.0f} debit is at risk before the thesis is abandoned."
            )

    if risk_to_invalidation is None or risk_to_invalidation <= 0:
        if invalidation_price is None:
            notes.append(
                "No invalidation level was supplied, so risk falls back to the full debit. "
                "Reward/risk is understated relative to how the trade would be managed."
            )
        elif risk_to_invalidation is not None and risk_to_invalidation <= 0:
            notes.append(
                "Modelled loss at the invalidation level is zero or negative; risk falls "
                "back to the full debit rather than reporting an infinite ratio."
            )
        risk_denominator = cost
    else:
        risk_denominator = risk_to_invalidation

    reward_to_risk = None
    if expected_value is not None and risk_denominator > 0:
        reward_to_risk = round(expected_value / risk_denominator, 3)

    # --- decay and volatility sensitivity ----------------------------------
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
        return_on_premium_at_target=return_on_premium,
        invalidation_underlying_price=invalidation_price,
        value_at_invalidation=value_at_invalidation,
        risk_to_invalidation=risk_to_invalidation,
        theta_burn_over_holding_period=theta_burn,
        theta_burn_pct_of_premium=theta_pct,
        iv_contraction_sensitivity=iv_sensitivity,
        method_notes=notes,
    )


__all__ = ["DEFAULT_EXIT_FRACTION", "DEFAULT_MIN_STOP_ATR", "compute_risk_reward"]
