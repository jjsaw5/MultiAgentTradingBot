"""Unusual Whales REST client.

Enabled with ``UNUSUAL_WHALES_BACKEND=rest`` plus ``UNUSUAL_WHALES_API_KEY``.

The response mapping below is written against the ticker-level aggregate
endpoints (``/api/stock/{ticker}/flow-per-strike-intraday`` style payloads).
Unusual Whales has revised field names across versions, so
:meth:`_first` tolerates several spellings and returns ``None`` rather than
guessing when nothing matches -- a missing field must stay missing, because a
fabricated flow number would silently move a trade's score.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.models.market_data import FlowSnapshot
from app.providers.base import (
    OptionsFlowProvider,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)


class UnusualWhalesRestProvider(OptionsFlowProvider):
    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://api.unusualwhales.com",
        timeout: int = 15,
        max_retries: int = 2,
        **kwargs,
    ) -> None:
        super().__init__(backend="rest", **kwargs)
        if not api_key:
            raise ProviderUnavailable(
                "unusual_whales", "UNUSUAL_WHALES_API_KEY is required for rest backend"
            )
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.get(f"{self._base_url}{path}", params=params or {})
                resp.raise_for_status()
                self._record(path, params or {}, started)
                return resp.json()
            except httpx.TimeoutException as exc:
                last_error = exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    self._record(
                        path, params or {}, started,
                        success=False, error=f"HTTP {exc.response.status_code}",
                    )
                    raise ProviderError(
                        "unusual_whales", f"HTTP {exc.response.status_code} for {path}"
                    ) from exc
                last_error = exc
            if attempt < self._max_retries:
                time.sleep(0.4 * (2**attempt))
        self._record(path, params or {}, started, success=False, error=type(last_error).__name__)
        raise ProviderTimeout("unusual_whales", f"{path} failed after retries")

    def get_flow_snapshot(self, symbol: str, window: str = "1d") -> FlowSnapshot:
        symbol = symbol.upper()
        payload = self._get(f"/api/stock/{symbol}/greek-flow")
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        row = data[0] if isinstance(data, list) and data else data
        if not isinstance(row, dict):
            raise ProviderError("unusual_whales", f"unexpected payload shape for {symbol}")

        return FlowSnapshot(
            underlying=symbol,
            window=window,
            call_premium=_f(_first(row, "call_premium", "callPremium")),
            put_premium=_f(_first(row, "put_premium", "putPremium")),
            bullish_premium=_f(_first(row, "bullish_premium", "bullishPremium")),
            bearish_premium=_f(_first(row, "bearish_premium", "bearishPremium")),
            ask_side_premium=_f(_first(row, "ask_side_premium", "askSidePremium")),
            bid_side_premium=_f(_first(row, "bid_side_premium", "bidSidePremium")),
            mid_side_premium=_f(_first(row, "mid_side_premium", "midSidePremium")),
            sweep_count=_i(_first(row, "sweep_count", "sweeps")),
            block_count=_i(_first(row, "block_count", "blocks")),
            large_trade_count=_i(_first(row, "large_trade_count")),
            multileg_share=_f(_first(row, "multileg_share")),
            total_volume=_i(_first(row, "volume", "total_volume")),
            total_open_interest=_i(_first(row, "open_interest", "total_open_interest")),
            net_delta_flow=_f(_first(row, "net_delta_flow", "delta_flow", "dir_delta_flow")),
            net_gamma_flow=_f(_first(row, "net_gamma_flow", "gamma_flow")),
            net_vega_flow=_f(_first(row, "net_vega_flow", "vega_flow")),
            gamma_exposure=_f(_first(row, "gamma_exposure", "gex")),
            iv_rank=_f(_first(row, "iv_rank", "ivRank")),
            iv30=_f(_first(row, "iv30", "implied_volatility_30d")),
            expected_move_pct=_f(_first(row, "expected_move_perc", "expected_move")),
            provenance=self._provenance(f"/api/stock/{symbol}/greek-flow"),
        )

    def get_market_flow_summary(self) -> dict[str, Any]:
        return self._get("/api/market/market-tide")


def _first(row: dict, *names: str) -> Any:
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    return None


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


__all__ = ["UnusualWhalesRestProvider"]
