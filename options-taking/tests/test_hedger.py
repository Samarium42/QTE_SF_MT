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
