"""Financial Modeling Prep REST client.

Implemented against FMP's documented v3/v4 endpoints. Enabled by setting
``FMP_BACKEND=rest`` and ``FMP_API_KEY``.

The API key is passed as a query parameter (FMP's scheme) but is stripped from
every audit record by :meth:`BaseProvider._record`, and the client never logs
full request URLs.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from typing import Any

import httpx

from app.models.enums import CatalystScope, CatalystType, EventImportance, EvidenceQuality
from app.models.market_brief import EconomicEvent, NewsItem
from app.models.market_data import EarningsEvent, PriceBar, PriceHistory, Quote
from app.providers.base import (
    MarketDataProvider,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)

_IMPORTANCE = {
    "Low": EventImportance.LOW,
    "Medium": EventImportance.MEDIUM,
    "High": EventImportance.HIGH,
}


class FMPRestProvider(MarketDataProvider):
    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://financialmodelingprep.com",
        timeout: int = 15,
        max_retries: int = 2,
        **kwargs,
    ) -> None:
        super().__init__(backend="rest", **kwargs)
        if not api_key:
            raise ProviderUnavailable("fmp", "FMP_API_KEY is required for FMP_BACKEND=rest")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    # ----------------------------------------------------------------- http
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        started = time.perf_counter()
        query = dict(params or {})
        query["apikey"] = self._api_key
        url = f"{self._base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.get(url, params=query)
                resp.raise_for_status()
                self._record(path, {k: v for k, v in query.items() if k != "apikey"}, started)
                return resp.json()
            except httpx.TimeoutException as exc:
                last_error = exc
            except httpx.HTTPStatusError as exc:
                # 4xx is not worth retrying; surface it immediately.
                if exc.response.status_code < 500:
                    self._record(
                        path,
                        {k: v for k, v in query.items() if k != "apikey"},
                        started,
                        success=False,
                        error=f"HTTP {exc.response.status_code}",
                    )
                    raise ProviderError("fmp", f"HTTP {exc.response.status_code} for {path}") from exc
                last_error = exc
            if attempt < self._max_retries:
                time.sleep(0.4 * (2**attempt))
        self._record(
            path,
            {k: v for k, v in query.items() if k != "apikey"},
            started,
            success=False,
            error=type(last_error).__name__,
        )
        raise ProviderTimeout("fmp", f"{path} failed after {self._max_retries + 1} attempts")

    # ----------------------------------------------------------------- data
    def get_quote(self, symbol: str) -> Quote:
        rows = self._get(f"/api/v3/quote/{symbol.upper()}")
        if not rows:
            raise ProviderError("fmp", f"no quote returned for {symbol}")
        r = rows[0]
        return Quote(
            symbol=r["symbol"],
            price=float(r["price"]),
            previous_close=_f(r.get("previousClose")),
            open=_f(r.get("open")),
            day_high=_f(r.get("dayHigh")),
            day_low=_f(r.get("dayLow")),
            volume=_i(r.get("volume")),
            average_volume=_i(r.get("avgVolume")),
            change_pct=_f(r.get("changesPercentage")),
            provenance=self._provenance(
                "/api/v3/quote", as_of=_ts(r.get("timestamp"))
            ),
        )

    def get_price_history(self, symbol: str, days: int = 260) -> PriceHistory:
        data = self._get(
            f"/api/v3/historical-price-full/{symbol.upper()}", {"timeseries": days}
        )
        rows = list(reversed(data.get("historical", [])))
        bars = [
            PriceBar(
                day=date.fromisoformat(r["date"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=int(r["volume"]),
            )
            for r in rows
        ]
        return PriceHistory(
            symbol=symbol.upper(),
            bars=bars,
            provenance=self._provenance("/api/v3/historical-price-full"),
        )

    def get_earnings_calendar(self, start: date, end: date) -> list[EarningsEvent]:
        rows = self._get(
            "/api/v3/earning_calendar", {"from": start.isoformat(), "to": end.isoformat()}
        )
        return [
            EarningsEvent(
                ticker=r["symbol"],
                event_date=date.fromisoformat(r["date"]),
                time_of_day=r.get("time"),
                eps_estimate=_f(r.get("epsEstimated")),
                revenue_estimate=_f(r.get("revenueEstimated")),
                confirmed=bool(r.get("date")),
                provenance=self._provenance("/api/v3/earning_calendar"),
            )
            for r in rows or []
        ]

    def get_economic_calendar(self, start: date, end: date) -> list[EconomicEvent]:
        rows = self._get(
            "/api/v3/economic_calendar", {"from": start.isoformat(), "to": end.isoformat()}
        )
        out: list[EconomicEvent] = []
        for r in rows or []:
            when = _parse_dt(r.get("date"))
            out.append(
                EconomicEvent(
                    name=r.get("event", "unknown"),
                    event_code=None,
                    scheduled_for=when,
                    scheduled_date=when.date() if when else None,
                    country=r.get("country", "US"),
                    importance=_IMPORTANCE.get(r.get("impact", ""), EventImportance.MEDIUM),
                    consensus=_s(r.get("estimate")),
                    previous=_s(r.get("previous")),
                    actual=_s(r.get("actual")),
                )
            )
        return out

    def get_company_news(self, symbol: str, limit: int = 20) -> list[NewsItem]:
        rows = self._get("/api/v3/stock_news", {"tickers": symbol.upper(), "limit": limit})
        return [
            NewsItem(
                headline=r.get("title", ""),
                summary=r.get("text"),
                url=r.get("url"),
                publisher=r.get("site"),
                published_at=_parse_dt(r.get("publishedDate")),
                tickers=[symbol.upper()],
                catalyst_type=CatalystType.OTHER,
                scope=CatalystScope.COMPANY,
                # Relevance is left neutral: classifying it is the agent's job,
                # and the provider must not assert an interpretation.
                relevance_confidence=0.5,
                evidence_quality=EvidenceQuality.REPORTED,
            )
            for r in rows or []
        ]

    def get_sector_performance(self) -> dict[str, float]:
        rows = self._get("/api/v3/sector-performance")
        out: dict[str, float] = {}
        for r in rows or []:
            pct = r.get("changesPercentage")
            if isinstance(pct, str):
                pct = pct.replace("%", "")
            val = _f(pct)
            if val is not None:
                out[r["sector"]] = val
        return out


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def _s(v: Any) -> str | None:
    return str(v) if v not in (None, "") else None


def _ts(v: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(v), tz=UTC)
    except (TypeError, ValueError):
        return None


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(v), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


__all__ = ["FMPRestProvider"]
