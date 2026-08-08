"""Shared fixtures and builders.

The builders construct scoring contexts directly rather than running the whole
pipeline, so a component test fails for exactly one reason.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.config.methodology import Methodology, load_methodology
from app.models.common import Provenance, utcnow
from app.models.enums import (
    Bias,
    CatalystType,
    DataProvider,
    Direction,
    EventImportance,
    EvidenceQuality,
    MarketRegime,
    OptionRight,
    PreliminaryQuality,
    StrategyType,
    TimeHorizon,
    ValidationVerdict,
    WorkflowStage,
)
from app.models.market_brief import CompanyCatalyst, IndexContext, MarketBrief, SectorObservation
from app.models.market_data import FlowSnapshot, OptionContract, Quote, TechnicalSnapshot
from app.models.trade_candidate import (
    CatalystRef,
    ExpectedMove,
    TechnicalContext,
    TradeCandidate,
)
from app.models.trade_structure import Leg, ProposedTrade
from app.models.validation import (
    CatalystValidation,
    MarketAlignment,
    UnderlyingPriceCheck,
    ValidationReport,
)
from app.scoring.context import ScoringContext

TODAY = date(2024, 6, 3)
RUN_ID = "run_test"


class _Unset:
    """Sentinel so `make_context(technicals=None)` means "absent", not "default"."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


UNSET = _Unset()


@pytest.fixture(scope="session")
def methodology() -> Methodology:
    return load_methodology("config/methodology.yaml")


def provenance(provider: DataProvider = DataProvider.FMP) -> Provenance:
    return Provenance(provider=provider, endpoint="test", as_of=utcnow())


def make_brief(
    *,
    spy: Bias = Bias.BULLISH,
    qqq: Bias = Bias.BULLISH,
    sector: str = "Semiconductors",
    sector_bias: Bias = Bias.BULLISH,
    catalysts: list[CompanyCatalyst] | None = None,
) -> MarketBrief:
    return MarketBrief(
        run_id=RUN_ID,
        as_of_trading_day=TODAY,
        market_regime=MarketRegime.TRENDING_UP,
        spy=IndexContext(symbol="SPY", bias=spy, last_price=530.0),
        qqq=IndexContext(symbol="QQQ", bias=qqq, last_price=460.0),
        sector_observations=[
            SectorObservation(
                sector=sector,
                bias=sector_bias,
                rationale="test",
                importance=EventImportance.MEDIUM,
            )
        ],
        company_catalysts=catalysts if catalysts is not None else [make_catalyst()],
    )


def make_catalyst(
    *,
    ticker: str = "NVDA",
    catalyst_type: CatalystType = CatalystType.MAJOR_CONTRACT,
    importance: float = 0.8,
    quality: EvidenceQuality = EvidenceQuality.CONFIRMED_FACT,
    priced_in: bool = False,
    scheduled: date | None = None,
) -> CompanyCatalyst:
    return CompanyCatalyst(
        ticker=ticker,
        catalyst_type=catalyst_type,
        headline="Large supply agreement announced",
        description="Test catalyst",
        source="Reuters",
        source_url="https://example.invalid/x",
        published_at=utcnow() - timedelta(days=1),
        expected_direction=Bias.BULLISH,
        importance_score=importance,
        expected_time_horizon=TimeHorizon.WEEKS_2_4,
        scheduled_event_date=scheduled,
        evidence_quality=quality,
        already_priced_in=priced_in,
    )


def make_candidate(
    *,
    ticker: str = "NVDA",
    direction: Direction = Direction.BULLISH,
    strategy: StrategyType = StrategyType.LONG_CALL,
    sector: str = "Semiconductors",
    # 13.5% over 21 sessions on a 40-IV name is about 1.4 standard deviations:
    # demanding enough to be a real thesis, achievable enough that the default
    # fixture represents a trade that should survive the hard rules.
    expected_move: float = 13.5,
    horizon: TimeHorizon = TimeHorizon.WEEKS_2_4,
    catalyst_type: CatalystType = CatalystType.MAJOR_CONTRACT,
    earnings: date | None = None,
    catalyst_date: date | None = None,
) -> TradeCandidate:
    return TradeCandidate(
        run_id=RUN_ID,
        ticker=ticker,
        sector=sector,
        direction=direction,
        strategy_type=strategy,
        thesis="A sufficiently long thesis string for validation purposes.",
        primary_catalyst=CatalystRef(
            ticker=ticker, catalyst_type=catalyst_type, headline="Large supply agreement announced"
        ),
        expected_holding_period=horizon,
        expected_move=ExpectedMove(percent=expected_move, rationale="test"),
        underlying_reference_price=120.0,
        technical_context=TechnicalContext(trend_description="uptrend"),
        invalidation_thesis="A close below 110 invalidates this.",
        earnings_date=earnings,
        catalyst_date=catalyst_date,
        preliminary_quality=PreliminaryQuality.PLAUSIBLE,
    )


def make_technicals(
    *,
    price: float = 120.0,
    sma20: float | None = 115.0,
    sma50: float | None = 110.0,
    rsi: float | None = 58.0,
    macd: float | None = 1.2,
    macd_signal: float | None = 0.7,
    atr_pct: float | None = 2.0,
    support: float | None = 116.0,
    resistance: float | None = 132.0,
    rvol: float | None = 1.6,
    rs: float | None = 3.0,
) -> TechnicalSnapshot:
    return TechnicalSnapshot(
        symbol="NVDA",
        price=price,
        sma20=sma20,
        sma50=sma50,
        rsi14=rsi,
        macd=macd,
        macd_signal=macd_signal,
        atr14=(atr_pct / 100 * price) if atr_pct else None,
        atr_pct=atr_pct,
        support=support,
        resistance=resistance,
        relative_volume=rvol,
        relative_strength_20d_vs_spy=rs,
        provenance=provenance(DataProvider.INTERNAL),
    )


def make_flow(
    *,
    bullish: float = 8_000_000,
    bearish: float = 2_000_000,
    ask: float = 6_500_000,
    bid: float = 3_500_000,
    sweeps: int = 8,
    volume: int = 300_000,
    oi: int = 200_000,
    net_delta: float = 120_000,
    iv_rank: float = 35.0,
    multileg: float = 0.2,
) -> FlowSnapshot:
    return FlowSnapshot(
        underlying="NVDA",
        bullish_premium=bullish,
        bearish_premium=bearish,
        ask_side_premium=ask,
        bid_side_premium=bid,
        sweep_count=sweeps,
        total_volume=volume,
        total_open_interest=oi,
        net_delta_flow=net_delta,
        iv_rank=iv_rank,
        multileg_share=multileg,
        provenance=provenance(DataProvider.UNUSUAL_WHALES),
    )


def make_contract(
    *,
    strike: float = 120.0,
    right: OptionRight = OptionRight.CALL,
    dte: int = 45,
    bid: float = 7.9,
    ask: float = 8.1,
    volume: int = 1500,
    oi: int = 5000,
    iv: float = 0.40,
    iv_rank: float = 35.0,
    delta: float = 0.50,
    theta: float = -8.0,
    vega: float = 15.0,
) -> OptionContract:
    return OptionContract(
        symbol=f"NVDA{strike:.0f}{right.value[0]}",
        underlying="NVDA",
        right=right,
        strike=strike,
        expiration=TODAY + timedelta(days=dte),
        bid=bid,
        ask=ask,
        volume=volume,
        open_interest=oi,
        implied_volatility=iv,
        iv_rank=iv_rank,
        delta=delta,
        gamma=0.01,
        theta=theta,
        vega=vega,
        provenance=provenance(DataProvider.ROBINHOOD),
    )


def make_trade(
    candidate: TradeCandidate,
    *,
    legs: list[Leg] | None = None,
    underlying: float = 120.0,
) -> ProposedTrade:
    legs = legs or [Leg(action="BUY", quantity=1, contract=make_contract())]
    buy = next(x for x in legs if x.action == "BUY")
    sell = next((x for x in legs if x.action == "SELL"), None)
    debit = (buy.contract.ask or 0) - (sell.contract.bid or 0 if sell else 0)
    mid = (buy.contract.mid or 0) - (sell.contract.mid or 0 if sell else 0)
    return ProposedTrade(
        candidate_id=candidate.candidate_id,
        ticker=candidate.ticker,
        strategy_type=candidate.strategy_type,
        underlying_price=underlying,
        expiration=buy.contract.expiration,
        legs=legs,
        net_debit_conservative=round(debit, 4),
        net_debit_mid=round(mid, 4),
    )


def make_validation(
    candidate: TradeCandidate,
    *,
    catalyst_verdict: ValidationVerdict = ValidationVerdict.CONFIRMED,
    days_since: int | None = 1,
    days_until: int | None = 10,
    priced_in: bool = False,
    prices: dict[str, float] | None = None,
    reconciled: bool = True,
) -> ValidationReport:
    prices = prices if prices is not None else {"fmp": 120.0, "robinhood": 120.05}
    values = list(prices.values())
    consensus = sum(values) / len(values) if values else None
    disagreement = (max(values) - min(values)) / consensus if consensus else None
    return ValidationReport(
        run_id=RUN_ID,
        candidate_id=candidate.candidate_id,
        ticker=candidate.ticker,
        catalyst=CatalystValidation(
            exists=True,
            is_recent=True,
            is_material=True,
            already_priced_in=priced_in,
            has_upcoming_timing_relevance=days_until is not None,
            days_since_published=days_since,
            days_until_catalyst=days_until,
            verdict=catalyst_verdict,
        ),
        alignment=MarketAlignment(spy_aligned=True, qqq_aligned=True, sector_aligned=True),
        price_check=UnderlyingPriceCheck(
            prices_by_provider=prices,
            consensus_price=consensus,
            max_disagreement_pct=disagreement,
            reconciled=reconciled,
        ),
        providers_queried=["fmp", "robinhood", "unusual_whales"],
    )


def make_context(
    methodology: Methodology,
    *,
    candidate: TradeCandidate | None = None,
    brief: MarketBrief | None = None,
    validation: ValidationReport | None = None,
    trade: ProposedTrade | None | _Unset = UNSET,
    technicals: TechnicalSnapshot | None | _Unset = UNSET,
    flow: FlowSnapshot | None | _Unset = UNSET,
    stage: WorkflowStage = WorkflowStage.MARKET_OPEN,
    **kwargs,
) -> ScoringContext:
    candidate = candidate or make_candidate()
    brief = brief or make_brief(catalysts=[make_catalyst(ticker=candidate.ticker)])
    validation = validation or make_validation(candidate)
    trade = make_trade(candidate) if isinstance(trade, _Unset) else trade
    technicals = make_technicals() if isinstance(technicals, _Unset) else technicals
    flow = make_flow() if isinstance(flow, _Unset) else flow

    risk_reward = None
    if trade is not None:
        from app.services.risk import compute_risk_reward

        risk_reward = compute_risk_reward(
            trade,
            expected_move_pct=candidate.expected_move.percent,
            direction_sign=candidate.direction.sign,
            holding_days=candidate.expected_holding_period.approx_days,
            reference_day=TODAY,
        )

    return ScoringContext(
        methodology=methodology,
        candidate=candidate,
        validation=validation,
        brief=brief,
        trading_day=TODAY,
        stage=stage,
        trade=trade,
        risk_reward=kwargs.pop("risk_reward", risk_reward),
        quote=kwargs.pop(
            "quote",
            Quote(symbol=candidate.ticker, price=120.0, provenance=provenance()),
        ),
        technicals=technicals,
        flow=flow,
        providers_expected=kwargs.pop("providers_expected", ["fmp", "robinhood", "unusual_whales"]),
        providers_responded=kwargs.pop(
            "providers_responded", ["fmp", "robinhood", "unusual_whales"]
        ),
        **kwargs,
    )
