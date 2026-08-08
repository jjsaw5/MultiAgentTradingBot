"""Agent 1 output schema: the MarketBrief.

This is the only thing Agent 1 is permitted to emit. Anything the agent
cannot support with a source must be marked ``EvidenceQuality.UNVERIFIED``
or omitted.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field, field_validator

from app.models.common import Base, SourceReference, new_id, utcnow
from app.models.enums import (
    Bias,
    CatalystScope,
    CatalystType,
    EventImportance,
    EvidenceQuality,
    MarketRegime,
    TimeHorizon,
    VolatilityRegime,
)


class EconomicEvent(Base):
    """A scheduled macro release or central-bank event."""

    name: str
    event_code: str | None = Field(
        default=None, description="Short code, e.g. CPI / FOMC / NFP, when recognised."
    )
    scheduled_for: datetime | None = None
    scheduled_date: date | None = None
    country: str = "US"
    importance: EventImportance = EventImportance.MEDIUM
    consensus: str | None = None
    previous: str | None = None
    actual: str | None = None
    notes: str | None = None
    source: SourceReference | None = None


class MacroObservation(Base):
    """An interpreted macro condition, e.g. 'yields backing off highs'."""

    topic: str
    observation: str
    direction: Bias = Bias.NEUTRAL
    importance: EventImportance = EventImportance.MEDIUM
    evidence_quality: EvidenceQuality = EvidenceQuality.INTERPRETATION
    sources: list[SourceReference] = Field(default_factory=list)


class SectorObservation(Base):
    sector: str
    bias: Bias
    rationale: str
    relative_strength_note: str | None = None
    representative_tickers: list[str] = Field(default_factory=list)
    importance: EventImportance = EventImportance.MEDIUM
    evidence_quality: EvidenceQuality = EvidenceQuality.INTERPRETATION
    sources: list[SourceReference] = Field(default_factory=list)


class NewsItem(Base):
    headline: str
    summary: str | None = None
    url: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    tickers: list[str] = Field(default_factory=list)
    catalyst_type: CatalystType = CatalystType.OTHER
    scope: CatalystScope = CatalystScope.COMPANY
    relevance_confidence: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Agent's confidence this item is RELEVANT, "
        "not its confidence in a trade. Never feeds the final trade score directly."
    )
    evidence_quality: EvidenceQuality = EvidenceQuality.REPORTED


class CompanyCatalyst(Base):
    """A ticker-specific reason the underlying could move."""

    ticker: str
    catalyst_type: CatalystType
    headline: str
    description: str
    scope: CatalystScope = CatalystScope.COMPANY
    source: str | None = None
    source_url: str | None = None
    published_at: datetime | None = None
    expected_direction: Bias = Bias.NEUTRAL
    importance_score: float = Field(
        default=0.5, ge=0.0, le=1.0, description="How market-moving the event is, 0..1."
    )
    expected_time_horizon: TimeHorizon = TimeHorizon.WEEKS_2_4
    scheduled_event_date: date | None = None
    is_scheduled: bool = False
    evidence_quality: EvidenceQuality = EvidenceQuality.REPORTED
    already_priced_in: bool | None = None
    conflicting_events: list[str] = Field(default_factory=list)

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class RiskEvent(Base):
    """Something that could invalidate the whole day's positioning."""

    description: str
    scope: CatalystScope
    occurs_at: datetime | None = None
    importance: EventImportance = EventImportance.MEDIUM
    affected_tickers: list[str] = Field(default_factory=list)


class IndexContext(Base):
    symbol: str
    bias: Bias
    last_price: float | None = None
    change_pct_1d: float | None = None
    change_pct_5d: float | None = None
    above_sma20: bool | None = None
    above_sma50: bool | None = None
    key_support: float | None = None
    key_resistance: float | None = None
    notes: str | None = None


class VolatilityContext(Base):
    vix_level: float | None = None
    vix_change_pct: float | None = None
    regime: VolatilityRegime = VolatilityRegime.NORMAL
    term_structure_note: str | None = None
    notes: str | None = None


class MarketBrief(Base):
    """Agent 1's structured worldview for a single run."""

    brief_id: str = Field(default_factory=lambda: new_id("brief"))
    run_id: str
    generated_at: datetime = Field(default_factory=utcnow)
    as_of_trading_day: date

    market_regime: MarketRegime = MarketRegime.UNCERTAIN
    volatility_regime: VolatilityRegime = VolatilityRegime.NORMAL

    spy: IndexContext
    qqq: IndexContext
    iwm: IndexContext | None = None
    volatility: VolatilityContext = Field(default_factory=VolatilityContext)

    breadth_note: str | None = None
    regime_rationale: str | None = None

    macro_observations: list[MacroObservation] = Field(default_factory=list)
    upcoming_economic_events: list[EconomicEvent] = Field(default_factory=list)
    sector_observations: list[SectorObservation] = Field(default_factory=list)
    company_catalysts: list[CompanyCatalyst] = Field(default_factory=list)
    news_items: list[NewsItem] = Field(default_factory=list)
    risk_events: list[RiskEvent] = Field(default_factory=list)

    sources: list[SourceReference] = Field(default_factory=list)
    unavailable_data: list[str] = Field(
        default_factory=list,
        description="Things the agent tried to establish and could not. Stating this "
        "is mandatory; fabricating a value instead is a hard failure.",
    )
    overall_relevance_confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    def catalysts_for(self, ticker: str) -> list[CompanyCatalyst]:
        t = ticker.upper()
        return [c for c in self.company_catalysts if c.ticker == t]

    def sector_bias(self, sector: str | None) -> Bias | None:
        if not sector:
            return None
        for obs in self.sector_observations:
            if obs.sector.lower() == sector.lower():
                return obs.bias
        return None


__all__ = [
    "CompanyCatalyst",
    "EconomicEvent",
    "IndexContext",
    "MacroObservation",
    "MarketBrief",
    "NewsItem",
    "RiskEvent",
    "SectorObservation",
    "VolatilityContext",
]
