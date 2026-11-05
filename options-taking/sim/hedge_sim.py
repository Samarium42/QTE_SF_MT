"""Old fixed hedger bands against Whalley-Wilmott bands, on simulated sessions.

Each path is one 6.5-hour session. The book holds options priced by Black-76 at the
realised vol, so with free continuous hedging its P&L would be zero: whatever the hedged
book makes or loses is hedging error, and fees plus half-spreads are the cost of hedging.
Both rules call the live code (strategy.risk.hedge_shares and band_hedge_shares).

    python -m sim.hedge_sim                 # default books, GBM and a gap
    python -m sim.hedge_sim --paths 400
"""

import argparse
import math
import random
import statistics
from dataclasses import dataclass
from decimal import Decimal

from strategy.bs import black76
from strategy.config import HedgerSettings
from strategy.risk import MULTIPLIER, band_hedge_shares, hedge_shares, ww_band

SESSION_SECONDS = 6.5 * 3600
YEAR_SECONDS = 252 * SESSION_SECONDS  # trading-time clock: vol accrues only in session
FEE = 0.30e-4  # stock taker fee, 0.30 bp of notional
WALK_BP = 1.5  # average ladder walk past the touch, half the 3 bp cap


@dataclass
class Book:
    name: str
    spot: float
    vol: float
    days: float  # to expiry, trading days
    legs: list[tuple[bool, float, int]]  # (call, strike, signed contracts)
    half_spread_bp: float
    depth_shares: int  # shares on the wall within the hedger's 3 bp slippage cap, per order

    def greeks(self, s: float, t_years: float) -> tuple[float, float, float]:
        """Value, dollar delta, gamma in shares per $1."""
        v = d = g = 0.0
        for call, k, q in self.legs:
            x = black76(call, s, k, t_years, self.vol)
            v += q * MULTIPLIER * x.price
            d += q * MULTIPLIER * x.delta * s
            g += q * MULTIPLIER * x.gamma
        return v, d, g


def run_path(book: Book, rule: dict, steps: int, z: list[float], gap_at: int | None, gap: float,
             cfg: HedgerSettings):
    dt = SESSION_SECONDS / steps
    s = book.spot
    t0 = book.days / 252
    lim = cfg.limits
    # Paid: half the spread, about half the 3 bp ladder walk on average, and the fee. The
    # live hedger prices its band on the same estimate.
    cost_frac = (book.half_spread_bp + WALK_BP) * 1e-4 + FEE
    # One order per 0.5 s per underlying, each at most what the wall shows within 3 bp.
    per_step = int(book.depth_shares * dt / 0.5)
    v0, d0, _ = book.greeks(s, t0)
    stock = -int(d0 / s)  # start flat
    cash = -stock * s
    paid = 0.0
    trades = 0
    worst_net = 0.0
    sig = book.vol * math.sqrt(dt / YEAR_SECONDS)
    for i in range(steps):
        s *= math.exp(-0.5 * sig * sig + sig * z[i])
        if gap_at is not None and i == gap_at:
            s *= 1 + gap
        t = t0 - (i + 1) * dt / YEAR_SECONDS
        _, d, g = book.greeks(s, t)
        S = Decimal(str(round(s, 4)))
        D = Decimal(str(round(d, 2)))
        if rule["kind"] == "fixed":
            n = hedge_shares(D, stock, S, threshold=lim.net_delta_per_underlying * cfg.trigger_fraction,
                             max_order=lim.max_hedge_order, tolerance=cfg.tolerance,
                             buffer=lim.net_delta_per_underlying * cfg.target_fraction)
        else:
            band = ww_band(Decimal(str(g)), S, Decimal(str(cost_frac)),
                           risk_aversion=Decimal(str(rule["gamma"])),
                           floor=cfg.band_floor, cap=cfg.band_cap)
            n = band_hedge_shares(D, stock, S, band=band, rehedge_to=Decimal(str(rule.get("to", 1))),
                                  max_order=lim.max_hedge_order, tolerance=cfg.tolerance)
        n = max(-per_step, min(per_step, n))
        if n:
            stock += n
            cash -= n * s
            paid += abs(n) * s * cost_frac
            trades += 1
        worst_net = max(worst_net, abs(d + stock * s))
    v1, _, _ = book.greeks(s, t0 - SESSION_SECONDS / YEAR_SECONDS)
    hedged = (v1 - v0) + stock * s + cash  # option P&L + stock P&L, before costs
    return hedged, paid, trades, worst_net


def compare(book: Book, rules: list[dict], paths: int, steps: int, gap: float, seed: int):
    cfg = HedgerSettings()
    rng = random.Random(seed)
    shocks = [[rng.gauss(0, 1) for _ in range(steps)] for _ in range(paths)]
    gap_at = steps // 2 if gap else None
    title = f"{book.name}" + (f", {gap:+.0%} gap mid-session" if gap else ", GBM")
    print(f"\n{title}  (spot {book.spot}, vol {book.vol:.0%}, half-spread {book.half_spread_bp} bp + {WALK_BP} bp walk + 0.30 bp fee, {book.depth_shares} sh/order)")
    print(f"  {'rule':26s} {'cost $':>8s} {'err sd $':>9s} {'total mean':>11s} {'total sd':>9s} "
          f"{'trades':>7s} {'max |net|':>10s}")
    for rule in rules:
        res = [run_path(book, rule, steps, z, gap_at, gap, cfg) for z in shocks]
        err = [r[0] for r in res]
        cost = [r[1] for r in res]
        total = [e - c for e, c in zip(err, cost)]
        print(f"  {rule['label']:26s} {statistics.mean(cost):8.0f} {statistics.pstdev(err):9.0f} "
              f"{statistics.mean(total):11.0f} {statistics.pstdev(total):9.0f} "
              f"{statistics.mean(r[2] for r in res):7.1f} {statistics.mean(r[3] for r in res):10.0f}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--paths", type=int, default=200)
    p.add_argument("--steps", type=int, default=2340, help="hedge checks per session (10 s)")
    p.add_argument("--seed", type=int, default=7)
    # Wall stats from the history service, 30 Oct 2026 (sim/wall_stats.py).
    p.add_argument("--spy-half-bp", type=float, default=1.7)
    p.add_argument("--googl-half-bp", type=float, default=2.5)
    a = p.parse_args()
    cfg = HedgerSettings()
    spy = Book("SPY short 40 ATM straddles", 765.0, 0.15, 18, [(True, 765, -40), (False, 765, -40)],
               a.spy_half_bp, 124)
    googl = Book("GOOGL short 60 ATM puts", 280.0, 0.35, 18, [(False, 280, -60)], a.googl_half_bp, 35)
    long_spy = Book("SPY long 40 ATM straddles", 765.0, 0.15, 18, [(True, 765, 40), (False, 765, 40)],
                    a.spy_half_bp, 124)
    rules = [{"kind": "fixed", "label": "fixed 150k -> 75k"}]
    for g in (1e-4, 3e-4, 1e-3, 3e-3):
        rules.append({"kind": "ww", "gamma": g, "label": f"WW risk aversion {g:.0e}"})
    for book in (spy, long_spy, googl):
        compare(book, rules, a.paths, a.steps, 0.0, a.seed)
    compare(googl, rules, a.paths, a.steps, -0.08, a.seed)


if __name__ == "__main__":
    main()
