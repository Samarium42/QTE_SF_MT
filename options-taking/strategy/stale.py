"""Stale student quotes: where another team's resting option order is on the wrong side of
the contract's current fair mid, so crossing it is worth more than the taker fee.

The fair mid is the middle of the exchange's wall. The wall is the live option quote plus
and minus the same padding (Rule 6002), rebuilt every 100 ms, so its middle is the live
mid, which the official mark follows within a second. When the underlying moves, the wall
moves with it on the next tick; a student quote priced off the old spot does not until
its owner amends it, and their amend has the same 150 ms delay as our order. Trading
against such a quote, stale or not, is ordinary direct aggression (Rule 2006).

Every price here is an int of micro-dollars.
"""

from dataclasses import dataclass

FLOOR = 10_000  # Rule 6002 floors the wall bid at one cent, which breaks its symmetry


@dataclass(frozen=True)
class Take:
    side: int  # +1 buy (lifting student offers), -1 sell (hitting student bids)
    price: int  # the limit: the worst student level worth taking
    size: int  # contracts across the levels worth taking, net of our own orders
    fair: int  # wall mid
    edge: int  # micro-dollars per share at the best level, after the fee


def find_takes(
    *,
    wall_bid: int,
    wall_ask: int,
    student_bids: list[tuple[int, int]],
    student_asks: list[tuple[int, int]],
    own_bids: dict[int, int],
    own_asks: dict[int, int],
    fee: int,
    min_edge: int,
) -> list[Take]:
    """The student levels worth crossing on one contract. A level is worth it when its
    price beats the wall mid by at least `min_edge` per share after `fee` per share.
    `own_bids` / `own_asks` map price to our team's resting size there, which the public
    book counts as student size too."""
    if wall_bid <= FLOOR or wall_ask <= wall_bid:
        return []
    fair2 = wall_bid + wall_ask  # twice the mid, to stay in whole micro-dollars
    out = []

    best_edge, worst, size = None, None, 0
    for price, qty in sorted(student_asks):
        edge2 = fair2 - 2 * price - 2 * fee
        qty -= own_asks.get(price, 0)
        if edge2 < 2 * min_edge or qty <= 0:
            continue
        best_edge = edge2 // 2 if best_edge is None else best_edge
        worst, size = price, size + qty
    if worst is not None:
        out.append(Take(1, worst, size, fair2 // 2, best_edge))

    best_edge, worst, size = None, None, 0
    for price, qty in sorted(student_bids, reverse=True):
        edge2 = 2 * price - fair2 - 2 * fee
        qty -= own_bids.get(price, 0)
        if edge2 < 2 * min_edge or qty <= 0:
            continue
        best_edge = edge2 // 2 if best_edge is None else best_edge
        worst, size = price, size + qty
    if worst is not None:
        out.append(Take(-1, worst, size, fair2 // 2, best_edge))
    return out
