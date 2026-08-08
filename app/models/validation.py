"""Agent 3 output: skeptical validation of a candidate against hard data.

The agent's job is to look for disconfirming evidence. Its structured verdicts
per category feed the deterministic scoring engine; the agent supplies
*findings*, the engine supplies *points*.
"""

from __future__ import annotations

from pydantic import Field

from app.models.common import Base, DataQualityFlag, MissingData, new_id
from app.models.enums import ValidationVerdict
from app.models.market_data import FlowSnapshot, Quote, TechnicalSnapshot


class CategoryFinding(Base):
    """One validation category's outcome."""

    category: str
    verdict: ValidationVerdict
    summary: str
    supporting_observations: list[str] = Field(default_factory=list)
    disconfirming_observations: list[str] = Field(default_factory=list)
    data_used: list[str] = Field(default_factory=list)
    missing: list[MissingData] = Field(default_factory=list)


class CatalystValidation(Base):
    exists: bool | None = None
    is_recent: bool | None = None
    is_material: bool | None = None
    already_priced_in: bool | None = None
    has_upcoming_timing_relevance: bool | None = None
    conflicts_with_scheduled_event: bool | None = None
    days_since_published: int | None = None
    days_until_catalyst: int | None = None
    verdict: ValidationVerdict = ValidationVerdict.INCONCLUSIVE
    notes: str = ""


class MarketAlignment(Base):
    spy_aligned: bool | None = None
    qqq_aligned: bool | None = None
    sector_aligned: bool | None = None
    relative_strength_20d: float | None = None
    fighting_the_tape: bool | None = None
    notes: str = ""


class FlowInterpretation(Base):
    """Explicitly separates measurement from inference.

    ``directional_conclusion`` may legitimately be ``INCONCLUSIVE``: a large
    print is not proof of direction, and heavy multi-leg flow makes naive
    call/put ratios misleading.
    """

    directional_conclusion: ValidationVerdict = ValidationVerdict.INCONCLUSIVE
    supports_thesis: bool | None = None
    reasoning: str = ""
    caveats: list[str] = Field(default_factory=list)


class UnderlyingPriceCheck(Base):
    """Cross-provider agreement on the one number everything depends on."""

    prices_by_provider: dict[str, float] = Field(default_factory=dict)
    consensus_price: float | None = None
    max_disagreement_pct: float | None = None
    reconciled: bool = True


class ValidationReport(Base):
    validation_id: str = Field(default_factory=lambda: new_id("val"))
    run_id: str
    candidate_id: str
    ticker: str

    overall_verdict: ValidationVerdict = ValidationVerdict.INCONCLUSIVE
    skeptic_summary: str = Field(
        default="", description="What would have to be true for this trade to fail."
    )

    quote: Quote | None = None
    technicals: TechnicalSnapshot | None = None
    flow: FlowSnapshot | None = None

    price_check: UnderlyingPriceCheck = Field(default_factory=UnderlyingPriceCheck)
    catalyst: CatalystValidation = Field(default_factory=CatalystValidation)
    alignment: MarketAlignment = Field(default_factory=MarketAlignment)
    flow_interpretation: FlowInterpretation = Field(default_factory=FlowInterpretation)

    findings: list[CategoryFinding] = Field(default_factory=list)
    missing_data: list[MissingData] = Field(default_factory=list)
    data_quality_flags: list[DataQualityFlag] = Field(default_factory=list)
    providers_queried: list[str] = Field(default_factory=list)
    providers_failed: list[str] = Field(default_factory=list)

    def finding(self, category: str) -> CategoryFinding | None:
        return next((f for f in self.findings if f.category == category), None)


__all__ = [
    "CatalystValidation",
    "CategoryFinding",
    "FlowInterpretation",
    "MarketAlignment",
    "UnderlyingPriceCheck",
    "ValidationReport",
]
