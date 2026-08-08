"""Mock news provider: market-wide headlines for Agent 1.

Real deployments should point this at a reputable financial newswire. Social
media is intentionally out of scope -- the schema records a publisher and a
URL, and unattributed posts cannot satisfy either without lying.
"""

from __future__ import annotations

import time

from app.models.enums import CatalystScope, CatalystType, EvidenceQuality
from app.models.market_brief import NewsItem
from app.providers.base import NewsProvider
from app.providers.mock_market import SyntheticMarket

_MARKET_HEADLINES: list[dict] = [
    {
        "headline": "Stocks grind higher as traders position ahead of the inflation print",
        "publisher": "Reuters",
        "url": "https://example.invalid/reuters/market-wrap",
        "age_days": 0,
        "type": CatalystType.MACRO_EVENT,
        "quality": EvidenceQuality.REPORTED,
        "tickers": ["SPY", "QQQ"],
    },
    {
        "headline": "Two-year yields ease as rate-cut odds firm for the autumn meeting",
        "publisher": "Bloomberg",
        "url": "https://example.invalid/bloomberg/rates",
        "age_days": 0,
        "type": CatalystType.FED_EVENT,
        "quality": EvidenceQuality.REPORTED,
        "tickers": ["SPY", "IWM", "JPM"],
    },
    {
        "headline": "Semiconductor index outperforms for a third straight session",
        "publisher": "Reuters",
        "url": "https://example.invalid/reuters/semis",
        "age_days": 1,
        "type": CatalystType.SECTOR_ROTATION,
        "quality": EvidenceQuality.CONFIRMED_FACT,
        "tickers": ["NVDA", "AMD"],
    },
    {
        "headline": "Crude extends losing streak on softer demand indicators",
        "publisher": "Reuters",
        "url": "https://example.invalid/reuters/crude-2",
        "age_days": 1,
        "type": CatalystType.INDUSTRY_DEVELOPMENT,
        "quality": EvidenceQuality.CONFIRMED_FACT,
        "tickers": ["XOM"],
    },
    {
        "headline": "Fed speakers keep the door open to a data-dependent path",
        "publisher": "Associated Press",
        "url": "https://example.invalid/ap/fed-speakers",
        "age_days": 2,
        "type": CatalystType.FED_EVENT,
        "quality": EvidenceQuality.REPORTED,
        "tickers": ["SPY"],
    },
    {
        "headline": "Volatility sellers remain active; front-month VIX futures stay in contango",
        "publisher": "Financial Times",
        "url": "https://example.invalid/ft/vol",
        "age_days": 1,
        "type": CatalystType.MACRO_EVENT,
        "quality": EvidenceQuality.INTERPRETATION,
        "tickers": ["SPY", "QQQ"],
    },
]


class MockNewsProvider(NewsProvider):
    def __init__(self, market: SyntheticMarket, **kwargs) -> None:
        super().__init__(backend="mock", **kwargs)
        self.market = market

    def _to_item(self, row: dict) -> NewsItem:
        return NewsItem(
            headline=row["headline"],
            url=row["url"],
            publisher=row["publisher"],
            published_at=self.market.event_datetime(-row["age_days"], hour=12),
            tickers=row.get("tickers", []),
            catalyst_type=row["type"],
            scope=CatalystScope.MARKET_WIDE
            if row["type"] in (CatalystType.MACRO_EVENT, CatalystType.FED_EVENT)
            else CatalystScope.SECTOR,
            relevance_confidence=0.7,
            evidence_quality=row["quality"],
        )

    def market_headlines(self, limit: int = 20) -> list[NewsItem]:
        started = time.perf_counter()
        items = [self._to_item(r) for r in _MARKET_HEADLINES][:limit]
        self._record("market_headlines", {"limit": limit}, started)
        return items

    def search(self, query: str, limit: int = 10) -> list[NewsItem]:
        started = time.perf_counter()
        q = query.lower()
        items = [
            self._to_item(r)
            for r in _MARKET_HEADLINES
            if q in r["headline"].lower() or q.upper() in r.get("tickers", [])
        ][:limit]
        self._record("search", {"query": query, "limit": limit}, started)
        return items


__all__ = ["MockNewsProvider"]
