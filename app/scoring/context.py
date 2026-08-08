"""Everything a scoring rule is allowed to look at.

Rules receive this object and nothing else. They may not call providers, may
not call an LLM, and may not reach for globals -- which is what makes a score
reproducible from stored data alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.config.methodology import Methodology
from app.models.enums import WorkflowStage
from app.models.market_brief import MarketBrief
from app.models.market_data import EarningsEvent, FlowSnapshot, Quote, TechnicalSnapshot
from app.models.trade_candidate import TradeCandidate
from app.models.trade_structure import ProposedTrade, RiskReward
from app.models.validation import ValidationReport


@dataclass
class ScoringContext:
    methodology: Methodology
    candidate: TradeCandidate
    validation: ValidationReport
    brief: MarketBrief
    trading_day: date
    stage: WorkflowStage

    trade: ProposedTrade | None = None
    risk_reward: RiskReward | None = None
    quote: Quote | None = None
    technicals: TechnicalSnapshot | None = None
    flow: FlowSnapshot | None = None
    earnings: EarningsEvent | None = None

    providers_expected: list[str] = field(default_factory=list)
    providers_responded: list[str] = field(default_factory=list)
    stale_inputs: list[str] = field(default_factory=list)

    @property
    def holding_days(self) -> int:
        return self.candidate.expected_holding_period.approx_days

    @property
    def direction_sign(self) -> int:
        return self.candidate.direction.sign


__all__ = ["ScoringContext"]
