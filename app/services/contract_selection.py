"""Deterministic option contract selection.

Selection is rule-based and configuration-driven. An LLM never picks a strike:
it supplies direction, timing, and magnitude, and this module translates that
into the specific contract that best matches the configured delta, expiration,
and liquidity preferences.

Cheapness is explicitly not a selection criterion. Contracts are ranked by
tradability (spread, open interest, volume) and fit to the delta target; the
premium only matters through the configured budget cap, applied later by the
hard-rejection rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.config.methodology import ContractSelectionRules
from app.models.enums import OptionRight, StrategyType
from app.models.market_data import OptionChain, OptionContract
from app.models.trade_candidate import TradeCandidate
from app.models.trade_structure import Leg, ProposedTrade


@dataclass
class SelectionOutcome:
    trade: ProposedTrade | None
    alternatives: list[ProposedTrade]
    reasons: list[str]


def _usable(c: OptionContract) -> bool:
    """A contract is only considered if it actually has a two-sided market."""
    return (
        c.bid is not None
        and c.ask is not None
        and c.ask > 0
        and c.bid > 0
        and c.delta is not None
        and c.open_interest is not None
        and c.volume is not None
    )


def _eligible_expirations(
    chain: OptionChain,
    rules: ContractSelectionRules,
    today: date,
    catalyst_date: date | None,
    holding_days: int,
) -> list[date]:
    out: list[date] = []
    for exp in chain.expirations():
        dte = (exp - today).days
        if dte < rules.absolute_dte_min or dte > rules.absolute_dte_max:
            continue
        # The contract must survive the thesis: it has to expire after the
        # catalyst plus a buffer, and after the intended holding period.
        if catalyst_date and dte < (catalyst_date - today).days + rules.min_days_past_catalyst:
            continue
        if dte < holding_days + rules.min_days_beyond_holding_period:
            continue
        out.append(exp)
    return out


def _expiration_rank(exp: date, today: date, rules: ContractSelectionRules) -> tuple[int, int]:
    """Prefer the preferred DTE window; inside it, prefer the middle."""
    dte = (exp - today).days
    in_window = rules.preferred_dte_min <= dte <= rules.preferred_dte_max
    midpoint = (rules.preferred_dte_min + rules.preferred_dte_max) / 2
    return (0 if in_window else 1, int(abs(dte - midpoint)))


def _closest_by_delta(
    contracts: list[OptionContract], target: float, *, lo: float | None = None, hi: float | None = None
) -> OptionContract | None:
    pool = [c for c in contracts if c.delta is not None]
    if lo is not None or hi is not None:
        bounded = [
            c
            for c in pool
            if (lo is None or abs(c.delta) >= lo) and (hi is None or abs(c.delta) <= hi)  # type: ignore[arg-type]
        ]
        # If nothing sits inside the configured delta band, fall back to the
        # whole pool rather than returning nothing -- the liquidity and
        # risk/reward rules downstream will reject it if it is genuinely bad.
        pool = bounded or pool
    if not pool:
        return None
    return min(pool, key=lambda c: abs(abs(c.delta) - target))  # type: ignore[arg-type]


def _quality_key(trade: ProposedTrade) -> tuple:
    """Lower is better. Tradability first, then cost efficiency."""
    spread = trade.worst_leg_spread_pct if trade.worst_leg_spread_pct is not None else 1.0
    oi = trade.min_leg_open_interest or 0
    vol = trade.min_leg_volume or 0
    return (round(spread, 3), -oi, -vol, trade.breakeven_move_pct)


def _build_single(
    candidate: TradeCandidate,
    contract: OptionContract,
    underlying_price: float,
) -> ProposedTrade:
    return ProposedTrade(
        candidate_id=candidate.candidate_id,
        ticker=candidate.ticker,
        strategy_type=candidate.strategy_type,
        underlying_price=underlying_price,
        expiration=contract.expiration,
        legs=[Leg(action="BUY", quantity=1, contract=contract)],
        net_debit_conservative=round(contract.ask or 0.0, 4),
        net_debit_mid=round(contract.mid or contract.ask or 0.0, 4),
        notes=[
            f"Long {contract.right.value} {contract.strike} exp {contract.expiration} "
            f"(delta {contract.delta:.2f})" if contract.delta is not None else "",
        ],
    )


def _build_vertical(
    candidate: TradeCandidate,
    long_c: OptionContract,
    short_c: OptionContract,
    underlying_price: float,
) -> ProposedTrade:
    debit_conservative = round((long_c.ask or 0.0) - (short_c.bid or 0.0), 4)
    debit_mid = round((long_c.mid or 0.0) - (short_c.mid or 0.0), 4)
    return ProposedTrade(
        candidate_id=candidate.candidate_id,
        ticker=candidate.ticker,
        strategy_type=candidate.strategy_type,
        underlying_price=underlying_price,
        expiration=long_c.expiration,
        legs=[
            Leg(action="BUY", quantity=1, contract=long_c),
            Leg(action="SELL", quantity=1, contract=short_c),
        ],
        net_debit_conservative=debit_conservative,
        net_debit_mid=debit_mid,
        notes=[
            f"Debit priced at long ask ({long_c.ask}) minus short bid ({short_c.bid}); "
            "a mid fill would cost less but is not assumed."
        ],
    )


def select_contracts(
    candidate: TradeCandidate,
    chain: OptionChain,
    rules: ContractSelectionRules,
    *,
    today: date,
    underlying_price: float,
) -> SelectionOutcome:
    reasons: list[str] = []
    right = candidate.strategy_type.option_right
    holding_days = candidate.expected_holding_period.approx_days

    expirations = _eligible_expirations(
        chain, rules, today, candidate.catalyst_date, holding_days
    )
    if not expirations:
        return SelectionOutcome(
            None,
            [],
            [
                "No expiration satisfies the configured DTE window "
                f"({rules.absolute_dte_min}-{rules.absolute_dte_max} DTE), the "
                f"{holding_days}-day holding period plus its "
                f"{rules.min_days_beyond_holding_period}-day extrinsic-value buffer, "
                f"and the {rules.min_days_past_catalyst}-day post-catalyst buffer."
            ],
        )

    expirations.sort(key=lambda e: _expiration_rank(e, today, rules))
    built: list[ProposedTrade] = []

    for exp in expirations[:3]:
        contracts = [c for c in chain.by_expiration(exp, right) if _usable(c)]
        if not contracts:
            reasons.append(f"{exp}: no contracts with a two-sided market and greeks.")
            continue

        if candidate.strategy_type.is_spread:
            long_c = _closest_by_delta(contracts, rules.spread_long_delta_target)
            if long_c is None:
                continue
            short_pool = [
                c
                for c in contracts
                if (
                    c.strike > long_c.strike
                    if right is OptionRight.CALL
                    else c.strike < long_c.strike
                )
                and rules.spread_min_width
                <= abs(c.strike - long_c.strike)
                <= rules.spread_max_width
            ]
            short_c = _closest_by_delta(short_pool, rules.spread_short_delta_target)
            if short_c is None:
                reasons.append(
                    f"{exp}: no short strike inside the configured "
                    f"{rules.spread_min_width}-{rules.spread_max_width} width band."
                )
                continue
            trade = _build_vertical(candidate, long_c, short_c, underlying_price)
            width = trade.spread_width or 0.0
            if width <= 0 or trade.net_debit_conservative <= 0:
                reasons.append(f"{exp}: degenerate spread pricing, skipped.")
                continue
            if trade.net_debit_conservative > rules.spread_max_debit_pct_of_width * width:
                reasons.append(
                    f"{exp}: debit {trade.net_debit_conservative:.2f} exceeds "
                    f"{rules.spread_max_debit_pct_of_width:.0%} of the {width:.2f} width."
                )
                continue
            built.append(trade)
        else:
            contract = _closest_by_delta(
                contracts,
                rules.long_option_delta_target,
                lo=rules.long_option_delta_min,
                hi=rules.long_option_delta_max,
            )
            if contract is None:
                reasons.append(f"{exp}: no strike inside the configured delta band.")
                continue
            built.append(_build_single(candidate, contract, underlying_price))

    if not built:
        reasons.append("No tradable structure could be assembled from the live chain.")
        return SelectionOutcome(None, [], reasons)

    built.sort(key=_quality_key)
    keep = built[: rules.max_candidate_contracts_per_trade]
    return SelectionOutcome(trade=keep[0], alternatives=keep[1:], reasons=reasons)


def allowed_strategy(strategy: StrategyType, allowed: list[str]) -> bool:
    return strategy.value in allowed


__all__ = ["SelectionOutcome", "allowed_strategy", "select_contracts"]
