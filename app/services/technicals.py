"""Deterministic technical indicator computation.

Extension point: add a field to :class:`TechnicalSnapshot`, compute it here,
and consult it from a scoring rule. Nothing else needs to change, and the new
measurement is automatically persisted and shown in the audit trail.
"""

from __future__ import annotations

from app.models.common import Provenance, utcnow
from app.models.enums import DataProvider
from app.models.market_data import PriceBar, PriceHistory, Quote, TechnicalSnapshot


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return round(sum(values[-window:]) / window, 4)


def ema(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    k = 2.0 / (window + 1)
    out = sum(values[:window]) / window
    for v in values[window:]:
        out = v * k + out * (1 - k)
    return round(out, 4)


def rsi(values: list[float], window: int = 14) -> float | None:
    if len(values) < window + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-window, 0):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    if losses == 0:
        return 100.0
    rs = (gains / window) / (losses / window)
    return round(100 - 100 / (1 + rs), 3)


def macd(values: list[float]) -> tuple[float | None, float | None]:
    fast, slow = ema(values, 12), ema(values, 26)
    if fast is None or slow is None:
        return None, None
    line = fast - slow
    # Signal approximated from the trailing MACD series for the last 9 points.
    series: list[float] = []
    for i in range(9):
        end = len(values) - i
        f, s = ema(values[:end], 12), ema(values[:end], 26)
        if f is None or s is None:
            break
        series.append(f - s)
    signal = round(sum(series) / len(series), 4) if series else None
    return round(line, 4), signal


def atr(bars: list[PriceBar], window: int = 14) -> float | None:
    if len(bars) < window + 1:
        return None
    trs: list[float] = []
    for i in range(-window, 0):
        prev_close = bars[i - 1].close
        b = bars[i]
        trs.append(max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close)))
    return round(sum(trs) / window, 4)


def swing_levels(bars: list[PriceBar], lookback: int = 40) -> tuple[float | None, float | None]:
    """Nearest swing support below and resistance above the latest close."""
    if len(bars) < 12:
        return None, None
    window = bars[-lookback:]
    price = window[-1].close
    highs = [b.high for b in window[:-1]]
    lows = [b.low for b in window[:-1]]
    above = [h for h in highs if h > price * 1.001]
    below = [lo for lo in lows if lo < price * 0.999]
    resistance = round(min(above), 4) if above else None
    support = round(max(below), 4) if below else None
    return support, resistance


def higher_highs(bars: list[PriceBar], window: int = 20) -> bool | None:
    if len(bars) < window * 2:
        return None
    recent = max(b.high for b in bars[-window:])
    prior = max(b.high for b in bars[-2 * window : -window])
    return recent > prior


def lower_lows(bars: list[PriceBar], window: int = 20) -> bool | None:
    if len(bars) < window * 2:
        return None
    recent = min(b.low for b in bars[-window:])
    prior = min(b.low for b in bars[-2 * window : -window])
    return recent < prior


def pct_return(closes: list[float], days: int) -> float | None:
    if len(closes) < days + 1:
        return None
    return round((closes[-1] / closes[-1 - days] - 1) * 100, 3)


def compute_snapshot(
    history: PriceHistory,
    quote: Quote | None = None,
    benchmark: PriceHistory | None = None,
) -> TechnicalSnapshot:
    """Build a full indicator snapshot. Missing inputs yield ``None`` fields."""
    bars = history.bars
    closes = [b.close for b in bars]
    price = quote.price if quote else closes[-1]

    macd_line, macd_signal = macd(closes)
    atr14 = atr(bars)
    support, resistance = swing_levels(bars)

    # A daily-bar VWAP proxy: typical price weighted by volume over 20 sessions.
    recent = bars[-20:]
    vol_total = sum(b.volume for b in recent) or 1
    vwap_proxy = round(
        sum(((b.high + b.low + b.close) / 3) * b.volume for b in recent) / vol_total, 4
    )

    gap_pct = None
    if len(bars) >= 2 and bars[-1].open:
        gap_pct = round((bars[-1].open / bars[-2].close - 1) * 100, 3)

    # Some providers omit average volume from the quote. Deriving it from the
    # history we already hold is cheaper and more consistent than a second
    # request, and keeps relative volume available rather than silently absent.
    relative_volume = quote.relative_volume if quote else None
    if relative_volume is None and quote and quote.volume and len(bars) >= 21:
        avg = sum(b.volume for b in bars[-21:-1]) / 20
        relative_volume = round(quote.volume / avg, 3) if avg else None

    rel_strength = None
    if benchmark is not None:
        b_closes = benchmark.closes()
        mine, theirs = pct_return(closes, 20), pct_return(b_closes, 20)
        if mine is not None and theirs is not None:
            rel_strength = round(mine - theirs, 3)

    return TechnicalSnapshot(
        symbol=history.symbol,
        as_of=utcnow(),
        price=price,
        sma20=sma(closes, 20),
        sma50=sma(closes, 50),
        sma200=sma(closes, 200),
        ema9=ema(closes, 9),
        vwap_proxy=vwap_proxy,
        rsi14=rsi(closes),
        macd=macd_line,
        macd_signal=macd_signal,
        atr14=atr14,
        atr_pct=round(atr14 / price * 100, 3) if atr14 and price else None,
        support=support,
        resistance=resistance,
        range_high_20d=round(max(b.high for b in bars[-20:]), 4) if len(bars) >= 20 else None,
        range_low_20d=round(min(b.low for b in bars[-20:]), 4) if len(bars) >= 20 else None,
        relative_volume=relative_volume,
        gap_pct=gap_pct,
        higher_highs=higher_highs(bars),
        lower_lows=lower_lows(bars),
        return_5d_pct=pct_return(closes, 5),
        return_20d_pct=pct_return(closes, 20),
        relative_strength_20d_vs_spy=rel_strength,
        provenance=Provenance(
            provider=DataProvider.INTERNAL,
            endpoint="technicals.compute_snapshot",
            as_of=utcnow(),
        ),
    )


__all__ = [
    "atr",
    "compute_snapshot",
    "ema",
    "higher_highs",
    "lower_lows",
    "macd",
    "pct_return",
    "rsi",
    "sma",
    "swing_levels",
]
