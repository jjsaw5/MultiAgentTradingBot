"""Unusual Whales client, pinned against recorded live responses.

The two derived values -- directional premium and IV rank -- get the most
attention here, because they are inferences rather than reported fields and a
silent change in either would move trade scores without anything failing.
"""

from __future__ import annotations

import pytest

from app.providers.unusual_whales.rest import UnusualWhalesRestProvider
from tests.fixtures.unusual_whales import payload_for


@pytest.fixture()
def provider(monkeypatch) -> UnusualWhalesRestProvider:
    p = UnusualWhalesRestProvider(api_key="test-token")

    def fake_get(path, params=None):
        from app.providers.unusual_whales.rest import _rows

        return _rows(payload_for(path))

    monkeypatch.setattr(p, "_get", fake_get)
    monkeypatch.setattr(p, "_try", fake_get)
    return p


@pytest.fixture()
def snapshot(provider):
    return provider.get_flow_snapshot("NVDA")


# ------------------------------------------------------------ aggregation
def test_premium_is_aggregated_across_expiries(snapshot):
    # 435,557,906 + 100,000,000 across the two current-session expiries.
    assert snapshot.call_premium == pytest.approx(535_557_906.0)
    assert snapshot.put_premium == pytest.approx(52_386_470.0)


def test_prior_session_rows_are_excluded(snapshot):
    """The feed carries older dates; only the latest session counts."""
    assert snapshot.call_premium < 900_000_000  # the stale row is ~1bn


def test_volume_totals_calls_and_puts(snapshot):
    assert snapshot.total_volume == 1_605_787 + 648_746 + 400_000 + 100_000


# ------------------------------------------------- derived: directionality
def test_bullish_and_bearish_premium_are_derived_from_side_of_market(snapshot):
    """Unusual Whales reports premium by side, not by directional intent.

    Bullish = calls bought on the ask + puts sold on the bid.
    Bearish = puts bought on the ask + calls sold on the bid.
    """
    assert snapshot.bullish_premium == pytest.approx(220_934_041 + 60_000_000 + 14_007_719 + 9_000_000)
    assert snapshot.bearish_premium == pytest.approx(14_989_553 + 8_000_000 + 134_019_105 + 30_000_000)


def test_directional_share_diverges_from_the_naive_call_put_ratio(snapshot):
    """The whole reason side-of-market attribution matters.

    Calls outweigh puts ten to one in raw premium, which a naive read would
    call overwhelmingly bullish. Once side of market is accounted for, the
    directional split is close to even.
    """
    naive_call_share = snapshot.call_premium / (snapshot.call_premium + snapshot.put_premium)
    assert naive_call_share > 0.9
    assert 0.4 < snapshot.directional_premium_share < 0.7


def test_ask_and_bid_side_premium_sum_both_rights(snapshot):
    assert snapshot.ask_side_premium == pytest.approx(220_934_041 + 60_000_000 + 14_989_553 + 8_000_000)
    assert snapshot.bid_side_premium == pytest.approx(134_019_105 + 30_000_000 + 14_007_719 + 9_000_000)


# --------------------------------------------------------- derived: IV rank
def test_iv_rank_is_computed_from_the_trailing_range(snapshot):
    """Current 0.50 within a 0.20-0.60 range sits at 75%."""
    assert snapshot.iv30 == pytest.approx(0.50)
    assert snapshot.iv_rank == pytest.approx(75.0)


def test_iv_rank_is_unscored_when_the_range_is_flat(provider, monkeypatch):
    """A percentile within a zero-width range is meaningless, not zero."""
    flat = {"data": [{"date": f"2026-08-0{i}", "implied_volatility": "0.30"} for i in range(1, 5)]}
    monkeypatch.setattr(provider, "_try", lambda path, params=None: flat["data"])
    assert provider._volatility("NVDA")["iv_rank"] is None


def test_expected_move_is_derived_from_implied_volatility(snapshot):
    from app.providers.unusual_whales.rest import EXPECTED_MOVE_DAYS

    expected = 0.50 * (EXPECTED_MOVE_DAYS / 365.0) ** 0.5 * 100
    assert snapshot.expected_move_pct == pytest.approx(expected, abs=0.01)


# ------------------------------------------------------------------ alerts
def test_sweeps_and_blocks_are_counted(snapshot):
    assert snapshot.sweep_count == 3
    assert snapshot.block_count == 1
    assert snapshot.large_trade_count == 4


def test_multileg_share_is_absent_when_the_feed_reports_none(snapshot):
    """The alerts feed is single-leg only and ignores `is_multileg`.

    Reporting 0.0 would assert "this tape has no multi-leg flow" from a source
    that cannot report any. It must stay unmeasured.
    """
    assert snapshot.multileg_share is None


def test_absent_multileg_share_does_not_suppress_sweep_credit(methodology, snapshot):
    """An unmeasurable guard must not silently behave like a measured zero."""
    from app.scoring.components import flow as flow_component
    from tests.conftest import make_context

    component = flow_component.score(make_context(methodology, flow=snapshot))
    sweeps = next(r for r in component.reasons if r.rule == "sweeps")
    assert sweeps.points > 0
    assert not any(r.rule == "multileg_caveat" for r in component.reasons)


# ------------------------------------------------------------- enrichment
def test_greek_flow_uses_the_most_recent_tick(snapshot):
    assert snapshot.net_delta_flow == pytest.approx(38390.73, abs=0.01)
    assert snapshot.net_vega_flow == pytest.approx(119.27, abs=0.01)


def test_open_interest_sums_the_chain(snapshot):
    assert snapshot.total_open_interest == 50_000
    assert snapshot.volume_oi_ratio == pytest.approx(snapshot.total_volume / 50_000, abs=1e-3)


def test_gamma_exposure_uses_the_latest_snapshot(snapshot):
    assert snapshot.gamma_exposure == pytest.approx(2_381_738_729.0)


def test_dark_pool_notional_and_bias(snapshot):
    assert snapshot.dark_pool_notional == pytest.approx(397_924.52)
    # Two prints above the midpoint, one below.
    assert snapshot.dark_pool_bias == "BULLISH"


def test_unreported_fields_stay_none(snapshot):
    """Never invented: the API supplies neither of these."""
    assert snapshot.mid_side_premium is None
    assert snapshot.net_gamma_flow is None


def test_market_flow_summary(provider):
    summary = provider.get_market_flow_summary()
    assert summary["net_call_premium"] == pytest.approx(-168_484_829.0)
    assert summary["ticks"] == 1


# ---------------------------------------------------------------- failures
def test_optional_endpoints_degrade_without_blocking(provider, monkeypatch):
    """Losing enrichment must not lose the snapshot.

    The transport is failed rather than `_try` itself, so the real
    swallow-and-continue path is what gets exercised.
    """
    from app.providers.base import ProviderError
    from app.providers.unusual_whales.rest import UnusualWhalesRestProvider, _rows

    required = ("flow-per-expiry", "flow-alerts")

    def transport(path, params=None):
        if any(n in path for n in required):
            return _rows(payload_for(path))
        raise ProviderError("unusual_whales", "boom")

    monkeypatch.setattr(provider, "_get", transport)
    monkeypatch.setattr(
        provider, "_try", UnusualWhalesRestProvider._try.__get__(provider)
    )
    snap = provider.get_flow_snapshot("NVDA")
    assert snap.call_premium is not None  # required endpoints still worked
    assert snap.iv_rank is None
    assert snap.gamma_exposure is None
    assert snap.dark_pool_notional is None


def test_a_missing_token_fails_loudly():
    from app.providers.base import ProviderUnavailable

    with pytest.raises(ProviderUnavailable, match="UNUSUAL_WHALES_API_KEY"):
        UnusualWhalesRestProvider(api_key=None)


def test_the_provider_exposes_no_execution_surface(provider):
    from app.providers.base import assert_no_execution_surface

    assert_no_execution_surface(provider)
