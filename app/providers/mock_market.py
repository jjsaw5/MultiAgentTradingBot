"""A deterministic synthetic market used by every mock provider.

Why this exists: mocks that each invent their own numbers would never
disagree in realistic ways, and would make the cross-provider reconciliation
logic untestable. Instead all mock providers read from one synthetic world and
apply their own small, characteristic distortions on top -- so the pipeline
sees the same kind of near-agreement it would see in production.

The world is a pure function of ``(seed, trading_day)``. Two runs with the same
seed and date produce byte-identical scans, which is what makes the scoring
engine testable.

None of this data is real. Everything produced here is stamped with
``DataProvider`` values and ``as_of`` timestamps like real data, and every mock
provider sets ``backend="mock"`` on its request records so a report generated
offline can never be mistaken for a live one.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache

from app.models.market_data import PriceBar
from app.services.pricing import black_scholes

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class Scenario:
    """A hand-authored setup so the demo exercises distinct score outcomes."""

    symbol: str
    name: str
    sector: str
    base_price: float
    annual_vol: float
    drift: float  # annualised
    iv_base: float
    iv_rank: float
    liquidity: str  # excellent | good | thin | illiquid
    earnings_in_days: int | None
    flow_bias: float  # 0..1 bullish share of directional premium
    ask_side_share: float
    sweeps: int
    beta: float = 1.0
    is_index: bool = False
    note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "SPY", "SPDR S&P 500 ETF", "Broad Market", 548.0, 0.13, 0.09, 0.13, 34, "excellent",
        None, 0.58, 0.55, 8, 1.0, True, "Benchmark", ("index",),
    ),
    Scenario(
        "QQQ", "Invesco QQQ Trust", "Broad Market", 472.0, 0.17, 0.13, 0.17, 38, "excellent",
        None, 0.61, 0.57, 9, 1.15, True, "Tech benchmark", ("index",),
    ),
    Scenario(
        "IWM", "iShares Russell 2000", "Broad Market", 214.0, 0.21, 0.02, 0.21, 46, "excellent",
        None, 0.47, 0.49, 4, 1.2, True, "Small caps lagging", ("index",),
    ),
    Scenario(
        "NVDA", "NVIDIA Corp", "Semiconductors", 126.4, 0.44, 0.55, 0.45, 41, "excellent",
        34, 0.72, 0.68, 14, 1.9, False,
        "Trend-leading semi with datacenter demand headlines", ("ai", "semis"),
    ),
    Scenario(
        "AMD", "Advanced Micro Devices", "Semiconductors", 158.2, 0.47, 0.28, 0.49, 55, "good",
        41, 0.66, 0.62, 9, 1.8, False, "Second-derivative AI beneficiary", ("ai", "semis"),
    ),
    Scenario(
        "MSFT", "Microsoft Corp", "Software", 428.5, 0.22, 0.18, 0.21, 27, "excellent",
        52, 0.63, 0.60, 6, 0.95, False, "Steady uptrend, analyst raises", ("ai", "megacap"),
    ),
    Scenario(
        "META", "Meta Platforms", "Interactive Media", 502.0, 0.31, 0.03, 0.33, 49, "good",
        29, 0.51, 0.50, 3, 1.25, False, "Range-bound, no clean edge", ("megacap",),
    ),
    Scenario(
        "TSLA", "Tesla Inc", "Consumer Discretionary", 244.7, 0.55, 0.05, 0.68, 88, "good",
        4, 0.55, 0.52, 11, 2.1, False,
        "Elevated IV into imminent earnings -- expected to fail hard rules", ("ev",),
    ),
    Scenario(
        "XOM", "Exxon Mobil", "Energy", 112.3, 0.24, -0.16, 0.26, 44, "good",
        61, 0.34, 0.40, 5, 0.65, False, "Crude weakness, downtrend", ("energy",),
    ),
    Scenario(
        "JPM", "JPMorgan Chase", "Financials", 208.9, 0.20, 0.08, 0.22, 31, "excellent",
        47, 0.56, 0.54, 4, 0.95, False, "Rate-sensitive, steady", ("banks",),
    ),
    Scenario(
        "LLY", "Eli Lilly", "Healthcare", 812.0, 0.29, 0.22, 0.31, 52, "good",
        58, 0.64, 0.61, 6, 0.7, False, "Pipeline catalyst pending", ("pharma",),
    ),
    Scenario(
        "SOFI", "SoFi Technologies", "Financials", 7.42, 0.52, 0.10, 0.58, 63, "illiquid",
        38, 0.60, 0.58, 2, 1.6, False,
        "Low-priced name with wide option markets -- expected liquidity reject", ("fintech",),
    ),
)

SCENARIO_BY_SYMBOL = {s.symbol: s for s in SCENARIOS}

SECTOR_ETF = {
    "Semiconductors": "SMH",
    "Software": "XLK",
    "Interactive Media": "XLC",
    "Consumer Discretionary": "XLY",
    "Energy": "XLE",
    "Financials": "XLF",
    "Healthcare": "XLV",
    "Broad Market": "SPY",
}


def _rng(*parts: object) -> random.Random:
    """Stable RNG derived from its arguments, independent of process state."""
    key = "|".join(str(p) for p in parts)
    seed = int.from_bytes(key.encode(), "little") % (2**63)
    return random.Random(seed)


@dataclass
class SyntheticMarket:
    seed: int
    trading_day: date

    # ---------------------------------------------------------------- prices
    def bars(self, symbol: str, days: int = 260) -> list[PriceBar]:
        sc = self._scenario(symbol)
        rng = _rng(self.seed, symbol, "bars", self.trading_day.isoformat())
        dt = 1.0 / TRADING_DAYS_PER_YEAR
        mu, sigma = sc.drift, sc.annual_vol

        # Walk backwards from the target close so the final price is anchored
        # to the scenario's base_price -- keeps demo output legible.
        log_path = [0.0]
        for _ in range(days):
            shock = rng.gauss(0.0, 1.0)
            log_path.append(log_path[-1] + (mu - 0.5 * sigma**2) * dt + sigma * math.sqrt(dt) * shock)
        anchor = log_path[-1]
        closes = [sc.base_price * math.exp(x - anchor) for x in log_path]

        out: list[PriceBar] = []
        day = self.trading_day - timedelta(days=int(days * 1.45))
        for close in closes:
            while day.weekday() >= 5:
                day += timedelta(days=1)
            intraday = sc.annual_vol / math.sqrt(TRADING_DAYS_PER_YEAR)
            hi = close * (1 + abs(rng.gauss(0, intraday * 0.6)))
            lo = close * (1 - abs(rng.gauss(0, intraday * 0.6)))
            op = lo + (hi - lo) * rng.random()
            base_vol = 4_000_000 if sc.is_index else 1_200_000
            vol = int(base_vol * (1.0 + abs(rng.gauss(0, 0.35))) * (60 / max(sc.base_price, 5)) ** 0.3)
            out.append(
                PriceBar(
                    day=day,
                    open=round(op, 2),
                    high=round(max(hi, op, close), 2),
                    low=round(min(lo, op, close), 2),
                    close=round(close, 2),
                    volume=vol,
                )
            )
            day += timedelta(days=1)
        return out[-days:]

    def last_price(self, symbol: str) -> float:
        return self.bars(symbol, 260)[-1].close

    def average_volume(self, symbol: str, window: int = 20) -> int:
        bars = self.bars(symbol, 260)[-window:]
        return int(sum(b.volume for b in bars) / len(bars))

    def today_volume(self, symbol: str) -> int:
        sc = self._scenario(symbol)
        rng = _rng(self.seed, symbol, "relvol", self.trading_day.isoformat())
        # Names with a live catalyst trade heavier; that is the point of relvol.
        boost = 1.55 if sc.earnings_in_days is not None and sc.earnings_in_days < 45 else 1.0
        multiplier = max(0.4, rng.gauss(1.05, 0.28) * boost)
        return int(self.average_volume(symbol) * multiplier)

    # --------------------------------------------------------------- options
    def expirations(self, symbol: str, count: int = 10) -> list[date]:
        """Weekly expirations (Fridays) for the next ``count`` weeks."""
        out: list[date] = []
        d = self.trading_day
        d += timedelta(days=(4 - d.weekday()) % 7)
        if d == self.trading_day:
            d += timedelta(days=7)
        for _ in range(count):
            out.append(d)
            d += timedelta(days=7)
        return out

    def strikes(self, symbol: str) -> list[float]:
        spot = self.last_price(symbol)
        step = self._strike_step(spot)
        n = 14
        base = round(spot / step) * step
        return [round(base + i * step, 2) for i in range(-n, n + 1) if base + i * step > 0]

    @staticmethod
    def _strike_step(spot: float) -> float:
        if spot < 15:
            return 0.5
        if spot < 50:
            return 1.0
        if spot < 150:
            return 2.5
        if spot < 400:
            return 5.0
        return 10.0

    def implied_vol(self, symbol: str, strike: float, expiry: date) -> float:
        sc = self._scenario(symbol)
        spot = self.last_price(symbol)
        dte = max((expiry - self.trading_day).days, 1)
        moneyness = math.log(strike / spot)
        # Downside skew plus a mild term-structure slope.
        skew = -0.55 * moneyness
        term = 0.04 * math.log(dte / 30.0) if dte > 0 else 0.0
        earnings_bump = 0.0
        if sc.earnings_in_days is not None and dte >= sc.earnings_in_days:
            earnings_bump = 0.06 if sc.earnings_in_days < 20 else 0.02
        return max(0.05, sc.iv_base + skew + term + earnings_bump)

    def option_quote(self, symbol: str, strike: float, expiry: date, is_call: bool) -> dict:
        sc = self._scenario(symbol)
        spot = self.last_price(symbol)
        dte = max((expiry - self.trading_day).days, 1)
        iv = self.implied_vol(symbol, strike, expiry)
        g = black_scholes(
            spot=spot,
            strike=strike,
            years_to_expiry=dte / 365.0,
            volatility=iv,
            is_call=is_call,
        )
        theo = g.price

        rel_spread = {
            "excellent": 0.014,
            "good": 0.032,
            "thin": 0.085,
            "illiquid": 0.19,
        }[sc.liquidity]
        # Cheap far-OTM contracts always quote wider in relative terms.
        rel_spread *= 1.0 + max(0.0, (1.0 - min(theo, 5.0) / 5.0)) * 1.6
        half = max(0.01, theo * rel_spread / 2.0)
        bid = max(0.01, round(theo - half, 2))
        ask = round(theo + half, 2)

        rng = _rng(self.seed, symbol, strike, expiry.isoformat(), is_call, "liq")
        atm_closeness = math.exp(-((math.log(strike / spot)) ** 2) / (2 * 0.0125))
        dte_factor = math.exp(-abs(dte - 30) / 70.0)
        liq_scale = {"excellent": 1.0, "good": 0.55, "thin": 0.16, "illiquid": 0.05}[sc.liquidity]
        oi = int(9000 * atm_closeness * dte_factor * liq_scale * rng.uniform(0.7, 1.4))
        volume = int(oi * rng.uniform(0.15, 0.75))

        return {
            "bid": bid,
            "ask": ask,
            "last": round((bid + ask) / 2, 2),
            "iv": round(iv, 4),
            "iv_rank": sc.iv_rank,
            "delta": round(g.delta, 4),
            "gamma": round(g.gamma, 5),
            "theta": round(g.theta * 100, 4),  # per contract, per day
            "vega": round(g.vega * 100, 4),  # per contract, per IV point
            "rho": round(g.rho * 100, 4),
            "open_interest": oi,
            "volume": volume,
        }

    # ------------------------------------------------------------------ flow
    def flow(self, symbol: str) -> dict:
        sc = self._scenario(symbol)
        rng = _rng(self.seed, symbol, "flow", self.trading_day.isoformat())
        total_premium = 2_000_000 * (1.0 + rng.random() * 4.0) * max(sc.beta, 0.5)
        bull_share = min(0.95, max(0.05, rng.gauss(sc.flow_bias, 0.035)))
        bullish = total_premium * bull_share
        bearish = total_premium * (1 - bull_share)
        ask_share = min(0.95, max(0.05, rng.gauss(sc.ask_side_share, 0.03)))
        oi = int(220_000 * max(sc.beta, 0.5) * rng.uniform(0.6, 1.5))
        vol = int(oi * rng.uniform(0.55, 1.6))
        return {
            "call_premium": round(bullish * rng.uniform(0.85, 1.1), 2),
            "put_premium": round(bearish * rng.uniform(0.85, 1.1), 2),
            "bullish_premium": round(bullish, 2),
            "bearish_premium": round(bearish, 2),
            "ask_side_premium": round(total_premium * ask_share, 2),
            "bid_side_premium": round(total_premium * (1 - ask_share) * 0.9, 2),
            "mid_side_premium": round(total_premium * 0.08, 2),
            "sweep_count": sc.sweeps,
            "block_count": max(0, sc.sweeps // 3),
            "large_trade_count": sc.sweeps + rng.randint(0, 6),
            "multileg_share": round(min(0.6, abs(rng.gauss(0.22, 0.08))), 3),
            "total_volume": vol,
            "total_open_interest": oi,
            "net_delta_flow": round((bull_share - 0.5) * 2 * vol * 0.4, 1),
            "net_gamma_flow": round(rng.gauss(0, 1500), 1),
            "net_vega_flow": round(rng.gauss(0, 90_000), 1),
            "gamma_exposure": round(rng.gauss(0, 4_000_000), 1),
            "dark_pool_notional": round(total_premium * rng.uniform(3, 9), 2),
            "dark_pool_bias": "BULLISH" if bull_share > 0.55 else ("BEARISH" if bull_share < 0.45 else "NEUTRAL"),
            "iv_rank": sc.iv_rank,
            "iv30": round(sc.iv_base, 4),
            "expected_move_pct": round(sc.iv_base * math.sqrt(21 / 365) * 100, 2),
        }

    # ------------------------------------------------------------- calendars
    def earnings_date(self, symbol: str) -> date | None:
        sc = SCENARIO_BY_SYMBOL.get(symbol.upper())
        if sc is None or sc.earnings_in_days is None:
            return None
        return self.trading_day + timedelta(days=sc.earnings_in_days)

    def economic_events(self) -> list[dict]:
        """A plausible forward macro calendar anchored to the trading day."""
        return [
            {
                "name": "CPI (MoM)", "code": "CPI", "offset": 2,
                "importance": "HIGH", "consensus": "0.2%", "previous": "0.3%",
            },
            {
                "name": "Initial Jobless Claims", "code": "CLAIMS", "offset": 3,
                "importance": "MEDIUM", "consensus": "225K", "previous": "231K",
            },
            {
                "name": "PPI (MoM)", "code": "PPI", "offset": 4,
                "importance": "MEDIUM", "consensus": "0.1%", "previous": "0.2%",
            },
            {
                "name": "Retail Sales", "code": "RETAIL", "offset": 8,
                "importance": "MEDIUM", "consensus": "0.3%", "previous": "0.1%",
            },
            {
                "name": "FOMC Rate Decision", "code": "FOMC", "offset": 14,
                "importance": "CRITICAL", "consensus": "hold", "previous": "hold",
            },
            {
                "name": "Core PCE Price Index", "code": "PCE", "offset": 21,
                "importance": "HIGH", "consensus": "0.2%", "previous": "0.2%",
            },
        ]

    def event_datetime(self, offset_days: int, hour: int = 8, minute: int = 30) -> datetime:
        return datetime.combine(
            self.trading_day + timedelta(days=offset_days), time(hour, minute), UTC
        )

    # ----------------------------------------------------------------- utils
    def _scenario(self, symbol: str) -> Scenario:
        sc = SCENARIO_BY_SYMBOL.get(symbol.upper())
        if sc is None:
            raise KeyError(
                f"{symbol} is not in the synthetic universe. Mock providers must not "
                f"invent data for unknown tickers."
            )
        return sc

    def known(self, symbol: str) -> bool:
        return symbol.upper() in SCENARIO_BY_SYMBOL

    def sector_of(self, symbol: str) -> str | None:
        sc = SCENARIO_BY_SYMBOL.get(symbol.upper())
        return sc.sector if sc else None


@lru_cache(maxsize=8)
def get_market(seed: int, trading_day: date) -> SyntheticMarket:
    return SyntheticMarket(seed=seed, trading_day=trading_day)


__all__ = ["SCENARIOS", "SCENARIO_BY_SYMBOL", "SECTOR_ETF", "Scenario", "SyntheticMarket", "get_market"]
