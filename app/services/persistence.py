"""Persist a completed scan.

Everything needed to answer "why was this recommended, at that moment?" is
written here: the methodology snapshot, the brief, each candidate, the
validator's findings, every contract quote as it stood, every score component
with its individual rules, and the provider request log.

Rejected candidates are persisted with the same fidelity as accepted ones.
That is deliberate -- the future performance engine needs the rejects to
answer "how often did the trades we passed on actually work?".
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.database import models as m
from app.database.session import session_scope
from app.models.common import new_id
from app.services.orchestrator import CandidateEvaluation, ScanResult


def persist_scan(result: ScanResult, session: Session | None = None) -> str:
    if session is not None:
        _write(result, session)
        session.flush()
        return result.run_id
    with session_scope() as s:
        _write(result, s)
    return result.run_id


def _write(result: ScanResult, s: Session) -> None:
    s.add(
        m.MarketRun(
            run_id=result.run_id,
            trading_day=result.trading_day,
            stage=result.stage.value,
            started_at=result.started_at,
            completed_at=result.completed_at,
            status="COMPLETED",
            methodology_version=result.methodology.version,
            methodology_fingerprint=result.methodology.fingerprint(),
            methodology_snapshot=result.methodology.snapshot(),
            provider_backends=result.provider_backends,
            llm_backend=result.llm_backend,
            universe=result.universe,
            notes=result.notes,
        )
    )
    # SQLAlchemy only orders inserts across tables joined by a relationship(),
    # not by a bare foreign key, so the parent row is flushed before anything
    # that references it.
    s.flush()

    for rec in result.agent_runs:
        s.add(
            m.AgentRun(
                agent_run_id=rec.agent_run_id,
                run_id=result.run_id,
                agent=rec.agent.value,
                started_at=rec.started_at,
                ended_at=rec.ended_at,
                duration_ms=rec.duration_ms,
                status=rec.status.value,
                llm_backend=rec.llm_backend,
                reasoning_mode=rec.reasoning_mode,
                input_summary=_jsonable(rec.input_summary),
                output_summary=_jsonable(rec.output_summary),
                tools_used=rec.tools_used,
                providers_queried=rec.providers_queried,
                providers_failed=rec.providers_failed,
                missing_data=rec.missing_data,
                warnings=rec.warnings,
                errors=rec.errors,
            )
        )

    for req in result.provider_requests:
        s.add(
            m.DataProviderRequest(
                run_id=result.run_id,
                provider=req.provider.value,
                backend=req.backend,
                operation=req.operation,
                params=_jsonable(req.params),
                started_at=req.started_at,
                duration_ms=req.duration_ms,
                success=req.success,
                error=req.error,
            )
        )

    for flag in result.data_quality_flags:
        s.add(
            m.DataQualityFlagRow(
                run_id=result.run_id,
                code=flag.code,
                severity=flag.severity.value,
                message=flag.message,
                provider=flag.provider.value if flag.provider else None,
                ticker=flag.ticker,
                field=flag.field,
                context=_jsonable(flag.context),
                created_at=flag.created_at,
            )
        )

    _write_brief(result, s)

    for e in result.evaluations:
        _write_candidate(result, e, s)


def _write_brief(result: ScanResult, s: Session) -> None:
    brief = result.brief
    s.add(
        m.MarketBriefRow(
            brief_id=brief.brief_id,
            run_id=result.run_id,
            generated_at=brief.generated_at,
            trading_day=brief.as_of_trading_day,
            market_regime=brief.market_regime.value,
            volatility_regime=brief.volatility_regime.value,
            spy_bias=brief.spy.bias.value,
            qqq_bias=brief.qqq.bias.value,
            payload=brief.model_dump(mode="json"),
        )
    )

    for obs in brief.macro_observations:
        s.add(
            m.MarketEvent(
                run_id=result.run_id,
                topic=obs.topic,
                observation=obs.observation,
                direction=obs.direction.value,
                importance=obs.importance.value,
                evidence_quality=obs.evidence_quality.value,
            )
        )

    for ev in brief.upcoming_economic_events:
        s.add(
            m.EconomicEventRow(
                run_id=result.run_id,
                name=ev.name,
                event_code=ev.event_code,
                scheduled_for=ev.scheduled_for,
                scheduled_date=ev.scheduled_date,
                country=ev.country,
                importance=ev.importance.value,
                consensus=ev.consensus,
                previous=ev.previous,
                actual=ev.actual,
            )
        )

    for item in brief.news_items:
        s.add(
            m.NewsItemRow(
                run_id=result.run_id,
                headline=item.headline,
                summary=item.summary,
                url=item.url,
                publisher=item.publisher,
                published_at=item.published_at,
                retrieved_at=item.retrieved_at,
                tickers=item.tickers,
                catalyst_type=item.catalyst_type.value,
                scope=item.scope.value,
                relevance_confidence=item.relevance_confidence,
                evidence_quality=item.evidence_quality.value,
            )
        )

    for cat in brief.company_catalysts:
        s.add(
            m.StockCatalyst(
                run_id=result.run_id,
                ticker=cat.ticker,
                catalyst_type=cat.catalyst_type.value,
                headline=cat.headline,
                description=cat.description,
                scope=cat.scope.value,
                source=cat.source,
                source_url=cat.source_url,
                published_at=cat.published_at,
                expected_direction=cat.expected_direction.value,
                importance_score=cat.importance_score,
                expected_time_horizon=cat.expected_time_horizon.value,
                scheduled_event_date=cat.scheduled_event_date,
                is_scheduled=cat.is_scheduled,
                evidence_quality=cat.evidence_quality.value,
                already_priced_in=cat.already_priced_in,
            )
        )


def _write_candidate(result: ScanResult, e: CandidateEvaluation, s: Session) -> None:
    c = e.candidate
    s.add(
        m.TradeCandidateRow(
            candidate_id=c.candidate_id,
            run_id=result.run_id,
            ticker=c.ticker,
            sector=c.sector,
            direction=c.direction.value,
            strategy_type=c.strategy_type.value,
            thesis=c.thesis,
            primary_catalyst_type=c.primary_catalyst.catalyst_type.value,
            expected_holding_period=c.expected_holding_period.value,
            expected_move_pct=c.expected_move.percent,
            underlying_reference_price=c.underlying_reference_price,
            invalidation_thesis=c.invalidation_thesis,
            earnings_date=c.earnings_date,
            catalyst_date=c.catalyst_date,
            preliminary_quality=c.preliminary_quality.value,
            payload=c.model_dump(mode="json"),
        )
    )
    s.flush()  # trade_validations references trade_candidates

    v = e.validation
    s.add(
        m.TradeValidation(
            validation_id=v.validation_id,
            run_id=result.run_id,
            candidate_id=c.candidate_id,
            ticker=v.ticker,
            overall_verdict=v.overall_verdict.value,
            catalyst_verdict=v.catalyst.verdict.value,
            flow_supports_thesis=v.flow_interpretation.supports_thesis,
            skeptic_summary=v.skeptic_summary,
            providers_queried=v.providers_queried,
            providers_failed=v.providers_failed,
            payload=v.model_dump(mode="json"),
        )
    )

    if v.technicals is not None:
        s.add(
            m.TechnicalSnapshotRow(
                run_id=result.run_id,
                candidate_id=c.candidate_id,
                symbol=v.technicals.symbol,
                as_of=v.technicals.as_of,
                price=v.technicals.price,
                payload=v.technicals.model_dump(mode="json"),
            )
        )

    if v.flow is not None:
        s.add(
            m.OptionsFlowSnapshotRow(
                run_id=result.run_id,
                candidate_id=c.candidate_id,
                underlying=v.flow.underlying,
                as_of=v.flow.as_of,
                window=v.flow.window,
                bullish_premium=v.flow.bullish_premium,
                bearish_premium=v.flow.bearish_premium,
                iv_rank=v.flow.iv_rank,
                payload=v.flow.model_dump(mode="json"),
            )
        )

    if e.trade is not None:
        for leg in e.trade.legs:
            oc = leg.contract
            s.add(
                m.OptionContractSnapshot(
                    run_id=result.run_id,
                    candidate_id=c.candidate_id,
                    structure_id=e.trade.structure_id,
                    leg_action=leg.action,
                    contract_symbol=oc.symbol,
                    underlying=oc.underlying,
                    right=oc.right.value,
                    strike=oc.strike,
                    expiration=oc.expiration,
                    dte=oc.dte(result.trading_day),
                    bid=oc.bid,
                    ask=oc.ask,
                    mid=oc.mid,
                    spread_pct=oc.spread_pct,
                    volume=oc.volume,
                    open_interest=oc.open_interest,
                    implied_volatility=oc.implied_volatility,
                    iv_rank=oc.iv_rank,
                    delta=oc.delta,
                    gamma=oc.gamma,
                    theta=oc.theta,
                    vega=oc.vega,
                    stale=oc.stale,
                    provider=oc.provenance.provider.value,
                    as_of=oc.provenance.as_of,
                    retrieved_at=oc.provenance.retrieved_at,
                )
            )

    for comp in e.breakdown.components:
        s.add(
            m.ScoreComponentRow(
                run_id=result.run_id,
                candidate_id=c.candidate_id,
                score_id=e.breakdown.score_id,
                name=comp.name,
                points=comp.points,
                max_points=comp.max_points,
                reasons=[r.model_dump(mode="json") for r in comp.reasons],
                unscored_due_to_missing_data=comp.unscored_due_to_missing_data,
            )
        )

    trade = e.trade
    short_leg = trade.short_leg if trade else None
    s.add(
        m.TradeRecommendation(
            recommendation_id=new_id("reco"),
            run_id=result.run_id,
            candidate_id=c.candidate_id,
            score_id=e.breakdown.score_id,
            structure_id=trade.structure_id if trade else None,
            ticker=c.ticker,
            strategy_type=c.strategy_type.value,
            direction=c.direction.value,
            total_score=e.breakdown.total,
            classification=e.scored.classification.value,
            classification_label=e.scored.classification_label,
            rank=e.scored.rank,
            presentable=e.scored.presentable,
            underlying_price=trade.underlying_price if trade else None,
            expiration=trade.expiration if trade else None,
            long_strike=trade.long_leg.contract.strike if trade else None,
            short_strike=short_leg.contract.strike if short_leg else None,
            net_debit=trade.net_debit_conservative if trade else None,
            max_loss=trade.max_loss if trade else None,
            max_profit=trade.max_profit if trade else None,
            breakeven=trade.breakeven if trade else None,
            reward_to_risk=e.risk_reward.reward_to_risk if e.risk_reward else None,
            net_delta=trade.net_delta if trade else None,
            hard_rejections=[hr.model_dump(mode="json") for hr in e.breakdown.hard_rejections],
            rejection_summary=e.scored.rejection_summary,
            methodology_fingerprint=e.breakdown.methodology_fingerprint,
            payload={
                "structure": trade.model_dump(mode="json") if trade else None,
                "risk_reward": e.risk_reward.model_dump(mode="json") if e.risk_reward else None,
                "breakdown": e.breakdown.model_dump(mode="json"),
                "selection_notes": e.selection_notes,
                "alternatives": [a.model_dump(mode="json") for a in e.alternatives],
            },
        )
    )


def _jsonable(value):
    """Coerce arbitrary values into something the JSON column accepts."""
    import json

    return json.loads(json.dumps(value, default=str))


__all__ = ["persist_scan"]
