"""The final human-facing artefact: a ranked trade report."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from app.models.common import Base, DataQualityFlag, new_id, utcnow
from app.models.enums import Classification, WorkflowStage
from app.models.market_brief import EconomicEvent, MarketBrief, RiskEvent
from app.models.scoring import ScoreBreakdown
from app.models.trade_candidate import TradeCandidate
from app.models.trade_structure import ProposedTrade, RiskReward
from app.models.validation import ValidationReport


class MarketSummary(Base):
    market_regime: str
    volatility_regime: str
    spy_bias: str
    spy_note: str | None = None
    qqq_bias: str
    qqq_note: str | None = None
    vix_level: float | None = None
    vix_note: str | None = None
    major_event_risks_today: list[RiskEvent] = Field(default_factory=list)
    upcoming_economic_events: list[EconomicEvent] = Field(default_factory=list)
    regime_rationale: str | None = None


class RankedTrade(Base):
    rank: int
    candidate: TradeCandidate
    validation: ValidationReport
    trade: ProposedTrade
    risk_reward: RiskReward
    breakdown: ScoreBreakdown
    classification: Classification
    classification_label: str
    entry_conditions: list[str] = Field(default_factory=list)
    profit_targets: list[str] = Field(default_factory=list)
    invalidation: str = ""
    risks: list[str] = Field(default_factory=list)
    flow_confirmation: str = ""
    technical_thesis: str = ""


class RejectedTrade(Base):
    candidate: TradeCandidate
    score: float | None = None
    classification: Classification = Classification.REJECTED
    reasons: list[str] = Field(default_factory=list)
    breakdown: ScoreBreakdown | None = None
    validation: ValidationReport | None = None


class TradeReport(Base):
    report_id: str = Field(default_factory=lambda: new_id("rpt"))
    run_id: str
    generated_at: datetime = Field(default_factory=utcnow)
    trading_day: date
    stage: WorkflowStage
    methodology_version: str
    methodology_fingerprint: str

    market_summary: MarketSummary
    top_trades: list[RankedTrade] = Field(default_factory=list)
    rejected: list[RejectedTrade] = Field(default_factory=list)

    candidates_considered: int = 0
    data_quality_flags: list[DataQualityFlag] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    brief: MarketBrief | None = None


__all__ = ["MarketSummary", "RankedTrade", "RejectedTrade", "TradeReport"]
