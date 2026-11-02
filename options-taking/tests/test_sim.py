"""The simulated exchange (strategy/sim.py) and the quoter's stop-losses."""

import asyncio
from decimal import Decimal

from qte_sdk.contract.v1.common_pb2 import BUY, RESIDUAL, SELL, STUDENT_TO_STUDENT, TRADE_BASED_MATCH
from qte_sdk.contract.v1.market_data_pb2 import (
    LIVE,
    OPTION_GREEKS_VALID,
    Book,
    Mark,
    OptionGreeks,
    SessionState,
    StudentLevel,
    TapePrint,
    Trades,
    WallLevel,
)
from qte_sdk.units import to_micros

from strategy.config import Settings
from strategy.sim import SimBot, SimClock, SimExchange

C = "SPY261120C00770000"
VEGA = to_micros("0.60")  # padding 0.12, reach 0.054


def m(x: str) -> int:
    return to_micros(x)


def setup(fill_share: float = 1.0) -> tuple[SimExchange, SimClock]:
    clock = SimClock(1000.0)
    sim = SimExchange(clock, fill_share=fill_share, latency=0.15)
    sim.on_market(OptionGreeks(instrument=C, status=OPTION_GREEKS_VALID, vega=VEGA,
                               delta=500_000_000_000))
    sim.on_market(Book(instrument=C, bid_levels=[WallLevel(price=m("4.80"), size=10)],
                       ask_levels=[WallLevel(price=m("5.30"), size=10)]))
    sim.on_market(Mark(instrument=C, value=m("5.05"), condition=LIVE))
    sim.on_market(Mark(instrument="SPY", value=m("770"), condition=LIVE))
    return sim, clock


def sell_print(price: str, size: int, kind=RESIDUAL) -> Trades:
    return Trades(instrument=C, prints=[TapePrint(price=m(price), size=size,
                                                  aggressor_side=SELL, kind=kind)])


def test_a_print_within_reach_fills_our_bid_at_our_price_with_the_rebate():
    sim, clock = setup()
    sim.new(C, BUY, m("4.95"), 5)
    clock.t += 1
    sim.on_market(sell_print("5.00", 3))  # 4.95 >= 5.00 - 0.054
    assert sim.positions[C] == 3
    assert sim.fills[0].price == m("4.95") and sim.fills[0].kind == "print"
    assert sim.cash == 2_500_000 - 3 * 4.95 * 100 + 3 * 0.20


def test_prints_out_of_reach_too_early_or_not_live_do_not_fill():
    sim, clock = setup()
    sim.new(C, BUY, m("4.90"), 5)
    sim.on_market(sell_print("5.00", 3))  # order delay: not resting yet
    clock.t += 1
    sim.on_market(sell_print("5.00", 3))  # 4.90 < 4.946: out of reach
    sim.on_market(sell_print("4.90", 3, kind=STUDENT_TO_STUDENT))  # not a live print
    assert C not in sim.positions
    sim.on_market(sell_print("4.92", 3, kind=TRADE_BASED_MATCH))
    assert sim.positions[C] == 3


def test_fill_share_carries_fractions_per_contract():
    sim, clock = setup(fill_share=0.5)
    sim.new(C, BUY, m("4.95"), 10)
    clock.t += 1
    sim.on_market(sell_print("5.00", 3))  # 1.5 -> 1
    sim.on_market(sell_print("5.00", 3))  # 0.5 + 1.5 -> 2
    assert sim.positions[C] == 3


def test_an_order_the_wall_reaches_is_purged():
    sim, clock = setup()
    sim.new(C, SELL, m("5.20"), 5)
    sim.on_market(Book(instrument=C, bid_levels=[WallLevel(price=m("5.20"), size=10)],
                       ask_levels=[WallLevel(price=m("5.60"), size=10)]))
    assert (C, SELL) not in sim.orders


def test_crossing_another_teams_order_trades_as_taker_at_their_price():
    sim, clock = setup()
    sim.on_market(Book(instrument=C, bid_levels=[WallLevel(price=m("4.80"), size=10)],
                       ask_levels=[WallLevel(price=m("5.30"), size=10)],
                       student_ask_levels=[StudentLevel(price=m("4.97"), size=5)]))
    sim.new(C, BUY, m("5.00"), 5)
    f = sim.fills[0]
    assert (f.kind, f.price, f.size, f.fee) == ("cross", m("4.97"), 5, 1.5)


def test_hedges_take_level_one_and_wrong_side_stock_counts_as_a_breach():
    sim, clock = setup()
    sim.on_market(Book(instrument="SPY", bid_levels=[WallLevel(price=m("769.99"), size=20)],
                       ask_levels=[WallLevel(price=m("770.01"), size=20)]))
    sim.positions[C] = 10  # long calls: option delta +$385k
    assert sim.hedge("SPY", SELL, m("769.99"), 300) == 20  # level one only
    assert sim.violations == 0
    sim.hedge("SPY", BUY, m("770.01"), 20)  # back to flat
    sim.hedge("SPY", BUY, m("770.01"), 5)  # 5 long: same side as the option delta
    assert sim.violations == 1


def test_stop_losses_tighten_through_the_day_and_never_lift():
    sim, clock = setup()
    bot = SimBot(sim, Settings())
    bot.on_market_data(SessionState(session_date="2026-11-02"))
    bot.account_known = True

    def at(pnl: int) -> str:
        bot.daily_pnl = Decimal(pnl)
        bot.check_stops()
        return bot.stop

    assert at(5_000) == ""
    assert at(-9_000) == ""  # $14k below the best: not yet
    assert at(-11_000) == "drawdown"  # $16k below it
    assert at(-26_000) == "soft"
    assert at(-41_000) == "hard"
    assert at(0) == "hard"
    bot.on_market_data(SessionState(session_date="2026-11-03"))
    assert bot.stop == ""


def test_a_hard_stop_quotes_nothing():
    sim, clock = setup()
    bot = SimBot(sim, Settings())
    sim.new(C, BUY, m("4.95"), 5)
    bot.stop = "hard"
    asyncio.run(bot.cancel_all())
    assert not sim.orders
