import asyncio
import random
from math import log

from qte_sdk.contract.v1 import market_data_pb2 as md
from qte_sdk.contract.v1.common_pb2 import MarketSessionPhase
from qte_sdk.options import OPTION_GREEKS_VALID, OPTION_TRADING

import strategy.svi_bot as svi_module
from strategy.bs import black76
from strategy.config import SviSettings
from strategy.stale import find_takes
from strategy.svi import Svi, SviPoint, butterfly_ok, fit_svi
from strategy.svi_bot import SviTrader

M = 1_000_000
T = 10 / 365
F = 650.0
TRUE = Svi(0.0004, 0.02, -0.6, 0.005, 0.03, T, 0.0, 0, 0.0, 0.0)  # a skewed short-dated smile
PUT = "SPY261120P00630000"


def smile(noise=0.0, kink=None, seed=1):
    rng = random.Random(seed)
    pts = []
    for strike in range(600, 701, 5):
        k = log(strike / F)
        iv = TRUE.iv(k) + rng.gauss(0, noise) + (kink[1] if kink and kink[0] == strike else 0.0)
        pts.append(SviPoint(k, iv, black76(strike >= F, F, strike, T, TRUE.iv(k) / 100).vega))
    return pts


# ---------- the fit ----------

def test_fit_finds_the_curve_it_was_made_from():
    fit = fit_svi(smile(noise=0.05), T)
    assert fit is not None and fit.used == 21
    gaps = [abs(fit.iv(p.k) - TRUE.iv(p.k)) for p in smile()]
    assert max(gaps[2:-2]) < 0.06  # within the 0.05 noise near the money
    assert max(gaps) < 0.2  # the far wings have little vega, so the fit trusts them less
    assert -0.7 < fit.rho < -0.5 and fit.rmse < 0.1


def test_one_strike_out_of_line_is_left_out_of_the_curve():
    pts = smile(noise=0.05, kink=(640, 1.5))
    fit = fit_svi(pts, T)
    assert fit.used == 20
    k = log(640 / F)
    assert abs(fit.iv(k) - TRUE.iv(k)) < 0.05  # the curve is where 640 should be...
    assert abs(pts[8].iv - fit.iv(k) - 1.5) < 0.1  # ...so 640 shows up 1.5 vol points rich


def test_too_few_strikes_is_no_fit():
    assert fit_svi(smile()[:5], T) is None


def test_butterfly_arbitrage_is_caught():
    # Gatheral and Jacquier (2014), section 5: a raw SVI slice with butterfly arbitrage
    bad = Svi(-0.0410, 0.1331, 0.3060, 0.3586, 0.4153, 1.0, 0.0, 0, 0.0, 0.0)
    assert not butterfly_ok(bad, -1.5, 1.5)
    assert butterfly_ok(fit_svi(smile(), T), -0.08, 0.08)


def test_takes_against_a_given_fair_value():
    # fair 4.80 instead of the wall mid 5.00: a 4.95 student bid is now worth hitting
    [t] = find_takes(wall_bid=4_700_000, wall_ask=5_300_000, student_bids=[(4_950_000, 3)],
                     student_asks=[], own_bids={}, own_asks={}, fee=3_000, min_edge=30_000,
                     fair=4_800_000)
    assert t.side == -1 and t.price == 4_950_000 and t.edge == 147_000


# ---------- the strategy ----------

class FakeSession:
    instrument_table = None


def make(tmp_path, monkeypatch, gap, student_bid=None):
    """A trader with a fitted SPY 20 Nov curve, and PUT `gap` vol points off it."""
    monkeypatch.setattr(svi_module.SviTrader, "status_path", tmp_path / "svi-status.json")
    tr = SviTrader(FakeSession(), "sf_svi_rv_v1", SviSettings(), dry_run=True)
    tr.connected = tr.account_known = True
    tr.underlyings = ["SPY"]
    tr.state = md.SessionState(state=MarketSessionPhase.OPEN, session_date="2026-11-10",
                               close_time=10**12, grid_time=0)
    fit = fit_svi(smile(), T)
    tr.fits[("SPY", svi_module.date(2026, 11, 20))] = fit
    iv = fit.iv(log(630 / F)) + gap
    tr.marks.update({"SPY": 650 * M, PUT: 5 * M})
    tr.greeks.update(md.OptionGreeks(instrument=PUT, grid_time=1, status=OPTION_GREEKS_VALID,
                                     forward=650 * M, implied_vol=round(iv * M), vega=200_000,
                                     delta=-300_000_000_000, gamma=10_000_000_000))
    book = md.Book(instrument=PUT, grid_time=1, condition=md.LIVE, trading_state=OPTION_TRADING,
                   bid_levels=[md.WallLevel(price=4_700_000, size=20)],
                   ask_levels=[md.WallLevel(price=5_300_000, size=20)],
                   student_bid_levels=[md.StudentLevel(price=student_bid, size=3)] if student_bid else [])
    return tr, book


def test_rich_strike_is_valued_below_its_mark_and_only_sold(tmp_path, monkeypatch):
    tr, _ = make(tmp_path, monkeypatch, gap=1.0)
    fair, side = tr.value(PUT)
    assert side == -1 and abs(fair - 4_800_000) <= 1  # mark 5.00 less vega 0.20 x 1 vol point


def test_small_gap_is_left_alone(tmp_path, monkeypatch):
    tr, _ = make(tmp_path, monkeypatch, gap=0.3)
    assert tr.value(PUT) is None


def test_in_the_money_contract_is_left_alone(tmp_path, monkeypatch):
    tr, _ = make(tmp_path, monkeypatch, gap=1.0)
    tr.greeks.update(md.OptionGreeks(instrument=PUT, grid_time=2, status=OPTION_GREEKS_VALID,
                                     forward=620 * M, implied_vol=20 * M, vega=200_000))
    assert tr.value(PUT) is None


def test_hits_a_student_bid_above_the_curve_price(tmp_path, monkeypatch):
    tr, book = make(tmp_path, monkeypatch, gap=1.0, student_bid=4_950_000)
    asyncio.run(tr.on_book(book))
    [row] = tr.recent
    assert row["side"] == "SELL" and row["price"] == 4.95 and row["size"] == 3
    assert abs(row["gap"] - 1.0) < 1e-6


def test_wall_is_crossed_only_when_the_gap_pays_for_it(tmp_path, monkeypatch):
    tr, book = make(tmp_path, monkeypatch, gap=1.0)  # curve 4.80, wall bid 4.70: not worth it
    asyncio.run(tr.on_book(book))
    assert not tr.recent
    tr, book = make(tmp_path, monkeypatch, gap=3.0)  # curve 4.40: selling at 4.70 is 0.30 over
    asyncio.run(tr.on_book(book))
    [row] = tr.recent
    assert row["side"] == "SELL" and row["price"] == 4.70 and row["size"] == 5  # max_order


def test_cheap_strike_is_not_sold_even_at_a_high_bid(tmp_path, monkeypatch):
    # under the curve: the only trade allowed is a buy, so a juicy bid is the picker's job
    tr, book = make(tmp_path, monkeypatch, gap=-1.0, student_bid=5_100_000)
    asyncio.run(tr.on_book(book))
    assert not tr.recent
