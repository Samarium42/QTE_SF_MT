"""Option structures: their legs, and their price at the wall against a Black-76 fair value.

A structure is bought (direction +1) or sold (-1) `qty` times. Each leg is one contract with
a signed ratio; buying a leg crosses at the wall offer and selling at the wall bid, each
paying the taker fee.
"""

from dataclasses import dataclass

STRUCTURES = {
    # name: (number of strikes, [(right, strike index, ratio)])
    "call": (1, [("C", 0, 1)]),
    "put": (1, [("P", 0, 1)]),
    "straddle": (1, [("C", 0, 1), ("P", 0, 1)]),
    "strangle": (2, [("P", 0, 1), ("C", 1, 1)]),
    "risk_reversal": (2, [("P", 0, -1), ("C", 1, 1)]),
    "call_spread": (2, [("C", 0, 1), ("C", 1, -1)]),
    "put_spread": (2, [("P", 1, 1), ("P", 0, -1)]),
    "call_butterfly": (3, [("C", 0, 1), ("C", 1, -2), ("C", 2, 1)]),
    "put_butterfly": (3, [("P", 0, 1), ("P", 1, -2), ("P", 2, 1)]),
}


@dataclass(frozen=True)
class LegSpec:
    right: str  # "C" or "P"
    strike: int  # micro-dollars
    qty: int  # signed contracts: positive buys


def legs(name: str, strikes: list[int], direction: int, qty: int) -> list[LegSpec]:
    """The legs of `qty` of a structure; strikes ascending, as many as it needs."""
    if name not in STRUCTURES:
        raise ValueError(f"unknown structure {name!r}")
    if direction not in (1, -1) or qty <= 0:
        raise ValueError("direction must be +1 or -1 and qty positive")
    count, shape = STRUCTURES[name]
    ks = sorted(strikes)
    if len(ks) != count or len(set(ks)) != count:
        raise ValueError(f"{name} needs {count} distinct strike(s)")
    return [LegSpec(right, ks[i], direction * ratio * qty) for right, i, ratio in shape]


def execution_order(costs: list[tuple[LegSpec, float]]) -> list[LegSpec]:
    """Legs to send, the biggest first (by premium at risk): the hardest leg to fill is
    tried while nothing else has been done yet."""
    return [leg for leg, _ in sorted(costs, key=lambda c: -abs(c[1]))]
