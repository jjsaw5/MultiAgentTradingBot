"""Mock Robinhood provider: executable-market view of the synthetic world.

Applies a small price offset relative to FMP so that the cross-provider
reconciliation check in Agent 3 has a realistic, non-zero disagreement to
evaluate rather than always seeing perfect agreement.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from app.models.enums import OptionRight
from app.models.market_data import OptionChain, OptionContract
from app.providers.base import OptionsMarketProvider
from app.providers.mock_market import SyntheticMarket


class MockRobinhoodProvider(OptionsMarketProvider):
    """Read-only. No order-submitting methods exist on this class by design."""

    PRICE_OFFSET_BPS = 4.0  # ~0.04% away from the FMP reference price

    def __init__(self, market: SyntheticMarket, **kwargs) -> None:
        super().__init__(backend="mock", **kwargs)
        self.market = market

    def get_underlying_price(self, symbol: str) -> float:
        return round(
            self.market.last_price(symbol.upper()) * (1 + self.PRICE_OFFSET_BPS / 10_000), 4
        )

    def get_option_chain(
        self,
        symbol: str,
        *,
        min_expiration: date | None = None,
        max_expiration: date | None = None,
    ) -> OptionChain:
        started = time.perf_counter()
        symbol = symbol.upper()
        spot = self.get_underlying_price(symbol)
        contracts: list[OptionContract] = []

        for expiry in self.market.expirations(symbol, count=12):
            if min_expiration and expiry < min_expiration:
                continue
            if max_expiration and expiry > max_expiration:
                continue
            for strike in self.market.strikes(symbol):
                # Only quote strikes within a plausible listed band of spot.
                if not (0.7 * spot <= strike <= 1.35 * spot):
                    continue
                for right in (OptionRight.CALL, OptionRight.PUT):
                    q = self.market.option_quote(
                        symbol, strike, expiry, right is OptionRight.CALL
                    )
                    contracts.append(
                        OptionContract(
                            symbol=_occ(symbol, expiry, right, strike),
                            underlying=symbol,
                            right=right,
                            strike=strike,
                            expiration=expiry,
                            bid=q["bid"],
                            ask=q["ask"],
                            last=q["last"],
                            volume=q["volume"],
                            open_interest=q["open_interest"],
                            implied_volatility=q["iv"],
                            iv_rank=q["iv_rank"],
                            delta=q["delta"],
                            gamma=q["gamma"],
                            theta=q["theta"],
                            vega=q["vega"],
                            rho=q["rho"],
                            provenance=self._provenance("/options/chains/"),
                        )
                    )

        chain = OptionChain(
            underlying=symbol,
            underlying_price=spot,
            contracts=contracts,
            provenance=self._provenance("/options/chains/"),
        )
        self._record(
            "get_option_chain",
            {"symbol": symbol, "contracts": len(contracts)},
            started,
        )
        return chain

    def get_account_summary(self) -> dict[str, Any]:
        started = time.perf_counter()
        summary = {
            "account_type": "mock",
            "options_level": 2,
            "buying_power": 25_000.0,
            "note": "Synthetic account. No brokerage connection is established.",
        }
        self._record("get_account_summary", {}, started)
        return summary

    def get_open_positions(self) -> list[dict[str, Any]]:
        started = time.perf_counter()
        self._record("get_open_positions", {}, started)
        return []


def _occ(symbol: str, expiry: date, right: OptionRight, strike: float) -> str:
    """OCC-style contract symbol, e.g. NVDA240719C00125000."""
    return (
        f"{symbol}{expiry:%y%m%d}{'C' if right is OptionRight.CALL else 'P'}"
        f"{int(round(strike * 1000)):08d}"
    )


__all__ = ["MockRobinhoodProvider"]
