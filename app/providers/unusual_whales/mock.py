"""Mock Unusual Whales provider backed by the synthetic market."""

from __future__ import annotations

import time
from typing import Any

from app.models.market_data import FlowSnapshot
from app.providers.base import OptionsFlowProvider
from app.providers.mock_market import SCENARIO_BY_SYMBOL, SyntheticMarket


class MockUnusualWhalesProvider(OptionsFlowProvider):
    def __init__(self, market: SyntheticMarket, **kwargs) -> None:
        super().__init__(backend="mock", **kwargs)
        self.market = market

    def get_flow_snapshot(self, symbol: str, window: str = "1d") -> FlowSnapshot:
        started = time.perf_counter()
        symbol = symbol.upper()
        raw = self.market.flow(symbol)
        snap = FlowSnapshot(
            underlying=symbol,
            window=window,
            provenance=self._provenance("/api/stock/{ticker}/flow-alerts"),
            **raw,
        )
        self._record("get_flow_snapshot", {"symbol": symbol, "window": window}, started)
        return snap

    def get_market_flow_summary(self) -> dict[str, Any]:
        started = time.perf_counter()
        rows = {}
        for sym in SCENARIO_BY_SYMBOL:
            f = self.market.flow(sym)
            rows[sym] = {
                "bullish_premium": f["bullish_premium"],
                "bearish_premium": f["bearish_premium"],
                "sweeps": f["sweep_count"],
                "iv_rank": f["iv_rank"],
            }
        self._record("get_market_flow_summary", {}, started)
        return {"as_of": self.market.trading_day.isoformat(), "tickers": rows}


__all__ = ["MockUnusualWhalesProvider"]
