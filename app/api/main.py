"""Read-and-record HTTP API.

Deliberately narrow. It can trigger a scan, read stored runs and their audit
trails, and record what a human decided. It exposes no endpoint that could
place, modify, or cancel an order, because no such capability exists in the
codebase.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.methodology import get_methodology
from app.config.settings import get_settings
from app.database import models as m
from app.database.session import get_session_factory, init_db
from app.models.enums import HumanDecision, WorkflowStage
from app.services.decisions import (
    RecommendationNotFound,
    record_decision,
    record_execution,
    record_result,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    title="Multi-Agent Options Research",
    description=(
        "Research and ranking only. This service never places, modifies, or "
        "cancels an order. Every recommendation requires human review."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


def get_db() -> Session:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------- models
class ScanRequest(BaseModel):
    stage: WorkflowStage | None = None
    trading_day: date | None = None
    universe: list[str] | None = None
    persist: bool = True


class DecisionRequest(BaseModel):
    decision: HumanDecision
    decided_by: str | None = None
    notes: str | None = None


class ExecutionRequest(BaseModel):
    contracts: list[dict[str, Any]] = Field(default_factory=list)
    quantity: int = 1
    entered_at: datetime
    entry_price: float
    entry_underlying_price: float | None = None
    stop_or_invalidation: float | None = None
    target: float | None = None
    notes: str | None = None


class ResultRequest(BaseModel):
    exited_at: datetime | None = None
    exit_price: float | None = None
    exit_underlying_price: float | None = None
    max_favorable_excursion: float | None = None
    max_adverse_excursion: float | None = None
    notes: str | None = None


# ---------------------------------------------------------------- system info
@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "order_execution": "not implemented"}


@app.get("/config")
def config() -> dict[str, Any]:
    methodology = get_methodology()
    return {
        "settings": get_settings().safe_dict(),
        "methodology_version": methodology.version,
        "methodology_fingerprint": methodology.fingerprint(),
        "score_weights": methodology.score_weights.as_dict(),
        "classification_bands": [b.model_dump() for b in methodology.classification_bands],
        "hard_rejections": methodology.hard_rejections.model_dump(),
        "allowed_strategies": methodology.strategies.allowed,
    }


# ----------------------------------------------------------------------- runs
@app.post("/scans")
def run_scan(request: ScanRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.services.orchestrator import Orchestrator
    from app.services.persistence import persist_scan

    orchestrator = Orchestrator(trading_day=request.trading_day, universe=request.universe)
    result = orchestrator.run(stage=request.stage)
    if request.persist:
        persist_scan(result, db)

    return {
        "run_id": result.run_id,
        "stage": result.stage.value,
        "trading_day": result.trading_day.isoformat(),
        "candidates_considered": len(result.evaluations),
        "presentable": len(result.report.top_trades),
        "rejected": len(result.report.rejected),
        "report": result.report.model_dump(mode="json"),
    }


@app.get("/runs")
def list_runs(limit: int = Query(20, le=100), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(m.MarketRun).order_by(m.MarketRun.started_at.desc()).limit(limit)
    ).all()
    return [
        {
            "run_id": r.run_id,
            "trading_day": r.trading_day.isoformat(),
            "stage": r.stage,
            "status": r.status,
            "started_at": r.started_at,
            "methodology_fingerprint": r.methodology_fingerprint,
            "provider_backends": r.provider_backends,
            "llm_backend": r.llm_backend,
        }
        for r in rows
    ]


@app.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(m.MarketRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id}")
    recos = db.scalars(
        select(m.TradeRecommendation)
        .where(m.TradeRecommendation.run_id == run_id)
        .order_by(m.TradeRecommendation.total_score.desc())
    ).all()
    return {
        "run_id": run.run_id,
        "trading_day": run.trading_day.isoformat(),
        "stage": run.stage,
        "methodology": {
            "version": run.methodology_version,
            "fingerprint": run.methodology_fingerprint,
        },
        "provider_backends": run.provider_backends,
        "notes": run.notes,
        "recommendations": [
            {
                "recommendation_id": r.recommendation_id,
                "ticker": r.ticker,
                "strategy": r.strategy_type,
                "score": r.total_score,
                "classification": r.classification_label,
                "rank": r.rank,
                "presentable": r.presentable,
                "expiration": r.expiration.isoformat() if r.expiration else None,
                "long_strike": r.long_strike,
                "short_strike": r.short_strike,
                "net_debit": r.net_debit,
                "max_loss": r.max_loss,
                "max_profit": r.max_profit,
                "breakeven": r.breakeven,
                "reward_to_risk": r.reward_to_risk,
                "hard_rejections": r.hard_rejections,
                "rejection_summary": r.rejection_summary,
            }
            for r in recos
        ],
    }


@app.get("/runs/{run_id}/audit")
def get_run_audit(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Everything needed to explain why a run recommended what it did."""
    if db.get(m.MarketRun, run_id) is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id}")

    agents = db.scalars(select(m.AgentRun).where(m.AgentRun.run_id == run_id)).all()
    components = db.scalars(
        select(m.ScoreComponentRow).where(m.ScoreComponentRow.run_id == run_id)
    ).all()
    requests = db.scalars(
        select(m.DataProviderRequest).where(m.DataProviderRequest.run_id == run_id)
    ).all()
    flags = db.scalars(
        select(m.DataQualityFlagRow).where(m.DataQualityFlagRow.run_id == run_id)
    ).all()

    return {
        "agent_runs": [
            {
                "agent": a.agent,
                "status": a.status,
                "duration_ms": a.duration_ms,
                "reasoning_mode": a.reasoning_mode,
                "llm_backend": a.llm_backend,
                "providers_queried": a.providers_queried,
                "providers_failed": a.providers_failed,
                "missing_data": a.missing_data,
                "warnings": a.warnings,
                "errors": a.errors,
            }
            for a in agents
        ],
        "score_components": [
            {
                "candidate_id": c.candidate_id,
                "name": c.name,
                "points": c.points,
                "max_points": c.max_points,
                "reasons": c.reasons,
                "unscored_due_to_missing_data": c.unscored_due_to_missing_data,
            }
            for c in components
        ],
        "provider_requests": [
            {
                "provider": r.provider,
                "backend": r.backend,
                "operation": r.operation,
                "success": r.success,
                "duration_ms": r.duration_ms,
                "error": r.error,
            }
            for r in requests
        ],
        "data_quality_flags": [
            {"code": f.code, "severity": f.severity, "message": f.message, "ticker": f.ticker}
            for f in flags
        ],
    }


# ------------------------------------------------------------------ decisions
@app.post("/recommendations/{recommendation_id}/decision")
def post_decision(
    recommendation_id: str, request: DecisionRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    try:
        row = record_decision(
            db,
            recommendation_id,
            request.decision,
            decided_by=request.decided_by,
            notes=request.notes,
        )
    except RecommendationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.flush()
    return {"decision_id": row.decision_id, "decision": row.decision}


@app.post("/decisions/{decision_id}/execution")
def post_execution(
    decision_id: str, request: ExecutionRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Record a fill the human already made. This does not place an order."""
    try:
        row = record_execution(db, decision_id, **request.model_dump())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.flush()
    return {"execution_id": row.execution_id}


@app.post("/executions/{execution_id}/result")
def post_result(
    execution_id: str, request: ResultRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    try:
        row = record_result(db, execution_id, **request.model_dump())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.flush()
    return {
        "result_id": row.result_id,
        "pnl": row.pnl,
        "pnl_pct": row.pnl_pct,
        "outcome": row.outcome,
        "days_held": row.days_held,
    }


__all__ = ["app"]
