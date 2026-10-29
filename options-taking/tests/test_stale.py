from qte_sdk.units import to_micros

from strategy.stale import find_takes

FEE = to_micros("0.003")  # $0.30 per contract
EDGE = to_micros("0.02")


def m(x: str) -> int:
    return to_micros(x)


def takes(bids=(), asks=(), own_bids=None, own_asks=None, wall=("4.88", "5.22")):
    return find_takes(wall_bid=m(wall[0]), wall_ask=m(wall[1]),
                      student_bids=[(m(p), q) for p, q in bids],
                      student_asks=[(m(p), q) for p, q in asks],
                      own_bids=own_bids or {}, own_asks=own_asks or {}, fee=FEE, min_edge=EDGE)


def test_nothing_when_student_quotes_are_fair():
    assert takes(bids=[("5.00", 5)], asks=[("5.10", 5)]) == []


def test_lifts_an_offer_below_the_wall_mid():
    # wall mid 5.05; offer 5.00 is 0.05 under, 0.047 after the fee
    [t] = takes(asks=[("5.00", 5), ("5.02", 3), ("5.04", 4)])
    assert t.side == 1 and t.fair == m("5.05")
    assert t.price == m("5.02") and t.size == 8  # 5.04 leaves 0.007, under the 0.02 bar
    assert t.edge == m("0.047")


def test_hits_a_bid_above_the_wall_mid():
    [t] = takes(bids=[("5.12", 2), ("5.06", 9)])
    assert t.side == -1 and t.price == m("5.12") and t.size == 2


def test_our_own_orders_are_not_taken():
    assert takes(asks=[("5.00", 5)], own_asks={m("5.00"): 5}) == []
    [t] = takes(asks=[("5.00", 5)], own_asks={m("5.00"): 2})
    assert t.size == 3


def test_stale_quote_outside_the_wall_is_still_taken():
    # the underlying fell and a student bid was left above the whole wall
    [t] = takes(bids=[("5.30", 4)])
    assert t.side == -1 and t.price == m("5.30")


def test_no_takes_on_a_floored_wall():
    assert takes(asks=[("0.01", 5)], wall=("0.01", "0.09")) == []
