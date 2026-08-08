"""Black-Scholes pricing and greeks.

Used for two things only:

* generating self-consistent **mock** option chains for offline development;
* sanity-checking greeks when a live provider omits them (the result is
  labelled as derived, never presented as vendor data).

It is not used to overwrite real quotes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float  # per calendar day, per share
    vega: float  # per 1 volatility point (0.01), per share
    rho: float


def black_scholes(
    *,
    spot: float,
    strike: float,
    years_to_expiry: float,
    volatility: float,
    rate: float = 0.045,
    dividend_yield: float = 0.0,
    is_call: bool = True,
) -> Greeks:
    """Return price and greeks for one share-equivalent of a European option."""
    t = max(years_to_expiry, 1e-6)
    vol = max(volatility, 1e-4)
    s = max(spot, 1e-6)
    k = max(strike, 1e-6)

    d1 = (math.log(s / k) + (rate - dividend_yield + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)

    disc_r = math.exp(-rate * t)
    disc_q = math.exp(-dividend_yield * t)

    if is_call:
        price = s * disc_q * _norm_cdf(d1) - k * disc_r * _norm_cdf(d2)
        delta = disc_q * _norm_cdf(d1)
        theta_year = (
            -(s * disc_q * _norm_pdf(d1) * vol) / (2 * math.sqrt(t))
            - rate * k * disc_r * _norm_cdf(d2)
            + dividend_yield * s * disc_q * _norm_cdf(d1)
        )
        rho = k * t * disc_r * _norm_cdf(d2) / 100.0
    else:
        price = k * disc_r * _norm_cdf(-d2) - s * disc_q * _norm_cdf(-d1)
        delta = -disc_q * _norm_cdf(-d1)
        theta_year = (
            -(s * disc_q * _norm_pdf(d1) * vol) / (2 * math.sqrt(t))
            + rate * k * disc_r * _norm_cdf(-d2)
            - dividend_yield * s * disc_q * _norm_cdf(-d1)
        )
        rho = -k * t * disc_r * _norm_cdf(-d2) / 100.0

    gamma = disc_q * _norm_pdf(d1) / (s * vol * math.sqrt(t))
    vega = s * disc_q * _norm_pdf(d1) * math.sqrt(t) / 100.0

    return Greeks(
        price=max(price, 0.0),
        delta=delta,
        gamma=gamma,
        theta=theta_year / 365.0,
        vega=vega,
        rho=rho,
    )


def implied_move_pct(*, iv: float, days: float) -> float:
    """One-standard-deviation move over ``days``, in percent."""
    return iv * math.sqrt(max(days, 0.0) / 365.0) * 100.0


__all__ = ["Greeks", "black_scholes", "implied_move_pct"]
