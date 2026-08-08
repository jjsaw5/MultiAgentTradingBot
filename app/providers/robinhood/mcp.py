"""Robinhood provider backed by the Robinhood Trading MCP server.

Robinhood's MCP tools are reachable from an MCP-capable client session, not
from an arbitrary background Python process. Rather than pretend otherwise,
this class takes a ``tool_caller`` injected by whatever runtime *does* have MCP
access, and maps the tool responses onto the provider interface.

Allowed tools are enumerated explicitly in :data:`READ_ONLY_TOOLS`. Any tool
name outside that set is refused before the call is made -- so even a
misconfigured runtime cannot reach an order-placement tool through this class.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from app.models.enums import OptionRight
from app.models.market_data import OptionChain, OptionContract
from app.providers.base import OptionsMarketProvider, ProviderError, ProviderUnavailable

ToolCaller = Callable[[str, dict[str, Any]], Any]

#: The only Robinhood MCP tools this system may invoke. Read-only by
#: construction: nothing here can create, modify, or cancel an order.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "get_option_chains",
        "get_option_instruments",
        "get_option_quotes",
        "get_option_historicals",
        "get_equity_quotes",
        "get_equity_historicals",
        "get_option_positions",
        "get_equity_positions",
        "get_accounts",
        "get_portfolio",
        "get_earnings_calendar",
    }
)


class RobinhoodMCPProvider(OptionsMarketProvider):
    def __init__(self, tool_caller: ToolCaller | None, **kwargs) -> None:
        super().__init__(backend="mcp", **kwargs)
        if tool_caller is None:
            raise ProviderUnavailable(
                "robinhood",
                "ROBINHOOD_BACKEND=mcp requires an MCP tool caller to be injected by the "
                "hosting runtime. Run with ROBINHOOD_BACKEND=mock for offline use.",
            )
        self._call = tool_caller

    def _tool(self, name: str, **params: Any) -> Any:
        if name not in READ_ONLY_TOOLS:
            raise ProviderError(
                "robinhood",
                f"tool '{name}' is not on the read-only allowlist and will not be called",
            )
        started = time.perf_counter()
        try:
            result = self._call(name, params)
            self._record(name, params, started)
            return result
        except Exception as exc:  # noqa: BLE001 - audited, then surfaced
            self._record(name, params, started, success=False, error=str(exc))
            raise ProviderError("robinhood", f"{name} failed: {exc}") from exc

    def get_option_chain(
        self,
        symbol: str,
        *,
        min_expiration: date | None = None,
        max_expiration: date | None = None,
    ) -> OptionChain:
        symbol = symbol.upper()
        instruments = self._tool(
            "get_option_instruments",
            symbol=symbol,
            expiration_dates=None,
            min_expiration=min_expiration.isoformat() if min_expiration else None,
            max_expiration=max_expiration.isoformat() if max_expiration else None,
        )
        rows = _rows(instruments)
        ids = [r.get("id") for r in rows if r.get("id")]
        quotes = _index_by_id(_rows(self._tool("get_option_quotes", ids=ids))) if ids else {}
        underlying = _first_float(
            _rows(self._tool("get_equity_quotes", symbols=[symbol])),
            "last_trade_price",
            "last_extended_hours_trade_price",
            "price",
        )

        contracts: list[OptionContract] = []
        for r in rows:
            q = quotes.get(r.get("id"), {})
            expiry = _date(r.get("expiration_date"))
            strike = _float(r.get("strike_price"))
            if expiry is None or strike is None:
                continue
            contracts.append(
                OptionContract(
                    symbol=r.get("symbol") or r.get("id", ""),
                    underlying=symbol,
                    right=OptionRight.CALL
                    if str(r.get("type", "")).lower() == "call"
                    else OptionRight.PUT,
                    strike=strike,
                    expiration=expiry,
                    bid=_float(q.get("bid_price")),
                    ask=_float(q.get("ask_price")),
                    last=_float(q.get("last_trade_price")),
                    mark=_float(q.get("adjusted_mark_price") or q.get("mark_price")),
                    volume=_int(q.get("volume")),
                    open_interest=_int(q.get("open_interest")),
                    implied_volatility=_float(q.get("implied_volatility")),
                    delta=_float(q.get("delta")),
                    gamma=_float(q.get("gamma")),
                    theta=_float(q.get("theta")),
                    vega=_float(q.get("vega")),
                    rho=_float(q.get("rho")),
                    provenance=self._provenance("mcp:get_option_quotes", as_of=_now(q)),
                )
            )

        return OptionChain(
            underlying=symbol,
            underlying_price=underlying,
            contracts=contracts,
            provenance=self._provenance("mcp:get_option_instruments"),
        )

    def get_account_summary(self) -> dict[str, Any]:
        accounts = _rows(self._tool("get_accounts"))
        if not accounts:
            return {}
        a = accounts[0]
        # Deliberately narrow: only what risk sizing needs. No PII is copied.
        return {
            "options_level": a.get("option_level"),
            "buying_power": _float(a.get("buying_power")),
            "account_type": a.get("type"),
        }

    def get_open_positions(self) -> list[dict[str, Any]]:
        return _rows(self._tool("get_option_positions"))


def _rows(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "items"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
        return [payload]
    return []


def _index_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r.get("id") or r.get("instrument_id") or r.get("option_id")
        if key:
            out[str(key)] = r
    return out


def _float(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    f = _float(v)
    return int(f) if f is not None else None


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _first_float(rows: list[dict[str, Any]], *keys: str) -> float | None:
    for r in rows:
        for k in keys:
            f = _float(r.get(k))
            if f is not None:
                return f
    return None


def _now(row: dict[str, Any]) -> datetime | None:
    from app.providers.fmp.rest import _parse_dt

    return _parse_dt(row.get("updated_at") or row.get("timestamp"))


__all__ = ["READ_ONLY_TOOLS", "RobinhoodMCPProvider", "ToolCaller"]
