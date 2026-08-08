"""Provider wiring.

One place decides which concrete implementation backs each interface. Adding a
new vendor means adding a branch here plus a class under ``app/providers/`` --
nothing else in the system changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.config.settings import ProviderBackend, Settings, get_settings
from app.models.enums import DataProvider
from app.providers.base import (
    MarketDataProvider,
    NewsProvider,
    OptionsFlowProvider,
    OptionsMarketProvider,
    ProviderRequestRecord,
    ProviderUnavailable,
    assert_no_execution_surface,
)
from app.providers.mock_market import get_market


@dataclass
class ProviderBundle:
    """Everything the pipeline is allowed to fetch data from."""

    market_data: MarketDataProvider
    options_market: OptionsMarketProvider
    options_flow: OptionsFlowProvider | None
    news: NewsProvider | None
    requests: list[ProviderRequestRecord] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for provider in (self.market_data, self.options_market, self.options_flow, self.news):
            if provider is not None:
                assert_no_execution_surface(provider)
                provider.set_sink(self.requests.append)

    def backends(self) -> dict[str, str]:
        return {
            DataProvider.FMP.value: self.market_data.backend,
            DataProvider.ROBINHOOD.value: self.options_market.backend,
            DataProvider.UNUSUAL_WHALES.value: (
                self.options_flow.backend if self.options_flow else "unavailable"
            ),
            DataProvider.NEWS.value: self.news.backend if self.news else "unavailable",
        }

    def all_mocked(self) -> bool:
        return all(b == "mock" for b in self.backends().values() if b != "unavailable")


def build_providers(
    trading_day: date,
    settings: Settings | None = None,
    *,
    robinhood_tool_caller=None,
) -> ProviderBundle:
    s = settings or get_settings()
    market = get_market(s.mock_seed, trading_day)
    unavailable: dict[str, str] = {}

    # --- market data (FMP) ------------------------------------------------
    if s.fmp_backend is ProviderBackend.REST:
        from app.providers.fmp.rest import FMPRestProvider

        market_data: MarketDataProvider = FMPRestProvider(
            api_key=s.secret("fmp_api_key"),
            base_url=s.fmp_base_url,
            timeout=s.provider_timeout_seconds,
            max_retries=s.provider_max_retries,
        )
    elif s.fmp_backend is ProviderBackend.MCP:
        raise ProviderUnavailable(
            "fmp",
            "FMP_BACKEND=mcp is not wired in this milestone. Use 'rest' or 'mock'.",
        )
    else:
        from app.providers.fmp.mock import MockFMPProvider

        market_data = MockFMPProvider(market)

    # --- options market (Robinhood) ---------------------------------------
    if s.robinhood_backend is ProviderBackend.MCP:
        from app.providers.robinhood.mcp import RobinhoodMCPProvider

        options_market: OptionsMarketProvider = RobinhoodMCPProvider(robinhood_tool_caller)
    elif s.robinhood_backend is ProviderBackend.REST:
        raise ProviderUnavailable(
            "robinhood",
            "No unofficial REST client is bundled. Use ROBINHOOD_BACKEND=mcp or mock.",
        )
    else:
        from app.providers.robinhood.mock import MockRobinhoodProvider

        options_market = MockRobinhoodProvider(market)

    # --- options flow (Unusual Whales) ------------------------------------
    options_flow: OptionsFlowProvider | None
    try:
        if s.unusual_whales_backend is ProviderBackend.REST:
            from app.providers.unusual_whales.rest import UnusualWhalesRestProvider

            options_flow = UnusualWhalesRestProvider(
                api_key=s.secret("unusual_whales_api_key"),
                base_url=s.unusual_whales_base_url,
                timeout=s.provider_timeout_seconds,
                max_retries=s.provider_max_retries,
            )
        elif s.unusual_whales_backend is ProviderBackend.MCP:
            raise ProviderUnavailable(
                "unusual_whales", "MCP backend not wired in this milestone."
            )
        else:
            from app.providers.unusual_whales.mock import MockUnusualWhalesProvider

            options_flow = MockUnusualWhalesProvider(market)
    except ProviderUnavailable as exc:
        # Flow is optional: the pipeline degrades to scoring flow at zero
        # rather than inventing a confirmation it never received.
        options_flow = None
        unavailable[DataProvider.UNUSUAL_WHALES.value] = str(exc)

    # --- news --------------------------------------------------------------
    # FMP and Unusual Whales both carry news, so no separate newswire
    # subscription is required. Whichever have credentials are combined.
    news: NewsProvider | None
    if s.news_backend is ProviderBackend.MOCK:
        from app.providers.news.mock import MockNewsProvider

        news = MockNewsProvider(market)
    elif s.news_backend is ProviderBackend.REST:
        from app.providers.news.rest import (
            CompositeNewsProvider,
            FMPNewsProvider,
            UnusualWhalesNewsProvider,
        )

        sources: list[NewsProvider] = []
        notes: list[str] = []
        for label, factory in (
            ("fmp", lambda: FMPNewsProvider(s.secret("fmp_api_key"), s.fmp_base_url)),
            (
                "unusual_whales",
                lambda: UnusualWhalesNewsProvider(
                    s.secret("unusual_whales_api_key"), s.unusual_whales_base_url
                ),
            ),
        ):
            try:
                sources.append(factory())
            except ProviderUnavailable as exc:
                notes.append(f"{label}: {exc}")

        if sources:
            news = CompositeNewsProvider(sources)
        else:
            news = None
            unavailable[DataProvider.NEWS.value] = (
                "No news source could be configured. " + " ".join(notes)
            )
    else:
        news = None
        unavailable[DataProvider.NEWS.value] = (
            f"NEWS_BACKEND={s.news_backend.value} has no bundled implementation."
        )

    return ProviderBundle(
        market_data=market_data,
        options_market=options_market,
        options_flow=options_flow,
        news=news,
        unavailable=unavailable,
    )


__all__ = ["ProviderBundle", "build_providers"]
