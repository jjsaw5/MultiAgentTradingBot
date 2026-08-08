"""The interpretation-only slice Agent 1's LLM is asked to produce.

Agent 1 originally emitted a whole :class:`MarketBrief`. That was wrong for two
reasons, one practical and one architectural:

* **Practical.** The brief carries every news item, source and economic event
  in the evidence pack. Asking the model to re-emit data it was just handed
  blew past the output token ceiling and truncated the JSON mid-object -- the
  run silently fell back to heuristics.
* **Architectural.** It is the same mistake :class:`ValidatorAssessment` exists
  to avoid. Measured data should be attached by code; the model should supply
  only what requires judgement.

So the model now returns a :class:`MarketAssessment`: regimes, biases,
observations and catalyst classifications. Prices, levels, news items, sources
and calendars are attached afterwards from the pack.

The catalyst schema is the important one. It carries no ``source_url`` and no
``published_at`` -- those are looked up by matching ``headline`` against the
news items actually supplied. A catalyst the model invents therefore cannot
acquire a citation, and :func:`app.agents.market_intelligence.agent.merge`
downgrades it to ``UNVERIFIED``.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from app.models.common import Base
from app.models.enums import (
    Bias,
    CatalystType,
    EvidenceQuality,
    MarketRegime,
    TimeHorizon,
    VolatilityRegime,
)
from app.models.market_brief import MacroObservation, RiskEvent, SectorObservation


class IndexRead(Base):
    """A directional read on an index. Prices are attached by code."""

    symbol: str
    bias: Bias
    notes: str | None = None


class CatalystRead(Base):
    """A classified catalyst.

    ``headline`` must reproduce a headline from the evidence pack verbatim; it
    is the join key used to recover the source and timestamp.
    """

    ticker: str
    catalyst_type: CatalystType
    headline: str
    description: str
    expected_direction: Bias = Bias.NEUTRAL
    importance_score: float = Field(ge=0.0, le=1.0)
    expected_time_horizon: TimeHorizon = TimeHorizon.WEEKS_2_4
    evidence_quality: EvidenceQuality = EvidenceQuality.REPORTED
    already_priced_in: bool | None = None
    scheduled_event_date: date | None = None


class MarketAssessment(Base):
    market_regime: MarketRegime = MarketRegime.UNCERTAIN
    volatility_regime: VolatilityRegime = VolatilityRegime.NORMAL
    regime_rationale: str | None = None
    breadth_note: str | None = None

    spy: IndexRead
    qqq: IndexRead
    iwm: IndexRead | None = None

    macro_observations: list[MacroObservation] = Field(default_factory=list)
    sector_observations: list[SectorObservation] = Field(default_factory=list)
    company_catalysts: list[CatalystRead] = Field(default_factory=list)
    risk_events: list[RiskEvent] = Field(default_factory=list)

    unavailable_data: list[str] = Field(
        default_factory=list,
        description="Anything you tried to establish and could not. Stating this is "
        "mandatory; inventing a value instead is a critical failure.",
    )
    overall_relevance_confidence: float = Field(default=0.5, ge=0.0, le=1.0)


__all__ = ["CatalystRead", "IndexRead", "MarketAssessment"]
