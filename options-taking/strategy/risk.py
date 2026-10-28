"""The Options Taking book's exposures, measured the way Rule 6005 measures them.

Greeks and money are Decimal (dollars); quantities are whole contracts or shares. The
exchange's own limits come from config.Limits; the bot keeps to a fraction of them.
"""

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

MULTIPLIER = 100  # shares per option contract
MIN_TENOR = Decimal(9) / Decimal(365)
ONE_MONTH = Decimal(1) / Decimal(12)


@dataclass(frozen=True)
class Leg:
    """One option position with the inputs its exposures need."""

    instrument: str
    underlying: str
    expiry: date
    qty: int  # signed contracts
    mark: Decimal  # premium per share
    delta: Decimal  # per share
    gamma: Decimal  # per share per dollar
    vega: Decimal  # dollars per share per volatility point


@dataclass
class Exposure:
    gross: Decimal = Decimal(0)
    option_delta: dict[str, Decimal] = field(default_factory=dict)  # dollar delta by underlying
    vega: dict[tuple[str, date], Decimal] = field(default_factory=dict)
    gamma: dict[str, Decimal] = field(default_factory=dict)  # dollar gamma by underlying

    def net_delta(self, underlying: str, stock_notional: Decimal) -> Decimal:
        return self.option_delta.get(underlying, Decimal(0)) + stock_notional


def vega_cap(v0: Decimal, today: date, expiry: date) -> Decimal:
    """C_V(T) = V0 * sqrt(max(T, 9/365) / (1/12)), T in calendar days / 365 (Rule 6005)."""
    tenor = Decimal((expiry - today).days) / Decimal(365)
    return v0 * (max(tenor, MIN_TENOR) / ONE_MONTH).sqrt()


def measure(legs: list[Leg], spots: dict[str, Decimal]) -> Exposure:
    out = Exposure()
    for leg in legs:
        spot = spots.get(leg.underlying)
        out.gross += abs(leg.qty) * MULTIPLIER * leg.mark
        key = (leg.underlying, leg.expiry)
        out.vega[key] = out.vega.get(key, Decimal(0)) + leg.qty * MULTIPLIER * leg.vega
        if spot is None:
            continue
        out.option_delta[leg.underlying] = (
            out.option_delta.get(leg.underlying, Decimal(0))
            + leg.qty * MULTIPLIER * leg.delta * spot
        )
        out.gamma[leg.underlying] = (
            out.gamma.get(leg.underlying, Decimal(0))
            + Decimal("0.01") * spot * spot * leg.qty * MULTIPLIER * leg.gamma
        )
    return out


@dataclass(frozen=True)
class SoftLimits:
    """The book limits the bot keeps to: the exchange's, times a safety fraction."""

    gross: Decimal
    net_delta: Decimal  # per underlying, after hedging
    v0: Decimal
    gamma: Decimal  # summed |dollar gamma| over underlyings
    today: date


def adds_risk(
    before: Exposure, leg: Leg, extra: int, spots: dict[str, Decimal], limits: SoftLimits,
    stock_notional: dict[str, Decimal],
) -> bool:
    """Whether trading `extra` more contracts (signed) of `leg` would take any limit past
    its soft cap, or further past it when it is already over. Delta counts the stock hedge
    already held, since the bot hedges it; a trade that reduces an excess is allowed."""
    trial = replace(leg, qty=extra)
    delta_ex = measure([trial], spots)

    def worse(old: Decimal, new: Decimal, cap: Decimal) -> bool:
        return abs(new) > cap and abs(new) > abs(old)

    key = (leg.underlying, leg.expiry)
    old_vega = before.vega.get(key, Decimal(0))
    if worse(old_vega, old_vega + delta_ex.vega.get(key, Decimal(0)),
             vega_cap(limits.v0, limits.today, leg.expiry)):
        return True

    old_gamma = sum((abs(g) for g in before.gamma.values()), Decimal(0))
    g_old = before.gamma.get(leg.underlying, Decimal(0))
    g_new = g_old + delta_ex.gamma.get(leg.underlying, Decimal(0))
    new_gamma = old_gamma - abs(g_old) + abs(g_new)
    if worse(old_gamma, new_gamma, limits.gamma):
        return True

    # Gross only grows when the trade opens or extends a position.
    if abs(leg.qty + extra) > abs(leg.qty):
        if before.gross + delta_ex.gross > limits.gross:
            return True

    stock = stock_notional.get(leg.underlying, Decimal(0))
    old_delta = before.net_delta(leg.underlying, stock)
    new_delta = old_delta + delta_ex.option_delta.get(leg.underlying, Decimal(0))
    return worse(old_delta, new_delta, limits.net_delta)


def hedge_shares(
    option_delta: Decimal,
    stock_shares: int,
    spot: Decimal,
    *,
    threshold: Decimal,
    max_order: Decimal,
    tolerance: Decimal,
    buffer: Decimal = Decimal(0),
) -> int:
    """Signed shares of the underlying to trade so the stock offsets the options' delta.

    Nothing until the unhedged dollar delta passes `threshold`. The target keeps the stock
    a qualifying hedge (Rule 6006): opposite in sign to the option delta and no bigger than
    it plus `tolerance`. Each order is capped at `max_order` dollars.

    Only the delta beyond `buffer` is hedged. Stock stops qualifying the moment fills take the
    option delta through zero against it, so a buffer the size of the fills that can land
    between two hedges keeps the stock on the right side."""
    if spot <= 0:
        return 0
    cap = int(max_order / spot)
    # A stock position outside the qualifying band is a breach whatever the net delta, so
    # bring it back to the band's nearest edge before applying the threshold.
    band = int(((abs(option_delta) + tolerance) / spot).to_integral_value(rounding="ROUND_FLOOR"))
    lo, hi = (-band, 0) if option_delta > 0 else (0, band) if option_delta < 0 else (0, 0)
    if not lo <= stock_shares <= hi:
        trade = min(max(stock_shares, lo), hi) - stock_shares
        return max(-cap, min(cap, trade))
    stock_notional = stock_shares * spot
    if abs(option_delta + stock_notional) < threshold:
        return 0
    room = max(Decimal(0), abs(option_delta) - buffer)
    target = (-room if option_delta > 0 else room) / spot
    trade = int(target.to_integral_value(rounding="ROUND_DOWN")) - stock_shares
    return max(-cap, min(cap, trade))
