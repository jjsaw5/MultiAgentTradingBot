"""Unusual Whales REST client.

Enabled with ``UNUSUAL_WHALES_BACKEND=rest`` plus ``UNUSUAL_WHALES_API_KEY``.
Field names and response shapes below were verified against the live API.

A ticker-level :class:`FlowSnapshot` is assembled from several endpoints,
because no single one carries everything the scoring engine consults:

============================  ==========================================
``/flow-per-expiry``          premium and volume, split by call/put and by
                              side of market
``/flow-alerts``              sweeps, multi-leg share, large prints
``/greek-flow``               directional delta and vega flow
``/volatility/realized``      implied-volatility history, from which IV rank
                              is computed
``/oi-change``                open interest, for the volume/OI ratio
``/spot-exposures``           gamma exposure
``/darkpool/{ticker}``        dark-pool notional
============================  ==========================================

The first four are required; the rest are best-effort and leave their fields
``None`` on failure rather than blocking the snapshot.

**Two values are derived rather than reported**, and both are labelled as such
here and in DATA_SOURCES.md:

* *Bullish and bearish premium.* Unusual Whales reports premium by side of
  market, not by directional intent. The standard construction is applied --
  calls bought on the ask and puts sold on the bid are bullish, puts bought on
  the ask and calls sold on the bid are bearish. This is arithmetic on measured
  values, not a judgement, but it is an inference and is documented as one.
* *IV rank.* Not published directly. Computed as the current implied volatility's
  percentile position within its trailing range, the conventional definition.

Anything the API does not return stays ``None``. A fabricated flow number would
silently move a trade's score, which is worse than an unscored component.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.models.market_data import FlowSnapshot
from app.providers.base import (
    OptionsFlowProvider,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)

#: Trailing window for the IV rank calculation, in trading days.
IV_RANK_LOOKBACK_DAYS = 252

#: Horizon used to translate implied volatility into an expected move.
EXPECTED_MOVE_DAYS = 21

#: Flow alerts are sampled to bound the request; enough to characterise the
#: day's tape without paging through everything.
FLOW_ALERT_LIMIT = 200


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

    # ----------------------------------------------------------------- http
    def _get(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.get(f"{self._base_url}{path}", params=params or {})
                resp.raise_for_status()
                self._record(path, params or {}, started)
                return _rows(resp.json())
            except httpx.TimeoutException as exc:
                last_error = exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    self._record(
                        path, params or {}, started,
                        success=False, error=f"HTTP {exc.response.status_code}",
                    )
                    raise ProviderError(
                        "unusual_whales",
                        f"HTTP {exc.response.status_code} for {path}: {exc.response.text[:140]}",
                    ) from exc
                last_error = exc
            if attempt < self._max_retries:
                time.sleep(0.4 * (2**attempt))
        self._record(path, params or {}, started, success=False, error=type(last_error).__name__)
        raise ProviderTimeout("unusual_whales", f"{path} failed after retries")

    def _try(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Best-effort fetch for optional enrichment; failures degrade to empty."""
        try:
            return self._get(path, params)
        except ProviderError:
            return []

    # ------------------------------------------------------------- snapshot
    def get_flow_snapshot(self, symbol: str, window: str = "1d") -> FlowSnapshot:
        symbol = symbol.upper()

        premium = self._premium_totals(symbol)
        alerts = self._alert_characteristics(symbol)
        greeks = self._greek_flow(symbol)
        iv = self._volatility(symbol)
        oi = self._open_interest(symbol)
        gex = self._gamma_exposure(symbol)
        dark = self._dark_pool(symbol)

        bullish = bearish = None
        if premium.get("call_ask") is not None and premium.get("put_bid") is not None:
            # Derived, not reported -- see the module docstring.
            bullish = round(premium["call_ask"] + premium["put_bid"], 2)
        if premium.get("put_ask") is not None and premium.get("call_bid") is not None:
            bearish = round(premium["put_ask"] + premium["call_bid"], 2)

        iv30 = iv.get("iv30")
        return FlowSnapshot(
            underlying=symbol,
            window=window,
            call_premium=premium.get("call"),
            put_premium=premium.get("put"),
            bullish_premium=bullish,
            bearish_premium=bearish,
            ask_side_premium=_add(premium.get("call_ask"), premium.get("put_ask")),
            bid_side_premium=_add(premium.get("call_bid"), premium.get("put_bid")),
            mid_side_premium=None,  # not reported; never inferred
            sweep_count=alerts.get("sweeps"),
            block_count=alerts.get("floor"),
            large_trade_count=alerts.get("total"),
            multileg_share=alerts.get("multileg_share"),
            total_volume=premium.get("volume"),
            total_open_interest=oi,
            net_delta_flow=greeks.get("delta"),
            net_gamma_flow=None,  # greek-flow reports delta and vega only
            net_vega_flow=greeks.get("vega"),
            gamma_exposure=gex,
            dark_pool_notional=dark.get("notional"),
            dark_pool_bias=dark.get("bias"),
            iv_rank=iv.get("iv_rank"),
            iv30=iv30,
            expected_move_pct=(
                round(iv30 * (EXPECTED_MOVE_DAYS / 365.0) ** 0.5 * 100, 2)
                if iv30 is not None
                else None
            ),
            provenance=self._provenance("/api/stock/{ticker}/flow-per-expiry"),
        )

    # ------------------------------------------------------------ fragments
    def _premium_totals(self, symbol: str) -> dict[str, float | int | None]:
        """Aggregate today's premium and volume across every expiry."""
        rows = self._get(f"/api/stock/{symbol}/flow-per-expiry")
        if not rows:
            return {}
        latest = max((r.get("date") or "") for r in rows)
        todays = [r for r in rows if (r.get("date") or "") == latest] or rows

        def total(field: str) -> float | None:
            values = [_f(r.get(field)) for r in todays]
            present = [v for v in values if v is not None]
            return round(sum(present), 2) if present else None

        volume = None
        call_v, put_v = total("call_volume"), total("put_volume")
        if call_v is not None or put_v is not None:
            volume = int((call_v or 0) + (put_v or 0))

        return {
            "call": total("call_premium"),
            "put": total("put_premium"),
            "call_ask": total("call_premium_ask_side"),
            "call_bid": total("call_premium_bid_side"),
            "put_ask": total("put_premium_ask_side"),
            "put_bid": total("put_premium_bid_side"),
            "volume": volume,
        }

    def _alert_characteristics(self, symbol: str) -> dict[str, Any]:
        """Sweep count and, where measurable, multi-leg share.

        The alerts feed returns single-leg prints only: every row observed
        carries ``has_singleleg: true`` and ``has_multileg: false``, and the
        ``is_multileg`` query parameter is ignored. Computing a share from it
        would therefore always yield 0.0 -- which would assert "this tape has no
        multi-leg flow" from a source that cannot report any.

        So the share is reported only when at least one multi-leg print is
        actually present, and left ``None`` otherwise. The flow scoring rules
        treat an absent share as "cannot tell" and simply do not apply the
        multi-leg suppression, rather than being told there is nothing to
        suppress.
        """
        rows = self._get(f"/api/stock/{symbol}/flow-alerts", {"limit": FLOW_ALERT_LIMIT})
        if not rows:
            return {}
        multileg = sum(1 for r in rows if r.get("has_multileg"))
        return {
            "sweeps": sum(1 for r in rows if r.get("has_sweep")),
            "floor": sum(1 for r in rows if r.get("has_floor")),
            "total": len(rows),
            "multileg_share": round(multileg / len(rows), 3) if multileg else None,
        }

    def _greek_flow(self, symbol: str) -> dict[str, float | None]:
        rows = self._try(f"/api/stock/{symbol}/greek-flow")
        if not rows:
            return {}
        # Intraday ticks; the most recent carries the day's directional flow.
        last = rows[-1]
        return {"delta": _f(last.get("dir_delta_flow")), "vega": _f(last.get("dir_vega_flow"))}

    def _volatility(self, symbol: str) -> dict[str, float | None]:
        """Current implied volatility, plus IV rank computed from its history."""
        rows = self._try(f"/api/stock/{symbol}/volatility/realized")
        series = [
            _f(r.get("implied_volatility"))
            for r in sorted(rows, key=lambda r: r.get("date") or "")
        ]
        series = [v for v in series if v is not None]
        if not series:
            return {}
        current = series[-1]
        window = series[-IV_RANK_LOOKBACK_DAYS:]
        low, high = min(window), max(window)
        # IV rank is where current IV sits within its trailing range. A flat
        # range makes the percentile meaningless, so it is left unscored.
        iv_rank = round((current - low) / (high - low) * 100, 2) if high > low else None
        return {"iv30": round(current, 4), "iv_rank": iv_rank}

    def _open_interest(self, symbol: str) -> int | None:
        rows = self._try(f"/api/stock/{symbol}/oi-change", {"limit": 500})
        totals = [
            _f(r.get("curr_oi") if r.get("curr_oi") is not None else r.get("current_oi"))
            for r in rows
        ]
        present = [v for v in totals if v is not None]
        return int(sum(present)) if present else None

    def _gamma_exposure(self, symbol: str) -> float | None:
        rows = self._try(f"/api/stock/{symbol}/spot-exposures")
        if not rows:
            return None
        return _f(rows[-1].get("gamma_per_one_percent_move_oi"))

    def _dark_pool(self, symbol: str) -> dict[str, Any]:
        rows = self._try(f"/api/darkpool/{symbol}", {"limit": 200})
        if not rows:
            return {}
        notional = sum(v for v in (_f(r.get("premium")) for r in rows) if v is not None)

        # Prints above the NBBO midpoint are buyer-initiated on the usual read.
        above = below = 0
        for r in rows:
            price, bid, ask = _f(r.get("price")), _f(r.get("nbbo_bid")), _f(r.get("nbbo_ask"))
            if price is None or bid is None or ask is None:
                continue
            mid = (bid + ask) / 2
            if price > mid:
                above += 1
            elif price < mid:
                below += 1

        bias = None
        if above or below:
            share = above / (above + below)
            bias = "BULLISH" if share > 0.55 else ("BEARISH" if share < 0.45 else "NEUTRAL")
        return {"notional": round(notional, 2) if notional else None, "bias": bias}

    # --------------------------------------------------------------- market
    def get_market_flow_summary(self) -> dict[str, Any]:
        rows = self._get("/api/market/market-tide")
        if not rows:
            return {}
        last = rows[-1]
        return {
            "as_of": last.get("timestamp") or last.get("date"),
            "net_call_premium": _f(last.get("net_call_premium")),
            "net_put_premium": _f(last.get("net_put_premium")),
            "net_volume": _f(last.get("net_volume")),
            "ticks": len(rows),
        }


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Unwrap the ``{"data": [...]}`` envelope; some endpoints return a bare list."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            return [data]
    return []


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _add(a: float | None, b: float | None) -> float | None:
    if a is None and b is None:
        return None
    return round((a or 0.0) + (b or 0.0), 2)


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        parsed = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "EXPECTED_MOVE_DAYS",
    "IV_RANK_LOOKBACK_DAYS",
    "UnusualWhalesRestProvider",
]
