"""SQLAlchemy schema.

Design goals, in order:

1. **Reproducibility.** Enough is stored that a recommendation can be
   re-derived: the methodology snapshot, every measurement that fed a score,
   and every point the scoring engine awarded.
2. **Post-hoc analysis.** Rejected candidates are first-class rows, not
   discarded, because "how often did rejected trades work?" is one of the
   questions this system exists to answer.
3. **Portability.** JSON columns use the generic :class:`sqlalchemy.JSON` type
   so the same models run on SQLite in development and PostgreSQL in
   production.

No credential, token, or API key is stored in any table.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.models.common import utcnow


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
class MarketRun(Base, TimestampMixin):
    __tablename__ = "market_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trading_day: Mapped[date] = mapped_column(Date, index=True)
    stage: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")

    methodology_version: Mapped[str] = mapped_column(String(32))
    methodology_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    methodology_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    provider_backends: Mapped[dict] = mapped_column(JSON, default=dict)
    llm_backend: Mapped[str] = mapped_column(String(32), default="scripted")
    universe: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[list] = mapped_column(JSON, default=list)

    agent_runs: Mapped[list[AgentRun]] = relationship(back_populates="run")
    candidates: Mapped[list[TradeCandidateRow]] = relationship(back_populates="run")


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"

    agent_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    agent: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32))
    llm_backend: Mapped[str] = mapped_column(String(32))
    reasoning_mode: Mapped[str] = mapped_column(String(32))
    input_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    output_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    tools_used: Mapped[list] = mapped_column(JSON, default=list)
    providers_queried: Mapped[list] = mapped_column(JSON, default=list)
    providers_failed: Mapped[list] = mapped_column(JSON, default=list)
    missing_data: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    errors: Mapped[list] = mapped_column(JSON, default=list)

    run: Mapped[MarketRun] = relationship(back_populates="agent_runs")


class DataProviderRequest(Base):
    __tablename__ = "data_provider_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    backend: Mapped[str] = mapped_column(String(16))
    operation: Mapped[str] = mapped_column(String(128))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)


class DataQualityFlagRow(Base):
    __tablename__ = "data_quality_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(32))
    ticker: Mapped[str | None] = mapped_column(String(16))
    field: Mapped[str | None] = mapped_column(String(64))
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Market intelligence
# ---------------------------------------------------------------------------
class MarketBriefRow(Base, TimestampMixin):
    __tablename__ = "market_briefs"

    brief_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trading_day: Mapped[date] = mapped_column(Date, index=True)
    market_regime: Mapped[str] = mapped_column(String(32))
    volatility_regime: Mapped[str] = mapped_column(String(32))
    spy_bias: Mapped[str] = mapped_column(String(32))
    qqq_bias: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)


class MarketEvent(Base):
    """Interpreted market/macro observations from a brief."""

    __tablename__ = "market_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    topic: Mapped[str] = mapped_column(String(64))
    observation: Mapped[str] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(String(32))
    importance: Mapped[str] = mapped_column(String(16))
    evidence_quality: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EconomicEventRow(Base):
    __tablename__ = "economic_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    event_code: Mapped[str | None] = mapped_column(String(32), index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_date: Mapped[date | None] = mapped_column(Date, index=True)
    country: Mapped[str] = mapped_column(String(8), default="US")
    importance: Mapped[str] = mapped_column(String(16))
    consensus: Mapped[str | None] = mapped_column(String(64))
    previous: Mapped[str | None] = mapped_column(String(64))
    actual: Mapped[str | None] = mapped_column(String(64))


class NewsItemRow(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(128))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tickers: Mapped[list] = mapped_column(JSON, default=list)
    catalyst_type: Mapped[str] = mapped_column(String(48))
    scope: Mapped[str] = mapped_column(String(24))
    relevance_confidence: Mapped[float] = mapped_column(Float)
    evidence_quality: Mapped[str] = mapped_column(String(32))


class StockCatalyst(Base):
    __tablename__ = "stock_catalysts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    catalyst_type: Mapped[str] = mapped_column(String(48), index=True)
    headline: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(24))
    source: Mapped[str | None] = mapped_column(String(128))
    source_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_direction: Mapped[str] = mapped_column(String(32))
    importance_score: Mapped[float] = mapped_column(Float)
    expected_time_horizon: Mapped[str] = mapped_column(String(24))
    scheduled_event_date: Mapped[date | None] = mapped_column(Date)
    is_scheduled: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_quality: Mapped[str] = mapped_column(String(32))
    already_priced_in: Mapped[bool | None] = mapped_column(Boolean)


# ---------------------------------------------------------------------------
# Candidates, validation, structures
# ---------------------------------------------------------------------------
class TradeCandidateRow(Base, TimestampMixin):
    __tablename__ = "trade_candidates"

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    sector: Mapped[str | None] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(16))
    strategy_type: Mapped[str] = mapped_column(String(32), index=True)
    thesis: Mapped[str] = mapped_column(Text)
    primary_catalyst_type: Mapped[str] = mapped_column(String(48), index=True)
    expected_holding_period: Mapped[str] = mapped_column(String(24))
    expected_move_pct: Mapped[float] = mapped_column(Float)
    underlying_reference_price: Mapped[float | None] = mapped_column(Float)
    invalidation_thesis: Mapped[str] = mapped_column(Text)
    earnings_date: Mapped[date | None] = mapped_column(Date)
    catalyst_date: Mapped[date | None] = mapped_column(Date)
    preliminary_quality: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)

    run: Mapped[MarketRun] = relationship(back_populates="candidates")
    validations: Mapped[list[TradeValidation]] = relationship(back_populates="candidate")


class TradeValidation(Base, TimestampMixin):
    __tablename__ = "trade_validations"

    validation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("trade_candidates.candidate_id"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    overall_verdict: Mapped[str] = mapped_column(String(32), index=True)
    catalyst_verdict: Mapped[str] = mapped_column(String(32))
    flow_supports_thesis: Mapped[bool | None] = mapped_column(Boolean)
    skeptic_summary: Mapped[str] = mapped_column(Text, default="")
    providers_queried: Mapped[list] = mapped_column(JSON, default=list)
    providers_failed: Mapped[list] = mapped_column(JSON, default=list)
    payload: Mapped[dict] = mapped_column(JSON)

    candidate: Mapped[TradeCandidateRow] = relationship(back_populates="validations")


class TechnicalSnapshotRow(Base):
    __tablename__ = "technical_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    price: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)


class OptionsFlowSnapshotRow(Base):
    __tablename__ = "options_flow_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(64), index=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window: Mapped[str] = mapped_column(String(16))
    bullish_premium: Mapped[float | None] = mapped_column(Float)
    bearish_premium: Mapped[float | None] = mapped_column(Float)
    iv_rank: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)


class OptionContractSnapshot(Base):
    """Every leg of a proposed structure, exactly as quoted at decision time."""

    __tablename__ = "option_contract_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    structure_id: Mapped[str] = mapped_column(String(64), index=True)
    leg_action: Mapped[str] = mapped_column(String(8))
    contract_symbol: Mapped[str] = mapped_column(String(48), index=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    right: Mapped[str] = mapped_column(String(8))
    strike: Mapped[float] = mapped_column(Float)
    expiration: Mapped[date] = mapped_column(Date, index=True)
    dte: Mapped[int | None] = mapped_column(Integer)
    bid: Mapped[float | None] = mapped_column(Float)
    ask: Mapped[float | None] = mapped_column(Float)
    mid: Mapped[float | None] = mapped_column(Float)
    spread_pct: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[int | None] = mapped_column(Integer)
    open_interest: Mapped[int | None] = mapped_column(Integer)
    implied_volatility: Mapped[float | None] = mapped_column(Float)
    iv_rank: Mapped[float | None] = mapped_column(Float)
    delta: Mapped[float | None] = mapped_column(Float)
    gamma: Mapped[float | None] = mapped_column(Float)
    theta: Mapped[float | None] = mapped_column(Float)
    vega: Mapped[float | None] = mapped_column(Float)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    provider: Mapped[str] = mapped_column(String(32))
    as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Scoring and recommendations
# ---------------------------------------------------------------------------
class ScoreComponentRow(Base):
    """One component of one score, with every rule that fired inside it."""

    __tablename__ = "score_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    score_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(48), index=True)
    points: Mapped[float] = mapped_column(Float)
    max_points: Mapped[float] = mapped_column(Float)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    unscored_due_to_missing_data: Mapped[list] = mapped_column(JSON, default=list)


class TradeRecommendation(Base, TimestampMixin):
    __tablename__ = "trade_recommendations"

    recommendation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("market_runs.run_id"), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    score_id: Mapped[str] = mapped_column(String(64), index=True)
    structure_id: Mapped[str | None] = mapped_column(String(64), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    strategy_type: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(16))

    total_score: Mapped[float] = mapped_column(Float, index=True)
    classification: Mapped[str] = mapped_column(String(32), index=True)
    classification_label: Mapped[str] = mapped_column(String(64))
    rank: Mapped[int | None] = mapped_column(Integer)
    presentable: Mapped[bool] = mapped_column(Boolean, index=True)

    underlying_price: Mapped[float | None] = mapped_column(Float)
    expiration: Mapped[date | None] = mapped_column(Date)
    long_strike: Mapped[float | None] = mapped_column(Float)
    short_strike: Mapped[float | None] = mapped_column(Float)
    net_debit: Mapped[float | None] = mapped_column(Float)
    max_loss: Mapped[float | None] = mapped_column(Float)
    max_profit: Mapped[float | None] = mapped_column(Float)
    breakeven: Mapped[float | None] = mapped_column(Float)
    reward_to_risk: Mapped[float | None] = mapped_column(Float)
    net_delta: Mapped[float | None] = mapped_column(Float)

    hard_rejections: Mapped[list] = mapped_column(JSON, default=list)
    rejection_summary: Mapped[list] = mapped_column(JSON, default=list)
    methodology_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)

    decisions: Mapped[list[TradeDecision]] = relationship(back_populates="recommendation")


Index("ix_reco_run_rank", TradeRecommendation.run_id, TradeRecommendation.rank)


# ---------------------------------------------------------------------------
# Human decisions and outcomes
# ---------------------------------------------------------------------------
class TradeDecision(Base, TimestampMixin):
    __tablename__ = "trade_decisions"

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("trade_recommendations.recommendation_id"), index=True
    )
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    decision: Mapped[str] = mapped_column(String(24), index=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)

    recommendation: Mapped[TradeRecommendation] = relationship(back_populates="decisions")
    executions: Mapped[list[TradeExecution]] = relationship(back_populates="decision")


class TradeExecution(Base, TimestampMixin):
    """A trade the human actually entered. Recorded, never placed by this system."""

    __tablename__ = "trade_executions"

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(
        ForeignKey("trade_decisions.decision_id"), index=True
    )
    recommendation_id: Mapped[str] = mapped_column(String(64), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    contracts: Mapped[list] = mapped_column(JSON, default=list)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[float] = mapped_column(Float)
    entry_underlying_price: Mapped[float | None] = mapped_column(Float)
    stop_or_invalidation: Mapped[float | None] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    decision: Mapped[TradeDecision] = relationship(back_populates="executions")
    result: Mapped[TradeResult | None] = relationship(back_populates="execution")


class TradeResult(Base, TimestampMixin):
    __tablename__ = "trade_results"

    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("trade_executions.execution_id"), index=True
    )
    recommendation_id: Mapped[str] = mapped_column(String(64), index=True)
    exited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_price: Mapped[float | None] = mapped_column(Float)
    exit_underlying_price: Mapped[float | None] = mapped_column(Float)
    pnl: Mapped[float | None] = mapped_column(Float)
    pnl_pct: Mapped[float | None] = mapped_column(Float)
    max_favorable_excursion: Mapped[float | None] = mapped_column(Float)
    max_adverse_excursion: Mapped[float | None] = mapped_column(Float)
    days_held: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(String(24), index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    execution: Mapped[TradeExecution] = relationship(back_populates="result")


__all__ = [
    "AgentRun",
    "Base",
    "DataProviderRequest",
    "DataQualityFlagRow",
    "EconomicEventRow",
    "MarketBriefRow",
    "MarketEvent",
    "MarketRun",
    "NewsItemRow",
    "OptionContractSnapshot",
    "OptionsFlowSnapshotRow",
    "ScoreComponentRow",
    "StockCatalyst",
    "TechnicalSnapshotRow",
    "TradeCandidateRow",
    "TradeDecision",
    "TradeExecution",
    "TradeRecommendation",
    "TradeResult",
    "TradeValidation",
]
