"""Where to rest passive option quotes so that live prints fill them.

The edge (rulebook 2007, 6002): an eligible live print fills a resting student order at
that order's own limit price, when a bid is at least P - kappa*delta for a sell print (an
offer at most P + kappa*delta for a buy print), where P is the print price, delta the
per-side padding and kappa = 0.45 from Revision 4 (28 October 2026; 0.25 before). Live sell
prints happen around the live bid, which is below the official mark (the live mid). So a
bid at about live_bid - kappa*delta qualifies for those prints and fills below the mark:
the gap is edge against the mark, plus the $0.20 maker rebate.

Every price here is an int of micro-dollars. Nothing here uses float.
"""

from dataclasses import dataclass

CENT = 10_000  # Rule 6002 rounds option padding up to a whole cent
MIN_PADDING = 2 * CENT  # Rule 6002: padding is at least $0.02 per side
KAPPA_PCT = 45  # Rule 2007 trade-based eligibility coefficient, in percent (Revision 4)


def ceil_to(value: int, step: int) -> int:
    return -(-value // step) * step


def floor_to(value: int, step: int) -> int:
    return (value // step) * step


def reach(pad: int) -> int:
    """How far outside a print price a resting order may sit and still qualify, rounded
    down so an order placed this far out always qualifies."""
    return pad * KAPPA_PCT // 100


def padding(vega: int) -> int:
    """Per-side wall padding, max($0.02, 0.20 * vega) rounded up to a cent (Rule 6002).

    `vega` is the published vega in micro-dollars per share per volatility point."""
    return ceil_to(max(MIN_PADDING, ceil_to(vega, 5) // 5), CENT)


@dataclass(frozen=True)
class Quote:
    bid: int | None
    ask: int | None


def passive_quotes(
    *,
    wall_bid: int,
    wall_ask: int,
    mark: int,
    vega: int,
    tick: int,
    min_edge: int,
    improve_ticks: int = 0,
) -> Quote:
    """The bid and offer to rest on one contract, or None for a side not worth quoting.

    The live quote is inferred from the wall: wall = live -/+ padding, unless the exchange
    widened the band to its minimum width, in which case the live quote is inside it and
    the mark (live mid) bounds it. Each side sits kappa paddings outside the live quote,
    the furthest point that still qualifies for live prints, then is clipped so it keeps at
    least `min_edge` against the mark and rests strictly inside the wall."""
    pad = padding(vega)
    out = reach(pad)
    live_bid = min(wall_bid + pad, mark)
    live_ask = max(wall_ask - pad, mark)

    bid: int | None = ceil_to(live_bid - out, tick) + improve_ticks * tick
    bid = min(bid, floor_to(mark - min_edge, tick))
    if bid <= wall_bid or bid < tick:
        bid = None

    ask: int | None = floor_to(live_ask + out, tick) - improve_ticks * tick
    ask = max(ask, ceil_to(mark + min_edge, tick))
    if ask >= wall_ask:
        ask = None

    if bid is not None and ask is not None and bid >= ask:
        return Quote(None, None)
    return Quote(bid, ask)


def is_inside(price: int, wall_bid: int, wall_ask: int) -> bool:
    """Whether a resting price is strictly inside the wall, so not stale (Rule 2006)."""
    return wall_bid < price < wall_ask
