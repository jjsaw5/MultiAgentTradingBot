"""Provider abstractions.

The rest of the system talks to these interfaces only. Swapping FMP's REST API
for its MCP server, or Unusual Whales for another flow vendor, must not require
edits outside ``app/providers``.

Two invariants are enforced here:

1. **No execution surface.** :func:`assert_no_execution_surface` refuses any
   provider that exposes an order-placing method. This is checked at wiring
   time so an execution capability cannot be introduced by accident.
2. **No fabrication.** A provider that cannot obtain a value raises or returns
   ``None``. It never substitutes a plausible number.
"""

from __future__ import annotations

import abc
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from pydantic import Field

from app.models.common import Base, Provenance, utcnow
from app.models.enums import DataProvider
from app.models.market_brief import EconomicEvent, NewsItem
from app.models.market_data import (
    EarningsEvent,
    FlowSnapshot,
    OptionChain,
    PriceHistory,
    Quote,
)

FORBIDDEN_METHOD_TOKENS = (
    "place_order",
    "submit_order",
    "buy",
    "sell",
    "execute_trade",
    "exercise",
    "cancel_order",
)


class ProviderError(RuntimeError):
    """Base class for provider failures. Never swallowed silently."""

    def __init__(self, provider: str, message: str, *, retriable: bool = False) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retriable = retriable


class ProviderUnavailable(ProviderError):
    """The provider is not configured or not reachable."""


class ProviderTimeout(ProviderError):
    def __init__(self, provider: str, message: str) -> None:
        super().__init__(provider, message, retriable=True)


class ProviderRequestRecord(Base):
    """Audit row for a single outbound provider call. Contains no credentials."""

    provider: DataProvider
    operation: str
    params: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utcnow)
    duration_ms: float | None = None
    success: bool = True
    error: str | None = None
    backend: str = "mock"
    cache_hit: bool = False


RequestSink = Callable[[ProviderRequestRecord], None]


class BaseProvider(abc.ABC):
    """Common plumbing: identity, backend label, and request auditing."""

    provider_id: DataProvider

    def __init__(self, backend: str = "mock", sink: RequestSink | None = None) -> None:
        self.backend = backend
        self._sink = sink

    def set_sink(self, sink: RequestSink | None) -> None:
        self._sink = sink

    def _record(
        self,
        operation: str,
        params: dict[str, Any],
        started: float,
        *,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        if self._sink is None:
            return
        self._sink(
            ProviderRequestRecord(
                provider=self.provider_id,
                operation=operation,
                params={k: v for k, v in params.items() if "key" not in k.lower()},
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                success=success,
                error=error,
                backend=self.backend,
            )
        )

    def _provenance(self, endpoint: str, as_of: datetime | None = None) -> Provenance:
        return Provenance(
            provider=self.provider_id,
            endpoint=endpoint,
            as_of=as_of or utcnow(),
            retrieved_at=utcnow(),
        )

    @property
    def available(self) -> bool:
        return True


class MarketDataProvider(BaseProvider):
    """Underlying prices, fundamentals, calendars, news. FMP fills this role."""

    provider_id = DataProvider.FMP

    @abc.abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abc.abstractmethod
    def get_price_history(self, symbol: str, days: int = 260) -> PriceHistory: ...

    @abc.abstractmethod
    def get_earnings_calendar(self, start: date, end: date) -> list[EarningsEvent]: ...

    @abc.abstractmethod
    def get_economic_calendar(self, start: date, end: date) -> list[EconomicEvent]: ...

    @abc.abstractmethod
    def get_company_news(self, symbol: str, limit: int = 20) -> list[NewsItem]: ...

    @abc.abstractmethod
    def get_sector_performance(self) -> dict[str, float]: ...

    # --- typed catalyst feeds --------------------------------------------
    # Optional. A provider that publishes pre-classified events (analyst
    # actions, price-target changes, company press releases) should override
    # these, because a typed catalyst does not need an LLM to recognise it.
    # Providers that only carry untyped headlines return nothing rather than
    # guessing a classification.

    def get_analyst_actions(self, symbol: str, limit: int = 10) -> list[NewsItem]:
        return []

    def get_price_target_changes(self, symbol: str, limit: int = 10) -> list[NewsItem]:
        return []

    def get_press_releases(self, symbol: str, limit: int = 10) -> list[NewsItem]:
        return []

    def get_next_earnings(
        self, symbol: str, horizon_days: int = 120, as_of: date | None = None
    ) -> EarningsEvent | None:
        """Next scheduled earnings on or after ``as_of``.

        The reference date is explicit because the pipeline is driven by a
        trading day, not by the wall clock -- a scan replayed for a past date
        must see the calendar as it stood then.
        """
        today = as_of or utcnow().date()
        events = self.get_earnings_calendar(today, _add_days(today, horizon_days))
        upcoming = sorted(
            (e for e in events if e.ticker == symbol.upper() and e.event_date >= today),
            key=lambda e: e.event_date,
        )
        return upcoming[0] if upcoming else None


class OptionsFlowProvider(BaseProvider):
    """Options-market intelligence. Unusual Whales fills this role."""

    provider_id = DataProvider.UNUSUAL_WHALES

    @abc.abstractmethod
    def get_flow_snapshot(self, symbol: str, window: str = "1d") -> FlowSnapshot: ...

    @abc.abstractmethod
    def get_market_flow_summary(self) -> dict[str, Any]: ...


class OptionsMarketProvider(BaseProvider):
    """Executable-market view: chains, quotes, positions.

    Deliberately read-only. See :func:`assert_no_execution_surface`.
    """

    provider_id = DataProvider.ROBINHOOD

    @abc.abstractmethod
    def get_option_chain(
        self,
        symbol: str,
        *,
        min_expiration: date | None = None,
        max_expiration: date | None = None,
    ) -> OptionChain: ...

    @abc.abstractmethod
    def get_account_summary(self) -> dict[str, Any]: ...

    @abc.abstractmethod
    def get_open_positions(self) -> list[dict[str, Any]]: ...


class NewsProvider(BaseProvider):
    provider_id = DataProvider.NEWS

    @abc.abstractmethod
    def search(self, query: str, limit: int = 10) -> list[NewsItem]: ...

    @abc.abstractmethod
    def market_headlines(self, limit: int = 20) -> list[NewsItem]: ...


def assert_no_execution_surface(provider: BaseProvider) -> None:
    """Fail loudly if a provider exposes anything that could submit an order.

    Called during provider wiring. The MVP is research-only; adding execution
    must be a deliberate, reviewed change, not a side effect of pulling in a
    broker SDK that happens to have ``place_order`` on it.
    """
    offenders = [
        name
        for name in dir(provider)
        if not name.startswith("_")
        and callable(getattr(provider, name, None))
        and any(tok in name.lower() for tok in FORBIDDEN_METHOD_TOKENS)
    ]
    if offenders:
        raise RuntimeError(
            f"{type(provider).__name__} exposes order-execution methods {offenders}; "
            "this system is research-only and must not be wired to execution."
        )


def _add_days(d: date, n: int) -> date:
    from datetime import timedelta

    return d + timedelta(days=n)


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


__all__ = [
    "BaseProvider",
    "MarketDataProvider",
    "NewsProvider",
    "OptionsFlowProvider",
    "OptionsMarketProvider",
    "ProviderError",
    "ProviderRequestRecord",
    "ProviderTimeout",
    "ProviderUnavailable",
    "assert_no_execution_surface",
]
