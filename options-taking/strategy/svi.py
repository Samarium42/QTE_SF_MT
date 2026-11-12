"""SVI: a smooth, arbitrage-checked curve through one expiry's implied vols (Gatheral 2004).

Total implied variance w = iv^2 * t, as a function of log-moneyness k = ln(K/F), is

    w(k) = a + b * (rho * (k - m) + sqrt((k - m)^2 + sigma^2))

  a      overall level of variance
  b      how steep the wings are (b >= 0)
  rho    skew, -1 < rho < 1: negative tilts the smile up on the put side
  m      where the bottom of the smile sits, in k
  sigma  how rounded the bottom is (sigma > 0)

Nobody picks these by hand: fit_svi finds the five numbers that make the curve closest to
the exchange's published vols, every refit. It uses the quasi-explicit method (Zeliade
2009): for a fixed m and sigma the curve is linear in the other three, so those come out
of a 3x3 least squares, and only m and sigma need searching (a grid, then finer grids
around the best). The fit is vega-weighted like the dashboard's quadratic smile, so the
near-the-money vols, which the price pins down, count most.

A fit is refused when it breaks Lee's wing bound or has butterfly arbitrage anywhere on the
strikes it was fitted to (Gatheral and Jacquier 2014, g(k) >= 0): a curve like that is not
a fair value to trade against.

Floats are fine here: these are model values, never order prices.
"""

from dataclasses import dataclass
from math import exp, log, sqrt

LEE = 2.0  # Lee's moment bound: the wings of w(k) rise no faster than 2|k|


@dataclass(frozen=True)
class SviPoint:
    k: float  # ln(strike / forward)
    iv: float  # volatility points, e.g. 18.5
    vega: float  # dollars per share per vol point: the weight


@dataclass(frozen=True)
class Svi:
    a: float
    b: float
    rho: float
    m: float
    sigma: float
    years: float
    rmse: float  # vega-weighted, in vol points, over the points kept
    used: int  # points kept in the final fit
    k_lo: float  # range of k it was fitted on: no trading outside it
    k_hi: float

    def w(self, k: float) -> float:
        x = k - self.m
        return self.a + self.b * (self.rho * x + sqrt(x * x + self.sigma * self.sigma))

    def iv(self, k: float) -> float:
        """The curve's vol at k, in vol points."""
        return 100.0 * sqrt(max(self.w(k), 0.0) / self.years)


def solve3(s: list[list[float]], t: list[float]) -> tuple[float, float, float] | None:
    """3x3 linear system by Cramer's rule, as in dashboard/analytics.fit_smile."""
    def det(q: list[list[float]]) -> float:
        return (q[0][0] * (q[1][1] * q[2][2] - q[1][2] * q[2][1])
                - q[0][1] * (q[1][0] * q[2][2] - q[1][2] * q[2][0])
                + q[0][2] * (q[1][0] * q[2][1] - q[1][1] * q[2][0]))

    base = det(s)
    if abs(base) < 1e-30:
        return None
    out = []
    for col in range(3):
        q = [row[:] for row in s]
        for i in range(3):
            q[i][col] = t[i]
        out.append(det(q) / base)
    return out[0], out[1], out[2]


def fit_slice(ks: list[float], ws: list[float], wts: list[float], m: float, sigma: float):
    """For a fixed m and sigma: the best a, b, rho and the weighted squared error.

    With y = (k - m) / sigma the curve is w = a + d*y + c*sqrt(y^2 + 1), where c = b*sigma
    and d = rho*b*sigma, which is linear in (a, d, c). The answer is pulled back inside
    0 <= c, |d| <= c and c + |d| <= LEE*sigma (b >= 0, |rho| <= 1 and Lee's bound), with a
    set so the lowest point of the smile is not negative."""
    s = [[0.0] * 3 for _ in range(3)]
    t = [0.0] * 3
    rows = []
    for k, w, wt in zip(ks, ws, wts):
        y = (k - m) / sigma
        xs = (1.0, y, sqrt(y * y + 1.0))
        rows.append((xs, w, wt))
        for i in range(3):
            t[i] += wt * xs[i] * w
            for j in range(3):
                s[i][j] += wt * xs[i] * xs[j]
    sol = solve3(s, t)
    if sol is None:
        return None
    a, d, c = sol
    c = min(max(c, 0.0), LEE * sigma)
    d = max(-c, min(d, c))
    d = max(-(LEE * sigma - c), min(d, LEE * sigma - c))
    if c != sol[2] or d != sol[1]:
        # Moved onto the boundary: refit a alone for the clipped c and d.
        total = sum(wt for _, _, wt in rows)
        a = sum(wt * (w - d * xs[1] - c * xs[2]) for xs, w, wt in rows) / total
    a = max(a, -sqrt(max(c * c - d * d, 0.0)))
    err = sum(wt * (w - a - d * xs[1] - c * xs[2]) ** 2 for xs, w, wt in rows)
    b = c / sigma
    rho = d / c if c > 0 else 0.0
    return err, a, b, rho


def search(ks: list[float], ws: list[float], wts: list[float]):
    """Grid over m and sigma, then three finer grids around the best."""
    lo, hi = min(ks), max(ks)
    span = max(hi - lo, 1e-4)
    best = None
    ms = [lo - 0.5 * span + 2.0 * span * i / 14 for i in range(15)]
    sigmas = [exp(log(1e-3) + (log(2.0) - log(1e-3)) * i / 13) for i in range(14)]
    for m in ms:
        for sigma in sigmas:
            got = fit_slice(ks, ws, wts, m, sigma)
            if got is not None and (best is None or got[0] < best[0]):
                best = (*got, m, sigma)
    if best is None:
        return None
    dm, ds = 2.0 * span / 14, (log(2.0) - log(1e-3)) / 13
    for _ in range(3):
        m0, s0 = best[4], best[5]
        for i in (-2, -1, 0, 1, 2):
            for j in (-2, -1, 0, 1, 2):
                m, sigma = m0 + dm * i / 2, s0 * exp(ds * j / 2)
                got = fit_slice(ks, ws, wts, m, sigma)
                if got is not None and got[0] < best[0]:
                    best = (*got, m, sigma)
        dm, ds = dm / 2, ds / 2
    return best


def butterfly_ok(fit: Svi, k_lo: float, k_hi: float, steps: int = 40) -> bool:
    """Gatheral-Jacquier g(k) >= 0 on [k_lo, k_hi]: no strike is worth less than the
    butterfly around it says it must be (the risk-neutral density is not negative)."""
    for i in range(steps + 1):
        k = k_lo + (k_hi - k_lo) * i / steps
        x = k - fit.m
        r = sqrt(x * x + fit.sigma * fit.sigma)
        w = fit.w(k)
        if w <= 0:
            return False
        w1 = fit.b * (fit.rho + x / r)
        w2 = fit.b * fit.sigma * fit.sigma / (r * r * r)
        g = (1 - k * w1 / (2 * w)) ** 2 - w1 * w1 / 4 * (1 / w + 0.25) + w2 / 2
        if g < -1e-9:
            return False
    return True


def fit_svi(points: list[SviPoint], years: float, min_points: int = 6,
            outlier_vols: float = 3.0) -> Svi | None:
    """The SVI curve for one expiry, or None when there are too few points or the fit
    has arbitrage in it.

    Weights: the dashboard's smile minimises sum(vega * vol_gap^2). In total variance, a
    vol gap is a w gap divided by 2 * iv * t, so the same weighting here is
    vega / (2 * iv * t)^2. After one fit, points more than `outlier_vols` times the fit's
    rmse away are dropped and it is fitted again, so the one strike that is out of line
    (the thing we want to trade) does not drag the curve towards itself."""
    pts = [p for p in points if p.iv > 0 and p.vega > 0]
    if years <= 0 or not pts:
        return None
    k_lo, k_hi = min(p.k for p in pts), max(p.k for p in pts)
    fit = None
    for _ in range(2):
        if len(pts) < min_points:
            return None
        ks = [p.k for p in pts]
        ws = [(p.iv / 100.0) ** 2 * years for p in pts]
        wts = [p.vega / (2.0 * p.iv / 100.0 * years) ** 2 for p in pts]
        got = search(ks, ws, wts)
        if got is None:
            return None
        _, a, b, rho, m, sigma = got
        trial = Svi(a, b, rho, m, sigma, years, 0.0, len(pts), k_lo, k_hi)
        gaps = [p.iv - trial.iv(p.k) for p in pts]
        total = sum(p.vega for p in pts)
        rmse = sqrt(sum(p.vega * g * g for p, g in zip(pts, gaps)) / total)
        fit = Svi(a, b, rho, m, sigma, years, rmse, len(pts), k_lo, k_hi)
        keep = [p for p, g in zip(pts, gaps) if abs(g) <= max(outlier_vols * rmse, 0.05)]
        if len(keep) == len(pts):
            break
        pts = keep
    if fit is None or not butterfly_ok(fit, fit.k_lo, fit.k_hi):
        return None
    return fit
