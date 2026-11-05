import asyncio
import json
import time

from qte_sdk.contract.v1 import market_data_pb2 as md
from qte_sdk.contract.v1.common_pb2 import MarketSessionPhase
from qte_sdk.options import OPTION_GREEKS_VALID

import strategy.hedger as hedger_module
from strategy.config import HedgerSettings
from strategy.hedger import Hedger

M = 1_000_000
CALL = "SPY261120C00650000"


class FakeSession:
    instrument_table = None


def make(tmp_path, monkeypatch, positions, delta="0.5", quoter=None):
    monkeypatch.setattr(hedger_module, "QUOTER_STATUS", tmp_path / "bot-status.json")
    monkeypatch.setattr(hedger_module, "STATUS", tmp_path / "hedger-status.json")
    if quoter is not None:
        (tmp_path / "bot-status.json").write_text(json.dumps({"t": time.time(), **quoter}))
    h = Hedger(FakeSession(), "sf_delta_hedger_v1", HedgerSettings(), dry_run=True)
    h.connected = h.account_known = True
    h.underlyings = ["SPY"]
    h.positions = positions
    h.state = md.SessionState(state=MarketSessionPhase.OPEN, session_date="2026-10-29",
                              close_time=10**12, grid_time=0)
    h.marks.update({"SPY": 650 * M, CALL: 10 * M})
    h.greeks.update(md.OptionGreeks(instrument=CALL, grid_time=1, status=OPTION_GREEKS_VALID,
                                    delta=int(float(delta) * 10**12)))
    h.books.update(md.Book(instrument="SPY", grid_time=1, condition=md.LIVE,
                           bid_levels=[md.WallLevel(price=649_900_000 - i * 10_000, size=20) for i in range(10)],
                           ask_levels=[md.WallLevel(price=650_100_000 + i * 10_000, size=20) for i in range(10)]))
    return h


def test_long_calls_are_hedged_by_selling_stock_down_the_ladder(tmp_path, monkeypatch):
    # 40 calls x 100 x 0.5 x $650 = $1.3M of delta, well past threshold and buffer
    h = make(tmp_path, monkeypatch, {CALL: 40})
    asyncio.run(h.on_grid())
    [row] = h.recent
    assert row["side"] == "SELL" and row["underlying"] == "SPY"
    # 3 bp of $649.90 is about 19 cents, so all ten 1-cent levels (to 649.81) are inside
    assert row["price"] == 649.81 and row["size"] == 200


def test_ladder_walk_stops_at_the_slippage_cap(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch, {CALL: 40})
    h.books.update(md.Book(instrument="SPY", grid_time=2, condition=md.LIVE,
                           bid_levels=[md.WallLevel(price=649_900_000 - i * 100_000, size=20) for i in range(10)],
                           ask_levels=[md.WallLevel(price=650_100_000, size=20)]))
    asyncio.run(h.on_grid())
    [row] = h.recent
    assert row["price"] == 649.80 and row["size"] == 40  # 649.70 is past 3 bp


def test_small_delta_is_left_alone(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch, {CALL: 2})  # $65k
    asyncio.run(h.on_grid())
    assert not h.recent


def test_wrong_side_stock_is_unwound_even_under_threshold(tmp_path, monkeypatch):
    # long calls (positive delta) but long stock too: stock no longer qualifies
    h = make(tmp_path, monkeypatch, {CALL: 1, "SPY": 50})
    asyncio.run(h.on_grid())
    [row] = h.recent
    assert row["side"] == "SELL" and row["size"] == 50


def test_nothing_sent_while_the_quoter_also_hedges(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch, {CALL: 40}, quoter={"dry_run": False, "hedging": True})
    asyncio.run(h.on_grid())
    assert h.quoter_hedging and not h.recent


def test_quoter_with_no_hedge_does_not_block(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch, {CALL: 40}, quoter={"dry_run": False, "hedging": False})
    asyncio.run(h.on_grid())
    assert not h.quoter_hedging and len(h.recent) == 1


# ---------- Whalley-Wilmott bands ----------

from dataclasses import replace
from decimal import Decimal as D

from strategy.risk import band_hedge_shares, ww_band


def band(g, spot="765", cost="3.5e-4", ra="1e-4"):
    return ww_band(D(g), D(spot), D(cost), risk_aversion=D(ra), floor=D(75_000), cap=D(600_000))


def test_ww_band_matches_the_formula():
    # (1.5 * 3.5e-4 * 765 * 104^2 / 1e-4) ** (1/3) = 351.4 shares, x $765
    assert abs(band(104) - D(268_800)) < 500


def test_ww_band_widens_with_gamma_and_cost_and_narrows_with_risk_aversion():
    assert band(200) > band(104)
    assert band(104, cost="7e-4") > band(104)
    assert band(104, ra="1e-3") < band(104)
    assert band(-104) == band(104)  # short gamma, same band


def test_ww_band_is_held_between_floor_and_cap():
    assert band(0) == 75_000
    assert band(10_000) == 600_000


def test_inside_the_band_nothing_trades():
    # options +$400k, stock -$100k: net $300k inside a $350k band
    assert band_hedge_shares(D(400_000), -1000, D(100), band=D(350_000), rehedge_to=D(1),
                             max_order=D(400_000), tolerance=D(75_000)) == 0


def test_outside_the_band_trades_back_to_its_edge():
    # net $500k, band $200k: sell $300k = 3000 shares, leaving net at the edge
    n = band_hedge_shares(D(500_000), 0, D(100), band=D(200_000), rehedge_to=D(1),
                          max_order=D(400_000), tolerance=D(75_000))
    assert n == -3000


def test_rehedge_to_goes_past_the_edge():
    n = band_hedge_shares(D(500_000), 0, D(100), band=D(200_000), rehedge_to=D("0.5"),
                          max_order=D(400_000), tolerance=D(75_000))
    assert n == -4000


def test_over_hedge_is_unwound_to_the_near_edge_within_qualifying_room():
    # options +$100k, stock -$400k is outside the qualifying band (|H| <= 100k + 75k): fix that first
    n = band_hedge_shares(D(100_000), -4000, D(100), band=D(75_000), rehedge_to=D(1),
                          max_order=D(400_000), tolerance=D(75_000))
    assert n == 4000 - 1750
    # options +$300k, stock -$340k: net -$40k outside a $30k band, buy back to the -$30k edge
    n = band_hedge_shares(D(300_000), -3400, D(100), band=D(30_000), rehedge_to=D(1),
                          max_order=D(400_000), tolerance=D(75_000))
    assert n == 100
    # options +$300k, stock -$370k: legal (under 300k + 75k) but the edge (-$360k) is past the
    # half-tolerance margin (300k + 37.5k), so it trims to -$337.5k
    n = band_hedge_shares(D(300_000), -3700, D(100), band=D(60_000), rehedge_to=D(1),
                          max_order=D(400_000), tolerance=D(75_000))
    assert n == 325


def test_band_order_is_capped():
    n = band_hedge_shares(D(2_000_000), 0, D(100), band=D(100_000), rehedge_to=D(1),
                          max_order=D(400_000), tolerance=D(75_000))
    assert n == -4000


def test_hedger_uses_the_gamma_band(tmp_path, monkeypatch):
    # 10 calls, delta 0.5 at $650 = $325k; gamma 0.01 x 1000 = 10 shares/$
    h = make(tmp_path, monkeypatch, {CALL: 10})
    h.greeks.update(md.OptionGreeks(instrument=CALL, grid_time=2, status=OPTION_GREEKS_VALID,
                                    delta=int(0.5 * 10**12), gamma=int(0.01 * 10**12)))
    asyncio.run(h.on_grid())
    v = h.view["SPY"]
    assert v["gamma_shares"] == 10
    # (1.5 x cost x 650 x 100 / 1e-3)^(1/3) x 650 is under the floor, so the band is $75k
    assert v["band"] == 75_000
    # the band sells $325k down to its edge
    assert v["want"] == -384  # (325k - 75k) / 650
    [row] = h.recent
    assert row["side"] == "SELL" and row["size"] == 200  # all the test wall shows within 3 bp


def test_fixed_mode_still_available(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch, {CALL: 40})
    h.cfg = replace(h.cfg, band_mode="fixed")
    asyncio.run(h.on_grid())
    assert h.view["SPY"]["band"] == 150_000
