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
    # |option dollar delta| the stock hedge can cover, per underlying and summed over both
    # (Rule 6005 hedge allowances, times config.HEDGEABLE_FRACTION). Unlimited if unset.
    option_delta: Decimal = Decimal("Infinity")
    book_option_delta: Decimal = Decimal("Infinity")


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

    # Option delta past what stock may hedge cannot be hedged, so it counts even when the
    # net delta after the hedge looks small.
    old_opt = before.option_delta.get(leg.underlying, Decimal(0))
    new_opt = old_opt + delta_ex.option_delta.get(leg.underlying, Decimal(0))
    if worse(old_opt, new_opt, limits.option_delta):
        return True
    old_book = sum((abs(d) for d in before.option_delta.values()), Decimal(0))
    if worse(old_book, old_book - abs(old_opt) + abs(new_opt), limits.book_option_delta):
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


def ww_band(
    gamma_shares: Decimal,
    spot: Decimal,
    cost: Decimal,
    *,
    risk_aversion: Decimal,
    floor: Decimal,
    cap: Decimal,
) -> Decimal:
    """Half-width, in dollars of delta, of the Whalley-Wilmott no-trade band.

    Whalley and Wilmott (1997): with a proportional cost `cost` (a fraction of notional
    paid on every share traded) and absolute risk aversion `risk_aversion` (per dollar),
    the hedge should not trade while the share count is within

        H = (3/2 * cost * spot * gamma^2 / risk_aversion) ** (1/3)    shares

    of the Black-Scholes hedge, and on leaving the band it trades back to the band's edge.
    `gamma_shares` is the book's gamma in shares per $1 of the underlying (sum of contracts
    x 100 x per-share gamma), long or short alike. More gamma widens the band: each hedge
    goes stale faster, so tight hedging pays the cost over and over for little risk saved.
    Higher costs widen it too; more risk aversion narrows it.

    The asymptotic formula goes to zero with gamma, and at zero the hedger would chase every
    fill, so the result is held between `floor` and `cap`."""
    if spot <= 0 or risk_aversion <= 0 or cost <= 0:
        return floor
    g = float(abs(gamma_shares))
    shares = (1.5 * float(cost) * float(spot) * g * g / float(risk_aversion)) ** (1.0 / 3.0)
    dollars = Decimal(str(round(shares * float(spot), 2)))
    return max(floor, min(cap, dollars))


def band_hedge_shares(
    option_delta: Decimal,
    stock_shares: int,
    spot: Decimal,
    *,
    band: Decimal,
    rehedge_to: Decimal,
    max_order: Decimal,
    tolerance: Decimal,
) -> int:
    """Signed shares to trade under a no-trade band of `band` dollars of net delta.

    Nothing while |options + stock| <= band. Outside it, trade back to `rehedge_to` x band
    on the same side (1 is the Whalley-Wilmott edge; less trades a little more each time so
    the next hedge comes later). The stock stays a qualifying hedge (Rule 6006): opposite in
    sign to the option delta and at most |option delta| + tolerance/2, half the rule's
    tolerance so fills against an over-hedge cannot push it out. Stock already outside the
    qualifying band is brought back first, as hedge_shares does. Orders capped at
    `max_order` dollars."""
    if spot <= 0:
        return 0
    cap = int(max_order / spot)
    legal = int(((abs(option_delta) + tolerance) / spot).to_integral_value(rounding="ROUND_FLOOR"))
    lo, hi = (-legal, 0) if option_delta > 0 else (0, legal) if option_delta < 0 else (0, 0)
    if not lo <= stock_shares <= hi:
        trade = min(max(stock_shares, lo), hi) - stock_shares
        return max(-cap, min(cap, trade))
    net = option_delta + stock_shares * spot
    if abs(net) <= band:
        return 0
    inner = band * rehedge_to
    target_net = inner if net > 0 else -inner
    target = (target_net - option_delta) / spot
    # Round towards the current position so the trade never overshoots the target.
    trade = target - stock_shares
    trade_i = int(trade.to_integral_value(rounding="ROUND_DOWN"))
    safe = int(((abs(option_delta) + tolerance / 2) / spot).to_integral_value(rounding="ROUND_FLOOR"))
    s_lo, s_hi = (-safe, 0) if option_delta > 0 else (0, safe) if option_delta < 0 else (0, 0)
    new = min(max(stock_shares + trade_i, s_lo), s_hi)
    return max(-cap, min(cap, new - stock_shares))
