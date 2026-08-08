"""Typed methodology configuration loaded from ``config/methodology.yaml``.

Every threshold that shapes a recommendation lives in the YAML file and is
surfaced here as a validated Pydantic model. Application code must read
values from this object rather than embedding literals.

The full config is snapshotted onto each run so that a recommendation made
months ago can be re-derived exactly, even after the methodology changes.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PipelineConfig(_Frozen):
    max_candidates_per_run: int = 10
    allow_empty_candidate_set: bool = True
    require_primary_catalyst: bool = True


class StrategyConfig(_Frozen):
    allowed: list[str]


class ClassificationBand(_Frozen):
    name: str
    min: float
    label: str


class ScoreWeights(_Frozen):
    catalyst_strength: float
    market_alignment: float
    technical_setup: float
    options_flow: float
    iv_greeks: float
    contract_liquidity: float
    risk_reward: float
    data_quality: float

    @model_validator(mode="after")
    def _must_total_100(self) -> ScoreWeights:
        total = sum(self.model_dump().values())
        if abs(total - 100.0) > 1e-6:
            raise ValueError(f"score_weights must sum to 100, got {total}")
        return self

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class IVRankBand(_Frozen):
    max: float
    points: float


class RRBand(_Frozen):
    min: float
    points: float


class CatalystStrengthRules(_Frozen):
    importance_points: float
    evidence_quality_points: dict[str, float]
    recency_days_full: int
    recency_days_partial: int
    recency_points_full: float
    recency_points_partial: float
    timing_points: float
    priced_in_penalty: float
    supporting_catalyst_points_each: float
    supporting_catalyst_points_cap: float


class MarketAlignmentRules(_Frozen):
    spy_aligned_points: float
    spy_neutral_points: float
    spy_fighting_points: float
    qqq_aligned_points: float
    qqq_neutral_points: float
    qqq_fighting_points: float
    sector_aligned_points: float
    sector_fighting_penalty: float
    relative_strength_threshold: float
    relative_strength_points: float


class TechnicalSetupRules(_Frozen):
    trend_alignment_points: float
    trend_partial_points: float
    key_level_points: float
    relative_volume_strong: float
    relative_volume_ok: float
    relative_volume_weak: float
    relative_volume_points_strong: float
    relative_volume_points_ok: float
    relative_volume_penalty_weak: float
    momentum_points: float
    rsi_bull_range: list[float]
    rsi_bear_range: list[float]
    rsi_overextended_bull: float
    rsi_overextended_bear: float
    overextension_penalty: float
    blocking_level_pct_severe: float
    blocking_level_pct_moderate: float
    blocking_level_penalty_severe: float
    blocking_level_penalty_moderate: float
    atr_feasibility_points: float
    atr_feasibility_max_multiple: float


class OptionsFlowRules(_Frozen):
    directional_premium_points: float
    directional_premium_floor: float
    ask_side_points: float
    ask_side_floor: float
    sweep_points: float
    sweep_min_count: int
    new_position_points: float
    volume_oi_ratio_new_position: float
    delta_flow_points: float
    contradiction_threshold: float
    contradiction_penalty: float
    insufficient_data_points: float


class IVGreeksRules(_Frozen):
    iv_rank_bands_long_premium: list[IVRankBand]
    iv_rank_bands_spread: list[IVRankBand]
    iv_expected_move_points: float
    iv_expected_move_tolerance: float
    theta_burden_points: float
    theta_burden_max_pct_of_premium: float
    vega_points: float
    vega_max_pct_of_premium_per_iv_pt: float


class LiquidityRules(_Frozen):
    spread_pct_excellent: float
    spread_pct_good: float
    spread_pct_acceptable: float
    spread_points_excellent: float
    spread_points_good: float
    spread_points_acceptable: float
    open_interest_strong: float
    open_interest_ok: float
    open_interest_points_strong: float
    open_interest_points_ok: float
    volume_strong: float
    volume_ok: float
    volume_points_strong: float
    volume_points_ok: float


class RiskRewardRules(_Frozen):
    rr_bands: list[RRBand]
    max_loss_within_budget_points: float
    breakeven_points: float
    breakeven_vs_expected_move_full: float
    breakeven_vs_expected_move_partial: float
    breakeven_points_partial: float


class DataQualityRules(_Frozen):
    all_providers_responded_points: float
    price_agreement_points: float
    price_agreement_tolerance_pct: float
    freshness_points: float


class ScoringRules(_Frozen):
    catalyst_strength: CatalystStrengthRules
    market_alignment: MarketAlignmentRules
    technical_setup: TechnicalSetupRules
    options_flow: OptionsFlowRules
    iv_greeks: IVGreeksRules
    contract_liquidity: LiquidityRules
    risk_reward: RiskRewardRules
    data_quality: DataQualityRules


class HardRejectionRules(_Frozen):
    max_bid_ask_spread_pct: float
    min_option_volume: int
    min_open_interest: int
    min_reward_to_risk: float
    max_premium_per_trade_usd: float
    max_theta_burn_pct_of_premium: float
    earnings_blackout_days_before: int
    earnings_blackout_days_after: int
    allow_earnings_when_catalyst_is_earnings: bool
    max_provider_price_disagreement_pct: float
    required_fields: list[str]
    require_validated_catalyst: bool


class ContractSelectionRules(_Frozen):
    preferred_dte_min: int
    preferred_dte_max: int
    absolute_dte_min: int
    absolute_dte_max: int
    long_option_delta_min: float
    long_option_delta_max: float
    long_option_delta_target: float
    spread_long_delta_target: float
    spread_short_delta_target: float
    spread_min_width: float
    spread_max_width: float
    spread_max_debit_pct_of_width: float
    min_days_past_catalyst: int
    min_days_beyond_holding_period: int
    max_candidate_contracts_per_trade: int


class EventRiskRules(_Frozen):
    high_impact_macro_events: list[str]
    macro_event_flag_days: int


class MarketScheduleConfig(_Frozen):
    timezone: str
    premarket_start: str
    regular_open: str
    regular_close: str
    options_quote_valid_from: str
    postmarket_end: str
    max_quote_age_seconds_live: int
    max_quote_age_seconds_premarket: int


class ProviderPolicy(_Frozen):
    timeout_seconds: int
    max_retries: int
    critical_for_validation: list[str]
    optional_for_validation: list[str]


class Methodology(_Frozen):
    version: str
    pipeline: PipelineConfig
    strategies: StrategyConfig
    classification_bands: list[ClassificationBand]
    min_presentable_score: float
    score_weights: ScoreWeights
    scoring: ScoringRules
    hard_rejections: HardRejectionRules
    contract_selection: ContractSelectionRules
    event_risk: EventRiskRules
    market_schedule: MarketScheduleConfig
    providers: ProviderPolicy

    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def _bands_descending(self) -> Methodology:
        mins = [b.min for b in self.classification_bands]
        if mins != sorted(mins, reverse=True):
            raise ValueError("classification_bands must be ordered by descending `min`")
        return self

    def classify(self, score: float) -> ClassificationBand:
        for band in self.classification_bands:
            if score >= band.min:
                return band
        return self.classification_bands[-1]

    def fingerprint(self) -> str:
        """Stable hash of the methodology, stored alongside every run."""
        payload = json.dumps(self.raw, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def snapshot(self) -> dict[str, Any]:
        return {"version": self.version, "fingerprint": self.fingerprint(), "config": self.raw}


def load_methodology(path: str | Path) -> Methodology:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Methodology config not found at {p.resolve()}")
    raw = yaml.safe_load(p.read_text()) or {}
    return Methodology(**raw, raw=raw)


@lru_cache(maxsize=4)
def get_methodology(path: str | None = None) -> Methodology:
    from app.config.settings import get_settings

    return load_methodology(path or get_settings().methodology_config_path)


def reset_methodology_cache() -> None:
    get_methodology.cache_clear()


__all__ = ["Methodology", "get_methodology", "load_methodology", "reset_methodology_cache"]
