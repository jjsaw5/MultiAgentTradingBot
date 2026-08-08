"""Agent 2 output schema: the TradeCandidate.

Note what is deliberately absent: there is no 0-100 confidence field. Agent 2
supplies a hypothesis and the evidence pointers behind it. Numeric conviction
is computed later by :mod:`app.scoring`.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field, field_validator, model_validator

from app.models.common import Base, new_id
from app.models.enums import (
    CatalystType,
    Direction,
    PreliminaryQuality,
    StrategyType,
    TimeHorizon,
)


class CatalystRef(Base):
    """Pointer from a candidate back to a catalyst in the MarketBrief."""

    ticker: str
    catalyst_type: CatalystType
    headline: str
    source_url: str | None = None
    scheduled_event_date: date | None = None


class ExpectedMove(Base):
    """The magnitude thesis, stated as a percentage of the underlying.

    ``percent`` is the move the agent expects to be *capturable* within the
    holding period; the scoring engine checks it against realised volatility
    and against the options market's own implied move.
    """

    percent: float = Field(gt=0, le=100, description="Expected absolute move, in percent.")
    rationale: str
    basis: str = Field(
        default="agent_estimate",
        description="How the number was derived, e.g. 'historical earnings move', 'ATR'.",
    )


class TechnicalContext(Base):
    """Agent 2's read of price structure. Independently re-measured by Agent 3."""

    trend_description: str
    key_support: float | None = None
    key_resistance: float | None = None
    breakout_level: float | None = None
    notes: str | None = None


class TradeCandidate(Base):
    candidate_id: str = Field(default_factory=lambda: new_id("cand"))
    run_id: str
    ticker: str
    sector: str | None = None
    direction: Direction
    strategy_type: StrategyType

    thesis: str = Field(min_length=20)
    primary_catalyst: CatalystRef
    supporting_catalysts: list[CatalystRef] = Field(default_factory=list)

    expected_holding_period: TimeHorizon
    expected_move: ExpectedMove
    underlying_reference_price: float | None = Field(
        default=None,
        description="Price the thesis was framed against. May be a prior close in premarket; "
        "Agent 3 re-fetches a live price before anything is scored.",
    )
    technical_context: TechnicalContext

    invalidation_thesis: str = Field(
        min_length=10, description="The observable condition that proves this trade wrong."
    )
    invalidation_price: float | None = Field(
        default=None,
        gt=0,
        description="The underlying price at which the thesis is abandoned. This is the "
        "machine-readable half of `invalidation_thesis`, and it is what the risk model "
        "measures risk against -- without it, risk falls back to the whole debit, which "
        "overstates the loss a managed trade would actually take.",
    )
    known_risks: list[str] = Field(default_factory=list)

    earnings_date: date | None = None
    catalyst_date: date | None = None

    preliminary_quality: PreliminaryQuality = PreliminaryQuality.PLAUSIBLE
    agent_reasoning_summary: str = Field(default="")

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def _strategy_matches_direction(self) -> TradeCandidate:
        if self.strategy_type.direction is not self.direction:
            raise ValueError(
                f"strategy {self.strategy_type} contradicts direction {self.direction}"
            )
        return self


class CandidateSet(Base):
    """Agent 2's full response. An empty list is a valid, and often correct, answer."""

    run_id: str
    candidates: list[TradeCandidate] = Field(default_factory=list)
    no_trade_rationale: str | None = Field(
        default=None,
        description="Required when `candidates` is empty: why the agent declined to force a setup.",
    )
    considered_and_discarded: list[str] = Field(
        default_factory=list, description="Tickers looked at and passed on, with a brief reason."
    )
    unavailable_data: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _empty_needs_reason(self) -> CandidateSet:
        if not self.candidates and not self.no_trade_rationale:
            raise ValueError("an empty candidate set must include a no_trade_rationale")
        return self


__all__ = [
    "CandidateSet",
    "CatalystRef",
    "ExpectedMove",
    "TechnicalContext",
    "TradeCandidate",
]
