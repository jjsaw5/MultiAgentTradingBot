"""Agent 2 -- Options Opportunity Generator.

Receives the MarketBrief and looks for setups worth taking. It is explicitly
*not* required to agree with Agent 1: a catalyst that Agent 1 flagged as
important can still be discarded here if price action, timing, or volatility
make it untradable.

Two behaviours are structural rather than advisory:

* the candidate cap comes from configuration, and
* returning zero candidates is a valid outcome. :class:`CandidateSet` refuses
  to validate an empty result that has no stated reason, which makes "no trade"
  a deliberate answer rather than a silent failure.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from app.agents.base import AgentRunRecord, trace
from app.agents.llm import LLMClient, LLMUnavailable
from app.agents.market_intelligence.agent import PRIMARY_CATALYST_TYPES
from app.agents.opportunity_generator.prompt import SYSTEM_PROMPT, build_user_prompt
from app.config.methodology import Methodology
from app.models.enums import (
    AgentName,
    CatalystType,
    Direction,
    PreliminaryQuality,
    StrategyType,
    TimeHorizon,
)
from app.models.market_brief import CompanyCatalyst, MarketBrief
from app.models.market_data import TechnicalSnapshot
from app.models.trade_candidate import (
    CandidateSet,
    CatalystRef,
    ExpectedMove,
    TechnicalContext,
    TradeCandidate,
)
from app.providers.mock_market import SCENARIO_BY_SYMBOL
from app.providers.registry import ProviderBundle
from app.services.technicals import compute_snapshot

#: Sentinel used only to keep catalyst sorting total when a timestamp is absent.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Below this, a catalyst is not strong enough to carry a thesis on its own.
MIN_IMPORTANCE_TO_TRADE = 0.5

#: An IV rank at or above this pushes the structure toward a debit vertical,
#: which is materially less exposed to an IV contraction than a naked long.
IV_RANK_PREFER_SPREAD = 50.0


class OpportunityGeneratorAgent:
    def __init__(
        self,
        providers: ProviderBundle,
        llm: LLMClient,
        methodology: Methodology,
        *,
        universe: list[str],
        use_llm: bool,
    ) -> None:
        self.providers = providers
        self.llm = llm
        self.methodology = methodology
        self.universe = universe
        self.use_llm = use_llm

    def run(
        self, run_id: str, trading_day: date, brief: MarketBrief
    ) -> tuple[CandidateSet, AgentRunRecord]:
        mode = "llm" if self.use_llm else "heuristic"
        with trace(run_id, AgentName.OPPORTUNITY_GENERATOR, self.llm.backend, mode) as rec:
            context = self._ticker_context(brief, rec)
            rec.input_summary = {
                "catalyst_tickers": sorted(context.keys()),
                "market_regime": brief.market_regime.value,
                "max_candidates": self.methodology.pipeline.max_candidates_per_run,
            }

            if self.use_llm:
                try:
                    result = self.llm.structured(
                        system=SYSTEM_PROMPT,
                        user=build_user_prompt(run_id, trading_day, brief, context, self.methodology),
                        schema=CandidateSet,
                    )
                    result.run_id = run_id
                    for c in result.candidates:
                        c.run_id = run_id
                except (LLMUnavailable, Exception) as exc:  # noqa: BLE001
                    rec.warnings.append(
                        f"LLM path failed ({type(exc).__name__}: {exc}); fell back to heuristics."
                    )
                    rec.reasoning_mode = "heuristic"
                    result = self._heuristic(run_id, trading_day, brief, context, rec)
            else:
                result = self._heuristic(run_id, trading_day, brief, context, rec)

            cap = self.methodology.pipeline.max_candidates_per_run
            if len(result.candidates) > cap:
                dropped = [c.ticker for c in result.candidates[cap:]]
                rec.warnings.append(
                    f"Candidate cap {cap} enforced; dropped {', '.join(dropped)}."
                )
                result.candidates = result.candidates[:cap]

            rec.output_summary = {
                "candidates": [
                    {"ticker": c.ticker, "strategy": c.strategy_type.value}
                    for c in result.candidates
                ],
                "discarded": result.considered_and_discarded,
            }
            return result, rec

    # ---------------------------------------------------------------- input
    def _ticker_context(
        self, brief: MarketBrief, rec: AgentRunRecord
    ) -> dict[str, dict[str, Any]]:
        """Price structure for every ticker that has a catalyst in the brief."""
        tickers = sorted({c.ticker for c in brief.company_catalysts} & set(self.universe))
        out: dict[str, dict[str, Any]] = {}
        rec.providers_queried.append("fmp")
        spy_history = None
        try:
            spy_history = self.providers.market_data.get_price_history("SPY", 260)
        except Exception as exc:  # noqa: BLE001
            rec.warnings.append(f"SPY benchmark unavailable for relative strength: {exc}")

        for ticker in tickers:
            try:
                quote = self.providers.market_data.get_quote(ticker)
                history = self.providers.market_data.get_price_history(ticker, 260)
                out[ticker] = {
                    "quote": quote,
                    "technicals": compute_snapshot(history, quote, spy_history),
                    "catalysts": brief.catalysts_for(ticker),
                }
            except Exception as exc:  # noqa: BLE001
                rec.providers_failed.append("fmp")
                rec.missing_data.append(f"{ticker}: {exc}")
        return out

    # ------------------------------------------------------ heuristic path
    def _heuristic(
        self,
        run_id: str,
        trading_day: date,
        brief: MarketBrief,
        context: dict[str, dict[str, Any]],
        rec: AgentRunRecord,
    ) -> CandidateSet:
        candidates: list[TradeCandidate] = []
        discarded: list[str] = []

        for ticker, data in sorted(context.items()):
            tech: TechnicalSnapshot = data["technicals"]
            cats: list[CompanyCatalyst] = data["catalysts"]

            best = self._best_catalyst(cats)
            if best is None:
                discarded.append(f"{ticker}: no catalyst strong enough to carry a thesis")
                continue

            direction = self._direction(best, tech)
            if direction is None:
                discarded.append(
                    f"{ticker}: catalyst direction is neutral and price structure gives no edge"
                )
                continue

            horizon = self._horizon(best, trading_day)
            expected = self._expected_move(tech, best, horizon)
            if expected is None:
                discarded.append(f"{ticker}: no measurable volatility to size an expected move")
                continue

            strategy = self._strategy(ticker, direction, tech, horizon)

            candidates.append(
                TradeCandidate(
                    run_id=run_id,
                    ticker=ticker,
                    sector=SCENARIO_BY_SYMBOL[ticker].sector
                    if ticker in SCENARIO_BY_SYMBOL
                    else None,
                    direction=direction,
                    strategy_type=strategy,
                    thesis=self._thesis(ticker, direction, best, tech, brief),
                    primary_catalyst=CatalystRef(
                        ticker=ticker,
                        catalyst_type=best.catalyst_type,
                        headline=best.headline,
                        source_url=best.source_url,
                        scheduled_event_date=best.scheduled_event_date,
                    ),
                    supporting_catalysts=[
                        CatalystRef(
                            ticker=ticker,
                            catalyst_type=c.catalyst_type,
                            headline=c.headline,
                            source_url=c.source_url,
                            scheduled_event_date=c.scheduled_event_date,
                        )
                        for c in cats
                        if c is not best
                    ][:3],
                    expected_holding_period=horizon,
                    expected_move=expected,
                    underlying_reference_price=tech.price,
                    technical_context=TechnicalContext(
                        trend_description=self._trend_description(tech),
                        key_support=tech.support,
                        key_resistance=tech.resistance,
                        breakout_level=tech.range_high_20d
                        if direction is Direction.BULLISH
                        else tech.range_low_20d,
                        notes=(
                            f"RSI14={tech.rsi14}, ATR={tech.atr_pct}% of price, "
                            f"rvol={tech.relative_volume}."
                        ),
                    ),
                    invalidation_thesis=self._invalidation(direction, tech),
                    invalidation_price=self._invalidation_price(direction, tech),
                    known_risks=self._risks(ticker, brief, best),
                    earnings_date=next(
                        (
                            c.scheduled_event_date
                            for c in cats
                            if c.catalyst_type is CatalystType.EARNINGS
                        ),
                        None,
                    ),
                    catalyst_date=best.scheduled_event_date,
                    preliminary_quality=self._quality(best, tech, direction),
                    agent_reasoning_summary=(
                        f"Primary catalyst {best.catalyst_type.value} "
                        f"(importance {best.importance_score:.2f}, "
                        f"evidence {best.evidence_quality.value}); "
                        f"{self._trend_description(tech)}; "
                        f"market regime {brief.market_regime.value}."
                    ),
                )
            )

        candidates.sort(
            key=lambda c: (
                -_quality_rank(c.preliminary_quality),
                -c.expected_move.percent,
                c.ticker,
            )
        )

        no_trade = None
        if not candidates:
            no_trade = (
                "No ticker in the universe presented both a catalyst above the "
                f"{MIN_IMPORTANCE_TO_TRADE} importance floor and a directional read that "
                "price structure supports. Forcing a setup was declined."
            )

        return CandidateSet(
            run_id=run_id,
            candidates=candidates,
            no_trade_rationale=no_trade,
            considered_and_discarded=discarded,
            unavailable_data=list(rec.missing_data),
        )

    # ---------------------------------------------------------------- rules
    @staticmethod
    def _best_catalyst(cats: list[CompanyCatalyst]) -> CompanyCatalyst | None:
        eligible = [
            c
            for c in cats
            if c.catalyst_type in PRIMARY_CATALYST_TYPES
            and c.importance_score >= MIN_IMPORTANCE_TO_TRADE
            and not c.already_priced_in
        ]
        if not eligible:
            return None
        return max(eligible, key=lambda c: (c.importance_score, c.published_at or _EPOCH))

    @staticmethod
    def _direction(catalyst: CompanyCatalyst, tech: TechnicalSnapshot) -> Direction | None:
        if catalyst.expected_direction.sign > 0:
            return Direction.BULLISH
        if catalyst.expected_direction.sign < 0:
            return Direction.BEARISH
        # Neutral catalyst: only trade it if price structure is unambiguous.
        if tech.sma20 and tech.sma50:
            if tech.price > tech.sma20 > tech.sma50:
                return Direction.BULLISH
            if tech.price < tech.sma20 < tech.sma50:
                return Direction.BEARISH
        return None

    @staticmethod
    def _expected_move(
        tech: TechnicalSnapshot, catalyst: CompanyCatalyst, horizon: TimeHorizon
    ) -> ExpectedMove | None:
        # The projection window must be the holding period the agent actually
        # assigned. Sizing a move over a fixed 21 days and then holding for 60
        # would understate the thesis and skew every downstream comparison
        # against implied volatility and reward/risk.
        if not tech.atr_pct:
            return None
        horizon_days = horizon.approx_days
        projected = tech.atr_pct * (horizon_days**0.5)
        # Scale by how much the catalyst should matter, bounded so a single
        # headline never implies an implausible move.
        scaled = projected * (0.55 + 0.45 * catalyst.importance_score)
        return ExpectedMove(
            percent=round(min(max(scaled, 1.0), 25.0), 2),
            rationale=(
                f"ATR of {tech.atr_pct:.2f}% per day projected over {horizon_days} sessions "
                f"({projected:.1f}%), scaled by catalyst importance {catalyst.importance_score:.2f}."
            ),
            basis="ATR projection scaled by catalyst importance",
        )

    @staticmethod
    def _horizon(catalyst: CompanyCatalyst, trading_day: date) -> TimeHorizon:
        if catalyst.scheduled_event_date:
            days = (catalyst.scheduled_event_date - trading_day).days
            if days <= 3:
                return TimeHorizon.DAYS_1_3
            if days <= 7:
                return TimeHorizon.WEEK_1
            if days <= 28:
                return TimeHorizon.WEEKS_2_4
            return TimeHorizon.MONTHS_1_3
        return TimeHorizon.WEEKS_2_4

    def _strategy(
        self,
        ticker: str,
        direction: Direction,
        tech: TechnicalSnapshot,
        horizon: TimeHorizon,
    ) -> StrategyType:
        """Choose between a long option and a debit vertical.

        Two independent reasons to prefer a spread:

        * **Volatility.** An elevated IV rank means a naked long is paying up
          for volatility that is likely to contract; the short leg hedges part
          of that.
        * **Affordability.** On a high-priced underlying a single at-the-money
          option can cost several times the per-trade budget. Proposing one
          anyway would just generate a candidate that the budget rule rejects,
          so the structure is downgraded to a vertical instead.
        """
        iv_rank = None
        iv = None
        if self.providers.options_flow is not None:
            try:
                snapshot = self.providers.options_flow.get_flow_snapshot(ticker)
                iv_rank, iv = snapshot.iv_rank, snapshot.iv30
            except Exception:  # noqa: BLE001 - flow is optional
                iv_rank = iv = None

        prefer_spread = iv_rank is not None and iv_rank >= IV_RANK_PREFER_SPREAD

        if not prefer_spread:
            cost = self._approx_long_option_cost(tech, iv, horizon)
            budget = self.methodology.hard_rejections.max_premium_per_trade_usd
            if cost is not None and cost > budget:
                prefer_spread = True

        if direction is Direction.BULLISH:
            return StrategyType.BULL_CALL_SPREAD if prefer_spread else StrategyType.LONG_CALL
        return StrategyType.BEAR_PUT_SPREAD if prefer_spread else StrategyType.LONG_PUT

    @staticmethod
    def _approx_long_option_cost(
        tech: TechnicalSnapshot, iv: float | None, horizon: TimeHorizon
    ) -> float | None:
        """Rough dollar cost of one at-the-money option.

        Uses the standard ATM approximation ``0.4 * S * sigma * sqrt(T)``. This
        is a sizing check only -- the real price comes from the chain, and this
        estimate never reaches a report.
        """
        if iv is None and tech.atr_pct:
            iv = tech.atr_pct / 100.0 * (252**0.5)  # realised vol as a stand-in
        if iv is None or not tech.price:
            return None
        # The contract must outlive the holding period; see contract_selection.
        days = horizon.approx_days + 21
        return 0.4 * tech.price * iv * (days / 365.0) ** 0.5 * 100

    @staticmethod
    def _trend_description(tech: TechnicalSnapshot) -> str:
        if tech.sma20 is None or tech.sma50 is None:
            return "trend undetermined (insufficient history)"
        if tech.price > tech.sma20 > tech.sma50:
            return "uptrend: price above a rising 20/50 stack"
        if tech.price < tech.sma20 < tech.sma50:
            return "downtrend: price below a falling 20/50 stack"
        if tech.price > tech.sma20:
            return "above the 20-day but the 20/50 stack is not aligned"
        return "below the 20-day with the 20/50 stack unaligned"

    def _thesis(
        self,
        ticker: str,
        direction: Direction,
        catalyst: CompanyCatalyst,
        tech: TechnicalSnapshot,
        brief: MarketBrief,
    ) -> str:
        return (
            f"{ticker} is {self._trend_description(tech)} with a "
            f"{catalyst.catalyst_type.value.lower().replace('_', ' ')} catalyst "
            f"(\"{catalyst.headline[:110]}\"). The market regime is "
            f"{brief.market_regime.value} with SPY {brief.spy.bias.value}, which "
            f"{'supports' if brief.spy.bias.sign == direction.sign or brief.spy.bias.sign == 0 else 'works against'} "
            f"a {direction.value.lower()} position. Thesis: the catalyst pulls price "
            f"{'higher' if direction is Direction.BULLISH else 'lower'} over the holding period."
        )

    @staticmethod
    def _invalidation_price(direction: Direction, tech: TechnicalSnapshot) -> float | None:
        """The level the prose invalidation refers to, as a number.

        Kept in lockstep with :meth:`_invalidation` -- the two must describe the
        same level, or the risk model and the human are reading different trades.
        """
        level = (
            (tech.support or tech.sma20)
            if direction is Direction.BULLISH
            else (tech.resistance or tech.sma20)
        )
        return round(level, 4) if level and level > 0 else None

    @staticmethod
    def _invalidation(direction: Direction, tech: TechnicalSnapshot) -> str:
        if direction is Direction.BULLISH:
            level = tech.support or tech.sma20
            return (
                f"A daily close below {level:.2f} invalidates the thesis."
                if level
                else "A daily close below the 20-day moving average invalidates the thesis."
            )
        level = tech.resistance or tech.sma20
        return (
            f"A daily close above {level:.2f} invalidates the thesis."
            if level
            else "A daily close above the 20-day moving average invalidates the thesis."
        )

    @staticmethod
    def _risks(ticker: str, brief: MarketBrief, catalyst: CompanyCatalyst) -> list[str]:
        risks = [r.description for r in brief.risk_events][:3]
        if catalyst.evidence_quality.value in ("INTERPRETATION", "RUMOR", "UNVERIFIED"):
            risks.append(
                f"The primary catalyst is {catalyst.evidence_quality.value}, not confirmed fact."
            )
        earnings = next(
            (
                c
                for c in brief.catalysts_for(ticker)
                if c.catalyst_type is CatalystType.EARNINGS and c.scheduled_event_date
            ),
            None,
        )
        if earnings:
            risks.append(f"Earnings scheduled for {earnings.scheduled_event_date}.")
        return risks

    @staticmethod
    def _quality(
        catalyst: CompanyCatalyst, tech: TechnicalSnapshot, direction: Direction
    ) -> PreliminaryQuality:
        confirmed = catalyst.evidence_quality.value == "CONFIRMED_FACT"
        trend_agrees = (
            tech.sma20 is not None
            and ((tech.price > tech.sma20) if direction is Direction.BULLISH else (tech.price < tech.sma20))
        )
        if confirmed and trend_agrees and catalyst.importance_score >= 0.7:
            return PreliminaryQuality.WELL_SUPPORTED
        if trend_agrees or catalyst.importance_score >= 0.7:
            return PreliminaryQuality.PLAUSIBLE
        return PreliminaryQuality.SPECULATIVE


_QUALITY_ORDER = {
    PreliminaryQuality.SPECULATIVE: 0,
    PreliminaryQuality.PLAUSIBLE: 1,
    PreliminaryQuality.WELL_SUPPORTED: 2,
}


def _quality_rank(q: PreliminaryQuality) -> int:
    return _QUALITY_ORDER[q]


def summarize_context(context: dict[str, dict[str, Any]]) -> str:
    return json.dumps(
        {
            ticker: {
                "price": data["quote"].price,
                "change_pct": data["quote"].change_pct,
                "relative_volume": data["quote"].relative_volume,
                "sma20": data["technicals"].sma20,
                "sma50": data["technicals"].sma50,
                "rsi14": data["technicals"].rsi14,
                "atr_pct": data["technicals"].atr_pct,
                "support": data["technicals"].support,
                "resistance": data["technicals"].resistance,
                "return_20d_pct": data["technicals"].return_20d_pct,
                "relative_strength_20d_vs_spy": data["technicals"].relative_strength_20d_vs_spy,
                "catalysts": [
                    {
                        "type": c.catalyst_type.value,
                        "headline": c.headline,
                        "importance": c.importance_score,
                        "direction": c.expected_direction.value,
                        "evidence_quality": c.evidence_quality.value,
                        "scheduled_event_date": str(c.scheduled_event_date),
                        "already_priced_in": c.already_priced_in,
                    }
                    for c in data["catalysts"]
                ],
            }
            for ticker, data in context.items()
        },
        indent=2,
        default=str,
    )


__all__ = [
    "IV_RANK_PREFER_SPREAD",
    "MIN_IMPORTANCE_TO_TRADE",
    "OpportunityGeneratorAgent",
    "summarize_context",
]
