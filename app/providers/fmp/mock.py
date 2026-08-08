"""Mock Financial Modeling Prep provider backed by the synthetic market."""

from __future__ import annotations

import time
from datetime import date, timedelta

from app.models.enums import CatalystScope, CatalystType, EventImportance, EvidenceQuality
from app.models.market_brief import EconomicEvent, NewsItem
from app.models.market_data import EarningsEvent, PriceHistory, Quote
from app.providers.base import MarketDataProvider
from app.providers.mock_market import SCENARIO_BY_SYMBOL, SyntheticMarket

# Curated headlines so Agent 1 has something concrete to reason about.
# Each entry mirrors the shape of a real FMP `stock_news` row.
_NEWS: dict[str, list[dict]] = {
    "NVDA": [
        {
            "headline": "NVIDIA says next-gen accelerator racks are sold out through the "
            "coming quarter as hyperscaler orders accelerate",
            "publisher": "Reuters",
            "url": "https://example.invalid/reuters/nvda-supply",
            "age_days": 1,
            "type": CatalystType.MAJOR_CONTRACT,
            "quality": EvidenceQuality.REPORTED,
        },
        {
            "headline": "Two banks raise NVIDIA price targets citing datacenter capex guidance",
            "publisher": "Barron's",
            "url": "https://example.invalid/barrons/nvda-pt",
            "age_days": 2,
            "type": CatalystType.PRICE_TARGET_CHANGE,
            "quality": EvidenceQuality.REPORTED,
        },
    ],
    "AMD": [
        {
            "headline": "AMD wins accelerator slot at a top-4 cloud provider, per supply chain checks",
            "publisher": "Bloomberg",
            "url": "https://example.invalid/bloomberg/amd-cloud",
            "age_days": 2,
            "type": CatalystType.MAJOR_CONTRACT,
            "quality": EvidenceQuality.REPORTED,
        },
    ],
    "MSFT": [
        {
            "headline": "Microsoft raises enterprise AI seat pricing; analysts lift estimates",
            "publisher": "CNBC",
            "url": "https://example.invalid/cnbc/msft-pricing",
            "age_days": 1,
            "type": CatalystType.EARNINGS_REVISION,
            "quality": EvidenceQuality.REPORTED,
        },
        {
            "headline": "Microsoft upgraded to Buy on cloud reacceleration",
            "publisher": "MarketWatch",
            "url": "https://example.invalid/mw/msft-upgrade",
            "age_days": 3,
            "type": CatalystType.ANALYST_UPGRADE,
            "quality": EvidenceQuality.CONFIRMED_FACT,
        },
    ],
    "META": [
        {
            "headline": "Meta ad checks described as 'in line' heading into the quarter",
            "publisher": "The Information",
            "url": "https://example.invalid/info/meta-ads",
            "age_days": 6,
            "type": CatalystType.INDUSTRY_DEVELOPMENT,
            "quality": EvidenceQuality.INTERPRETATION,
        },
    ],
    "TSLA": [
        {
            "headline": "Tesla confirms quarterly results date; options market prices an outsized move",
            "publisher": "Company release",
            "url": "https://example.invalid/tsla/ir",
            "age_days": 1,
            "type": CatalystType.EARNINGS,
            "quality": EvidenceQuality.CONFIRMED_FACT,
        },
    ],
    "XOM": [
        {
            "headline": "Crude slides for a fourth session on demand concerns; energy majors lag",
            "publisher": "Reuters",
            "url": "https://example.invalid/reuters/crude",
            "age_days": 1,
            "type": CatalystType.INDUSTRY_DEVELOPMENT,
            "quality": EvidenceQuality.CONFIRMED_FACT,
        },
        {
            "headline": "Exxon downgraded on narrowing refining margins",
            "publisher": "Reuters",
            "url": "https://example.invalid/reuters/xom-downgrade",
            "age_days": 2,
            "type": CatalystType.ANALYST_DOWNGRADE,
            "quality": EvidenceQuality.REPORTED,
        },
    ],
    "JPM": [
        {
            "headline": "Bank net interest income guidance nudged higher across the group",
            "publisher": "Financial Times",
            "url": "https://example.invalid/ft/banks-nii",
            "age_days": 4,
            "type": CatalystType.GUIDANCE,
            "quality": EvidenceQuality.REPORTED,
        },
    ],
    "LLY": [
        {
            "headline": "Lilly readout expected at an upcoming medical conference",
            "publisher": "STAT",
            "url": "https://example.invalid/stat/lly-readout",
            "age_days": 5,
            "type": CatalystType.CONFERENCE,
            "quality": EvidenceQuality.REPORTED,
        },
    ],
    "SOFI": [
        {
            "headline": "Consumer fintech lenders rally on rate-cut expectations",
            "publisher": "Yahoo Finance",
            "url": "https://example.invalid/yf/fintech",
            "age_days": 3,
            "type": CatalystType.SECTOR_ROTATION,
            "quality": EvidenceQuality.INTERPRETATION,
        },
    ],
}


class MockFMPProvider(MarketDataProvider):
    """Deterministic stand-in for FMP.

    Applies a tiny, characteristic price offset so cross-provider
    reconciliation has something real to reconcile.
    """

    PRICE_OFFSET_BPS = 0.0  # FMP is treated as the reference price

    def __init__(self, market: SyntheticMarket, **kwargs) -> None:
        super().__init__(backend="mock", **kwargs)
        self.market = market

    # ------------------------------------------------------------------ data
    def get_quote(self, symbol: str) -> Quote:
        started = time.perf_counter()
        symbol = symbol.upper()
        try:
            bars = self.market.bars(symbol, 260)
            last, prev = bars[-1], bars[-2]
            price = round(last.close * (1 + self.PRICE_OFFSET_BPS / 10_000), 4)
            quote = Quote(
                symbol=symbol,
                price=price,
                previous_close=prev.close,
                open=last.open,
                day_high=last.high,
                day_low=last.low,
                volume=self.market.today_volume(symbol),
                average_volume=self.market.average_volume(symbol),
                change_pct=round((last.close / prev.close - 1) * 100, 3),
                provenance=self._provenance("/api/v3/quote"),
            )
            self._record("get_quote", {"symbol": symbol}, started)
            return quote
        except Exception as exc:  # noqa: BLE001 - recorded then re-raised
            self._record("get_quote", {"symbol": symbol}, started, success=False, error=str(exc))
            raise

    def get_price_history(self, symbol: str, days: int = 260) -> PriceHistory:
        started = time.perf_counter()
        symbol = symbol.upper()
        history = PriceHistory(
            symbol=symbol,
            bars=self.market.bars(symbol, days),
            provenance=self._provenance("/api/v3/historical-price-full"),
        )
        self._record("get_price_history", {"symbol": symbol, "days": days}, started)
        return history

    def get_earnings_calendar(self, start: date, end: date) -> list[EarningsEvent]:
        started = time.perf_counter()
        out: list[EarningsEvent] = []
        for sym in SCENARIO_BY_SYMBOL:
            d = self.market.earnings_date(sym)
            if d and start <= d <= end:
                out.append(
                    EarningsEvent(
                        ticker=sym,
                        event_date=d,
                        time_of_day="AMC",
                        confirmed=True,
                        provenance=self._provenance("/api/v3/earning_calendar"),
                    )
                )
        self._record(
            "get_earnings_calendar", {"start": str(start), "end": str(end)}, started
        )
        return sorted(out, key=lambda e: e.event_date)

    def get_economic_calendar(self, start: date, end: date) -> list[EconomicEvent]:
        started = time.perf_counter()
        out: list[EconomicEvent] = []
        for row in self.market.economic_events():
            when = self.market.event_datetime(row["offset"])
            if start <= when.date() <= end:
                out.append(
                    EconomicEvent(
                        name=row["name"],
                        event_code=row["code"],
                        scheduled_for=when,
                        scheduled_date=when.date(),
                        importance=EventImportance(row["importance"]),
                        consensus=row.get("consensus"),
                        previous=row.get("previous"),
                    )
                )
        self._record(
            "get_economic_calendar", {"start": str(start), "end": str(end)}, started
        )
        return out

    def get_company_news(self, symbol: str, limit: int = 20) -> list[NewsItem]:
        started = time.perf_counter()
        symbol = symbol.upper()
        today = self.market.trading_day
        items = [
            NewsItem(
                headline=row["headline"],
                summary=None,
                url=row["url"],
                publisher=row["publisher"],
                published_at=self.market.event_datetime(-row["age_days"], hour=13),
                tickers=[symbol],
                catalyst_type=row["type"],
                scope=CatalystScope.COMPANY,
                relevance_confidence=0.8,
                evidence_quality=row["quality"],
            )
            for row in _NEWS.get(symbol, [])
            if (today - timedelta(days=row["age_days"])) <= today
        ][:limit]
        self._record("get_company_news", {"symbol": symbol, "limit": limit}, started)
        return items

    def get_sector_performance(self) -> dict[str, float]:
        started = time.perf_counter()
        out: dict[str, float] = {}
        for sc in SCENARIO_BY_SYMBOL.values():
            if sc.is_index:
                continue
            bars = self.market.bars(sc.symbol, 10)
            perf = (bars[-1].close / bars[-6].close - 1) * 100
            out.setdefault(sc.sector, 0.0)
            out[sc.sector] = round((out[sc.sector] + perf) / 2 if out[sc.sector] else perf, 3)
        self._record("get_sector_performance", {}, started)
        return out


__all__ = ["MockFMPProvider"]
