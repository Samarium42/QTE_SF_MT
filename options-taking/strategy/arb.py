"""Model-free no-arbitrage checks on one expiry's executable quotes (Natenberg, ch. 11).

Each check prices a spread at the side we would trade (buy at the ask, sell at the bid) and
reports it only when it beats the bound by more than the fees. None needs a pricing model,
an interest rate or a dividend: for strikes K1 < K2 < K3 of one expiry,

- a call vertical C(K1) - C(K2) and a put vertical P(K2) - P(K1) lie in [0, K2 - K1];
- a butterfly (calls or puts) is worth at least zero;
- a box, the call vertical plus the put vertical, lies in [0, K2 - K1].
"""

from dataclasses import dataclass
from decimal import Decimal


def _k(micros: int) -> str:
    return f"{Decimal(micros) / 1_000_000:f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Strike:
    strike: int  # micro-dollars
    call_bid: int | None
    call_ask: int | None
    put_bid: int | None
    put_ask: int | None


@dataclass(frozen=True)
class Opportunity:
    kind: str
    legs: str  # human-readable, e.g. "+C750 -C755"
    profit: int  # micro-dollars per share, after fees


def _vertical(lo, hi, width, fee, sign):
    """lo/hi are (bid, ask) of the option worth more and the one worth less."""
    out = []
    lo_bid, lo_ask = lo
    hi_bid, hi_ask = hi
    if lo_ask is not None and hi_bid is not None:  # buy the spread
        p = hi_bid - lo_ask - 2 * fee  # receive this now, spread worth >= 0
        if p > 0:
            out.append(Opportunity("vertical", sign[0], p))
    if lo_bid is not None and hi_ask is not None:  # sell the spread
        p = lo_bid - hi_ask - width - 2 * fee  # spread worth <= width
        if p > 0:
            out.append(Opportunity("vertical", sign[1], p))
    return out


def scan(strikes: list[Strike], fee: int) -> list[Opportunity]:
    """Every violation in one expiry. `fee` is the taker fee per share, per leg."""
    s = sorted(strikes, key=lambda x: x.strike)
    found: list[Opportunity] = []
    for a, b in zip(s, s[1:]):
        w = b.strike - a.strike
        k1, k2 = _k(a.strike), _k(b.strike)
        found += _vertical((a.call_bid, a.call_ask), (b.call_bid, b.call_ask), w, fee,
                           (f"+C{k1} -C{k2}", f"-C{k1} +C{k2}"))
        found += _vertical((b.put_bid, b.put_ask), (a.put_bid, a.put_ask), w, fee,
                           (f"+P{k2} -P{k1}", f"-P{k2} +P{k1}"))
        legs = (a.call_ask, b.call_bid, b.put_ask, a.put_bid)
        if None not in legs:  # buy the box for at most zero... or less than nothing
            cost = a.call_ask - b.call_bid + b.put_ask - a.put_bid
            if -cost - 4 * fee > 0:
                found.append(Opportunity("box", f"+C{k1} -C{k2} +P{k2} -P{k1}", -cost - 4 * fee))
        legs = (a.call_bid, b.call_ask, b.put_bid, a.put_ask)
        if None not in legs:  # sell the box for more than the width
            got = a.call_bid - b.call_ask + b.put_bid - a.put_ask
            if got - w - 4 * fee > 0:
                found.append(Opportunity("box", f"-C{k1} +C{k2} -P{k2} +P{k1}", got - w - 4 * fee))
    for a, b, c in zip(s, s[1:], s[2:]):
        # Unequal spacing: weights so the payoff is a tent, (c-b) * A - (c-a) * B + (b-a) * C.
        wa, wb, wc = c.strike - b.strike, c.strike - a.strike, b.strike - a.strike
        for kind, (ab, bb, cb) in (
            ("C", ((a.call_ask, a.call_bid), (b.call_ask, b.call_bid), (c.call_ask, c.call_bid))),
            ("P", ((a.put_ask, a.put_bid), (b.put_ask, b.put_bid), (c.put_ask, c.put_bid))),
        ):
            if ab[0] is None or bb[1] is None or cb[0] is None:
                continue
            cost = (wa * ab[0] - wb * bb[1] + wc * cb[0]) / wb  # per unit of the middle leg
            p = int(-cost) - 4 * fee
            if p > 0:
                ks = [_k(x.strike) for x in (a, b, c)]
                found.append(Opportunity("butterfly", f"{kind} {ks[0]}/{ks[1]}/{ks[2]}", p))
    return found
