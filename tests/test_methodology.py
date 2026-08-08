"""Configuration integrity.

The methodology file is the trading rulebook. A malformed rulebook must fail
loudly at load time, not silently produce different scores.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from app.config.methodology import Methodology, load_methodology
from app.models.enums import Classification, StrategyType


@pytest.fixture()
def raw() -> dict:
    with open("config/methodology.yaml") as fh:
        return yaml.safe_load(fh)


def build(raw: dict) -> Methodology:
    return Methodology(**raw, raw=raw)


def test_weights_sum_to_one_hundred(methodology):
    assert sum(methodology.score_weights.as_dict().values()) == pytest.approx(100.0)


def test_weights_that_do_not_sum_to_one_hundred_are_rejected(raw):
    broken = copy.deepcopy(raw)
    broken["score_weights"]["technical_setup"] = 40
    with pytest.raises(ValueError, match="must sum to 100"):
        build(broken)


def test_unknown_configuration_keys_are_rejected(raw):
    broken = copy.deepcopy(raw)
    broken["pipeline"]["max_candidtes_per_run"] = 5  # typo
    with pytest.raises(ValueError):
        build(broken)


def test_classification_bands_must_descend(raw):
    broken = copy.deepcopy(raw)
    broken["classification_bands"].reverse()
    with pytest.raises(ValueError, match="descending"):
        build(broken)


@pytest.mark.parametrize(
    "score,expected",
    [
        (100, Classification.EXCEPTIONAL),
        (90, Classification.EXCEPTIONAL),
        (89.9, Classification.HIGH_CONVICTION),
        (80, Classification.HIGH_CONVICTION),
        (79, Classification.GOOD_CANDIDATE),
        (70, Classification.GOOD_CANDIDATE),
        (65, Classification.WATCHLIST),
        (60, Classification.WATCHLIST),
        (59.9, Classification.REJECTED),
        (0, Classification.REJECTED),
    ],
)
def test_classification_boundaries(methodology, score, expected):
    assert Classification(methodology.classify(score).name) is expected


def test_only_defined_risk_strategies_are_allowed(methodology):
    allowed = set(methodology.strategies.allowed)
    assert allowed == {
        StrategyType.LONG_CALL.value,
        StrategyType.LONG_PUT.value,
        StrategyType.BULL_CALL_SPREAD.value,
        StrategyType.BEAR_PUT_SPREAD.value,
    }
    for name in allowed:
        assert not name.startswith("SHORT_")
        assert "NAKED" not in name


def test_fingerprint_is_stable_and_change_sensitive(raw):
    a = build(copy.deepcopy(raw))
    b = build(copy.deepcopy(raw))
    assert a.fingerprint() == b.fingerprint()

    changed = copy.deepcopy(raw)
    changed["hard_rejections"]["min_reward_to_risk"] = 1.5
    assert build(changed).fingerprint() != a.fingerprint()


def test_snapshot_carries_the_whole_rulebook(methodology):
    snapshot = methodology.snapshot()
    assert snapshot["version"] == methodology.version
    assert snapshot["fingerprint"] == methodology.fingerprint()
    assert "score_weights" in snapshot["config"]
    assert "hard_rejections" in snapshot["config"]


def test_methodology_is_immutable_once_loaded(methodology):
    with pytest.raises(ValueError):
        methodology.min_presentable_score = 10  # type: ignore[misc]


def test_missing_file_fails_loudly():
    with pytest.raises(FileNotFoundError):
        load_methodology("config/does-not-exist.yaml")


def test_contract_selection_windows_are_coherent(methodology):
    cs = methodology.contract_selection
    assert cs.absolute_dte_min <= cs.preferred_dte_min < cs.preferred_dte_max <= cs.absolute_dte_max
    assert cs.long_option_delta_min < cs.long_option_delta_target < cs.long_option_delta_max
    assert cs.spread_short_delta_target < cs.spread_long_delta_target
    assert cs.spread_min_width < cs.spread_max_width


def test_liquidity_thresholds_are_ordered(methodology):
    lq = methodology.scoring.contract_liquidity
    assert lq.spread_pct_excellent < lq.spread_pct_good < lq.spread_pct_acceptable
    assert lq.open_interest_ok < lq.open_interest_strong
    assert lq.volume_ok < lq.volume_strong


def test_hard_rejection_spread_ceiling_matches_the_scoring_floor(methodology):
    """A trade scoring zero for spread should be at or past the hard limit."""
    assert (
        methodology.scoring.contract_liquidity.spread_pct_acceptable
        <= methodology.hard_rejections.max_bid_ask_spread_pct
    )
