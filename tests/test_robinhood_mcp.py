"""Robinhood MCP adapter, pinned against recorded live responses.

Every assertion here corresponds to something the adapter got wrong before it
was checked against the real server. Payload shapes drift; these tests are the
tripwire.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.models.enums import OptionRight
from app.providers.base import ProviderError
from app.providers.robinhood.mcp import (
    DEFAULT_CONTRACT_MULTIPLIER,
    RobinhoodMCPProvider,
    occ_symbol,
)
from tests.fixtures.robinhood_mcp import OPTION_QUOTES, fake_tool_caller


@pytest.fixture()
def provider() -> RobinhoodMCPProvider:
    return RobinhoodMCPProvider(fake_tool_caller)


# ------------------------------------------------------------ payload shape
def test_responses_wrapped_in_data_are_unwrapped(provider):
    """Payloads are {"data": {"<name>": [...]}}; reaching for the top-level
    "results" key yields nothing at all."""
    chain = provider.get_option_chain("NVDA")
    assert chain.contracts, "no contracts parsed -- the data wrapper was not unwrapped"


def test_underlying_price_is_read_from_the_nested_quote(provider):
    """Equity quotes arrive as results[].quote, one level deeper than expected."""
    chain = provider.get_option_chain("NVDA")
    assert chain.underlying_price == pytest.approx(223.90)


def test_expirations_come_from_chain_metadata(provider):
    assert date(2026, 9, 18) in provider.expirations("NVDA")


# ----------------------------------------------------------------- contracts
def test_contract_fields_are_mapped(provider):
    chain = provider.get_option_chain("NVDA")
    c = next(x for x in chain.contracts if x.strike == 220.0)

    assert c.underlying == "NVDA"
    assert c.right is OptionRight.CALL
    assert c.expiration == date(2026, 9, 18)
    assert c.bid == pytest.approx(14.20)
    assert c.ask == pytest.approx(14.35)
    assert c.mark == pytest.approx(14.28)  # adjusted_mark_price preferred
    assert c.volume == 4368
    assert c.open_interest == 39578
    assert c.implied_volatility == pytest.approx(0.391528)
    assert c.delta == pytest.approx(0.591626)


def test_greeks_are_scaled_from_per_share_to_per_contract(provider):
    """Robinhood reports theta as -0.15/share; this system works per contract.

    Unscaled, theta burden and vega exposure would be wrong by 100x, and every
    long-premium trade would look costless to hold.
    """
    chain = provider.get_option_chain("NVDA")
    c = next(x for x in chain.contracts if x.strike == 220.0)

    raw = OPTION_QUOTES["data"]["results"][0]["quote"]
    assert c.theta == pytest.approx(float(raw["theta"]) * DEFAULT_CONTRACT_MULTIPLIER)
    assert c.vega == pytest.approx(float(raw["vega"]) * DEFAULT_CONTRACT_MULTIPLIER)
    assert c.theta == pytest.approx(-15.018)
    # Delta and gamma are dimensionless ratios and must NOT be scaled.
    assert abs(c.delta) < 1.0
    assert abs(c.gamma) < 1.0


def test_derived_spread_metrics_are_sane(provider):
    chain = provider.get_option_chain("NVDA")
    c = next(x for x in chain.contracts if x.strike == 220.0)
    assert c.mid == pytest.approx(14.275)
    assert c.spread_abs == pytest.approx(0.15)
    assert c.spread_pct == pytest.approx(0.0105, abs=1e-3)


def test_contracts_get_a_composed_occ_symbol(provider):
    """Robinhood identifies contracts by UUID and returns no OCC symbol."""
    chain = provider.get_option_chain("NVDA")
    c = next(x for x in chain.contracts if x.strike == 220.0)
    assert c.symbol == "NVDA260918C00220000"


def test_occ_symbol_formats_puts_and_fractional_strikes():
    assert occ_symbol("SPY", date(2026, 1, 16), OptionRight.PUT, 512.5) == "SPY260116P00512500"


def test_inactive_contracts_are_excluded(provider):
    chain = provider.get_option_chain("NVDA")
    assert all(c.strike != 235.0 for c in chain.contracts), "expired contract was not filtered"


def test_expiration_window_is_applied(provider):
    empty = provider.get_option_chain(
        "NVDA", min_expiration=date(2027, 1, 1), max_expiration=date(2027, 6, 1)
    )
    assert empty.contracts == []


def test_quote_timestamp_survives_nanosecond_precision(provider):
    """Robinhood stamps quotes with 9 fractional digits and a Z suffix, which
    `datetime.fromisoformat` rejects outright."""
    chain = provider.get_option_chain("NVDA")
    c = next(x for x in chain.contracts if x.strike == 220.0)
    assert c.provenance.as_of is not None
    assert c.provenance.as_of.year == 2026
    assert c.provenance.as_of.tzinfo is not None


def test_iv_rank_is_absent_rather_than_invented(provider):
    """Robinhood publishes no IV rank; the field must stay None so the scoring
    engine falls back to the flow provider instead of scoring a made-up value."""
    chain = provider.get_option_chain("NVDA")
    assert all(c.iv_rank is None for c in chain.contracts)


# ------------------------------------------------------------------ account
def test_account_summary_extracts_the_option_level_numeral(provider):
    summary = provider.get_account_summary()
    assert summary["options_level"] == 3  # from "option_level_3"
    assert summary["account_type"] == "individual"


def test_account_summary_reports_no_buying_power(provider):
    """`get_accounts` does not report it reliably, so it stays absent rather
    than being read from a field that means something else."""
    assert provider.get_account_summary()["buying_power"] is None


def test_account_summary_copies_no_identifiers(provider):
    summary = provider.get_account_summary()
    assert not any("account_number" in k for k in summary)
    assert "000000001" not in str(summary)


# ------------------------------------------------------------------- safety
def test_order_tools_are_refused_before_the_call_is_made():
    calls: list[str] = []

    def recording_caller(name, params):
        calls.append(name)
        return {}

    provider = RobinhoodMCPProvider(recording_caller)
    for tool in ("place_option_order", "place_equity_order", "cancel_option_order"):
        with pytest.raises(ProviderError, match="allowlist"):
            provider._tool(tool, symbol="NVDA")
    assert calls == [], "a forbidden tool reached the MCP caller"


def test_a_missing_tool_caller_fails_loudly():
    from app.providers.base import ProviderUnavailable

    with pytest.raises(ProviderUnavailable, match="MCP tool caller"):
        RobinhoodMCPProvider(None)


def test_the_provider_exposes_no_execution_surface(provider):
    from app.providers.base import assert_no_execution_surface

    assert_no_execution_surface(provider)
