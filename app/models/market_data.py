"""Measured market data.

These models hold *facts retrieved from providers*, never agent opinion. Each
carries the provider and timestamps so staleness is auditable.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field, computed_field

from app.models.common import Base, Provenance, utcnow
from app.models.enums import OptionRight


class Quote(Base):
    """Underlying equity/ETF quote."""

    symbol: str
    price: float
    previous_close: float | None = None
    open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    volume: int | None = None
    average_volume: int | None = None
    change_pct: float | None = None
    provenance: Provenance
    stale: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def relative_volume(self) -> float | None:
        if self.volume is None or not self.average_volume:
            return None
        return round(self.volume / self.average_volume, 3)


class PriceBar(Base):
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class PriceHistory(Base):
    symbol: str
    bars: list[PriceBar]
    provenance: Provenance

    def closes(self) -> list[float]:
        return [b.close for b in self.bars]


class TechnicalSnapshot(Base):
    """Deterministically computed indicators. No agent input at all.

    Indicators are additive by design: add a field here plus its computation in
    :mod:`app.services.technicals` and the scoring rules can consult it.
    """

    symbol: str
    as_of: datetime = Field(default_factory=utcnow)
    price: float

    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    ema9: float | None = None
    vwap_proxy: float | None = None

    rsi14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    atr14: float | None = None
    atr_pct: float | None = None

    support: float | None = None
    resistance: float | None = None
    range_high_20d: float | None = None
    range_low_20d: float | None = None

    relative_volume: float | None = None
    gap_pct: float | None = None
    higher_highs: bool | None = None
    lower_lows: bool | None = None

    return_5d_pct: float | None = None
    return_20d_pct: float | None = None
    relative_strength_20d_vs_spy: float | None = None

    provenance: Provenance

    def distance_pct(self, level: float | None) -> float | None:
        if level is None or not self.price:
            return None
        return round((level - self.price) / self.price * 100.0, 3)


class OptionContract(Base):
    """A single option contract with market data attached."""

    symbol: str
    underlying: str
    right: OptionRight
    strike: float
    expiration: date

    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    mark: float | None = None
    volume: int | None = None
    open_interest: int | None = None

    implied_volatility: float | None = Field(default=None, description="Decimal, e.g. 0.42")
    iv_rank: float | None = Field(default=None, ge=0, le=100)
    iv_percentile: float | None = Field(default=None, ge=0, le=100)

    delta: float | None = None
    gamma: float | None = None
    theta: float | None = Field(default=None, description="Per contract, per day, in dollars.")
    vega: float | None = Field(default=None, description="Per contract, per 1 IV point.")
    rho: float | None = None

    provenance: Provenance
    stale: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return self.mark if self.mark is not None else self.last
        return round((self.bid + self.ask) / 2.0, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread_abs(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return round(self.ask - self.bid, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread_pct(self) -> float | None:
        m = self.mid
        s = self.spread_abs
        if m is None or s is None or m <= 0:
            return None
        return round(s / m, 4)

    def dte(self, reference: date | None = None) -> int:
        return (self.expiration - (reference or utcnow().date())).days


class OptionChain(Base):
    underlying: str
    underlying_price: float | None = None
    contracts: list[OptionContract] = Field(default_factory=list)
    provenance: Provenance
    stale: bool = False

    def expirations(self) -> list[date]:
        return sorted({c.expiration for c in self.contracts})

    def by_expiration(self, expiration: date, right: OptionRight) -> list[OptionContract]:
        return sorted(
            (c for c in self.contracts if c.expiration == expiration and c.right == right),
            key=lambda c: c.strike,
        )


class FlowSnapshot(Base):
    """Aggregated options-flow intelligence for one underlying.

    Interpretation caveat baked into the schema: raw premium totals are kept
    separate from side-of-market attribution, because a large print is not
    directional information on its own.
    """

    underlying: str
    window: str = Field(default="1d", description="Aggregation window, e.g. '1d', '5d'.")
    as_of: datetime = Field(default_factory=utcnow)

    call_premium: float | None = None
    put_premium: float | None = None
    bullish_premium: float | None = Field(
        default=None, description="Premium attributed to bullish positioning by the provider."
    )
    bearish_premium: float | None = None

    ask_side_premium: float | None = None
    bid_side_premium: float | None = None
    mid_side_premium: float | None = None

    sweep_count: int | None = None
    block_count: int | None = None
    large_trade_count: int | None = None
    multileg_share: float | None = Field(
        default=None, ge=0, le=1, description="Share of flow that is multi-leg -- "
        "high values make single-leg directional inference unreliable."
    )

    total_volume: int | None = None
    total_open_interest: int | None = None
    net_delta_flow: float | None = None
    net_gamma_flow: float | None = None
    net_vega_flow: float | None = None
    gamma_exposure: float | None = None

    dark_pool_notional: float | None = None
    dark_pool_bias: str | None = None

    iv_rank: float | None = Field(default=None, ge=0, le=100)
    iv30: float | None = None
    expected_move_pct: float | None = None

    provenance: Provenance
    stale: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def directional_premium_share(self) -> float | None:
        """Bullish share of directionally-attributed premium, 0..1."""
        if self.bullish_premium is None or self.bearish_premium is None:
            return None
        total = self.bullish_premium + self.bearish_premium
        if total <= 0:
            return None
        return round(self.bullish_premium / total, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ask_side_share(self) -> float | None:
        if self.ask_side_premium is None or self.bid_side_premium is None:
            return None
        total = self.ask_side_premium + self.bid_side_premium
        if total <= 0:
            return None
        return round(self.ask_side_premium / total, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def volume_oi_ratio(self) -> float | None:
        if not self.total_open_interest or self.total_volume is None:
            return None
        return round(self.total_volume / self.total_open_interest, 4)


class EarningsEvent(Base):
    ticker: str
    event_date: date
    time_of_day: str | None = Field(default=None, description="BMO / AMC / UNKNOWN")
    eps_estimate: float | None = None
    revenue_estimate: float | None = None
    confirmed: bool = False
    provenance: Provenance


__all__ = [
    "EarningsEvent",
    "FlowSnapshot",
    "OptionChain",
    "OptionContract",
    "PriceBar",
    "PriceHistory",
    "Quote",
    "TechnicalSnapshot",
]
