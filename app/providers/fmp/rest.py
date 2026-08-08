"""Financial Modeling Prep REST client.

Written against FMP's **stable** API. The older ``/api/v3/*`` endpoints now
return HTTP 403 with a "Legacy Endpoint" message for keys issued after the
cutover, so this client targets ``/stable/*`` exclusively. Field names below
were verified against live responses rather than inferred from documentation.

Enabled by setting ``FMP_BACKEND=rest`` and ``FMP_API_KEY``.

The API key is passed as a query parameter (FMP's scheme) but is stripped from
every audit record by :meth:`BaseProvider._record`, and the client never logs
full request URLs.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta
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

#: Calendars are market-wide and identical for every ticker in a run, but
#: `get_next_earnings` is called once per candidate. Without a cache a single
#: scan would re-download the same several-thousand-row calendar repeatedly.
_CALENDAR_TTL_SECONDS = 900

#: FMP truncates the market-wide earnings calendar at this many rows without
#: signalling it in the payload. Hitting it means the response is incomplete.
_CALENDAR_ROW_CAP = 4000

logger = logging.getLogger(__name__)


class FMPRestProvider(MarketDataProvider):
    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://financialmodelingprep.com",
        timeout: int = 15,
        max_retries: int = 2,
        country: str = "US",
        **kwargs,
    ) -> None:
        super().__init__(backend="rest", **kwargs)
        if not api_key:
            raise ProviderUnavailable("fmp", "FMP_API_KEY is required for FMP_BACKEND=rest")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._country = country
        self._client = httpx.Client(timeout=timeout)
        self._cache: dict[str, tuple[float, Any]] = {}

    def close(self) -> None:
        self._client.close()

    # ----------------------------------------------------------------- http
    def _get(self, path: str, params: dict[str, Any] | None = None, *, cache: bool = False) -> Any:
        key = f"{path}?{sorted((params or {}).items())}"
        if cache and key in self._cache:
            cached_at, payload = self._cache[key]
            if time.time() - cached_at < _CALENDAR_TTL_SECONDS:
                return payload

        started = time.perf_counter()
        query = {k: v for k, v in (params or {}).items() if v is not None}
        query["apikey"] = self._api_key
        url = f"{self._base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.get(url, params=query)
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, dict) and "Error Message" in payload:
                    raise ProviderError("fmp", f"{path}: {payload['Error Message'][:160]}")
                self._record(path, {k: v for k, v in query.items() if k != "apikey"}, started)
                if cache:
                    self._cache[key] = (time.time(), payload)
                return payload
            except httpx.TimeoutException as exc:
                last_error = exc
            except httpx.HTTPStatusError as exc:
                # 4xx is a configuration or entitlement problem, not a blip.
                if exc.response.status_code < 500:
                    detail = exc.response.text[:160]
                    self._record(
                        path,
                        {k: v for k, v in query.items() if k != "apikey"},
                        started,
                        success=False,
                        error=f"HTTP {exc.response.status_code}",
                    )
                    raise ProviderError(
                        "fmp", f"HTTP {exc.response.status_code} for {path}: {detail}"
                    ) from exc
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
        rows = self._get("/stable/quote", {"symbol": symbol.upper()})
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
            # The stable quote carries no average volume. Rather than issue a
            # second request per ticker, this is left absent and relative
            # volume is derived from the price history in `services.technicals`.
            average_volume=None,
            change_pct=_f(r.get("changePercentage")),
            provenance=self._provenance("/stable/quote", as_of=_ts(r.get("timestamp"))),
        )

    def get_price_history(self, symbol: str, days: int = 260) -> PriceHistory:
        end = datetime.now(UTC).date()
        # Calendar days, padded for weekends and holidays.
        start = end - timedelta(days=int(days * 1.5) + 10)
        rows = self._get(
            "/stable/historical-price-eod/full",
            {"symbol": symbol.upper(), "from": start.isoformat(), "to": end.isoformat()},
        )
        # FMP returns newest first; the rest of the system expects oldest first.
        ordered = sorted(rows or [], key=lambda r: r["date"])
        bars = [
            PriceBar(
                day=date.fromisoformat(r["date"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=int(r["volume"]),
            )
            for r in ordered
            if r.get("open") is not None and r.get("volume") is not None
        ]
        return PriceHistory(
            symbol=symbol.upper(),
            bars=bars[-days:],
            provenance=self._provenance("/stable/historical-price-eod/full"),
        )

    def get_earnings_calendar(self, start: date, end: date) -> list[EarningsEvent]:
        """Market-wide earnings calendar.

        Warning: FMP caps this endpoint at :data:`_CALENDAR_ROW_CAP` rows and
        truncates silently, so a given ticker may be absent even though it
        reports inside the window. Never use this to decide whether a specific
        ticker has earnings -- :meth:`get_next_earnings` queries the per-symbol
        endpoint for exactly that reason.
        """
        rows = self._get(
            "/stable/earnings-calendar",
            {"from": start.isoformat(), "to": end.isoformat()},
            cache=True,
        ) or []
        if len(rows) >= _CALENDAR_ROW_CAP:
            logger.warning(
                "FMP earnings-calendar returned %d rows, at or above the %d cap: the "
                "response is truncated and must not be treated as complete.",
                len(rows),
                _CALENDAR_ROW_CAP,
            )
        return [e for e in (self._earnings_row(r, "/stable/earnings-calendar") for r in rows) if e]

    def get_next_earnings(self, symbol: str, horizon_days: int = 120) -> EarningsEvent | None:
        """Next scheduled earnings for one ticker, from the per-symbol endpoint.

        Overrides the base implementation, which filters the market-wide
        calendar. That calendar is row-capped and would report "no earnings"
        for a ticker that does in fact report -- which would silently disable
        the earnings blackout rule, the exact failure this override prevents.
        """
        rows = self._get("/stable/earnings", {"symbol": symbol.upper(), "limit": 8})
        today = datetime.now(UTC).date()
        horizon = today + timedelta(days=horizon_days)
        events = [
            e
            for e in (self._earnings_row(r, "/stable/earnings") for r in rows or [])
            if e and today <= e.event_date <= horizon
        ]
        return min(events, key=lambda e: e.event_date) if events else None

    def _earnings_row(self, r: dict[str, Any], endpoint: str) -> EarningsEvent | None:
        when = _date(r.get("date"))
        if when is None or not r.get("symbol"):
            return None
        return EarningsEvent(
            ticker=r["symbol"],
            event_date=when,
            # The stable feed carries no BMO/AMC marker. Left as None rather
            # than guessed -- the blackout rule works on dates, and a
            # fabricated session would be worse than an absent one.
            time_of_day=None,
            eps_estimate=_f(r.get("epsEstimated")),
            revenue_estimate=_f(r.get("revenueEstimated")),
            confirmed=r.get("epsActual") is not None,
            provenance=self._provenance(endpoint),
        )

    def get_economic_calendar(self, start: date, end: date) -> list[EconomicEvent]:
        rows = self._get(
            "/stable/economic-calendar",
            {"from": start.isoformat(), "to": end.isoformat()},
            cache=True,
        )
        out: list[EconomicEvent] = []
        for r in rows or []:
            # The feed is global; this system trades US equities and options.
            if self._country and r.get("country") != self._country:
                continue
            when = _parse_dt(r.get("date"))
            out.append(
                EconomicEvent(
                    name=r.get("event", "unknown"),
                    event_code=_event_code(r.get("event", "")),
                    scheduled_for=when,
                    scheduled_date=when.date() if when else None,
                    country=r.get("country", self._country),
                    importance=_IMPORTANCE.get(r.get("impact", ""), EventImportance.MEDIUM),
                    consensus=_s(r.get("estimate")),
                    previous=_s(r.get("previous")),
                    actual=_s(r.get("actual")),
                )
            )
        return out

    def get_company_news(self, symbol: str, limit: int = 20) -> list[NewsItem]:
        rows = self._get("/stable/news/stock", {"symbols": symbol.upper(), "limit": limit})
        return [
            NewsItem(
                headline=r.get("title", ""),
                summary=r.get("text"),
                url=r.get("url"),
                publisher=r.get("publisher") or r.get("site"),
                published_at=_parse_dt(r.get("publishedDate")),
                tickers=[symbol.upper()],
                # Classifying the catalyst type and judging relevance is the
                # agent's job. A provider asserting either would corrupt the
                # evidence chain the scoring engine depends on.
                catalyst_type=CatalystType.OTHER,
                scope=CatalystScope.COMPANY,
                relevance_confidence=0.5,
                evidence_quality=EvidenceQuality.REPORTED,
            )
            for r in rows or []
        ]

    def get_sector_performance(self) -> dict[str, float]:
        # The snapshot is published per completed session; today's may not
        # exist yet, so fall back a day at a time.
        today = datetime.now(UTC).date()
        rows: list[dict[str, Any]] = []
        for back in range(0, 5):
            rows = self._get(
                "/stable/sector-performance-snapshot",
                {"date": (today - timedelta(days=back)).isoformat()},
                cache=True,
            ) or []
            if rows:
                break

        # One row per (sector, exchange); average across exchanges.
        totals: dict[str, list[float]] = {}
        for r in rows:
            change = _f(r.get("averageChange"))
            if change is None:
                continue
            totals.setdefault(r["sector"], []).append(change)
        return {k: round(sum(v) / len(v), 4) for k, v in totals.items()}


#: Maps FMP's free-text event names onto the short codes the event-risk config
#: is written in. Unrecognised events keep ``None`` rather than a guess.
_EVENT_CODES = {
    "CPI": ("consumer price index", "cpi"),
    "PPI": ("producer price index", "ppi"),
    "PCE": ("pce price index", "core pce"),
    "GDP": ("gdp growth rate", "gdp"),
    "NFP": ("non farm payrolls", "nonfarm payrolls"),
    "CLAIMS": ("initial jobless claims", "jobless claims"),
    "FOMC": ("fed interest rate decision", "fomc", "fed press conference"),
    "RETAIL": ("retail sales",),
    "ISM": ("ism manufacturing", "ism services"),
    "CONFIDENCE": ("consumer confidence",),
    "UNEMPLOYMENT": ("unemployment rate",),
}


def _event_code(name: str) -> str | None:
    lowered = name.lower()
    for code, needles in _EVENT_CODES.items():
        if any(n in lowered for n in needles):
            return code
    return None


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


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    text = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    # ISO-8601 with a Z suffix and/or sub-second precision.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


__all__ = ["FMPRestProvider"]
