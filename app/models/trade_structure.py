"""The concrete, priced trade: legs, cost, and defined-risk arithmetic.

All risk arithmetic here is deterministic and unit-tested. Agents never
compute max loss, breakeven, or reward/risk.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field, computed_field, model_validator

from app.models.common import Base, new_id
from app.models.enums import OptionRight, StrategyType
from app.models.market_data import OptionContract

CONTRACT_MULTIPLIER = 100


class Leg(Base):
    action: str = Field(description="BUY or SELL")
    quantity: int = 1
    contract: OptionContract

    @model_validator(mode="after")
    def _valid_action(self) -> Leg:
        if self.action not in ("BUY", "SELL"):
            raise ValueError("leg action must be BUY or SELL")
        return self


class TradeStructure:
    """Namespace marker; the concrete model is :class:`ProposedTrade`."""


class ProposedTrade(Base):
    """A fully specified, priced, defined-risk trade for one candidate."""

    structure_id: str = Field(default_factory=lambda: new_id("struct"))
    candidate_id: str
    ticker: str
    strategy_type: StrategyType
    underlying_price: float
    expiration: date
    legs: list[Leg]

    # Cost is computed conservatively: you pay the ask and sell at the bid.
    # A mid-price fill is an assumption, not a fact, so both are recorded.
    net_debit_conservative: float
    net_debit_mid: float

    quantity: int = 1
    notes: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def long_leg(self) -> Leg:
        return next(leg for leg in self.legs if leg.action == "BUY")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def short_leg(self) -> Leg | None:
        return next((leg for leg in self.legs if leg.action == "SELL"), None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread_width(self) -> float | None:
        short = self.short_leg
        if short is None:
            return None
        return round(abs(self.long_leg.contract.strike - short.contract.strike), 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def max_loss(self) -> float:
        """Total dollars at risk. Every allowed strategy is defined-risk."""
        return round(self.net_debit_conservative * CONTRACT_MULTIPLIER * self.quantity, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def max_profit(self) -> float | None:
        """Defined for verticals; unbounded (``None``) for long single options."""
        width = self.spread_width
        if width is None:
            return None
        return round(
            (width - self.net_debit_conservative) * CONTRACT_MULTIPLIER * self.quantity, 2
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def breakeven(self) -> float:
        long_c = self.long_leg.contract
        if long_c.right is OptionRight.CALL:
            return round(long_c.strike + self.net_debit_conservative, 4)
        return round(long_c.strike - self.net_debit_conservative, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def breakeven_move_pct(self) -> float:
        """Percentage move in the underlying required just to break even."""
        return round(
            abs(self.breakeven - self.underlying_price) / self.underlying_price * 100.0, 3
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net_delta(self) -> float | None:
        deltas = [leg.contract.delta for leg in self.legs]
        if any(d is None for d in deltas):
            return None
        total = 0.0
        for leg in self.legs:
            sign = 1 if leg.action == "BUY" else -1
            total += sign * leg.contract.delta * leg.quantity  # type: ignore[operator]
        return round(total, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net_theta(self) -> float | None:
        if any(leg.contract.theta is None for leg in self.legs):
            return None
        total = 0.0
        for leg in self.legs:
            sign = 1 if leg.action == "BUY" else -1
            total += sign * leg.contract.theta * leg.quantity  # type: ignore[operator]
        return round(total, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net_vega(self) -> float | None:
        if any(leg.contract.vega is None for leg in self.legs):
            return None
        total = 0.0
        for leg in self.legs:
            sign = 1 if leg.action == "BUY" else -1
            total += sign * leg.contract.vega * leg.quantity  # type: ignore[operator]
        return round(total, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def worst_leg_spread_pct(self) -> float | None:
        pcts = [leg.contract.spread_pct for leg in self.legs]
        present = [p for p in pcts if p is not None]
        if len(present) != len(pcts) or not present:
            return None
        return max(present)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def min_leg_open_interest(self) -> int | None:
        ois = [leg.contract.open_interest for leg in self.legs]
        if any(o is None for o in ois):
            return None
        return min(ois)  # type: ignore[arg-type]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def min_leg_volume(self) -> int | None:
        vols = [leg.contract.volume for leg in self.legs]
        if any(v is None for v in vols):
            return None
        return min(vols)  # type: ignore[arg-type]


class RiskReward(Base):
    """Reward/risk assessment for a proposed trade at a stated price target."""

    structure_id: str
    max_loss: float = Field(description="The whole debit -- the worst case if held to zero.")
    max_profit: float | None
    breakeven: float
    breakeven_move_pct: float
    target_underlying_price: float
    expected_value_at_target: float | None = None

    reward_to_risk: float | None = Field(
        default=None,
        description="Profit at target divided by the loss taken at the invalidation "
        "level. Falls back to the full debit as the denominator when no "
        "invalidation level is available.",
    )
    return_on_premium_at_target: float | None = Field(
        default=None,
        description="Profit at target as a multiple of the full debit. Reported "
        "alongside reward_to_risk because they answer different questions.",
    )

    invalidation_underlying_price: float | None = None
    value_at_invalidation: float | None = Field(
        default=None, description="Modelled position value if the thesis level breaks."
    )
    risk_to_invalidation: float | None = Field(
        default=None, description="Dollars actually at risk before the trade is abandoned."
    )

    theta_burn_over_holding_period: float | None = None
    theta_burn_pct_of_premium: float | None = None
    iv_contraction_sensitivity: float | None = Field(
        default=None, description="Dollar P&L impact of a 5-IV-point contraction."
    )
    method_notes: list[str] = Field(default_factory=list)


__all__ = ["CONTRACT_MULTIPLIER", "Leg", "ProposedTrade", "RiskReward", "TradeStructure"]
