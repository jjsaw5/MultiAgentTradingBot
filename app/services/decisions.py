"""Human decision and outcome tracking.

The system recommends; a person decides. This module records that decision and,
if the person entered the trade, what actually happened -- so the methodology
can eventually be measured against reality rather than against its own opinion
of itself.

Nothing here places an order. ``record_execution`` records a fill the human
already made elsewhere.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import models as m
from app.models.common import new_id, utcnow
from app.models.enums import HumanDecision


class RecommendationNotFound(LookupError):
    pass


def record_decision(
    session: Session,
    recommendation_id: str,
    decision: HumanDecision,
    *,
    decided_by: str | None = None,
    notes: str | None = None,
) -> m.TradeDecision:
    reco = session.get(m.TradeRecommendation, recommendation_id)
    if reco is None:
        raise RecommendationNotFound(recommendation_id)

    row = m.TradeDecision(
        decision_id=new_id("dec"),
        recommendation_id=recommendation_id,
        run_id=reco.run_id,
        decision=decision.value,
        decided_at=utcnow(),
        decided_by=decided_by,
        notes=notes,
    )
    session.add(row)
    return row


def record_execution(
    session: Session,
    decision_id: str,
    *,
    contracts: list[dict],
    quantity: int,
    entered_at: datetime,
    entry_price: float,
    entry_underlying_price: float | None = None,
    stop_or_invalidation: float | None = None,
    target: float | None = None,
    notes: str | None = None,
) -> m.TradeExecution:
    decision = session.get(m.TradeDecision, decision_id)
    if decision is None:
        raise LookupError(decision_id)
    if decision.decision != HumanDecision.ENTERED.value:
        # Keep the record honest: an execution implies the decision was ENTERED.
        decision.decision = HumanDecision.ENTERED.value

    row = m.TradeExecution(
        execution_id=new_id("exec"),
        decision_id=decision_id,
        recommendation_id=decision.recommendation_id,
        ticker=session.get(m.TradeRecommendation, decision.recommendation_id).ticker,
        contracts=contracts,
        quantity=quantity,
        entered_at=entered_at,
        entry_price=entry_price,
        entry_underlying_price=entry_underlying_price,
        stop_or_invalidation=stop_or_invalidation,
        target=target,
        notes=notes,
    )
    session.add(row)
    return row


def record_result(
    session: Session,
    execution_id: str,
    *,
    exited_at: datetime | None = None,
    exit_price: float | None = None,
    exit_underlying_price: float | None = None,
    max_favorable_excursion: float | None = None,
    max_adverse_excursion: float | None = None,
    notes: str | None = None,
) -> m.TradeResult:
    execution = session.get(m.TradeExecution, execution_id)
    if execution is None:
        raise LookupError(execution_id)

    pnl = pnl_pct = None
    if exit_price is not None:
        # Option prices are per share; a contract controls 100.
        pnl = round((exit_price - execution.entry_price) * 100 * execution.quantity, 2)
        cost = execution.entry_price * 100 * execution.quantity
        pnl_pct = round(pnl / cost * 100, 3) if cost else None

    days_held = None
    if exited_at is not None:
        days_held = (exited_at.date() - execution.entered_at.date()).days

    outcome = None
    if pnl is not None:
        outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "SCRATCH")

    row = m.TradeResult(
        result_id=new_id("res"),
        execution_id=execution_id,
        recommendation_id=execution.recommendation_id,
        exited_at=exited_at,
        exit_price=exit_price,
        exit_underlying_price=exit_underlying_price,
        pnl=pnl,
        pnl_pct=pnl_pct,
        max_favorable_excursion=max_favorable_excursion,
        max_adverse_excursion=max_adverse_excursion,
        days_held=days_held,
        outcome=outcome,
        notes=notes,
    )
    session.add(row)
    return row


def pending_recommendations(session: Session, run_id: str | None = None) -> list[m.TradeRecommendation]:
    stmt = select(m.TradeRecommendation).where(m.TradeRecommendation.presentable.is_(True))
    if run_id:
        stmt = stmt.where(m.TradeRecommendation.run_id == run_id)
    return list(session.scalars(stmt.order_by(m.TradeRecommendation.rank)))


__all__ = [
    "RecommendationNotFound",
    "pending_recommendations",
    "record_decision",
    "record_execution",
    "record_result",
]
