"""Agent 1 -- Market Intelligence.

Question: *what conditions, events, news items, catalysts or scheduled events
have a meaningful chance of moving the market, a sector, or an individual stock
inside the relevant trading horizon?*

Both execution paths build the same **evidence pack** from providers first. The
LLM path asks Claude to interpret that pack into a :class:`MarketBrief`; the
heuristic path derives the same structure with explicit rules. Neither path is
allowed to introduce a market measurement that is not in the pack.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from app.agents.base import AgentRunRecord, trace
from app.agents.llm import LLMClient, LLMUnavailable
from app.agents.market_intelligence.prompt import SYSTEM_PROMPT, build_user_prompt
from app.models.common import SourceReference, utcnow
from app.models.enums import (
    AgentName,
    Bias,
    CatalystScope,
    CatalystType,
    EventImportance,
    EvidenceQuality,
    MarketRegime,
    TimeHorizon,
    VolatilityRegime,
)
from app.models.market_brief import (
    CompanyCatalyst,
    IndexContext,
    MacroObservation,
    MarketBrief,
    RiskEvent,
    SectorObservation,
    VolatilityContext,
)
from app.providers.registry import ProviderBundle
from app.services.technicals import compute_snapshot

INDEXES = ("SPY", "QQQ", "IWM")

#: Catalyst types that, on their own, are worth a trade thesis. Types outside
#: this set can support a thesis but not carry it.
PRIMARY_CATALYST_TYPES = {
    CatalystType.EARNINGS,
    CatalystType.GUIDANCE,
    CatalystType.EARNINGS_REVISION,
    CatalystType.ANALYST_UPGRADE,
    CatalystType.ANALYST_DOWNGRADE,
    CatalystType.PRICE_TARGET_CHANGE,
    CatalystType.MERGER_ACQUISITION,
    CatalystType.PRODUCT_LAUNCH,
    CatalystType.FDA_DECISION,
    CatalystType.MAJOR_CONTRACT,
    CatalystType.REGULATORY_ACTION,
    CatalystType.INDUSTRY_DEVELOPMENT,
    CatalystType.CONFERENCE,
}

_IMPORTANCE_BY_TYPE: dict[CatalystType, float] = {
    CatalystType.EARNINGS: 0.9,
    CatalystType.FDA_DECISION: 0.9,
    CatalystType.MERGER_ACQUISITION: 0.9,
    CatalystType.MAJOR_CONTRACT: 0.75,
    CatalystType.GUIDANCE: 0.75,
    CatalystType.EARNINGS_REVISION: 0.7,
    CatalystType.ANALYST_UPGRADE: 0.6,
    CatalystType.ANALYST_DOWNGRADE: 0.6,
    CatalystType.PRICE_TARGET_CHANGE: 0.5,
    CatalystType.PRODUCT_LAUNCH: 0.6,
    CatalystType.REGULATORY_ACTION: 0.7,
    CatalystType.CONFERENCE: 0.45,
    CatalystType.INDUSTRY_DEVELOPMENT: 0.5,
    CatalystType.SECTOR_ROTATION: 0.4,
}

_DIRECTION_BY_TYPE: dict[CatalystType, Bias] = {
    CatalystType.ANALYST_UPGRADE: Bias.BULLISH,
    CatalystType.ANALYST_DOWNGRADE: Bias.BEARISH,
    CatalystType.PRICE_TARGET_CHANGE: Bias.BULLISH,
    CatalystType.EARNINGS_REVISION: Bias.BULLISH,
    CatalystType.MAJOR_CONTRACT: Bias.BULLISH,
    CatalystType.PRODUCT_LAUNCH: Bias.BULLISH,
    CatalystType.MERGER_ACQUISITION: Bias.BULLISH,
    CatalystType.GUIDANCE: Bias.NEUTRAL,
    CatalystType.EARNINGS: Bias.NEUTRAL,
}


class MarketIntelligenceAgent:
    def __init__(
        self,
        providers: ProviderBundle,
        llm: LLMClient,
        *,
        universe: list[str],
        use_llm: bool,
    ) -> None:
        self.providers = providers
        self.llm = llm
        self.universe = universe
        self.use_llm = use_llm

    # ------------------------------------------------------------------ run
    def run(self, run_id: str, trading_day: date) -> tuple[MarketBrief, AgentRunRecord]:
        mode = "llm" if self.use_llm else "heuristic"
        with trace(run_id, AgentName.MARKET_INTELLIGENCE, self.llm.backend, mode) as rec:
            pack = self._evidence_pack(trading_day, rec)
            rec.input_summary = {
                "universe": self.universe,
                "indexes": list(INDEXES),
                "news_items": len(pack["news"]),
                "economic_events": len(pack["economic_events"]),
            }

            if self.use_llm:
                try:
                    brief = self.llm.structured(
                        system=SYSTEM_PROMPT,
                        user=build_user_prompt(run_id, trading_day, pack),
                        schema=MarketBrief,
                    )
                    brief.run_id = run_id
                    brief.as_of_trading_day = trading_day
                except (LLMUnavailable, Exception) as exc:  # noqa: BLE001
                    rec.warnings.append(
                        f"LLM path failed ({type(exc).__name__}: {exc}); fell back to heuristics."
                    )
                    rec.reasoning_mode = "heuristic"
                    brief = self._heuristic_brief(run_id, trading_day, pack, rec)
            else:
                brief = self._heuristic_brief(run_id, trading_day, pack, rec)

            rec.output_summary = {
                "market_regime": brief.market_regime.value,
                "volatility_regime": brief.volatility_regime.value,
                "company_catalysts": len(brief.company_catalysts),
                "sector_observations": len(brief.sector_observations),
                "risk_events": len(brief.risk_events),
            }
            rec.missing_data.extend(brief.unavailable_data)
            return brief, rec

    # -------------------------------------------------------- evidence pack
    def _evidence_pack(self, trading_day: date, rec: AgentRunRecord) -> dict[str, Any]:
        md = self.providers.market_data
        pack: dict[str, Any] = {
            "indexes": {},
            "news": [],
            "company_news": {},
            "economic_events": [],
            "earnings": {},
            "sector_performance": {},
            "unavailable": [],
        }
        rec.providers_queried.append("fmp")

        spy_history = None
        for symbol in INDEXES:
            try:
                quote = md.get_quote(symbol)
                history = md.get_price_history(symbol, 260)
                if symbol == "SPY":
                    spy_history = history
                snapshot = compute_snapshot(history, quote, spy_history)
                pack["indexes"][symbol] = {"quote": quote, "technicals": snapshot}
            except Exception as exc:  # noqa: BLE001
                rec.providers_failed.append("fmp")
                pack["unavailable"].append(f"{symbol} index data: {exc}")

        try:
            pack["economic_events"] = md.get_economic_calendar(
                trading_day, trading_day + timedelta(days=30)
            )
        except Exception as exc:  # noqa: BLE001
            pack["unavailable"].append(f"economic calendar: {exc}")

        try:
            earnings = md.get_earnings_calendar(trading_day, trading_day + timedelta(days=120))
            pack["earnings"] = {e.ticker: e for e in earnings}
        except Exception as exc:  # noqa: BLE001
            pack["unavailable"].append(f"earnings calendar: {exc}")

        try:
            pack["sector_performance"] = md.get_sector_performance()
        except Exception as exc:  # noqa: BLE001
            pack["unavailable"].append(f"sector performance: {exc}")

        for ticker in self.universe:
            try:
                pack["company_news"][ticker] = md.get_company_news(ticker, limit=10)
            except Exception as exc:  # noqa: BLE001
                pack["unavailable"].append(f"{ticker} news: {exc}")

        if self.providers.news is not None:
            rec.providers_queried.append("news")
            try:
                pack["news"] = self.providers.news.market_headlines(limit=20)
            except Exception as exc:  # noqa: BLE001
                rec.providers_failed.append("news")
                pack["unavailable"].append(f"market headlines: {exc}")
        else:
            pack["unavailable"].append("news provider not configured")

        # Volatility context: VIX is not in the mock universe, so rather than
        # invent a level the pack carries realised volatility instead and the
        # brief says so.
        pack["realized_vol_proxy"] = self._realized_vol(pack)
        return pack

    @staticmethod
    def _realized_vol(pack: dict[str, Any]) -> float | None:
        spy = pack["indexes"].get("SPY")
        if not spy:
            return None
        atr_pct = spy["technicals"].atr_pct
        return round(atr_pct * (252**0.5), 2) if atr_pct else None

    # ------------------------------------------------------ heuristic path
    def _heuristic_brief(
        self, run_id: str, trading_day: date, pack: dict[str, Any], rec: AgentRunRecord
    ) -> MarketBrief:
        indexes = {sym: self._index_context(sym, pack) for sym in INDEXES}
        spy_ctx = indexes.get("SPY") or IndexContext(symbol="SPY", bias=Bias.NEUTRAL)
        qqq_ctx = indexes.get("QQQ") or IndexContext(symbol="QQQ", bias=Bias.NEUTRAL)

        regime, rationale = self._regime(spy_ctx, qqq_ctx, indexes.get("IWM"))
        vol_regime, vol_ctx = self._volatility(pack)

        sector_obs = self._sector_observations(pack)
        catalysts = self._company_catalysts(pack, trading_day)
        macro = self._macro_observations(pack)
        risks = self._risk_events(pack, trading_day)

        news_items = list(pack["news"])
        for items in pack["company_news"].values():
            news_items.extend(items)

        sources = [
            SourceReference(
                title=n.headline,
                url=n.url,
                publisher=n.publisher,
                published_at=n.published_at,
                tickers=n.tickers,
            )
            for n in news_items
            if n.url
        ]

        return MarketBrief(
            run_id=run_id,
            as_of_trading_day=trading_day,
            market_regime=regime,
            volatility_regime=vol_regime,
            spy=spy_ctx,
            qqq=qqq_ctx,
            iwm=indexes.get("IWM"),
            volatility=vol_ctx,
            breadth_note=self._breadth_note(indexes),
            regime_rationale=rationale,
            macro_observations=macro,
            upcoming_economic_events=list(pack["economic_events"]),
            sector_observations=sector_obs,
            company_catalysts=catalysts,
            news_items=news_items,
            risk_events=risks,
            sources=sources,
            unavailable_data=list(pack["unavailable"]),
            overall_relevance_confidence=0.6 if not pack["unavailable"] else 0.45,
        )

    def _index_context(self, symbol: str, pack: dict[str, Any]) -> IndexContext | None:
        entry = pack["indexes"].get(symbol)
        if not entry:
            return None
        q, t = entry["quote"], entry["technicals"]
        above20 = t.sma20 is not None and t.price > t.sma20
        above50 = t.sma50 is not None and t.price > t.sma50
        ret5 = t.return_5d_pct or 0.0

        if above20 and above50 and ret5 > 1.0:
            bias = Bias.STRONG_BULLISH
        elif above20 and above50:
            bias = Bias.BULLISH
        elif not above20 and not above50 and ret5 < -1.0:
            bias = Bias.STRONG_BEARISH
        elif not above20 and not above50:
            bias = Bias.BEARISH
        else:
            bias = Bias.NEUTRAL

        return IndexContext(
            symbol=symbol,
            bias=bias,
            last_price=q.price,
            change_pct_1d=q.change_pct,
            change_pct_5d=t.return_5d_pct,
            above_sma20=above20,
            above_sma50=above50,
            key_support=t.support,
            key_resistance=t.resistance,
            notes=(
                f"RSI14={t.rsi14}, 20d return {t.return_20d_pct}%, "
                f"ATR {t.atr_pct}% of price."
            ),
        )

    @staticmethod
    def _regime(
        spy: IndexContext, qqq: IndexContext, iwm: IndexContext | None
    ) -> tuple[MarketRegime, str]:
        signs = [spy.bias.sign, qqq.bias.sign] + ([iwm.bias.sign] if iwm else [])
        net = sum(signs)
        detail = (
            f"SPY={spy.bias.value}, QQQ={qqq.bias.value}"
            + (f", IWM={iwm.bias.value}" if iwm else "")
        )
        if net >= 2:
            return MarketRegime.TRENDING_UP, f"Indexes broadly above trend ({detail})."
        if net <= -2:
            return MarketRegime.TRENDING_DOWN, f"Indexes broadly below trend ({detail})."
        if net == 0 and all(s == 0 for s in signs):
            return MarketRegime.RANGE_BOUND, f"No index shows a clean trend ({detail})."
        return MarketRegime.ROTATIONAL, f"Indexes disagree; leadership is rotating ({detail})."

    @staticmethod
    def _volatility(pack: dict[str, Any]) -> tuple[VolatilityRegime, VolatilityContext]:
        rv = pack.get("realized_vol_proxy")
        note = (
            "No VIX feed is configured; annualised realised volatility from SPY ATR is "
            "used as a proxy and is labelled as such."
        )
        if rv is None:
            return VolatilityRegime.NORMAL, VolatilityContext(
                regime=VolatilityRegime.NORMAL,
                notes="Volatility could not be measured; regime defaulted to NORMAL.",
            )
        if rv < 10:
            regime = VolatilityRegime.LOW
        elif rv < 16:
            regime = VolatilityRegime.NORMAL
        elif rv < 24:
            regime = VolatilityRegime.ELEVATED
        elif rv < 35:
            regime = VolatilityRegime.HIGH
        else:
            regime = VolatilityRegime.EXTREME
        return regime, VolatilityContext(
            regime=regime, notes=f"{note} Realised vol proxy = {rv:.1f}%."
        )

    @staticmethod
    def _breadth_note(indexes: dict[str, IndexContext | None]) -> str:
        present = {k: v for k, v in indexes.items() if v}
        up = [k for k, v in present.items() if v.bias.sign > 0]
        down = [k for k, v in present.items() if v.bias.sign < 0]
        return (
            f"{len(up)}/{len(present)} tracked indexes above trend"
            + (f" (leaders: {', '.join(up)})" if up else "")
            + (f"; lagging: {', '.join(down)}" if down else "")
        )

    def _sector_observations(self, pack: dict[str, Any]) -> list[SectorObservation]:
        out: list[SectorObservation] = []
        for sector, perf in sorted(pack["sector_performance"].items()):
            if perf > 1.0:
                bias = Bias.BULLISH
            elif perf < -1.0:
                bias = Bias.BEARISH
            else:
                bias = Bias.NEUTRAL
            tickers = [
                t
                for t in self.universe
                if self._sector_of(t, pack) == sector
            ]
            out.append(
                SectorObservation(
                    sector=sector,
                    bias=bias,
                    rationale=f"5-session average member return {perf:+.2f}%.",
                    representative_tickers=tickers,
                    importance=EventImportance.MEDIUM,
                    evidence_quality=EvidenceQuality.CONFIRMED_FACT,
                )
            )
        return out

    @staticmethod
    def _sector_of(ticker: str, pack: dict[str, Any]) -> str | None:
        from app.providers.mock_market import SCENARIO_BY_SYMBOL

        sc = SCENARIO_BY_SYMBOL.get(ticker.upper())
        return sc.sector if sc else None

    def _company_catalysts(
        self, pack: dict[str, Any], trading_day: date
    ) -> list[CompanyCatalyst]:
        out: list[CompanyCatalyst] = []
        for ticker, items in pack["company_news"].items():
            earnings = pack["earnings"].get(ticker)
            for item in items:
                age_days = (
                    (utcnow() - item.published_at).days if item.published_at else None
                )
                direction = _DIRECTION_BY_TYPE.get(item.catalyst_type, Bias.NEUTRAL)
                importance = _IMPORTANCE_BY_TYPE.get(item.catalyst_type, 0.4)
                out.append(
                    CompanyCatalyst(
                        ticker=ticker,
                        catalyst_type=item.catalyst_type,
                        headline=item.headline,
                        description=item.summary or item.headline,
                        scope=CatalystScope.COMPANY,
                        source=item.publisher,
                        source_url=item.url,
                        published_at=item.published_at,
                        expected_direction=direction,
                        importance_score=importance,
                        expected_time_horizon=TimeHorizon.WEEKS_2_4,
                        scheduled_event_date=(
                            earnings.event_date
                            if earnings and item.catalyst_type is CatalystType.EARNINGS
                            else None
                        ),
                        is_scheduled=item.catalyst_type is CatalystType.EARNINGS,
                        evidence_quality=item.evidence_quality,
                        # A headline older than a week has usually been traded on.
                        already_priced_in=(age_days is not None and age_days > 7),
                    )
                )
            if earnings and not any(
                c.ticker == ticker and c.catalyst_type is CatalystType.EARNINGS for c in out
            ):
                out.append(
                    CompanyCatalyst(
                        ticker=ticker,
                        catalyst_type=CatalystType.EARNINGS,
                        headline=f"{ticker} scheduled earnings on {earnings.event_date}",
                        description="Scheduled event from the earnings calendar.",
                        source="earnings calendar",
                        expected_direction=Bias.NEUTRAL,
                        importance_score=_IMPORTANCE_BY_TYPE[CatalystType.EARNINGS],
                        expected_time_horizon=TimeHorizon.WEEKS_2_4,
                        scheduled_event_date=earnings.event_date,
                        is_scheduled=True,
                        evidence_quality=EvidenceQuality.CONFIRMED_FACT,
                        already_priced_in=False,
                    )
                )
        return out

    @staticmethod
    def _macro_observations(pack: dict[str, Any]) -> list[MacroObservation]:
        out: list[MacroObservation] = []
        for item in pack["news"]:
            if item.catalyst_type.value in ("MACRO_EVENT", "FED_EVENT"):
                out.append(
                    MacroObservation(
                        topic=item.catalyst_type.value,
                        observation=item.headline,
                        direction=Bias.NEUTRAL,
                        importance=EventImportance.MEDIUM,
                        evidence_quality=item.evidence_quality,
                        sources=[
                            SourceReference(
                                title=item.headline,
                                url=item.url,
                                publisher=item.publisher,
                                published_at=item.published_at,
                            )
                        ],
                    )
                )
        return out

    @staticmethod
    def _risk_events(pack: dict[str, Any], trading_day: date) -> list[RiskEvent]:
        out: list[RiskEvent] = []
        for ev in pack["economic_events"]:
            if ev.importance in (EventImportance.HIGH, EventImportance.CRITICAL):
                out.append(
                    RiskEvent(
                        description=f"{ev.name} scheduled for {ev.scheduled_date}",
                        scope=CatalystScope.MARKET_WIDE,
                        occurs_at=ev.scheduled_for,
                        importance=ev.importance,
                    )
                )
        return out


def summarize_pack(pack: dict[str, Any]) -> str:
    """Compact JSON view of the evidence pack, for prompts and audit records."""
    return json.dumps(
        {
            "indexes": {
                sym: {
                    "price": e["quote"].price,
                    "change_pct": e["quote"].change_pct,
                    "sma20": e["technicals"].sma20,
                    "sma50": e["technicals"].sma50,
                    "rsi14": e["technicals"].rsi14,
                    "return_5d_pct": e["technicals"].return_5d_pct,
                    "return_20d_pct": e["technicals"].return_20d_pct,
                    "atr_pct": e["technicals"].atr_pct,
                    "support": e["technicals"].support,
                    "resistance": e["technicals"].resistance,
                }
                for sym, e in pack["indexes"].items()
            },
            "sector_performance": pack["sector_performance"],
            "economic_events": [
                {
                    "name": e.name,
                    "code": e.event_code,
                    "date": str(e.scheduled_date),
                    "importance": e.importance.value,
                    "consensus": e.consensus,
                    "previous": e.previous,
                }
                for e in pack["economic_events"]
            ],
            "earnings": {t: str(e.event_date) for t, e in pack["earnings"].items()},
            "market_news": [
                {
                    "headline": n.headline,
                    "publisher": n.publisher,
                    "url": n.url,
                    "published_at": str(n.published_at),
                    "tickers": n.tickers,
                }
                for n in pack["news"]
            ],
            "company_news": {
                t: [
                    {
                        "headline": n.headline,
                        "publisher": n.publisher,
                        "url": n.url,
                        "published_at": str(n.published_at),
                        "evidence_quality": n.evidence_quality.value,
                    }
                    for n in items
                ]
                for t, items in pack["company_news"].items()
            },
            "unavailable": pack["unavailable"],
        },
        indent=2,
        default=str,
    )


__all__ = ["INDEXES", "PRIMARY_CATALYST_TYPES", "MarketIntelligenceAgent", "summarize_pack"]
