"""San Francisco, Options Taking: SVI rich/cheap on SPY and GOOGL options.

Every couple of seconds it fits an SVI curve (strategy/svi.py) through each expiry's
published implied vols, out-of-the-money strikes only. A strike whose own vol sits well
above the curve is rich compared with its neighbours, and one well below is cheap. When a
contract is at least `min_vol_gap` vol points off the curve, it sells the rich one or buys
the cheap one, crossing a student quote or the wall, but only at a price that still beats
the curve's price by `min_edge` per share after the taker fee. The curve's price is the
mark moved by vega times the vol gap, so it uses only the exchange's own numbers.

The bet is that kinks in the smile get smoothed out: the official mark moves back towards
the curve, and the position is marked up. If a kink stays (real flow at that strike), the
position is just held to the 3-day close at the mark, fee-free, with its delta hedged by
the hedger. Position caps are small for that reason.

It is its own strategy with its own strat_id, and it reuses the picker's machinery
(strategy/stale_bot.py): connection, risk checks, cancels, stop from the dashboard. It does
not hedge: run the hedger alongside it.

    python -m strategy.svi_bot --strat-id <registered id> --dry-run   # fits and logs, sends nothing
    python -m strategy.svi_bot --strat-id <registered id>             # trades
"""

import argparse
import asyncio
import logging
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from math import log as ln
from pathlib import Path
from zoneinfo import ZoneInfo

import qte_sdk.logs
from qte_sdk.contract.v1.common_pb2 import BUY
from qte_sdk.contract.v1.order_events_pb2 import Execution
from qte_sdk.options import (
    OPTION_GREEKS_VALID,
    chain_contracts,
    expiry_date,
    parse_option_symbol,
    vol_to_decimal,
)
from qte_sdk.units import to_decimal

from strategy.bot import no_quickedit
from strategy.config import SviSettings
from strategy.stale_bot import Picker, run
from strategy.svi import Svi, SviPoint, fit_svi

log = logging.getLogger("sf_svi")

STATUS = Path("logs/svi-status.json")  # read by the dashboard
NEW_YORK = ZoneInfo("America/New_York")


class SviTrader(Picker):
    log = log
    status_path = STATUS
    stop_key = "svi"
    fair_name = "SVI value"
    take_wall = True  # a rich or cheap strike is worth crossing the wall for, if the gap pays it

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fits: dict[tuple[str, date], Svi] = {}
        self.fit_at: dict[tuple[str, date], float] = {}  # monotonic time of the last refit
        self.fit_logged: dict[tuple[str, date], float] = {}
        self.gaps: dict[str, tuple[float, float]] = {}  # contract -> (its vol, the curve's vol)
        self.take_fair: dict[str, int] = {}  # contract -> curve price when we last took it
        self.svi_edge = Decimal(0)  # dollars the fills were better than the curve, before fees

    # ---------- fitting ----------

    def years(self, expiry: date) -> float:
        close = datetime(expiry.year, expiry.month, expiry.day, 16, tzinfo=NEW_YORK)
        return max(0.0, (close - datetime.now(timezone.utc)).total_seconds() / (365.0 * 86400))

    def points(self, underlying: str, expiry: date) -> list[tuple[str, SviPoint]]:
        """Out-of-the-money contracts in one expiry with valid published Greeks."""
        out = []
        assert self.chain is not None
        for c in chain_contracts(self.chain, underlying=underlying, expiry=expiry):
            g = self.greeks.get(c.instrument)
            if (g is None or g.status != OPTION_GREEKS_VALID or not g.HasField("forward")
                    or not g.HasField("implied_vol") or not g.HasField("vega")):
                continue
            sym = parse_option_symbol(c.instrument)
            forward, strike = float(to_decimal(g.forward)), sym.strike / 1e6
            if forward <= 0 or (sym.right == "C" and strike < forward) or (sym.right == "P" and strike > forward):
                continue
            out.append((c.instrument, SviPoint(ln(strike / forward), float(vol_to_decimal(g.implied_vol)),
                                               float(to_decimal(g.vega)))))
        return out

    def refit(self, now: float) -> None:
        """Refit the one expiry that has waited longest, if it is due. One per grid tick
        keeps each tick quick (a fit takes about 10 ms)."""
        if self.chain is None or self.state is None:
            return
        today = date.fromisoformat(self.state.session_date)
        due = []
        for e in self.chain.expiries:
            key = (e.underlying, expiry_date(e))
            if e.underlying not in self.underlyings:
                continue
            if e.reducing_only or (key[1] - today).days < self.cfg.min_days:
                self.fits.pop(key, None)
                continue
            if now - self.fit_at.get(key, 0.0) >= self.cfg.refit_seconds:
                due.append((self.fit_at.get(key, 0.0), key))
        if not due:
            return
        _, key = min(due)
        self.fit_at[key] = now
        pts = self.points(*key)
        fit = fit_svi([p for _, p in pts], self.years(key[1]), self.cfg.min_points, self.cfg.outlier_vols)
        if fit is None or fit.rmse > self.cfg.max_rmse:
            if self.fits.pop(key, None) is not None or now - self.fit_logged.get(key, 0.0) > 60:
                self.fit_logged[key] = now
                self.log.info("no SVI fit for %s %s (%d strikes%s): not trading it", key[0], key[1],
                              len(pts), "" if fit is None else f", rmse {fit.rmse:.2f} vol pts")
            return
        self.fits[key] = fit
        for instrument, p in pts:
            self.gaps[instrument] = (p.iv, fit.iv(p.k))
        if now - self.fit_logged.get(key, 0.0) > 60:
            self.fit_logged[key] = now
            worst = sorted(pts, key=lambda ip: -abs(ip[1].iv - fit.iv(ip[1].k)))[:3]
            self.log.info("SVI %s %s: a=%.6f b=%.5f rho=%.3f m=%.4f sigma=%.4f, rmse %.2f vol pts "
                          "over %d strikes; furthest off: %s", key[0], key[1], fit.a, fit.b, fit.rho,
                          fit.m, fit.sigma, fit.rmse, fit.used,
                          ", ".join(f"{i} {p.iv - fit.iv(p.k):+.2f}" for i, p in worst))

    async def on_grid(self) -> None:
        self.refit(time.monotonic())
        await super().on_grid()

    # ---------- deciding ----------

    def value(self, contract: str) -> tuple[int | None, int] | None:
        """The curve's price for this contract and the side the gap says to trade: sell when
        its vol is over the curve, buy when under. None when there is no fit, the contract
        is in the money or outside the fitted strikes, or the gap is under min_vol_gap."""
        sym = parse_option_symbol(contract)
        fit = self.fits.get((sym.underlying, sym.expiry))
        g = self.greeks.get(contract)
        mark = self.marks.get(contract)
        if (fit is None or mark is None or g is None or g.status != OPTION_GREEKS_VALID
                or not g.HasField("forward") or not g.HasField("implied_vol") or not g.HasField("vega")):
            return None
        forward, strike = float(to_decimal(g.forward)), sym.strike / 1e6
        if forward <= 0 or (sym.right == "C" and strike < forward) or (sym.right == "P" and strike > forward):
            return None
        k = ln(strike / forward)
        if not fit.k_lo <= k <= fit.k_hi:
            return None
        iv = float(vol_to_decimal(g.implied_vol))
        curve = fit.iv(k)
        self.gaps[contract] = (iv, curve)
        gap = iv - curve
        if abs(gap) < self.cfg.min_vol_gap:
            return None
        fair = mark - round(g.vega * gap)  # vega is micro-dollars per share per vol point
        if fair <= 0:
            return None
        self.take_fair[contract] = fair
        return fair, (-1 if gap > 0 else 1)

    def note(self, contract: str) -> dict:
        iv, curve = self.gaps.get(contract, (0.0, 0.0))
        return {"iv": iv, "curve_iv": curve, "gap": iv - curve}

    def on_order_event(self, message: object) -> None:
        super().on_order_event(message)
        if isinstance(message, Execution) and message.strat_id == self.strat_id:
            fair = self.take_fair.get(message.instrument)
            if fair is not None:
                sign = 1 if message.side == BUY else -1
                self.svi_edge += to_decimal(sign * (fair - message.fill_price)) * 100 * message.fill_size

    # ---------- status ----------

    def status(self, now: float) -> dict:
        out = super().status(now)
        out["svi_edge"] = float(self.svi_edge)
        out["min_vol_gap"] = self.cfg.min_vol_gap
        out["fits"] = [{"underlying": u, "expiry": e.isoformat(), "a": f.a, "b": f.b, "rho": f.rho,
                        "m": f.m, "sigma": f.sigma, "rmse": f.rmse, "used": f.used,
                        "age": now - self.fit_at.get((u, e), now)}
                       for (u, e), f in sorted(self.fits.items())]
        fitted = set(self.fits)
        off = []
        for contract, (iv, curve) in self.gaps.items():
            sym = parse_option_symbol(contract)
            if (sym.underlying, sym.expiry) in fitted:
                off.append({"contract": contract, "iv": iv, "curve_iv": curve, "gap": iv - curve,
                            "position": self.positions.get(contract, 0)})
        out["off_curve"] = sorted(off, key=lambda r: -abs(r["gap"]))[:12]
        return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strat-id", required=True, help="strategy ID registered for SVI rich/cheap")
    parser.add_argument("--dry-run", action="store_true", help="fit and log what it would take; send nothing")
    parser.add_argument("--seconds", type=float, default=None, help="stop after this long")
    args = parser.parse_args(argv)
    no_quickedit()

    Path("logs").mkdir(exist_ok=True)
    qte_sdk.logs.configure()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(f"logs/svi-{date.today().isoformat()}.log")],
    )
    return asyncio.run(run(args, SviTrader, SviSettings()))


if __name__ == "__main__":
    sys.exit(main())
