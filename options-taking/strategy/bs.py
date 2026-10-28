"""Black-76 on the exchange's published forward (Natenberg, appendix B).

Pricing off the forward rather than the spot means dividends and carry are already in the
price: the exchange publishes the forward each contract's Greeks were calculated with.
Floats are fine here: these are model values for display and comparison, never order
prices (those stay integer micro-dollars).
"""

from dataclasses import dataclass
from math import erf, exp, log, pi, sqrt


def _n(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _pdf(x: float) -> float:
    return exp(-0.5 * x * x) / sqrt(2.0 * pi)


@dataclass(frozen=True)
class Value:
    price: float
    delta: float  # per share, against the forward
    gamma: float  # per share per dollar
    vega: float  # dollars per share per volatility point
    theta: float  # dollars per share per calendar day


def black76(call: bool, forward: float, strike: float, years: float, vol: float,
            rate: float = 0.0) -> Value:
    """`vol` is a fraction (0.20 for 20%); `years` the time to expiry."""
    df = exp(-rate * years)
    if years <= 0 or vol <= 0:
        intrinsic = max(0.0, (forward - strike) if call else (strike - forward))
        d = (1.0 if forward > strike else 0.0) if call else (-1.0 if forward < strike else 0.0)
        return Value(df * intrinsic, d, 0.0, 0.0, 0.0)
    s = vol * sqrt(years)
    d1 = (log(forward / strike) + 0.5 * s * s) / s
    d2 = d1 - s
    if call:
        price = df * (forward * _n(d1) - strike * _n(d2))
        delta = df * _n(d1)
    else:
        price = df * (strike * _n(-d2) - forward * _n(-d1))
        delta = -df * _n(-d1)
    gamma = df * _pdf(d1) / (forward * s)
    vega = df * forward * _pdf(d1) * sqrt(years) / 100.0
    theta = (-df * forward * _pdf(d1) * vol / (2.0 * sqrt(years)) + rate * price) / 365.0
    return Value(price, delta, gamma, vega, theta)


def implied_vol(call: bool, price: float, forward: float, strike: float, years: float,
                rate: float = 0.0) -> float | None:
    """The volatility (a fraction) that prices the option at `price`, by bisection."""
    lo, hi = 1e-4, 5.0
    if not black76(call, forward, strike, years, lo, rate).price <= price <= \
            black76(call, forward, strike, years, hi, rate).price:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if black76(call, forward, strike, years, mid, rate).price < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
