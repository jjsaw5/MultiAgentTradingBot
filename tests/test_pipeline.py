"""End-to-end pipeline, persistence, and decision tracking."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config.methodology import load_methodology
from app.config.settings import Settings
from app.database import models as m
from app.database.models import Base
from app.database.session import get_engine
from app.models.enums import Classification, HumanDecision, WorkflowStage
from app.providers.registry import build_providers
from app.services.decisions import record_decision, record_execution, record_result
from app.services.orchestrator import Orchestrator
from app.services.persistence import persist_scan

SCAN_DAY = date(2024, 6, 3)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        llm_backend="scripted",
        fmp_backend="mock",
        robinhood_backend="mock",
        unusual_whales_backend="mock",
        news_backend="mock",
        mock_seed=20240101,
    )


@pytest.fixture(scope="module")
def scan(settings):
    methodology = load_methodology("config/methodology.yaml")
    orchestrator = Orchestrator(
        settings=settings,
        methodology=methodology,
        providers=build_providers(SCAN_DAY, settings),
        trading_day=SCAN_DAY,
    )
    return orchestrator.run(stage=WorkflowStage.MARKET_OPEN)


@pytest.fixture()
def db_session():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()
    engine.dispose()


# ------------------------------------------------------------------ pipeline
def test_pipeline_runs_end_to_end(scan):
    assert scan.run_id
    assert scan.brief is not None
    assert scan.report is not None
    assert scan.evaluations, "expected at least one candidate to be evaluated"


def test_every_candidate_is_scored_including_rejects(scan):
    for e in scan.evaluations:
        assert len(e.breakdown.components) == 8
        assert 0 <= e.breakdown.total <= 100


def test_rejected_candidates_are_retained_not_discarded(scan):
    presentable = [e for e in scan.evaluations if e.scored.presentable]
    rejected = [e for e in scan.evaluations if not e.scored.presentable]
    assert rejected, "the demo universe is built to produce rejections"
    assert len(presentable) + len(rejected) == len(scan.evaluations)
    assert len(scan.report.rejected) == len(rejected)


def test_every_rejection_states_a_reason(scan):
    for rej in scan.report.rejected:
        assert rej.reasons


def test_ranking_is_ordered_by_score(scan):
    scores = [t.breakdown.total for t in scan.report.top_trades]
    assert scores == sorted(scores, reverse=True)
    assert [t.rank for t in scan.report.top_trades] == list(
        range(1, len(scan.report.top_trades) + 1)
    )


def test_agent_runs_are_traced(scan):
    agents = {r.agent.value for r in scan.agent_runs}
    assert "market_intelligence" in agents
    assert "opportunity_generator" in agents
    if scan.evaluations:
        assert "trade_validator" in agents
    for r in scan.agent_runs:
        assert r.duration_ms is not None
        assert r.reasoning_mode in ("llm", "heuristic")


def test_provider_requests_are_logged_without_credentials(scan):
    assert scan.provider_requests
    for req in scan.provider_requests:
        assert not any("key" in k.lower() for k in req.params)


def test_the_run_is_reproducible_for_a_given_seed(settings):
    methodology = load_methodology("config/methodology.yaml")

    def once():
        return Orchestrator(
            settings=settings,
            methodology=methodology,
            providers=build_providers(SCAN_DAY, settings),
            trading_day=SCAN_DAY,
        ).run(stage=WorkflowStage.MARKET_OPEN)

    a, b = once(), once()
    assert [(e.candidate.ticker, e.breakdown.total) for e in a.evaluations] == [
        (e.candidate.ticker, e.breakdown.total) for e in b.evaluations
    ]


def test_premarket_never_presents_an_entry(settings):
    methodology = load_methodology("config/methodology.yaml")
    result = Orchestrator(
        settings=settings,
        methodology=methodology,
        providers=build_providers(SCAN_DAY, settings),
        trading_day=SCAN_DAY,
    ).run(stage=WorkflowStage.PREMARKET)

    assert result.report.top_trades == []
    assert all(
        e.scored.classification is Classification.REJECTED for e in result.evaluations
    )
    assert any(
        "STALE_QUOTES" in hr.code.value
        for e in result.evaluations
        for hr in e.breakdown.hard_rejections
    )


def test_candidate_cap_is_enforced(settings):
    methodology = load_methodology("config/methodology.yaml")
    capped = methodology.model_copy(
        update={"pipeline": methodology.pipeline.model_copy(update={"max_candidates_per_run": 2})}
    )
    result = Orchestrator(
        settings=settings,
        methodology=capped,
        providers=build_providers(SCAN_DAY, settings),
        trading_day=SCAN_DAY,
    ).run(stage=WorkflowStage.MARKET_OPEN)
    assert len(result.candidate_set.candidates) <= 2


def test_mock_data_is_labelled_as_such(scan):
    assert any(f.code == "MOCK_DATA" for f in scan.report.data_quality_flags)


# --------------------------------------------------------------- persistence
def test_scan_persists_and_is_reconstructable(scan, db_session):
    persist_scan(scan, db_session)
    db_session.commit()

    run = db_session.get(m.MarketRun, scan.run_id)
    assert run is not None
    assert run.methodology_snapshot["fingerprint"] == scan.methodology.fingerprint()
    assert run.provider_backends["fmp"] == "mock"

    recos = list(
        db_session.scalars(
            select(m.TradeRecommendation).where(m.TradeRecommendation.run_id == scan.run_id)
        )
    )
    assert len(recos) == len(scan.evaluations)

    components = list(
        db_session.scalars(
            select(m.ScoreComponentRow).where(m.ScoreComponentRow.run_id == scan.run_id)
        )
    )
    assert len(components) == len(scan.evaluations) * 8
    # Every awarded point is stored with the measurement that produced it.
    for comp in components:
        for reason in comp.reasons:
            assert "rule" in reason and "measurement" in reason


def test_contract_snapshots_capture_the_quote_at_decision_time(scan, db_session):
    persist_scan(scan, db_session)
    db_session.commit()
    rows = list(
        db_session.scalars(
            select(m.OptionContractSnapshot).where(
                m.OptionContractSnapshot.run_id == scan.run_id
            )
        )
    )
    structured = [e for e in scan.evaluations if e.trade is not None]
    assert len(rows) == sum(len(e.trade.legs) for e in structured)
    for row in rows:
        assert row.retrieved_at is not None
        assert row.provider


def test_brief_children_are_persisted(scan, db_session):
    persist_scan(scan, db_session)
    db_session.commit()
    assert db_session.scalars(
        select(m.StockCatalyst).where(m.StockCatalyst.run_id == scan.run_id)
    ).first()
    assert db_session.scalars(
        select(m.EconomicEventRow).where(m.EconomicEventRow.run_id == scan.run_id)
    ).first()


# ------------------------------------------------------------------ decisions
def test_decision_execution_and_result_round_trip(scan, db_session):
    persist_scan(scan, db_session)
    db_session.commit()

    reco = db_session.scalars(
        select(m.TradeRecommendation).where(m.TradeRecommendation.run_id == scan.run_id)
    ).first()

    decision = record_decision(
        db_session, reco.recommendation_id, HumanDecision.ENTERED, decided_by="tester"
    )
    db_session.commit()

    execution = record_execution(
        db_session,
        decision.decision_id,
        contracts=[{"symbol": "TEST", "action": "BUY"}],
        quantity=2,
        entered_at=datetime(2024, 6, 3, 14, 0, tzinfo=UTC),
        entry_price=5.00,
        entry_underlying_price=120.0,
    )
    db_session.commit()

    result = record_result(
        db_session,
        execution.execution_id,
        exited_at=datetime(2024, 6, 13, 14, 0, tzinfo=UTC),
        exit_price=7.50,
    )
    db_session.commit()

    assert result.pnl == pytest.approx(500.0)  # (7.50-5.00) * 100 * 2
    assert result.pnl_pct == pytest.approx(50.0)
    assert result.days_held == 10
    assert result.outcome == "WIN"


def test_a_losing_trade_is_recorded_as_a_loss(scan, db_session):
    persist_scan(scan, db_session)
    db_session.commit()
    reco = db_session.scalars(
        select(m.TradeRecommendation).where(m.TradeRecommendation.run_id == scan.run_id)
    ).first()
    decision = record_decision(db_session, reco.recommendation_id, HumanDecision.ENTERED)
    execution = record_execution(
        db_session,
        decision.decision_id,
        contracts=[],
        quantity=1,
        entered_at=datetime(2024, 6, 3, tzinfo=UTC),
        entry_price=4.0,
    )
    result = record_result(
        db_session,
        execution.execution_id,
        exited_at=datetime(2024, 6, 5, tzinfo=UTC),
        exit_price=1.0,
    )
    assert result.pnl == pytest.approx(-300.0)
    assert result.outcome == "LOSS"


def test_recording_an_execution_forces_the_decision_to_entered(scan, db_session):
    persist_scan(scan, db_session)
    db_session.commit()
    reco = db_session.scalars(
        select(m.TradeRecommendation).where(m.TradeRecommendation.run_id == scan.run_id)
    ).first()
    decision = record_decision(db_session, reco.recommendation_id, HumanDecision.WATCHED)
    record_execution(
        db_session,
        decision.decision_id,
        contracts=[],
        quantity=1,
        entered_at=datetime(2024, 6, 3, tzinfo=UTC),
        entry_price=1.0,
    )
    assert decision.decision == HumanDecision.ENTERED.value


# --------------------------------------------------------------- report shape
def test_report_renders_to_markdown(scan):
    from app.reports.markdown import render

    text = render(scan.report)
    assert "# Trade Report" in text
    assert "## Market summary" in text
    assert "does not place orders" in text


def test_report_renders_to_console_without_error(scan):
    from io import StringIO

    from rich.console import Console

    from app.reports.console import render

    buffer = StringIO()
    render(scan.report, Console(file=buffer, width=120), show_audit=True)
    output = buffer.getvalue()
    assert "Market Summary" in output
    assert "SYNTHETIC DATA" in output


def test_top_trades_carry_a_complete_actionable_specification(scan):
    for t in scan.report.top_trades:
        assert t.trade.long_leg.contract.strike > 0
        assert t.trade.expiration > SCAN_DAY
        assert t.risk_reward.max_loss > 0
        assert t.entry_conditions
        assert t.profit_targets
        assert t.invalidation
        assert t.breakdown.total >= scan.methodology.min_presentable_score
