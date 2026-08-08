"""Recorded Robinhood MCP responses, verbatim in shape.

Captured from live calls so the adapter's field mapping is pinned against what
the server actually returns rather than what the documentation implies. The
option and quote payloads are public market data. The account payload is
shape-accurate but the identifiers are synthetic -- no real account number is
committed to this repository.
"""

from __future__ import annotations

from typing import Any

# --- get_equity_quotes(symbols=["NVDA"]) ------------------------------------
EQUITY_QUOTES: dict[str, Any] = {
    "data": {
        "results": [
            {
                "quote": {
                    "symbol": "NVDA",
                    "last_trade_price": "223.900000",
                    "venue_last_trade_time": "2026-08-07T19:59:59.993671629Z",
                    "last_non_reg_trade_price": "223.800000",
                    "adjusted_previous_close": "218.990000",
                    "previous_close": "218.990000",
                    "previous_close_date": "2026-08-06",
                    "bid_price": "223.700000",
                    "ask_price": "238.000000",
                    "has_traded": True,
                    "state": "active",
                },
                "close": {
                    "symbol": "NVDA",
                    "date": "2026-08-06",
                    "price": "218.99",
                    "interpolated": False,
                },
            }
        ]
    }
}

# --- get_option_chains(underlying_symbol="NVDA") ----------------------------
OPTION_CHAINS: dict[str, Any] = {
    "data": {
        "chains": [
            {
                "id": "8d629e37-6050-47e4-906e-1c0c4de93f71",
                "symbol": "NVDA",
                "expiration_dates": ["2026-08-14", "2026-09-18", "2026-12-18"],
                "trade_value_multiplier": "100.0000",
                "underlying_type": "equity",
            }
        ]
    }
}

# --- get_option_instruments(chain_symbol="NVDA", expiration_dates=...) ------
def _instrument(uuid: str, strike: str, right: str = "call") -> dict[str, Any]:
    return {
        "id": uuid,
        "chain_id": "8d629e37-6050-47e4-906e-1c0c4de93f71",
        "chain_symbol": "NVDA",
        "underlying_type": "equity",
        "expiration_date": "2026-09-18",
        "sellout_datetime": "2026-09-18T19:30:00+00:00",
        "strike_price": strike,
        "type": right,
        "state": "active",
        "tradability": "tradable",
        "trade_value_multiplier": "100.0000",
        "min_ticks": {"above_tick": "0.05", "below_tick": "0.01", "cutoff_price": "3.00"},
    }


OPTION_INSTRUMENTS: dict[str, Any] = {
    "data": {
        "instruments": [
            _instrument("2503d86d-2bb8-40e8-be73-fdeb4af43119", "220.0000"),
            _instrument("534669b1-9b50-44b5-86df-5ee12e9f5a45", "225.0000"),
            _instrument("5313c930-9e6e-4c6b-8ad0-523fbb39df10", "230.0000"),
            # An expired contract, to prove the state filter works.
            {
                **_instrument("00000000-0000-0000-0000-000000000001", "235.0000"),
                "state": "expired",
            },
        ]
    }
}

# --- get_option_quotes(instrument_ids=[...]) --------------------------------
# Note: greeks here are PER SHARE, as Robinhood reports them.
OPTION_QUOTES: dict[str, Any] = {
    "data": {
        "results": [
            {
                "quote": {
                    "instrument_id": "2503d86d-2bb8-40e8-be73-fdeb4af43119",
                    "ask_price": "14.350000",
                    "ask_size": 38,
                    "bid_price": "14.200000",
                    "bid_size": 15,
                    "break_even_price": "234.280000",
                    "adjusted_mark_price": "14.280000",
                    "mark_price": "14.275000",
                    "previous_close_price": "12.150000",
                    "implied_volatility": "0.391528",
                    "delta": "0.591626",
                    "gamma": "0.013101",
                    "rho": "0.135230",
                    "theta": "-0.150180",
                    "vega": "0.294143",
                    "open_interest": 39578,
                    "volume": 4368,
                    "updated_at": "2026-08-07T19:59:59.994391059Z",
                },
                "close": {"instrument_id": "2503d86d-2bb8-40e8-be73-fdeb4af43119", "price": "12.15"},
            },
            {
                "quote": {
                    "instrument_id": "534669b1-9b50-44b5-86df-5ee12e9f5a45",
                    "ask_price": "11.700000",
                    "bid_price": "11.550000",
                    "adjusted_mark_price": "11.630000",
                    "mark_price": "11.625000",
                    "implied_volatility": "0.387058",
                    "delta": "0.524417",
                    "gamma": "0.013586",
                    "rho": "0.121046",
                    "theta": "-0.150762",
                    "vega": "0.301582",
                    "open_interest": 54675,
                    "volume": 8151,
                    "updated_at": "2026-08-07T19:59:59.993389164Z",
                }
            },
            {
                "quote": {
                    "instrument_id": "5313c930-9e6e-4c6b-8ad0-523fbb39df10",
                    "ask_price": "9.400000",
                    "bid_price": "9.300000",
                    "adjusted_mark_price": "9.350000",
                    "mark_price": "9.350000",
                    "implied_volatility": "0.383928",
                    "delta": "0.456775",
                    "gamma": "0.013644",
                    "rho": "0.106325",
                    "theta": "-0.147737",
                    "vega": "0.300373",
                    "open_interest": 30278,
                    "volume": 5096,
                    "updated_at": "2026-08-07T19:59:59.995133446Z",
                }
            },
        ]
    }
}

# --- get_accounts() ---------------------------------------------------------
# Shape-accurate; identifiers are synthetic.
ACCOUNTS: dict[str, Any] = {
    "data": {
        "accounts": [
            {
                "account_number": "000000001",
                "type": "margin",
                "brokerage_account_type": "individual",
                "is_default": True,
                "option_level": "option_level_3",
                "state": "active",
                "deactivated": False,
            },
            {
                "account_number": "000000002",
                "type": "cash",
                "brokerage_account_type": "ira_roth",
                "is_default": False,
                "option_level": "option_level_2",
                "state": "active",
                "deactivated": False,
            },
        ]
    }
}


def fake_tool_caller(name: str, params: dict[str, Any]) -> Any:
    """Stand-in for the injected MCP caller, backed by the recorded payloads."""
    return {
        "get_equity_quotes": EQUITY_QUOTES,
        "get_option_chains": OPTION_CHAINS,
        "get_option_instruments": OPTION_INSTRUMENTS,
        "get_option_quotes": OPTION_QUOTES,
        "get_accounts": ACCOUNTS,
        "get_option_positions": {"data": {"results": []}},
    }[name]


__all__ = [
    "ACCOUNTS",
    "EQUITY_QUOTES",
    "OPTION_CHAINS",
    "OPTION_INSTRUMENTS",
    "OPTION_QUOTES",
    "fake_tool_caller",
]
