from datetime import date
from decimal import Decimal

from qte_sdk.units import to_micros

from strategy.pricing import padding, passive_quotes
from strategy.risk import Leg, SoftLimits, adds_risk, hedge_shares, measure, vega_cap

TICK = to_micros("0.01")
EDGE = to_micros("0.01")


def m(x: str) -> int:
    return to_micros(x)


def test_padding_floor_and_vega_scaling():
    assert padding(m("0.05")) == m("0.02")  # 0.2 * 0.05 = 0.01 < 0.02 floor
    assert padding(m("0.60")) == m("0.12")
    assert padding(m("0.61")) == m("0.13")  # 0.122 rounds up to a cent


def test_quotes_sit_kappa_paddings_outside_the_live_quote():
    # live 5.00 / 5.10, mark 5.05, vega 0.60 -> pad 0.12, reach 0.45 * 0.12 = 0.054
    q = passive_quotes(wall_bid=m("4.88"), wall_ask=m("5.22"), mark=m("5.05"),
                       vega=m("0.60"), tick=TICK, min_edge=EDGE)
    assert q.bid == m("4.95")  # ceil(5.00 - 0.054) to a cent: still qualifies
    assert q.ask == m("5.15")  # floor(5.10 + 0.054)


def test_quotes_keep_edge_when_band_was_widened():
    # tight live market, padding 0.02, band widened to 8 cents around mark 1.00
    q = passive_quotes(wall_bid=m("0.96"), wall_ask=m("1.04"), mark=m("1.00"),
                       vega=m("0.05"), tick=TICK, min_edge=EDGE)
    assert q.bid is not None and q.bid <= m("0.99") and q.bid > m("0.96")
    assert q.ask is not None and q.ask >= m("1.01") and q.ask < m("1.04")


def test_no_quote_when_wall_leaves_no_room():
    q = passive_quotes(wall_bid=m("0.01"), wall_ask=m("0.03"), mark=m("0.02"),
                       vega=m("0.01"), tick=TICK, min_edge=EDGE)
    assert q.bid is None and q.ask is None


def test_mark_off_tick_is_handled():
    q = passive_quotes(wall_bid=m("2.38"), wall_ask=m("2.67"), mark=m("2.525"),
                       vega=m("0.50"), tick=TICK, min_edge=EDGE)
    assert q.bid is not None and q.bid % TICK == 0 and q.bid <= m("2.515")
    assert q.ask is not None and q.ask % TICK == 0 and q.ask >= m("2.535")


def leg(qty: int, vega="0.75", gamma="0.014", delta="0.5") -> Leg:
    return Leg("SPY261120C00650000", "SPY", date(2026, 11, 20), qty, Decimal("10"),
               Decimal(delta), Decimal(gamma), Decimal(vega))


SPOTS = {"SPY": Decimal("650")}
LIMITS = SoftLimits(gross=Decimal("1875000"), net_delta=Decimal("750000"), v0=Decimal("7500"),
                    gamma=Decimal("720000"), today=date(2026, 10, 26))


def test_vega_cap_scales_with_tenor():
    assert vega_cap(Decimal(15000), date(2026, 10, 26), date(2026, 11, 25)) > Decimal(14000)
    short = vega_cap(Decimal(15000), date(2026, 10, 26), date(2026, 10, 28))
    assert short == vega_cap(Decimal(15000), date(2026, 10, 26), date(2026, 11, 4))


def test_measure_dollar_greeks():
    e = measure([leg(10)], SPOTS)
    assert e.option_delta["SPY"] == Decimal("10") * 100 * Decimal("0.5") * 650
    assert e.gross == Decimal(10 * 100 * 10)


def test_adds_risk_blocks_growth_past_vega_cap_but_allows_reduction():
    big = leg(95)  # 95 * 100 * 0.75 = 7125 vega, near the ~6.8k soft cap
    before = measure([big], SPOTS)
    stock = {"SPY": -before.option_delta["SPY"]}  # delta hedged
    assert adds_risk(before, big, 5, SPOTS, LIMITS, stock)
    assert not adds_risk(before, big, -5, SPOTS, LIMITS, stock)


def test_hedge_offsets_delta_and_stays_qualifying():
    # long 10 calls, delta 0.5, spot 650: D = $325k -> sell 500 shares
    d = Decimal(10 * 100) * Decimal("0.5") * 650
    kw = dict(threshold=Decimal(100000), max_order=Decimal(400000), tolerance=Decimal(100000))
    assert hedge_shares(d, 0, Decimal(650), **kw) == -500
    assert hedge_shares(d, -500, Decimal(650), **kw) == 0
    assert hedge_shares(Decimal(50000), 0, Decimal(650), **kw) == 0  # under threshold
    # Never on the same side as the option delta: flat options unwind a short hedge.
    assert hedge_shares(Decimal(0), -500, Decimal(650), **kw) == 500
    # Short stock against a short option delta is not a hedge, even under the threshold.
    assert hedge_shares(Decimal(-46000), -33, Decimal(770), **kw) == 33
    # With a buffer only the delta beyond it is hedged, so a fill cannot strand the stock.
    assert hedge_shares(d, 0, Decimal(650), buffer=Decimal(200000), **kw) == -192
    assert hedge_shares(Decimal(150000), 0, Decimal(650), buffer=Decimal(200000), **kw) == 0


from strategy.arb import Strike, scan  # noqa: E402

FEE = m("0.003")


def test_scan_finds_nothing_in_a_consistent_chain():
    chain = [Strike(m("100"), m("5.00"), m("5.10"), m("2.00"), m("2.10")),
             Strike(m("105"), m("2.40"), m("2.50"), m("4.40"), m("4.50")),
             Strike(m("110"), m("1.00"), m("1.10"), m("8.00"), m("8.10"))]
    assert scan(chain, FEE) == []


def test_scan_finds_inverted_vertical_and_cheap_butterfly():
    # Natenberg p. 235: 95/100/105 calls at 8.00 / 4.80 / 1.60 make a free butterfly;
    # make it cost less than nothing, and the 100 call bid above the 95 call ask.
    chain = [Strike(m("95"), m("7.90"), m("8.00"), None, None),
             Strike(m("100"), m("4.90"), m("5.00"), None, None),
             Strike(m("105"), m("1.50"), m("1.60"), None, None)]
    kinds = {o.kind for o in scan(chain, FEE)}
    assert kinds == {"butterfly"}
    chain[1] = Strike(m("100"), m("8.10"), m("8.20"), None, None)
    assert "vertical" in {o.kind for o in scan(chain, FEE)}

