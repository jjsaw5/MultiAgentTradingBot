"""Real news providers and the typed catalyst feeds.

The typed feeds are what let a rating change be recognised as a rating change
without a model reading the headline. That is the difference between 2% and 28%
of real catalysts being tradable, so the classification is pinned here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.enums import CatalystScope, CatalystType, EvidenceQuality
from app.models.market_brief import NewsItem
from app.providers.base import NewsProvider, ProviderError
from app.providers.news.rest import (
    CompositeNewsProvider,
    FMPNewsProvider,
    UnusualWhalesNewsProvider,
)

# --- recorded payloads ------------------------------------------------------
FMP_GENERAL = [
    {
        "symbol": "NVDA",
        "publishedDate": "2026-08-07 17:42:10",
        "publisher": "Reuters",
        "site": "reuters.com",
        "title": "Chip demand stays firm into the second half",
        "text": "Summary text.",
        "url": "https://example.invalid/a",
    },
    {
        "symbol": None,
        "publishedDate": "2026-08-07 16:00:00",
        "publisher": "Bloomberg",
        "site": "bloomberg.com",
        "title": "Yields ease after the auction",
        "text": "Summary text.",
        "url": "https://example.invalid/b",
    },
]

FMP_GRADES = [
    {
        "symbol": "NVDA",
        "publishedDate": "2026-07-29T17:29:58.000Z",
        "newsURL": "https://example.invalid/g1",
        "newsTitle": "Nvidia upgraded to Buy at Citigroup",
        "newsPublisher": "TheFly",
        "newGrade": "Buy",
        "previousGrade": "Neutral",
        "gradingCompany": "Citigroup",
        "action": "upgrade",
    },
    {
        "symbol": "NVDA",
        "publishedDate": "2026-07-28T12:00:00.000Z",
        "newsTitle": "Nvidia cut to Sell",
        "newGrade": "Sell",
        "previousGrade": "Hold",
        "gradingCompany": "Some Bank",
        "action": "downgrade",
    },
    {
        "symbol": "NVDA",
        "publishedDate": "2026-07-27T12:00:00.000Z",
        "newsTitle": "Nvidia Hold reiterated",
        "newGrade": "Hold",
        "previousGrade": "Hold",
        "gradingCompany": "Another Bank",
        "action": "hold",
    },
]

FMP_TARGETS = [
    {
        "symbol": "NVDA",
        "publishedDate": "2026-07-14T09:47:55.000Z",
        "newsURL": "https://example.invalid/pt",
        "newsTitle": "Nvidia price target raised to $330 from $310 at KeyBanc",
        "analystCompany": "KeyBanc",
        "priceTarget": 330,
        "adjPriceTarget": 330,
        "priceWhenPosted": 220.0,
    }
]

UW_HEADLINES = {
    "data": [
        {
            "source": "PR NewsWire",
            "created_at": "2026-08-08T01:10:18Z",
            "tickers": ["NVDA"],
            "headline": "NVIDIA announces expanded datacenter partnership",
            "is_major": True,
            "sentiment": "neutral",
        },
        {
            "source": "GlobeNewswire",
            "created_at": "2026-08-08T00:10:18Z",
            "tickers": [],
            "headline": "Minor corporate filing notice",
            "is_major": False,
            "sentiment": "neutral",
        },
    ]
}


# --- typed catalyst feeds ----------------------------------------------------
@pytest.fixture()
def fmp(monkeypatch):
    from app.providers.fmp.rest import FMPRestProvider

    p = FMPRestProvider(api_key="test")

    def fake_get(path, params=None, cache=False):
        if "grades-news" in path:
            return FMP_GRADES
        if "price-target-news" in path:
            return FMP_TARGETS
        if "press-releases" in path:
            return FMP_GENERAL
        return []

    monkeypatch.setattr(p, "_get", fake_get)
    return p


def test_analyst_actions_are_typed_by_the_vendor(fmp):
    items = fmp.get_analyst_actions("NVDA")
    by_type = {i.catalyst_type for i in items}
    assert CatalystType.ANALYST_UPGRADE in by_type
    assert CatalystType.ANALYST_DOWNGRADE in by_type


def test_a_reiterated_hold_is_not_given_a_direction(fmp):
    """A maintained rating is news, but it is not a bullish or bearish event."""
    held = [i for i in fmp.get_analyst_actions("NVDA") if "Hold reiterated" in i.headline]
    assert held and held[0].catalyst_type is CatalystType.OTHER


def test_a_rating_change_is_recorded_as_fact(fmp):
    upgrade = next(
        i for i in fmp.get_analyst_actions("NVDA")
        if i.catalyst_type is CatalystType.ANALYST_UPGRADE
    )
    assert upgrade.evidence_quality is EvidenceQuality.CONFIRMED_FACT
    assert "Neutral -> Buy" in (upgrade.summary or "")
    assert upgrade.publisher == "Citigroup"


def test_price_target_carries_the_implied_move_but_asserts_no_direction(fmp):
    """Sell-side targets skew above spot, so a high target proves nothing."""
    item = fmp.get_price_target_changes("NVDA")[0]
    assert item.catalyst_type is CatalystType.PRICE_TARGET_CHANGE
    assert "+50.0%" in (item.summary or "")
    assert item.evidence_quality is EvidenceQuality.REPORTED


def test_press_releases_are_treated_as_a_primary_source(fmp):
    items = fmp.get_press_releases("NVDA")
    assert all(i.evidence_quality is EvidenceQuality.CONFIRMED_FACT for i in items)
    # What a release *means* is still interpretation, so the type stays open.
    assert all(i.catalyst_type is CatalystType.OTHER for i in items)


def test_providers_without_typed_feeds_return_nothing_rather_than_guessing():
    """The base implementation must not invent a classification."""
    from datetime import date

    from app.providers.fmp.mock import MockFMPProvider
    from app.providers.mock_market import get_market

    mock = MockFMPProvider(get_market(20240101, date(2024, 6, 3)))
    assert mock.get_analyst_actions("NVDA") == []
    assert mock.get_price_target_changes("NVDA") == []


# --- news providers ----------------------------------------------------------
@pytest.fixture()
def fmp_news(monkeypatch):
    p = FMPNewsProvider(api_key="test")
    monkeypatch.setattr(p, "_get", lambda path, params: FMP_GENERAL)
    return p


@pytest.fixture()
def uw_news(monkeypatch):
    p = UnusualWhalesNewsProvider(api_key="test")
    monkeypatch.setattr(p, "_headlines", lambda limit: UW_HEADLINES["data"])
    return p


def test_fmp_news_scopes_by_whether_a_ticker_is_attached(fmp_news):
    items = fmp_news.market_headlines()
    assert items[0].scope is CatalystScope.COMPANY
    assert items[1].scope is CatalystScope.MARKET_WIDE


def test_news_arrives_unclassified(fmp_news):
    """Typing a headline is interpretation and belongs to the agent."""
    assert all(i.catalyst_type is CatalystType.OTHER for i in fmp_news.market_headlines())


def test_unusual_whales_major_flag_raises_relevance(uw_news):
    major, minor = uw_news.market_headlines()
    assert major.relevance_confidence > minor.relevance_confidence


def test_unusual_whales_sentiment_does_not_influence_the_item():
    """Sentiment read "neutral" on all 100 rows of a live sample.

    A field that never varies carries no information, so changing it must
    change nothing downstream. Otherwise the pipeline would be manufacturing a
    direction signal out of a constant.
    """
    base = {
        "source": "PR NewsWire",
        "created_at": "2026-08-08T01:10:18Z",
        "tickers": ["NVDA"],
        "headline": "Same headline",
        "is_major": True,
    }
    bullish = UnusualWhalesNewsProvider._item({**base, "sentiment": "very_bullish"})
    bearish = UnusualWhalesNewsProvider._item({**base, "sentiment": "very_bearish"})
    # `retrieved_at` is a wall-clock stamp and differs between the two calls.
    ignore = {"retrieved_at"}
    assert bullish.model_dump(exclude=ignore) == bearish.model_dump(exclude=ignore)


def test_composite_merges_and_deduplicates(fmp_news, uw_news):
    duplicate = NewsItem(
        headline="Chip demand stays firm into the second half",  # same as FMP's first
        published_at=datetime(2026, 8, 7, tzinfo=UTC),
    )

    class Echo(NewsProvider):
        def market_headlines(self, limit: int = 20):
            return [duplicate]

        def search(self, query: str, limit: int = 10):
            return [duplicate]

    merged = CompositeNewsProvider([fmp_news, uw_news, Echo()]).market_headlines(limit=20)
    headlines = [i.headline for i in merged]
    assert len(headlines) == len(set(headlines)), "duplicate headline survived the merge"
    assert len(merged) == 4  # 2 FMP + 2 UW, the echo deduped away


def test_composite_survives_a_failing_source(fmp_news):
    class Broken(NewsProvider):
        def market_headlines(self, limit: int = 20):
            raise ProviderError("news", "down")

        def search(self, query: str, limit: int = 10):
            raise ProviderError("news", "down")

    merged = CompositeNewsProvider([Broken(), fmp_news]).market_headlines()
    assert len(merged) == 2, "a failing feed should not blind the run"


def test_composite_orders_newest_first(fmp_news, uw_news):
    merged = CompositeNewsProvider([fmp_news, uw_news]).market_headlines(limit=10)
    stamps = [i.published_at for i in merged if i.published_at]
    assert stamps == sorted(stamps, reverse=True)


def test_composite_requires_at_least_one_source():
    from app.providers.base import ProviderUnavailable

    with pytest.raises(ProviderUnavailable):
        CompositeNewsProvider([])
