"""Agent 3 -- Trade Validator.

Posture: *assume the candidate is wrong and look for data that says so.*

The division of labour matters. This module fetches and computes every
measurement itself -- quotes from two independent providers, indicators, flow,
earnings dates -- and attaches them to the report as facts. The LLM, when
enabled, is handed those measurements and asked only for *interpretation*:
verdicts per category, what would have to be true for the trade to fail, and
caveats about how the flow should be read.

That is why :class:`ValidatorAssessment` contains no numbers. A model cannot
move a price, a greek, or an open-interest figure in this system, because it is
never asked for one.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from pydantic import Field

from app.agents.base import AgentRunRecord, trace
from app.agents.llm import LLMClient, LLMUnavailable
from app.agents.trade_validator.prompt import SYSTEM_PROMPT, build_user_prompt
from app.config.methodology import Methodology
from app.models.common import Base, DataQualityFlag, MissingData, utcnow
from app.models.enums import (
    AgentName,
    CatalystType,
    DataProvider,
    DataQualitySeverity,
    Direction,
    ValidationVerdict,
)
from app.models.market_brief import MarketBrief
from app.models.market_data import EarningsEvent, FlowSnapshot, Quote, TechnicalSnapshot
from app.models.trade_candidate import TradeCandidate
from app.models.validation import (
    CatalystValidation,
    CategoryFinding,
    FlowInterpretation,
    MarketAlignment,
    UnderlyingPriceCheck,
    ValidationReport,
)
from app.providers.registry import ProviderBundle
from app.services.market_calendar import SessionInfo
from app.services.technicals import compute_snapshot

CATEGORIES = (
    "price_technical_structure",
    "market_alignment",
    "catalyst_validation",
    "options_flow",
    "contract_quality",
    "risk_reward",
)


class ValidatorAssessment(Base):
    """The interpretation-only slice an LLM is permitted to produce."""

    overall_verdict: ValidationVerdict
    skeptic_summary: str = Field(
        description="What would have to be true for this trade to lose money."
    )
    catalyst_verdict: ValidationVerdict
    catalyst_notes: str = ""
    catalyst_already_priced_in: bool | None = None
    flow_supports_thesis: bool | None = None
    flow_reasoning: str = ""
    flow_caveats: list[str] = Field(default_factory=list)
    category_findings: list[CategoryFinding] = Field(default_factory=list)


class TradeValidatorAgent:
    def __init__(
        self,
        providers: ProviderBundle,
        llm: LLMClient,
        methodology: Methodology,
        *,
        use_llm: bool,
    ) -> None:
        self.providers = providers
        self.llm = llm
        self.methodology = methodology
        self.use_llm = use_llm

    # ------------------------------------------------------------------ run
    def run(
        self,
        run_id: str,
        trading_day: date,
        candidate: TradeCandidate,
        brief: MarketBrief,
        session: SessionInfo,
    ) -> tuple[ValidationReport, dict[str, Any], AgentRunRecord]:
        """Return the report plus the measured bundle the scorer needs."""
        mode = "llm" if self.use_llm else "heuristic"
        with trace(run_id, AgentName.TRADE_VALIDATOR, self.llm.backend, mode) as rec:
            measured = self._measure(candidate, brief, trading_day, session, rec)
            rec.input_summary = {
                "candidate_id": candidate.candidate_id,
                "ticker": candidate.ticker,
                "strategy": candidate.strategy_type.value,
            }

            report = ValidationReport(
                run_id=run_id,
                candidate_id=candidate.candidate_id,
                ticker=candidate.ticker,
                quote=measured["quote"],
                technicals=measured["technicals"],
                flow=measured["flow"],
                price_check=measured["price_check"],
                catalyst=measured["catalyst"],
                alignment=measured["alignment"],
                missing_data=measured["missing"],
                data_quality_flags=measured["flags"],
                providers_queried=sorted(set(rec.providers_queried)),
                providers_failed=sorted(set(rec.providers_failed)),
            )

            assessment: ValidatorAssessment | None = None
            if self.use_llm:
                try:
                    assessment = self.llm.structured(
                        system=SYSTEM_PROMPT,
                        user=build_user_prompt(candidate, brief, measured, session),
                        schema=ValidatorAssessment,
                    )
                except (LLMUnavailable, Exception) as exc:  # noqa: BLE001
                    rec.warnings.append(
                        f"LLM path failed ({type(exc).__name__}: {exc}); fell back to heuristics."
                    )
                    rec.reasoning_mode = "heuristic"

            if assessment is not None:
                report.overall_verdict = assessment.overall_verdict
                report.skeptic_summary = assessment.skeptic_summary
                report.catalyst.verdict = assessment.catalyst_verdict
                report.catalyst.notes = assessment.catalyst_notes
                if assessment.catalyst_already_priced_in is not None:
                    report.catalyst.already_priced_in = assessment.catalyst_already_priced_in
                report.flow_interpretation = FlowInterpretation(
                    directional_conclusion=assessment.overall_verdict,
                    supports_thesis=assessment.flow_supports_thesis,
                    reasoning=assessment.flow_reasoning,
                    caveats=assessment.flow_caveats,
                )
                report.findings = assessment.category_findings or self._findings(
                    candidate, measured
                )
            else:
                report.findings = self._findings(candidate, measured)
                report.flow_interpretation = self._flow_interpretation(candidate, measured)
                report.overall_verdict = self._overall(report)
                report.skeptic_summary = self._skeptic_summary(candidate, measured)

            rec.output_summary = {
                "overall_verdict": report.overall_verdict.value,
                "catalyst_verdict": report.catalyst.verdict.value,
                "flow_supports_thesis": report.flow_interpretation.supports_thesis,
                "missing_data": [m.field for m in report.missing_data],
            }
            rec.missing_data.extend(m.field for m in report.missing_data)
            return report, measured, rec

    # -------------------------------------------------------------- measure
    def _measure(
        self,
        candidate: TradeCandidate,
        brief: MarketBrief,
        trading_day: date,
        session: SessionInfo,
        rec: AgentRunRecord,
    ) -> dict[str, Any]:
        ticker = candidate.ticker
        missing: list[MissingData] = []
        flags: list[DataQualityFlag] = []
        prices: dict[str, float] = {}

        # --- underlying quote (FMP) ---------------------------------------
        quote: Quote | None = None
        rec.providers_queried.append("fmp")
        try:
            quote = self.providers.market_data.get_quote(ticker)
            prices[DataProvider.FMP.value] = quote.price
        except Exception as exc:  # noqa: BLE001
            rec.providers_failed.append("fmp")
            missing.append(
                MissingData(field="underlying_price", provider=DataProvider.FMP, reason=str(exc))
            )

        # --- technicals ----------------------------------------------------
        technicals: TechnicalSnapshot | None = None
        try:
            history = self.providers.market_data.get_price_history(ticker, 260)
            spy = self.providers.market_data.get_price_history("SPY", 260)
            technicals = compute_snapshot(history, quote, spy)
        except Exception as exc:  # noqa: BLE001
            missing.append(
                MissingData(field="technicals", provider=DataProvider.FMP, reason=str(exc))
            )

        # --- option chain (Robinhood) --------------------------------------
        chain = None
        rec.providers_queried.append("robinhood")
        try:
            chain = self.providers.options_market.get_option_chain(ticker)
            if chain.underlying_price is not None:
                prices[DataProvider.ROBINHOOD.value] = chain.underlying_price
            if not session.options_quotes_actionable:
                chain.stale = True
                for c in chain.contracts:
                    c.stale = True
                flags.append(
                    DataQualityFlag(
                        code="STALE_OPTION_QUOTES",
                        severity=DataQualitySeverity.WARNING,
                        message=session.note,
                        provider=DataProvider.ROBINHOOD,
                        ticker=ticker,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            rec.providers_failed.append("robinhood")
            missing.append(
                MissingData(
                    field="option_chain", provider=DataProvider.ROBINHOOD, reason=str(exc)
                )
            )

        # --- options flow (Unusual Whales, optional) -----------------------
        flow: FlowSnapshot | None = None
        if self.providers.options_flow is not None:
            rec.providers_queried.append("unusual_whales")
            try:
                flow = self.providers.options_flow.get_flow_snapshot(ticker)
            except Exception as exc:  # noqa: BLE001
                rec.providers_failed.append("unusual_whales")
                missing.append(
                    MissingData(
                        field="options_flow",
                        provider=DataProvider.UNUSUAL_WHALES,
                        reason=str(exc),
                    )
                )
        else:
            missing.append(
                MissingData(
                    field="options_flow",
                    provider=DataProvider.UNUSUAL_WHALES,
                    reason="provider not configured",
                )
            )

        # --- earnings ------------------------------------------------------
        earnings: EarningsEvent | None = None
        try:
            earnings = self.providers.market_data.get_next_earnings(
                ticker, as_of=trading_day
            )
        except Exception as exc:  # noqa: BLE001
            missing.append(
                MissingData(field="earnings_date", provider=DataProvider.FMP, reason=str(exc))
            )

        # --- cross-provider price reconciliation ---------------------------
        price_check = self._reconcile(prices, flags, ticker)

        return {
            "quote": quote,
            "technicals": technicals,
            "chain": chain,
            "flow": flow,
            "earnings": earnings,
            "price_check": price_check,
            "catalyst": self._validate_catalyst(candidate, brief, trading_day, earnings),
            "alignment": self._validate_alignment(candidate, brief, technicals),
            "missing": missing,
            "flags": flags,
            "session": session,
        }

    def _reconcile(
        self, prices: dict[str, float], flags: list[DataQualityFlag], ticker: str
    ) -> UnderlyingPriceCheck:
        if not prices:
            return UnderlyingPriceCheck(reconciled=False)
        values = list(prices.values())
        consensus = round(sum(values) / len(values), 4)
        spread = (max(values) - min(values)) / consensus if consensus else None
        tolerance = self.methodology.hard_rejections.max_provider_price_disagreement_pct
        reconciled = spread is None or spread <= tolerance
        if spread is not None and not reconciled:
            flags.append(
                DataQualityFlag(
                    code="PROVIDER_PRICE_DISAGREEMENT",
                    severity=DataQualitySeverity.ERROR,
                    message=(
                        f"Underlying price disagreement of {spread:.2%} exceeds the "
                        f"{tolerance:.2%} tolerance."
                    ),
                    ticker=ticker,
                    context={k: v for k, v in prices.items()},
                )
            )
        return UnderlyingPriceCheck(
            prices_by_provider=prices,
            consensus_price=consensus,
            max_disagreement_pct=round(spread, 6) if spread is not None else None,
            reconciled=reconciled,
        )

    # ------------------------------------------------------------ catalysts
    @staticmethod
    def _validate_catalyst(
        candidate: TradeCandidate,
        brief: MarketBrief,
        trading_day: date,
        earnings: EarningsEvent | None,
    ) -> CatalystValidation:
        primary = candidate.primary_catalyst
        matches = [
            c
            for c in brief.catalysts_for(candidate.ticker)
            if c.catalyst_type is primary.catalyst_type
        ]
        if not matches:
            return CatalystValidation(
                exists=False,
                verdict=ValidationVerdict.CONTRADICTED,
                notes=(
                    f"No catalyst of type {primary.catalyst_type.value} for "
                    f"{candidate.ticker} exists in the MarketBrief. The thesis cites "
                    "something the evidence does not support."
                ),
            )

        match = matches[0]
        days_since = (
            (utcnow() - match.published_at).days if match.published_at else None
        )
        catalyst_date = match.scheduled_event_date or candidate.catalyst_date
        days_until = (catalyst_date - trading_day).days if catalyst_date else None

        conflicts = False
        if catalyst_date and earnings and match.catalyst_type is not CatalystType.EARNINGS:
            # An unrelated catalyst landing on top of an earnings print is not a
            # clean read on either event.
            conflicts = abs((earnings.event_date - catalyst_date).days) <= 2

        material = match.importance_score >= 0.5
        recent = days_since is not None and days_since <= 5

        if match.evidence_quality.value in ("CONFIRMED_FACT", "REPORTED") and material:
            verdict = ValidationVerdict.CONFIRMED
        elif material:
            verdict = ValidationVerdict.PARTIALLY_CONFIRMED
        else:
            verdict = ValidationVerdict.INCONCLUSIVE

        return CatalystValidation(
            exists=True,
            is_recent=recent,
            is_material=material,
            already_priced_in=match.already_priced_in,
            has_upcoming_timing_relevance=days_until is not None and days_until >= 0,
            conflicts_with_scheduled_event=conflicts,
            days_since_published=days_since,
            days_until_catalyst=days_until,
            verdict=verdict,
            notes=(
                f"{match.catalyst_type.value} from {match.source or 'unknown source'}"
                + (f", published {days_since}d ago" if days_since is not None else "")
                + f", importance {match.importance_score:.2f}, evidence "
                f"{match.evidence_quality.value}."
            ),
        )

    @staticmethod
    def _validate_alignment(
        candidate: TradeCandidate, brief: MarketBrief, technicals: TechnicalSnapshot | None
    ) -> MarketAlignment:
        sign = candidate.direction.sign
        spy_sign = brief.spy.bias.sign
        qqq_sign = brief.qqq.bias.sign
        sector_bias = brief.sector_bias(candidate.sector)
        rs = technicals.relative_strength_20d_vs_spy if technicals else None

        return MarketAlignment(
            spy_aligned=None if spy_sign == 0 else spy_sign == sign,
            qqq_aligned=None if qqq_sign == 0 else qqq_sign == sign,
            sector_aligned=None if sector_bias is None or sector_bias.sign == 0 else sector_bias.sign == sign,
            relative_strength_20d=rs,
            fighting_the_tape=spy_sign != 0 and spy_sign != sign,
            notes=(
                f"SPY {brief.spy.bias.value}, QQQ {brief.qqq.bias.value}"
                + (f", sector {sector_bias.value}" if sector_bias else "")
                + (f", RS20 {rs:+.2f}pp" if rs is not None else "")
            ),
        )

    # ------------------------------------------------------ heuristic verdicts
    def _flow_interpretation(
        self, candidate: TradeCandidate, measured: dict[str, Any]
    ) -> FlowInterpretation:
        flow: FlowSnapshot | None = measured["flow"]
        if flow is None:
            return FlowInterpretation(
                directional_conclusion=ValidationVerdict.DATA_UNAVAILABLE,
                supports_thesis=None,
                reasoning="No options flow provider responded; flow is treated as unknown.",
                caveats=["Absence of flow data is not evidence against the thesis."],
            )

        share = flow.directional_premium_share
        caveats: list[str] = []
        if flow.multileg_share and flow.multileg_share > 0.45:
            caveats.append(
                f"{flow.multileg_share:.0%} of flow is multi-leg; call/put premium ratios "
                "do not cleanly imply direction here."
            )
        if flow.total_volume and flow.total_open_interest and flow.volume_oi_ratio:
            if flow.volume_oi_ratio < 0.5:
                caveats.append(
                    "Volume is well below open interest, so today's tape is more "
                    "consistent with existing positions than new ones."
                )
        caveats.append(
            "Large prints may be hedges or rolls; size alone is not directional evidence."
        )

        if share is None:
            return FlowInterpretation(
                directional_conclusion=ValidationVerdict.INCONCLUSIVE,
                supports_thesis=None,
                reasoning="The provider returned no bullish/bearish premium split.",
                caveats=caveats,
            )

        aligned = share if candidate.direction is Direction.BULLISH else 1 - share
        if aligned >= 0.62:
            conclusion, supports = ValidationVerdict.CONFIRMED, True
        elif aligned <= 0.38:
            conclusion, supports = ValidationVerdict.CONTRADICTED, False
        else:
            conclusion, supports = ValidationVerdict.INCONCLUSIVE, None

        return FlowInterpretation(
            directional_conclusion=conclusion,
            supports_thesis=supports,
            reasoning=(
                f"Directional premium aligned with the thesis is {aligned:.0%} "
                f"(bullish ${flow.bullish_premium:,.0f} vs bearish ${flow.bearish_premium:,.0f}); "
                f"{'ask' if candidate.direction is Direction.BULLISH else 'bid'}-side share "
                f"{(flow.ask_side_share if candidate.direction is Direction.BULLISH else (1 - (flow.ask_side_share or 0))):.0%}"
                if flow.ask_side_share is not None
                else f"Directional premium aligned with the thesis is {aligned:.0%}."
            ),
            caveats=caveats,
        )

    def _findings(
        self, candidate: TradeCandidate, measured: dict[str, Any]
    ) -> list[CategoryFinding]:
        tech: TechnicalSnapshot | None = measured["technicals"]
        flow: FlowSnapshot | None = measured["flow"]
        alignment: MarketAlignment = measured["alignment"]
        catalyst: CatalystValidation = measured["catalyst"]
        chain = measured["chain"]
        bullish = candidate.direction is Direction.BULLISH
        out: list[CategoryFinding] = []

        # --- price / technical structure -----------------------------------
        if tech is None:
            out.append(
                CategoryFinding(
                    category="price_technical_structure",
                    verdict=ValidationVerdict.DATA_UNAVAILABLE,
                    summary="Price history could not be retrieved.",
                    missing=[
                        MissingData(field="technicals", reason="price history unavailable")
                    ],
                )
            )
        else:
            supporting, disconfirming = [], []
            if tech.sma20 and tech.sma50:
                stacked = (
                    tech.price > tech.sma20 > tech.sma50
                    if bullish
                    else tech.price < tech.sma20 < tech.sma50
                )
                (supporting if stacked else disconfirming).append(
                    f"price {tech.price:.2f} vs SMA20 {tech.sma20:.2f} / SMA50 {tech.sma50:.2f}"
                )
            if tech.rsi14 is not None:
                extended = tech.rsi14 > 75 if bullish else tech.rsi14 < 25
                (disconfirming if extended else supporting).append(f"RSI14 {tech.rsi14:.1f}")
            if tech.relative_volume is not None:
                (supporting if tech.relative_volume >= 1.2 else disconfirming).append(
                    f"relative volume {tech.relative_volume:.2f}"
                )
            blocker = tech.resistance if bullish else tech.support
            if blocker:
                dist = abs(blocker - tech.price) / tech.price * 100
                if dist <= 2:
                    disconfirming.append(
                        f"{'resistance' if bullish else 'support'} at {blocker:.2f} is "
                        f"only {dist:.1f}% away"
                    )
            out.append(
                CategoryFinding(
                    category="price_technical_structure",
                    verdict=_verdict_from(supporting, disconfirming),
                    summary=(
                        f"{len(supporting)} supporting and {len(disconfirming)} disconfirming "
                        "technical observations."
                    ),
                    supporting_observations=supporting,
                    disconfirming_observations=disconfirming,
                    data_used=["fmp:historical-price-full", "internal:technicals"],
                )
            )

        # --- market alignment ----------------------------------------------
        supporting = [f"{k} aligned" for k, v in (
            ("SPY", alignment.spy_aligned),
            ("QQQ", alignment.qqq_aligned),
            ("sector", alignment.sector_aligned),
        ) if v is True]
        disconfirming = [f"{k} opposed" for k, v in (
            ("SPY", alignment.spy_aligned),
            ("QQQ", alignment.qqq_aligned),
            ("sector", alignment.sector_aligned),
        ) if v is False]
        out.append(
            CategoryFinding(
                category="market_alignment",
                verdict=_verdict_from(supporting, disconfirming),
                summary=alignment.notes,
                supporting_observations=supporting,
                disconfirming_observations=disconfirming,
                data_used=["internal:market_brief"],
            )
        )

        # --- catalyst -------------------------------------------------------
        out.append(
            CategoryFinding(
                category="catalyst_validation",
                verdict=catalyst.verdict,
                summary=catalyst.notes,
                supporting_observations=[
                    s
                    for s in (
                        "catalyst exists in the brief" if catalyst.exists else None,
                        "recent" if catalyst.is_recent else None,
                        "material" if catalyst.is_material else None,
                        f"{catalyst.days_until_catalyst}d until the scheduled event"
                        if catalyst.days_until_catalyst is not None
                        else None,
                    )
                    if s
                ],
                disconfirming_observations=[
                    s
                    for s in (
                        "already largely priced in" if catalyst.already_priced_in else None,
                        "conflicts with another scheduled event"
                        if catalyst.conflicts_with_scheduled_event
                        else None,
                        "not recent" if catalyst.is_recent is False else None,
                    )
                    if s
                ],
                data_used=["fmp:stock_news", "fmp:earning_calendar"],
            )
        )

        # --- options flow ----------------------------------------------------
        interp = self._flow_interpretation(candidate, measured)
        out.append(
            CategoryFinding(
                category="options_flow",
                verdict=interp.directional_conclusion,
                summary=interp.reasoning,
                supporting_observations=(
                    [f"sweeps={flow.sweep_count}", f"volume/OI={flow.volume_oi_ratio}"]
                    if flow and interp.supports_thesis
                    else []
                ),
                disconfirming_observations=interp.caveats,
                data_used=["unusual_whales:greek-flow"] if flow else [],
                missing=[]
                if flow
                else [MissingData(field="options_flow", reason="provider unavailable")],
            )
        )

        # --- contract quality -------------------------------------------------
        if chain is None:
            out.append(
                CategoryFinding(
                    category="contract_quality",
                    verdict=ValidationVerdict.DATA_UNAVAILABLE,
                    summary="No option chain was retrieved; contract quality is unknown.",
                    missing=[MissingData(field="option_chain", reason="provider unavailable")],
                )
            )
        else:
            out.append(
                CategoryFinding(
                    category="contract_quality",
                    verdict=ValidationVerdict.PARTIALLY_CONFIRMED,
                    summary=(
                        f"{len(chain.contracts)} contracts retrieved across "
                        f"{len(chain.expirations())} expirations; per-contract liquidity is "
                        "evaluated by the selection and scoring stages."
                    ),
                    data_used=["robinhood:option_chain"],
                )
            )

        return out

    @staticmethod
    def _overall(report: ValidationReport) -> ValidationVerdict:
        verdicts = [f.verdict for f in report.findings]
        if any(v is ValidationVerdict.CONTRADICTED for v in verdicts):
            return ValidationVerdict.CONTRADICTED
        if report.catalyst.verdict is ValidationVerdict.CONTRADICTED:
            return ValidationVerdict.CONTRADICTED
        confirmed = sum(1 for v in verdicts if v is ValidationVerdict.CONFIRMED)
        if confirmed >= max(2, len(verdicts) // 2):
            return ValidationVerdict.CONFIRMED
        if confirmed:
            return ValidationVerdict.PARTIALLY_CONFIRMED
        return ValidationVerdict.INCONCLUSIVE

    @staticmethod
    def _skeptic_summary(candidate: TradeCandidate, measured: dict[str, Any]) -> str:
        tech: TechnicalSnapshot | None = measured["technicals"]
        alignment: MarketAlignment = measured["alignment"]
        parts = [
            f"For this {candidate.strategy_type.value} to lose, it is enough that "
            f"{candidate.invalidation_thesis[0].lower() + candidate.invalidation_thesis[1:]}"
        ]
        if alignment.fighting_the_tape:
            parts.append("The position is against the broad-market bias.")
        if tech and tech.atr_pct:
            parts.append(
                f"The underlying moves about {tech.atr_pct:.2f}% a day, so the thesis needs "
                f"roughly {candidate.expected_move.percent / tech.atr_pct:.0f} average sessions "
                "of favourable movement with no offsetting decay."
            )
        if measured["catalyst"].already_priced_in:
            parts.append("The catalyst appears to have been absorbed already.")
        return " ".join(parts)


def _verdict_from(supporting: list[str], disconfirming: list[str]) -> ValidationVerdict:
    if not supporting and not disconfirming:
        return ValidationVerdict.INCONCLUSIVE
    if disconfirming and not supporting:
        return ValidationVerdict.CONTRADICTED
    if len(supporting) > len(disconfirming):
        return ValidationVerdict.CONFIRMED
    if len(supporting) == len(disconfirming):
        return ValidationVerdict.PARTIALLY_CONFIRMED
    return ValidationVerdict.INCONCLUSIVE


def summarize_measurements(measured: dict[str, Any]) -> str:
    quote: Quote | None = measured["quote"]
    tech: TechnicalSnapshot | None = measured["technicals"]
    flow: FlowSnapshot | None = measured["flow"]
    chain = measured["chain"]
    return json.dumps(
        {
            "quote": quote.model_dump(mode="json") if quote else None,
            "technicals": tech.model_dump(mode="json") if tech else None,
            "options_flow": flow.model_dump(mode="json") if flow else None,
            "option_chain_summary": (
                {
                    "underlying_price": chain.underlying_price,
                    "expirations": [str(e) for e in chain.expirations()],
                    "contract_count": len(chain.contracts),
                    "stale": chain.stale,
                }
                if chain
                else None
            ),
            "earnings": (
                measured["earnings"].model_dump(mode="json") if measured["earnings"] else None
            ),
            "price_check": measured["price_check"].model_dump(mode="json"),
            "catalyst_validation": measured["catalyst"].model_dump(mode="json"),
            "market_alignment": measured["alignment"].model_dump(mode="json"),
            "missing": [m.model_dump(mode="json") for m in measured["missing"]],
        },
        indent=2,
        default=str,
    )


__all__ = ["CATEGORIES", "TradeValidatorAgent", "ValidatorAssessment", "summarize_measurements"]
