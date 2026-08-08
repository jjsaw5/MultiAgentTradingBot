"""The orchestrator: Agent 1 -> Agent 2 -> Agent 3 -> rules -> scoring -> report.

Stage handling is the part worth reading carefully. In ``PREMARKET`` the
pipeline still runs Agents 1 and 2 and still assembles a provisional structure,
because knowing *what you would trade at the open* is useful. What it will not
do is present that structure as an entry: the ``stale_quotes`` hard rule fires,
the candidate is classified as rejected-for-now, and the report says why. Only
a ``MARKET_OPEN`` run produces actionable entries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from app.agents.base import AgentRunRecord
from app.agents.llm import build_llm
from app.agents.market_intelligence.agent import MarketIntelligenceAgent
from app.agents.opportunity_generator.agent import OpportunityGeneratorAgent
from app.agents.trade_validator.agent import TradeValidatorAgent
from app.config.methodology import Methodology, get_methodology
from app.config.settings import LLMBackend, Settings, get_settings
from app.models.common import DataQualityFlag, new_id, utcnow
from app.models.enums import (
    Classification,
    DataQualitySeverity,
    Direction,
    WorkflowStage,
)
from app.models.market_brief import MarketBrief
from app.models.report import (
    MarketSummary,
    RankedTrade,
    RejectedTrade,
    TradeReport,
)
from app.models.scoring import ScoreBreakdown, ScoredCandidate
from app.models.trade_candidate import CandidateSet, TradeCandidate
from app.models.trade_structure import ProposedTrade, RiskReward
from app.models.validation import ValidationReport
from app.providers.base import ProviderRequestRecord
from app.providers.registry import ProviderBundle, build_providers
from app.scoring import engine as scoring_engine
from app.scoring.context import ScoringContext
from app.services.contract_selection import select_contracts
from app.services.market_calendar import MarketCalendar, SessionInfo
from app.services.risk import compute_risk_reward

logger = logging.getLogger(__name__)

DEFAULT_UNIVERSE = [
    "NVDA", "AMD", "MSFT", "META", "TSLA", "XOM", "JPM", "LLY", "SOFI",
]


@dataclass
class CandidateEvaluation:
    candidate: TradeCandidate
    validation: ValidationReport
    measured: dict
    trade: ProposedTrade | None
    alternatives: list[ProposedTrade]
    risk_reward: RiskReward | None
    breakdown: ScoreBreakdown
    scored: ScoredCandidate
    selection_notes: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    run_id: str
    trading_day: date
    stage: WorkflowStage
    session: SessionInfo
    started_at: datetime
    completed_at: datetime
    methodology: Methodology
    brief: MarketBrief
    candidate_set: CandidateSet
    evaluations: list[CandidateEvaluation]
    report: TradeReport
    agent_runs: list[AgentRunRecord]
    provider_requests: list[ProviderRequestRecord]
    provider_backends: dict[str, str]
    llm_backend: str
    universe: list[str]
    notes: list[str] = field(default_factory=list)
    data_quality_flags: list[DataQualityFlag] = field(default_factory=list)


class Orchestrator:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        methodology: Methodology | None = None,
        providers: ProviderBundle | None = None,
        universe: list[str] | None = None,
        trading_day: date | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.methodology = methodology or get_methodology()
        self.calendar = MarketCalendar(self.methodology.market_schedule)
        self.session = self.calendar.session()
        self.trading_day = trading_day or self.session.trading_day
        self.providers = providers or build_providers(self.trading_day, self.settings)
        self.universe = universe or DEFAULT_UNIVERSE
        self.llm = build_llm(self.settings)
        self.use_llm = self.settings.llm_backend is LLMBackend.ANTHROPIC

    # ------------------------------------------------------------------ run
    def run(self, *, stage: WorkflowStage | None = None) -> ScanResult:
        run_id = new_id("run")
        started = utcnow()
        stage = stage or self.session.stage
        session = self._session_for(stage)
        agent_runs: list[AgentRunRecord] = []
        notes: list[str] = [session.note]
        flags: list[DataQualityFlag] = []

        if self.providers.all_mocked():
            notes.append(
                "ALL PROVIDERS ARE MOCKED. Figures below are synthetic and must not be "
                "used to make a real trading decision."
            )
            flags.append(
                DataQualityFlag(
                    code="MOCK_DATA",
                    severity=DataQualitySeverity.WARNING,
                    message="Every provider is running its mock backend.",
                )
            )
        for name, reason in self.providers.unavailable.items():
            notes.append(f"Provider '{name}' unavailable: {reason}")
            flags.append(
                DataQualityFlag(
                    code="PROVIDER_UNAVAILABLE",
                    severity=DataQualitySeverity.WARNING,
                    message=f"{name}: {reason}",
                )
            )

        # --- Agent 1 --------------------------------------------------------
        logger.info("run %s: market intelligence (stage=%s)", run_id, stage.value)
        agent1 = MarketIntelligenceAgent(
            self.providers, self.llm, universe=self.universe, use_llm=self.use_llm
        )
        brief, rec1 = agent1.run(run_id, self.trading_day)
        agent_runs.append(rec1)

        # --- Agent 2 --------------------------------------------------------
        logger.info("run %s: opportunity generation", run_id)
        agent2 = OpportunityGeneratorAgent(
            self.providers,
            self.llm,
            self.methodology,
            universe=self.universe,
            use_llm=self.use_llm,
        )
        candidate_set, rec2 = agent2.run(run_id, self.trading_day, brief)
        agent_runs.append(rec2)

        # --- Agent 3 + structuring + rules + scoring -------------------------
        agent3 = TradeValidatorAgent(
            self.providers, self.llm, self.methodology, use_llm=self.use_llm
        )
        evaluations: list[CandidateEvaluation] = []
        for candidate in candidate_set.candidates:
            logger.info("run %s: validating %s", run_id, candidate.ticker)
            validation, measured, rec3 = agent3.run(
                run_id, self.trading_day, candidate, brief, session
            )
            agent_runs.append(rec3)
            flags.extend(validation.data_quality_flags)
            evaluations.append(
                self._evaluate(candidate, validation, measured, brief, stage, session)
            )

        report = self._build_report(
            run_id, brief, evaluations, candidate_set, stage, flags, notes
        )

        return ScanResult(
            run_id=run_id,
            trading_day=self.trading_day,
            stage=stage,
            session=session,
            started_at=started,
            completed_at=utcnow(),
            methodology=self.methodology,
            brief=brief,
            candidate_set=candidate_set,
            evaluations=evaluations,
            report=report,
            agent_runs=agent_runs,
            provider_requests=list(self.providers.requests),
            provider_backends=self.providers.backends(),
            llm_backend=self.llm.backend,
            universe=self.universe,
            notes=notes,
            data_quality_flags=flags,
        )

    def _session_for(self, stage: WorkflowStage) -> SessionInfo:
        """Honour an explicitly requested stage while keeping the real clock."""
        if stage is self.session.stage:
            return self.session
        actionable = stage is WorkflowStage.MARKET_OPEN
        return SessionInfo(
            stage=stage,
            now_local=self.session.now_local,
            trading_day=self.trading_day,
            options_quotes_actionable=actionable,
            max_quote_age_seconds=(
                self.methodology.market_schedule.max_quote_age_seconds_live
                if actionable
                else self.methodology.market_schedule.max_quote_age_seconds_premarket
            ),
            note=(
                f"Stage forced to {stage.value} by the caller. "
                + (
                    "Option quotes are treated as actionable."
                    if actionable
                    else "Option quotes are treated as non-actionable; entries are provisional."
                )
            ),
        )

    # ----------------------------------------------------------- evaluation
    def _evaluate(
        self,
        candidate: TradeCandidate,
        validation: ValidationReport,
        measured: dict,
        brief: MarketBrief,
        stage: WorkflowStage,
        session: SessionInfo,
    ) -> CandidateEvaluation:
        chain = measured.get("chain")
        quote = measured.get("quote")
        underlying = (
            validation.price_check.consensus_price
            or (quote.price if quote else None)
            or candidate.underlying_reference_price
        )

        trade: ProposedTrade | None = None
        alternatives: list[ProposedTrade] = []
        selection_notes: list[str] = []
        if chain is not None and underlying is not None:
            outcome = select_contracts(
                candidate,
                chain,
                self.methodology.contract_selection,
                today=self.trading_day,
                underlying_price=underlying,
                max_premium_usd=self.methodology.hard_rejections.max_premium_per_trade_usd,
            )
            trade, alternatives, selection_notes = (
                outcome.trade,
                outcome.alternatives,
                outcome.reasons,
            )
        elif chain is None:
            selection_notes.append("No option chain available; no structure was assembled.")
        else:
            selection_notes.append("No underlying price available; no structure was assembled.")

        risk_reward = None
        if trade is not None:
            risk_reward = compute_risk_reward(
                trade,
                expected_move_pct=candidate.expected_move.percent,
                direction_sign=candidate.direction.sign,
                holding_days=candidate.expected_holding_period.approx_days,
                invalidation_price=candidate.invalidation_price,
                atr=measured.get("technicals").atr14 if measured.get("technicals") else None,
                risk_model=self.methodology.risk_model,
                reference_day=self.trading_day,
            )

        stale_inputs = self._stale_inputs(measured, session)
        ctx = ScoringContext(
            methodology=self.methodology,
            candidate=candidate,
            validation=validation,
            brief=brief,
            trading_day=self.trading_day,
            stage=stage,
            trade=trade,
            risk_reward=risk_reward,
            quote=quote,
            technicals=measured.get("technicals"),
            flow=measured.get("flow"),
            earnings=measured.get("earnings"),
            providers_expected=list(self.methodology.providers.critical_for_validation)
            + list(self.methodology.providers.optional_for_validation),
            providers_responded=[
                p for p in validation.providers_queried if p not in validation.providers_failed
            ],
            stale_inputs=stale_inputs,
        )

        breakdown = scoring_engine.score_candidate(ctx)
        scored = scoring_engine.classify(breakdown, ctx)
        scored.rejection_summary.extend(selection_notes)

        return CandidateEvaluation(
            candidate=candidate,
            validation=validation,
            measured=measured,
            trade=trade,
            alternatives=alternatives,
            risk_reward=risk_reward,
            breakdown=breakdown,
            scored=scored,
            selection_notes=selection_notes,
        )

    def _stale_inputs(self, measured: dict, session: SessionInfo) -> list[str]:
        out: list[str] = []
        chain = measured.get("chain")
        if chain is not None and chain.stale:
            out.append("option_chain")
        quote = measured.get("quote")
        if quote is not None and self.calendar.is_quote_stale(
            quote.provenance.as_of, session
        ):
            out.append("underlying_quote")
        flow = measured.get("flow")
        if flow is not None and flow.stale:
            out.append("options_flow")
        return out

    # --------------------------------------------------------------- report
    def _build_report(
        self,
        run_id: str,
        brief: MarketBrief,
        evaluations: list[CandidateEvaluation],
        candidate_set: CandidateSet,
        stage: WorkflowStage,
        flags: list[DataQualityFlag],
        notes: list[str],
    ) -> TradeReport:
        ranked = scoring_engine.rank([e.scored for e in evaluations])
        by_candidate = {e.candidate.candidate_id: e for e in evaluations}

        top: list[RankedTrade] = []
        for scored in ranked:
            e = by_candidate[scored.candidate_id]
            if e.trade is None or e.risk_reward is None:
                continue
            top.append(
                RankedTrade(
                    rank=scored.rank or 0,
                    candidate=e.candidate,
                    validation=e.validation,
                    trade=e.trade,
                    risk_reward=e.risk_reward,
                    breakdown=e.breakdown,
                    classification=scored.classification,
                    classification_label=scored.classification_label,
                    entry_conditions=self._entry_conditions(e),
                    profit_targets=self._profit_targets(e),
                    invalidation=e.candidate.invalidation_thesis,
                    risks=self._risks(e),
                    flow_confirmation=e.validation.flow_interpretation.reasoning
                    or "No options flow confirmation available.",
                    technical_thesis=e.candidate.technical_context.trend_description,
                )
            )

        rejected = [
            RejectedTrade(
                candidate=e.candidate,
                score=e.breakdown.total,
                classification=Classification.REJECTED,
                reasons=e.scored.rejection_summary
                or ["Did not meet the minimum presentable score."],
                breakdown=e.breakdown,
                validation=e.validation,
            )
            for e in evaluations
            if not e.scored.presentable
        ]

        if candidate_set.no_trade_rationale:
            notes = [*notes, f"Agent 2 returned no candidates: {candidate_set.no_trade_rationale}"]

        return TradeReport(
            run_id=run_id,
            trading_day=self.trading_day,
            stage=stage,
            methodology_version=self.methodology.version,
            methodology_fingerprint=self.methodology.fingerprint(),
            market_summary=MarketSummary(
                market_regime=brief.market_regime.value,
                volatility_regime=brief.volatility_regime.value,
                spy_bias=brief.spy.bias.value,
                spy_note=brief.spy.notes,
                qqq_bias=brief.qqq.bias.value,
                qqq_note=brief.qqq.notes,
                vix_level=brief.volatility.vix_level,
                vix_note=brief.volatility.notes,
                major_event_risks_today=brief.risk_events,
                upcoming_economic_events=brief.upcoming_economic_events,
                regime_rationale=brief.regime_rationale,
            ),
            top_trades=top,
            rejected=rejected,
            candidates_considered=len(evaluations),
            data_quality_flags=flags,
            notes=notes,
            brief=brief,
        )

    @staticmethod
    def _entry_conditions(e: CandidateEvaluation) -> list[str]:
        assert e.trade is not None
        out = [
            f"Underlying trading near {e.trade.underlying_price:.2f} "
            f"(thesis framed at {e.candidate.underlying_reference_price or e.trade.underlying_price:.2f}).",
            f"Pay no more than the {e.trade.net_debit_conservative:.2f} conservative debit; "
            f"work the order toward the {e.trade.net_debit_mid:.2f} mid.",
        ]
        tech = e.validation.technicals
        if tech and e.candidate.direction is Direction.BULLISH and tech.support:
            out.append(f"Abandon the entry if price loses {tech.support:.2f} before you fill.")
        elif tech and e.candidate.direction is Direction.BEARISH and tech.resistance:
            out.append(f"Abandon the entry if price reclaims {tech.resistance:.2f} before you fill.")
        if e.trade.worst_leg_spread_pct is not None:
            out.append(
                f"Worst-leg quoted spread is {e.trade.worst_leg_spread_pct:.1%}; use a limit order."
            )
        return out

    @staticmethod
    def _profit_targets(e: CandidateEvaluation) -> list[str]:
        assert e.trade is not None and e.risk_reward is not None
        out = [
            f"Primary target: underlying at {e.risk_reward.target_underlying_price:.2f} "
            f"({e.candidate.expected_move.percent:.1f}% move), modelled at "
            f"{(e.risk_reward.expected_value_at_target or 0) + e.risk_reward.max_loss:,.0f} "
            f"against a {e.risk_reward.max_loss:,.0f} cost."
        ]
        if e.trade.max_profit is not None:
            out.append(
                f"Structure caps at ${e.trade.max_profit:,.0f} with the underlying above "
                f"{max(leg.contract.strike for leg in e.trade.legs):.2f} at expiration."
            )
        out.append(f"Breakeven at expiration: {e.trade.breakeven:.2f}.")
        return out

    @staticmethod
    def _risks(e: CandidateEvaluation) -> list[str]:
        risks = list(e.candidate.known_risks)
        if e.risk_reward and e.risk_reward.theta_burn_over_holding_period:
            risks.append(
                f"Time decay of about ${e.risk_reward.theta_burn_over_holding_period:,.0f} "
                f"over the holding period, before any move."
            )
        if e.risk_reward and e.risk_reward.iv_contraction_sensitivity:
            risks.append(
                f"A 5-point IV contraction with price unchanged is modelled at "
                f"${e.risk_reward.iv_contraction_sensitivity:,.0f}."
            )
        for caveat in e.validation.flow_interpretation.caveats:
            risks.append(f"Flow caveat: {caveat}")
        if e.validation.skeptic_summary:
            risks.append(f"Validator: {e.validation.skeptic_summary}")
        return risks


__all__ = ["CandidateEvaluation", "DEFAULT_UNIVERSE", "Orchestrator", "ScanResult"]
