"""Robinhood provider backed by the Robinhood Trading MCP server.

Robinhood's MCP tools are reachable from an MCP-capable client session, not
from an arbitrary background Python process. Rather than pretend otherwise,
this class takes a ``tool_caller`` injected by whatever runtime *does* have MCP
access, and maps the tool responses onto the provider interface.

Allowed tools are enumerated explicitly in :data:`READ_ONLY_TOOLS`. Any tool
name outside that set is refused before the call is made -- so even a
misconfigured runtime cannot reach an order-placement tool through this class.

The response mapping below was verified against live Robinhood MCP responses.
Four things about the real payloads are easy to get wrong, and each one was
wrong here before it was checked:

1. **Everything is wrapped in ``{"data": {...}}``**, and the inner key varies by
   tool -- ``accounts``, ``instruments``, ``results``. Reaching for ``results``
   at the top level silently yields nothing.
2. **Quotes are nested one level deeper**, as ``results[].quote``, paired with a
   ``results[].close`` giving the official prior-session close.
3. **Greeks are per share, not per contract.** Robinhood reports theta as
   ``-0.15``; this system's models are denominated per contract, so every greek
   is scaled by the contract multiplier. Without that, theta and vega scoring
   would be wrong by a factor of 100.
4. **Contracts carry no OCC symbol** -- only ``chain_symbol``, ``strike_price``
   and ``expiration_date`` -- so the symbol is composed locally.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime
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

DEFAULT_CONTRACT_MULTIPLIER = 100.0

#: Robinhood returns option instruments in pages; this bounds a single chain
#: fetch so a malformed cursor cannot loop forever.
_MAX_INSTRUMENT_PAGES = 20


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
            result = self._call(name, {k: v for k, v in params.items() if v is not None})
            self._record(name, params, started)
            return result
        except Exception as exc:  # noqa: BLE001 - audited, then surfaced
            self._record(name, params, started, success=False, error=str(exc))
            raise ProviderError("robinhood", f"{name} failed: {exc}") from exc

    # ------------------------------------------------------------- chain
    def expirations(self, symbol: str) -> list[date]:
        """Listed expirations for an underlying, from the chain metadata."""
        chains = _rows(self._tool("get_option_chains", underlying_symbol=symbol.upper()), "chains")
        out: set[date] = set()
        for chain in chains:
            for value in chain.get("expiration_dates") or []:
                parsed = _date(value)
                if parsed:
                    out.add(parsed)
        return sorted(out)

    def get_option_chain(
        self,
        symbol: str,
        *,
        min_expiration: date | None = None,
        max_expiration: date | None = None,
    ) -> OptionChain:
        symbol = symbol.upper()

        # The instruments tool has no min/max expiration filter, so the window
        # is resolved from the chain metadata first and passed as an explicit
        # date list. If chain metadata is unavailable the request falls back to
        # an unfiltered fetch and the window is applied locally.
        wanted: list[date] = []
        try:
            wanted = [
                e
                for e in self.expirations(symbol)
                if (min_expiration is None or e >= min_expiration)
                and (max_expiration is None or e <= max_expiration)
            ]
        except ProviderError:
            wanted = []

        instruments = self._instruments(symbol, wanted)
        instruments = [
            r
            for r in instruments
            if _within(_date(r.get("expiration_date")), min_expiration, max_expiration)
            and r.get("state", "active") == "active"
            and r.get("tradability", "tradable") == "tradable"
        ]

        quotes = self._quotes([r["id"] for r in instruments if r.get("id")])
        underlying = self._underlying_price(symbol)

        contracts: list[OptionContract] = []
        for r in instruments:
            q = quotes.get(str(r.get("id")), {})
            expiry = _date(r.get("expiration_date"))
            strike = _float(r.get("strike_price"))
            if expiry is None or strike is None:
                continue
            multiplier = _float(r.get("trade_value_multiplier")) or DEFAULT_CONTRACT_MULTIPLIER
            right = (
                OptionRight.CALL if str(r.get("type", "")).lower() == "call" else OptionRight.PUT
            )
            contracts.append(
                OptionContract(
                    symbol=occ_symbol(symbol, expiry, right, strike),
                    underlying=symbol,
                    right=right,
                    strike=strike,
                    expiration=expiry,
                    bid=_float(q.get("bid_price")),
                    ask=_float(q.get("ask_price")),
                    last=_float(q.get("last_trade_price")),
                    mark=_float(q.get("adjusted_mark_price") or q.get("mark_price")),
                    volume=_int(q.get("volume")),
                    open_interest=_int(q.get("open_interest")),
                    implied_volatility=_float(q.get("implied_volatility")),
                    # Robinhood publishes no IV rank; left absent so the scoring
                    # engine falls back to the flow provider rather than
                    # inventing a percentile.
                    iv_rank=None,
                    delta=_float(q.get("delta")),
                    gamma=_float(q.get("gamma")),
                    # Greeks arrive per share. This system denominates them per
                    # contract, so they are scaled -- see the module docstring.
                    theta=_scaled(q.get("theta"), multiplier),
                    vega=_scaled(q.get("vega"), multiplier),
                    rho=_scaled(q.get("rho"), multiplier),
                    provenance=self._provenance(
                        "mcp:get_option_quotes", as_of=_parse_dt(q.get("updated_at"))
                    ),
                )
            )

        return OptionChain(
            underlying=symbol,
            underlying_price=underlying,
            contracts=contracts,
            provenance=self._provenance("mcp:get_option_instruments"),
        )

    def _instruments(self, symbol: str, expirations: list[date]) -> list[dict[str, Any]]:
        expiration_dates = ",".join(e.isoformat() for e in expirations) or None
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_INSTRUMENT_PAGES):
            payload = self._tool(
                "get_option_instruments",
                chain_symbol=symbol,
                expiration_dates=expiration_dates,
                state="active",
                cursor=cursor,
            )
            out.extend(_rows(payload, "instruments"))
            cursor = _next_cursor(payload)
            if not cursor:
                break
        return out

    def _quotes(self, instrument_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch quotes in batches, keyed by instrument id."""
        quotes: dict[str, dict[str, Any]] = {}
        batch_size = 20  # above this the tool drops the paired closes
        for i in range(0, len(instrument_ids), batch_size):
            batch = instrument_ids[i : i + batch_size]
            if not batch:
                continue
            payload = self._tool("get_option_quotes", instrument_ids=batch)
            for row in _rows(payload, "results"):
                quote = row.get("quote", row)
                key = quote.get("instrument_id") or quote.get("id")
                if key:
                    quotes[str(key)] = quote
        return quotes

    def _underlying_price(self, symbol: str) -> float | None:
        try:
            payload = self._tool("get_equity_quotes", symbols=[symbol])
        except ProviderError:
            return None
        for row in _rows(payload, "results"):
            quote = row.get("quote", row)
            for key in ("last_trade_price", "last_non_reg_trade_price"):
                value = _float(quote.get(key))
                if value:
                    return value
        return None

    # ----------------------------------------------------------- account
    def get_account_summary(self) -> dict[str, Any]:
        """Only what position sizing needs. No account identifiers are copied."""
        accounts = _rows(self._tool("get_accounts"), "accounts")
        active = [a for a in accounts if a.get("state") == "active"] or accounts
        if not active:
            return {}
        account = next((a for a in active if a.get("is_default")), active[0])
        return {
            # `option_level` is a string like "option_level_3"; the numeral is
            # extracted so callers can compare levels without string matching.
            "options_level": _option_level(account.get("option_level")),
            "account_type": account.get("brokerage_account_type"),
            "brokerage_trading_type": account.get("type"),
            # Buying power is deliberately absent: `get_accounts` does not
            # report it reliably, and this system does not size positions from
            # a balance it cannot trust.
            "buying_power": None,
        }

    def get_open_positions(self) -> list[dict[str, Any]]:
        return _rows(self._tool("get_option_positions"), "results")


def occ_symbol(symbol: str, expiry: date, right: OptionRight, strike: float) -> str:
    """OCC-style contract symbol, e.g. ``NVDA260918C00225000``.

    Robinhood identifies contracts by UUID and returns no OCC symbol, so one is
    composed here to keep contract identity readable in reports and stable
    across providers.
    """
    return (
        f"{symbol}{expiry:%y%m%d}{'C' if right is OptionRight.CALL else 'P'}"
        f"{int(round(strike * 1000)):08d}"
    )


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Extract the list of records from an MCP payload.

    Responses are shaped ``{"data": {"<name>": [...]}}`` where ``<name>``
    differs per tool, so the wrapper is unwrapped first and the named keys are
    tried before falling back to the first list-valued entry.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []

    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in (*keys, "results", "items"):
        value = body.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    for value in body.values():
        if isinstance(value, list) and all(isinstance(r, dict) for r in value):
            return value
    return []


def _next_cursor(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    nxt = body.get("next")
    if not nxt:
        return None
    if isinstance(nxt, str) and "cursor=" in nxt:
        return nxt.split("cursor=", 1)[1].split("&", 1)[0]
    return nxt if isinstance(nxt, str) else None


def _within(value: date | None, low: date | None, high: date | None) -> bool:
    if value is None:
        return False
    return (low is None or value >= low) and (high is None or value <= high)


def _option_level(value: Any) -> int | None:
    """`option_level_3` -> 3. Absent or unrecognised stays None."""
    if not value:
        return None
    tail = str(value).rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _scaled(value: Any, multiplier: float) -> float | None:
    v = _float(value)
    return round(v * multiplier, 4) if v is not None else None


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


def _parse_dt(v: Any) -> datetime | None:
    """Parse Robinhood timestamps, which carry nanosecond precision and a Z."""
    if not v:
        return None
    text = str(v).strip().replace("Z", "+00:00")
    # fromisoformat accepts at most microseconds; trim any extra digits.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(c for c in tail if c.isdigit())[:6]
        offset = tail[len(tail) - 6 :] if "+" in tail or "-" in tail[1:] else ""
        text = f"{head}.{digits or '0'}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "DEFAULT_CONTRACT_MULTIPLIER",
    "READ_ONLY_TOOLS",
    "RobinhoodMCPProvider",
    "ToolCaller",
    "occ_symbol",
]
