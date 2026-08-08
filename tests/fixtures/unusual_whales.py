"""Recorded Unusual Whales responses, verbatim in shape.

Captured from live calls so the client's field mapping and the two derived
values (bullish/bearish premium, IV rank) are pinned against what the API
actually returns. All of it is public market data.
"""

from __future__ import annotations

from typing import Any

# --- /api/stock/{t}/flow-per-expiry -----------------------------------------
# Two expiries on the same date, to prove aggregation across the chain.
FLOW_PER_EXPIRY: dict[str, Any] = {
    "data": [
        {
            "date": "2026-08-07",
            "ticker": "NVDA",
            "expiry": "2026-08-07",
            "call_volume": 1_605_787,
            "put_volume": 648_746,
            "call_premium": "435557906.00",
            "put_premium": "32386470.00",
            "call_premium_ask_side": "220934041.00",
            "call_premium_bid_side": "134019105.00",
            "put_premium_ask_side": "14989553.00",
            "put_premium_bid_side": "14007719.00",
            "call_trades": 171_127,
            "put_trades": 80_480,
        },
        {
            "date": "2026-08-07",
            "ticker": "NVDA",
            "expiry": "2026-08-14",
            "call_volume": 400_000,
            "put_volume": 100_000,
            "call_premium": "100000000.00",
            "put_premium": "20000000.00",
            "call_premium_ask_side": "60000000.00",
            "call_premium_bid_side": "30000000.00",
            "put_premium_ask_side": "8000000.00",
            "put_premium_bid_side": "9000000.00",
            "call_trades": 40_000,
            "put_trades": 10_000,
        },
        # A stale row from a prior session; must be excluded from the totals.
        {
            "date": "2026-08-06",
            "ticker": "NVDA",
            "expiry": "2026-08-07",
            "call_volume": 999_999,
            "put_volume": 999_999,
            "call_premium": "999999999.00",
            "put_premium": "999999999.00",
            "call_premium_ask_side": "1.00",
            "call_premium_bid_side": "1.00",
            "put_premium_ask_side": "1.00",
            "put_premium_bid_side": "1.00",
        },
    ]
}

# --- /api/stock/{t}/flow-alerts ---------------------------------------------
# Every row is single-leg: the feed does not report multi-leg prints.
FLOW_ALERTS: dict[str, Any] = {
    "data": [
        {
            "type": "call",
            "ticker": "NVDA",
            "created_at": "2026-08-07T20:00:56.772327Z",
            "price": "28.82",
            "open_interest": 28,
            "volume": 117,
            "expiry": "2026-09-25",
            "strike": "200",
            "underlying_price": "223.9",
            "total_premium": "144102",
            "has_floor": False,
            "has_multileg": False,
            "has_sweep": True,
            "has_singleleg": True,
            "option_chain": "NVDA260925C00200000",
        },
        {
            "type": "put",
            "ticker": "NVDA",
            "created_at": "2026-08-07T19:50:00.000000Z",
            "total_premium": "90000",
            "has_floor": True,
            "has_multileg": False,
            "has_sweep": False,
            "has_singleleg": True,
        },
        {
            "type": "call",
            "ticker": "NVDA",
            "created_at": "2026-08-07T19:40:00.000000Z",
            "total_premium": "55000",
            "has_floor": False,
            "has_multileg": False,
            "has_sweep": True,
            "has_singleleg": True,
        },
        {
            "type": "call",
            "ticker": "NVDA",
            "created_at": "2026-08-07T19:30:00.000000Z",
            "total_premium": "72000",
            "has_floor": False,
            "has_multileg": False,
            "has_sweep": True,
            "has_singleleg": True,
        },
    ]
}

# --- /api/stock/{t}/greek-flow ----------------------------------------------
GREEK_FLOW: dict[str, Any] = {
    "data": [
        {
            "timestamp": "2026-08-07T13:30:00Z",
            "ticker": "NVDA",
            "dir_delta_flow": "4963.186288879952",
            "dir_vega_flow": "-2365.958989750511",
        },
        {
            "timestamp": "2026-08-07T19:55:00Z",
            "ticker": "NVDA",
            "dir_delta_flow": "38390.733593696240",
            "dir_vega_flow": "119.268364922155",
        },
    ]
}

# --- /api/stock/{t}/volatility/realized -------------------------------------
# Current IV sits three quarters of the way up a 0.20-0.60 range.
VOLATILITY_REALIZED: dict[str, Any] = {
    "data": [
        {"date": "2026-08-03", "implied_volatility": "0.200000"},
        {"date": "2026-08-04", "implied_volatility": "0.600000"},
        {"date": "2026-08-05", "implied_volatility": "0.400000"},
        {"date": "2026-08-07", "implied_volatility": "0.500000"},
    ]
}

# --- /api/stock/{t}/oi-change ------------------------------------------------
OI_CHANGE: dict[str, Any] = {
    "data": [
        {"option_symbol": "NVDA260807C00225000", "curr_oi": 30_000, "volume": 318_621},
        {"option_symbol": "NVDA260807P00220000", "curr_oi": 20_000, "volume": 100_000},
    ]
}

# --- /api/stock/{t}/spot-exposures -------------------------------------------
SPOT_EXPOSURES: dict[str, Any] = {
    "data": [
        {"time": "2026-08-07T10:30:00.000000Z", "gamma_per_one_percent_move_oi": "-1058809.24"},
        {"time": "2026-08-07T19:30:00.000000Z", "gamma_per_one_percent_move_oi": "2381738729.00"},
    ]
}

# --- /api/darkpool/{t} --------------------------------------------------------
DARKPOOL: dict[str, Any] = {
    "data": [
        # Above the midpoint -> buyer-initiated on the usual read.
        {"premium": "247924.52", "price": "223.80", "nbbo_bid": "223.70", "nbbo_ask": "223.85"},
        {"premium": "100000.00", "price": "223.84", "nbbo_bid": "223.70", "nbbo_ask": "223.85"},
        {"premium": "50000.00", "price": "223.72", "nbbo_bid": "223.70", "nbbo_ask": "223.85"},
    ]
}

# --- /api/market/market-tide --------------------------------------------------
MARKET_TIDE: dict[str, Any] = {
    "data": [
        {
            "timestamp": "2026-08-07T16:10:00-04:00",
            "date": "2026-08-07",
            "net_call_premium": "-168484829.0000",
            "net_put_premium": "-338649079.0000",
            "net_volume": "553910",
        }
    ]
}

_BY_PATH = {
    "flow-per-expiry": FLOW_PER_EXPIRY,
    "flow-alerts": FLOW_ALERTS,
    "greek-flow": GREEK_FLOW,
    "volatility/realized": VOLATILITY_REALIZED,
    "oi-change": OI_CHANGE,
    "spot-exposures": SPOT_EXPOSURES,
    "darkpool": DARKPOOL,
    "market-tide": MARKET_TIDE,
}


def payload_for(path: str) -> Any:
    for needle, payload in _BY_PATH.items():
        if needle in path:
            return payload
    raise KeyError(path)


__all__ = [
    "DARKPOOL",
    "FLOW_ALERTS",
    "FLOW_PER_EXPIRY",
    "GREEK_FLOW",
    "MARKET_TIDE",
    "OI_CHANGE",
    "SPOT_EXPOSURES",
    "VOLATILITY_REALIZED",
    "payload_for",
]
