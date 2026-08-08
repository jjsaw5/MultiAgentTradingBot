"""Real news providers, backed by FMP and Unusual Whales.

Both vendors already carry news, so no separate newswire subscription is
needed. They cover different ground:

* **FMP** publishes a general market feed and company press releases, with a
  publisher, a URL and a timestamp on every item.
* **Unusual Whales** publishes a headline feed with an ``is_major`` flag, which
  is a genuine importance signal, and a partial ticker association.

:class:`CompositeNewsProvider` merges them and de-duplicates by headline.

One field is deliberately ignored: Unusual Whales returns a ``sentiment``
value, but it read ``"neutral"`` on all 100 rows of a live sample. A field that
never varies carries no information, and passing it through as direction would
manufacture a signal out of a constant.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.models.enums import CatalystScope, CatalystType, EvidenceQuality
from app.models.market_brief import NewsItem
from app.providers.base import NewsProvider, ProviderError, ProviderUnavailable


class FMPNewsProvider(NewsProvider):
    """Market-wide headlines and company press releases from FMP."""

    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://financialmodelingprep.com",
        timeout: int = 15,
        **kwargs,
    ) -> None:
        super().__init__(backend="rest", **kwargs)
        if not api_key:
            raise ProviderUnavailable("news", "FMP_API_KEY is required for the FMP news feed")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        started = time.perf_counter()
        try:
            resp = self._client.get(
                f"{self._base_url}{path}", params={**params, "apikey": self._api_key}
            )
            resp.raise_for_status()
            payload = resp.json()
            self._record(path, params, started)
            return payload if isinstance(payload, list) else []
        except httpx.HTTPError as exc:
            self._record(path, params, started, success=False, error=type(exc).__name__)
            raise ProviderError("news", f"{path}: {exc}") from exc

    @staticmethod
    def _item(row: dict[str, Any], *, quality: EvidenceQuality) -> NewsItem:
        symbol = row.get("symbol")
        return NewsItem(
            headline=row.get("title", ""),
            summary=row.get("text"),
            url=row.get("url"),
            publisher=row.get("publisher") or row.get("site"),
            published_at=_parse_dt(row.get("publishedDate")),
            tickers=[symbol] if symbol else [],
            # Classification is the agent's job; a provider asserting one would
            # corrupt the evidence chain the scoring engine depends on.
            catalyst_type=CatalystType.OTHER,
            scope=CatalystScope.COMPANY if symbol else CatalystScope.MARKET_WIDE,
            relevance_confidence=0.5,
            evidence_quality=quality,
        )

    def market_headlines(self, limit: int = 20) -> list[NewsItem]:
        rows = self._get("/stable/news/general-latest", {"limit": limit, "page": 0})
        return [self._item(r, quality=EvidenceQuality.REPORTED) for r in rows]

    def search(self, query: str, limit: int = 10) -> list[NewsItem]:
        """Ticker-scoped search. FMP's news search is by symbol, not free text."""
        rows = self._get("/stable/news/stock", {"symbols": query.upper(), "limit": limit})
        return [self._item(r, quality=EvidenceQuality.REPORTED) for r in rows]

    def press_releases(self, symbol: str, limit: int = 10) -> list[NewsItem]:
        rows = self._get("/stable/news/press-releases", {"symbols": symbol.upper(), "limit": limit})
        # A company press release is a primary source.
        return [self._item(r, quality=EvidenceQuality.CONFIRMED_FACT) for r in rows]


class UnusualWhalesNewsProvider(NewsProvider):
    """Headline feed with an importance flag."""

    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://api.unusualwhales.com",
        timeout: int = 15,
        **kwargs,
    ) -> None:
        super().__init__(backend="rest", **kwargs)
        if not api_key:
            raise ProviderUnavailable(
                "news", "UNUSUAL_WHALES_API_KEY is required for the Unusual Whales news feed"
            )
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def _headlines(self, limit: int) -> list[dict[str, Any]]:
        started = time.perf_counter()
        path = "/api/news/headlines"
        try:
            resp = self._client.get(f"{self._base_url}{path}", params={"limit": limit})
            resp.raise_for_status()
            payload = resp.json()
            self._record(path, {"limit": limit}, started)
            data = payload.get("data", payload) if isinstance(payload, dict) else payload
            return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
        except httpx.HTTPError as exc:
            self._record(path, {"limit": limit}, started, success=False, error=type(exc).__name__)
            raise ProviderError("news", f"{path}: {exc}") from exc

    @staticmethod
    def _item(row: dict[str, Any]) -> NewsItem:
        tickers = [t for t in (row.get("tickers") or []) if isinstance(t, str)]
        return NewsItem(
            headline=row.get("headline", ""),
            url=None,  # the feed carries a source name but no article URL
            publisher=row.get("source"),
            published_at=_parse_dt(row.get("created_at")),
            tickers=tickers,
            catalyst_type=CatalystType.OTHER,
            scope=CatalystScope.COMPANY if tickers else CatalystScope.MARKET_WIDE,
            # `is_major` is the vendor's own importance flag and is the one
            # piece of metadata here worth carrying. `sentiment` is not used:
            # it read "neutral" on every row of a live sample.
            relevance_confidence=0.7 if row.get("is_major") else 0.4,
            evidence_quality=EvidenceQuality.REPORTED,
        )

    def market_headlines(self, limit: int = 20) -> list[NewsItem]:
        return [self._item(r) for r in self._headlines(limit)]

    def search(self, query: str, limit: int = 10) -> list[NewsItem]:
        needle = query.upper()
        matches = [
            r
            for r in self._headlines(200)
            if needle in (r.get("headline", "") or "").upper()
            or needle in [t.upper() for t in (r.get("tickers") or [])]
        ]
        return [self._item(r) for r in matches[:limit]]


class CompositeNewsProvider(NewsProvider):
    """Merges several news sources, de-duplicating by headline.

    A source that fails is skipped rather than failing the whole fetch -- news
    is corroborating context, and losing one feed should not blind the run.
    """

    def __init__(self, sources: list[NewsProvider], **kwargs) -> None:
        super().__init__(backend="rest", **kwargs)
        if not sources:
            raise ProviderUnavailable("news", "CompositeNewsProvider requires at least one source")
        self.sources = sources

    def set_sink(self, sink) -> None:
        super().set_sink(sink)
        for source in self.sources:
            source.set_sink(sink)

    @staticmethod
    def _dedupe(items: list[NewsItem]) -> list[NewsItem]:
        seen: set[str] = set()
        out: list[NewsItem] = []
        for item in items:
            key = " ".join((item.headline or "").lower().split())[:120]
            if key and key not in seen:
                seen.add(key)
                out.append(item)
        return out

    def _gather(self, call, limit: int) -> list[NewsItem]:
        collected: list[NewsItem] = []
        for source in self.sources:
            try:
                collected.extend(call(source))
            except ProviderError:
                continue
        merged = self._dedupe(collected)
        # Most recent first; undated items sort last rather than being dropped.
        merged.sort(key=lambda i: i.published_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return merged[:limit]

    def market_headlines(self, limit: int = 20) -> list[NewsItem]:
        return self._gather(lambda s: s.market_headlines(limit), limit)

    def search(self, query: str, limit: int = 10) -> list[NewsItem]:
        return self._gather(lambda s: s.search(query, limit), limit)


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    text = str(v).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(v).strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "CompositeNewsProvider",
    "FMPNewsProvider",
    "UnusualWhalesNewsProvider",
]
