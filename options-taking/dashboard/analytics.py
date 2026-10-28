"""Dashboard analytics: the score's Sharpe, the volatility smile and mispricings against it,
a spot/vol scenario grid, and fill markouts. Pure functions on plain numbers (floats in
dollars), so they can be tested without an exchange.
"""

from dataclasses import dataclass
from math import log, sqrt
from statistics import mean, stdev

from strategy.bs import black76

# ---------- Sharpe, as Rule 9001 scores it ----------


@dataclass(frozen=True)
class DayResult:
    date: str
    pnl: float  # the day's equity change, financing included (account daily_pnl)
    idle: float  # what an idle cash-only book would have earned that day


def idle_accrual(equity: float, sofr: float, days: int) -> float:
    """Rule 2009: idle cash earns C * (SOFR - 0.25%) / 360 per active calendar day."""
    return equity * (sofr - 0.0025) / 360.0 * days


def sharpe(days: list[DayResult], e0: float) -> dict:
    """Rule 9001: daily excess returns on the fixed starting equity, annualised by sqrt(252).
    Zero standard deviation (or fewer than two days) gives zero."""
    returns = [(d.pnl - d.idle) / e0 for d in days]
    if len(returns) < 2:
        s = 0.0
    else:
        sd = stdev(returns)
        s = 0.0 if sd == 0 else sqrt(252) * mean(returns) / sd
    return {
        "sharpe": s,
        "days": len(returns),
        "mean_daily": mean(returns) if returns else 0.0,
        "stdev_daily": stdev(returns) if len(returns) > 1 else 0.0,
        "net_pnl": sum(d.pnl for d in days),
        "excess_pnl": sum(d.pnl - d.idle for d in days),
        "returns": [{"date": d.date, "excess": r} for d, r in zip(days, returns)],
    }


# ---------- volatility smile ----------


@dataclass(frozen=True)
class SmilePoint:
    instrument: str
    call: bool
    strike: float
    forward: float
    years: float
    iv: float  # volatility points, e.g. 18.5
    vega: float  # dollars per share per vol point: the fit's weight

    @property
    def x(self) -> float:
        """Natenberg's smile coordinate: ln(K/F) / sqrt(t) (ch. 18)."""
        return log(self.strike / self.forward) / sqrt(max(self.years, 1e-6))


def fit_smile(points: list[SmilePoint]) -> tuple[float, float, float] | None:
    """Vega-weighted least-squares quadratic iv = a + b x + c x^2, or None with too few
    points. Vega weighting trusts the near-the-money vols most: there the IV is pinned
    down by the price, while a far wing's IV moves a lot for a cent of premium."""
    if len(points) < 4:
        return None
    # Normal equations for weighted least squares, solved by Cramer's rule (3x3).
    s = [[0.0] * 3 for _ in range(3)]
    t = [0.0] * 3
    for p in points:
        w = max(p.vega, 1e-6)
        xs = (1.0, p.x, p.x * p.x)
        for i in range(3):
            t[i] += w * xs[i] * p.iv
            for j in range(3):
                s[i][j] += w * xs[i] * xs[j]

    def det(m: list[list[float]]) -> float:
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))

    base = det(s)
    if abs(base) < 1e-18:
        return None
    coef = []
    for k in range(3):
        m = [row[:] for row in s]
        for i in range(3):
            m[i][k] = t[i]
        coef.append(det(m) / base)
    return coef[0], coef[1], coef[2]


def smile_iv(fit: tuple[float, float, float], x: float) -> float:
    a, b, c = fit
    return a + b * x + c * x * x


# ---------- mispricings against the smile ----------


@dataclass(frozen=True)
class Quote:
    point: SmilePoint
    bid: float | None
    ask: float | None
    mark: float | None


def mispricings(quotes: list[Quote], fit: tuple[float, float, float], fee_per_share: float,
                min_edge: float = 0.0) -> list[dict]:
    """For each contract, the edge per contract (x100) of buying at the wall ask or selling
    at the wall bid against its Black-76 value at the fitted smile vol, after the taker fee.
    Sorted best first; only edges above `min_edge` dollars per contract."""
    out = []
    for q in quotes:
        p = q.point
        vol = smile_iv(fit, p.x)
        if vol <= 0:
            continue
        fair = black76(p.call, p.forward, p.strike, p.years, vol / 100.0).price
        rows = []
        if q.ask is not None:
            rows.append(("BUY", (fair - q.ask - fee_per_share) * 100, q.ask))
        if q.bid is not None:
            rows.append(("SELL", (q.bid - fair - fee_per_share) * 100, q.bid))
        for side, edge, price in rows:
            if edge > min_edge:
                out.append({"instrument": p.instrument, "side": side, "price": price,
                            "fair": fair, "mark": q.mark, "iv": p.iv, "smile_iv": vol,
                            "vol_gap": p.iv - vol, "edge": edge})
    return sorted(out, key=lambda r: -r["edge"])


# ---------- scenario grid ----------


@dataclass(frozen=True)
class Holding:
    underlying: str
    qty: int  # contracts for options, shares for stock
    call: bool | None  # None for stock
    strike: float = 0.0
    forward: float = 0.0
    years: float = 0.0
    iv: float = 0.0  # vol points
    spot: float = 0.0


def scenario_grid(holdings: list[Holding], spot_moves: list[float], vol_moves: list[float]) -> list[list[float]]:
    """P&L of the book, in dollars, for each (vol move in points) row and (spot move as a
    fraction) column, every underlying moving together. Options are repriced with Black-76
    on the shocked forward at the shocked vol, at today's time to expiry."""
    def value(h: Holding, ds: float, dv: float) -> float:
        if h.call is None:
            return h.qty * h.spot * (1 + ds)
        vol = max(0.01, h.iv + dv) / 100.0
        return h.qty * 100 * black76(h.call, h.forward * (1 + ds), h.strike, h.years, vol).price

    base = sum(value(h, 0.0, 0.0) for h in holdings)
    return [[sum(value(h, ds, dv) for h in holdings) - base for ds in spot_moves] for dv in vol_moves]


# ---------- markouts ----------

MARKOUT_HORIZONS = (1, 10, 60, 300)  # seconds after the fill


def markout(side: str, fill_price: float, later_mark: float, size: int, multiplier: int) -> float:
    """Dollars a fill made against the mark some time later: positive means the market
    moved our way after we traded (good), negative that we were picked off."""
    sign = 1 if side == "BUY" else -1
    return sign * (later_mark - fill_price) * size * multiplier
