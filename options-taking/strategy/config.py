"""Settings for the San Francisco Options Taking bot.

The exchange limits below are the rulebook's (Revision 3, Rule 6005 / 3004, taker column,
lambda = 0.10). They are set by the exchange and published per term: check them against
the term's risk-parameter notice and update them here if they change. Message budgets are
the Options Taking row of docs/developing-your-algo.md for the Winter term.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ExchangeLimits:
    gross: Decimal = Decimal("3_750_000")  # 150% of $2.5M
    net_delta_per_underlying: Decimal = Decimal("1_500_000")
    v0: Decimal = Decimal("15_000")  # vega base, dollars per vol point
    gamma: Decimal = Decimal("1_440_000")
    max_option_order: int = 25  # contracts
    max_hedge_order: Decimal = Decimal("400_000")  # dollars of stock
    hedge_tolerance: Decimal = Decimal("100_000")  # |H| <= |D| + $100k (Rule 6006)
    # Most stock the team may hold as a hedge (Rule 6005): per underlying, and summed.
    hedge_allowance_underlying: Decimal = Decimal("8_000_000")
    hedge_allowance_book: Decimal = Decimal("9_500_000")
    loss_halt: Decimal = Decimal("150_000")
    sustained_per_minute: int = 2_400
    burst_per_second: int = 800


# Every strategy refuses a trade that takes the book's option delta past this fraction of
# what stock is allowed to hedge (hedge allowance above): delta past it cannot be hedged at
# all and goes straight into net delta (13 Nov: SPY options $9.45M against the $8M cap, net
# $1.47M of the $1.5M limit). Team-wide, not times risk_fraction: each strategy checks the
# whole book. strategy.risk.adds_risk.
HEDGEABLE_FRACTION = Decimal("0.9")


@dataclass(frozen=True)
class Settings:
    limits: ExchangeLimits = ExchangeLimits()
    # Fraction of each exchange risk limit the bot keeps to.
    risk_fraction: Decimal = Decimal("0.5")
    # Stop-losses on the day's P&L (strategy/bot.py, check_stops). The exchange warns at
    # -$90k and halts at -$150k; these act well before that.
    drawdown_stop: Decimal = Decimal("15_000")  # reduce-only once P&L is this far below the day's best
    soft_loss: Decimal = Decimal("25_000")  # reduce-only at this day loss
    hard_loss: Decimal = Decimal("40_000")  # no quotes for the rest of the day at this loss

    # Which contracts: per underlying, the nearest strikes to the forward in the nearest
    # expiry open to new positions, calls and puts both.
    strikes_per_underlying: int = 8
    # Contracts per quote, and the most the bot will hold long or short in one contract.
    quote_size: int = 10
    max_position: int = 40

    # Least edge, in dollars per share, a quote must keep against the mark.
    min_edge: Decimal = Decimal("0.01")
    # Ticks to improve on the qualifying price (more fills, less edge).
    improve_ticks: int = 0
    # Inventory skew (Natenberg p. 414): at the position cap, the quote that would add to
    # the position is backed off by this many ticks, in proportion to the position held.
    inventory_skew_ticks: int = 4
    # Least seconds between two messages on the same quote, and least price move (ticks)
    # worth an amend.
    requote_seconds: float = 2.0
    requote_ticks: int = 1

    # Delta hedging in the underlying.
    hedge_threshold: Decimal = Decimal("100_000")  # dollars of unhedged delta
    hedge_cooldown_seconds: float = 5.0
    # Delta left unhedged, in fills of quote_size at this delta: keeps stock qualifying (Rule 6006).
    hedge_buffer_fills: int = 1
    hedge_buffer_delta: Decimal = Decimal("0.6")

    # Stop quoting this long before the session's close.
    stop_before_close_seconds: int = 10 * 60
    # Scan the front expiry's wall quotes for no-arbitrage violations this often (logged).
    arb_scan_seconds: float = 5.0
    taker_fee_per_contract: Decimal = Decimal("0.30")

    # Ask the exchange for the account this often.
    account_query_seconds: float = 5.0
    # Dead-man on wake: a gap this long between grid ticks cancels our quotes, then the bot
    # holds this long before quoting again.
    stall_seconds: float = 10.0
    stall_hold_seconds: float = 5.0


@dataclass(frozen=True)
class StaleSettings:
    """The stale-quote picker (strategy/stale_bot.py), a separate strategy from the quoter."""

    limits: ExchangeLimits = ExchangeLimits()
    # Fraction of each exchange risk limit this strategy keeps to. The quoter's fraction
    # is separate, but both trade the one team book, so keep the two summing under 1.
    risk_fraction: Decimal = Decimal("0.25")
    soft_loss: Decimal = Decimal("20_000")  # reduce-only once the day's loss reaches this

    # Least edge per share against the wall mid, after the taker fee, worth crossing for.
    min_edge: Decimal = Decimal("0.02")
    taker_fee_per_contract: Decimal = Decimal("0.30")
    max_order: int = 10  # contracts per take (exchange cap 25)
    max_position: int = 10  # contracts held long or short in one contract
    # Least seconds between two takes on one contract, so a quote we already hit (whose
    # book has not caught up) is not hit twice.
    cooldown_seconds: float = 1.0
    # Messages: the team's 2,400 per minute is shared. The SDK pacer keeps each process at
    # 80% of its own budget: quoter 1,920 + dashboard 192 + this 200 + hedger 80 = 2,392.
    sustained_per_minute: int = 250
    burst_per_second: int = 100
    account_query_seconds: float = 30.0


@dataclass(frozen=True)
class HedgerSettings:
    """The team-wide delta hedger (strategy/hedger.py). It hedges the option delta of the
    whole book, whichever strategy's fills made it. Run the quoter with --no-hedge."""

    limits: ExchangeLimits = ExchangeLimits()
    # Hedge an underlying once its net delta (options plus stock) passes trigger_fraction
    # of the exchange's per-underlying delta limit ($1.5M), and hedge it back down to
    # target_fraction of that limit, not to zero. What is left unhedged is room for fills
    # to land without taking option delta through zero against the stock (Rule 6006).
    # These two are the "fixed" mode only. The default is "ww" below.
    trigger_fraction: Decimal = Decimal("0.10")  # $150k
    target_fraction: Decimal = Decimal("0.05")  # $75k

    # "ww": a Whalley-Wilmott no-trade band per underlying, from the book's net gamma in
    # that underlying and the cost of a hedge (strategy.risk.ww_band). "fixed": the
    # 150k/75k rule above. Run `python -m sim.hedge_sim` to compare the two before changing these.
    band_mode: str = "ww"
    # Absolute risk aversion, per dollar. The one free choice in the formula: higher hedges
    # tighter (less P&L swing, more fees). 1e-3 is from sim/hedge_sim.py. Against fixed
    # 150k/75k it roughly ties on SPY (cost $518 vs $566, swing about 3-8% wider) and beats
    # it on GOOGL (swing -18%, -25% with an -8% gap) for $15-20 more cost per session.
    # GOOGL sits on the floor, so raising this further only tightens SPY.
    risk_aversion: Decimal = Decimal("1e-3")
    # The band never goes below floor (so a near-zero-gamma book does not chase each fill)
    # or above cap (so it stays well inside the $1.5M limit and the quoter's own limits).
    band_floor: Decimal = Decimal("75_000")
    band_cap: Decimal = Decimal("600_000")
    # On leaving the band, trade back to this fraction of it (1 = the band's edge, as in
    # Whalley-Wilmott). Under 1 leaves room so the next hedge comes later.
    rehedge_to: Decimal = Decimal("1")
    # Cost of a hedge, as a fraction of notional: half the wall's spread, read live from
    # the book (default_half_spread_bp until there is one), plus half of max_slippage_bp for
    # walking the ladder, plus the stock taker fee (0.30 bp, rulebook 2.13).
    stock_taker_fee_bp: Decimal = Decimal("0.30")
    default_half_spread_bp: Decimal = Decimal("1.5")
    # The qualifying band per underlying is |H| <= |D| + tolerance. Rule 6006 allows $100k
    # per underlying but only $150k summed over both, so $75k each keeps both tests.
    tolerance: Decimal = Decimal("75_000")
    # Walk the stock wall's ladder at most this far past the touch, in basis points.
    max_slippage_bp: Decimal = Decimal("3")
    # Least seconds between two hedges in one underlying, and how long an unanswered
    # hedge blocks the next.
    min_interval_seconds: float = 0.5
    pending_timeout_seconds: float = 2.0
    # Stop sending this long before the close (the close cancels everything anyway).
    stop_before_close_seconds: int = 60
    # Messages. The SDK pacer counts the whole team's messages, not just the hedger's, so
    # a 400 slice (320 after headroom) froze the hedger whenever the team sent more than
    # that: 13 Nov from 16:36 it held every send 8-28 s at a time and no hedge went out.
    # So the hedger gets the team's whole budget with more headroom than the quoter
    # (0.9 against 0.8): it waits only when the team passes 2,160 a minute, after the
    # quoter's own pacer has already backed off at 1,920.
    sustained_per_minute: int = 2_400
    burst_per_second: int = 800
    pacer_headroom: float = 0.9
    account_query_seconds: float = 5.0
    # Stop this far inside the hedge allowances, so a hedge is not refused RISK_LIMIT_BREACH.
    allowance_margin: Decimal = Decimal("100_000")
    # After a HEDGE_ONLY refusal, send no stock that adds to a hedge, in either underlying,
    # for this long (reductions still go). 12 Nov: every addition from 15:09 to 20:49 was
    # refused while the team held SPY 785P with no published Greeks, and retrying every 2 s
    # sent ~26 refused orders a minute.
    hedge_only_hold_seconds: float = 60.0


@dataclass(frozen=True)
class SviSettings:
    """The SVI rich/cheap strategy (strategy/svi_bot.py), its own strat_id like the picker.

    The curve's five numbers are not set here: they are fitted to the published vols every
    refit_seconds (strategy/svi.py). These settings say when to trust a fit and how far off
    it a contract must be before it is worth trading."""

    limits: ExchangeLimits = ExchangeLimits()
    # Fraction of each exchange risk limit this strategy keeps to: quoter 0.5 + picker 0.25
    # + this 0.2 keeps the three under the exchange's limits together.
    risk_fraction: Decimal = Decimal("0.2")
    soft_loss: Decimal = Decimal("15_000")  # reduce-only once the day's loss reaches this

    # Fitting. Only out-of-the-money contracts with valid published Greeks go in.
    refit_seconds: float = 2.0
    min_points: int = 6  # an expiry with fewer such strikes is not fitted (SVI has 5 numbers)
    # A fit whose vega-weighted error is more than this many vol points is not traded against.
    max_rmse: float = 1.0
    # Strikes this many rmse off the first fit are left out of the second, so the strike we
    # want to trade does not pull the curve towards itself.
    outlier_vols: float = 3.0
    # Leave expiries this close alone: they go reducing-only about 3 days out anyway.
    min_days: int = 4

    # Trading. A contract is traded only when its own vol (from the official mark) is at
    # least min_vol_gap off the curve: buy when under it, sell when over it. The price we
    # cross at must still beat the curve's price by min_edge per share after the fee.
    min_vol_gap: float = 0.5
    min_edge: Decimal = Decimal("0.03")
    taker_fee_per_contract: Decimal = Decimal("0.30")
    max_order: int = 5  # contracts per take (exchange cap 25)
    max_position: int = 10  # contracts held long or short in one contract
    cooldown_seconds: float = 5.0  # least seconds between two takes on one contract
    # Messages. The SDK pacer counts the whole team's messages, so a small slice here would
    # freeze it whenever the quoter is busy (the hedger, 13 Nov). The team's budget with the
    # default 0.8 headroom makes it wait only when the quoter's own pacer does. It sends only
    # a few takes and cancels a minute itself.
    sustained_per_minute: int = 2_400
    burst_per_second: int = 800
    account_query_seconds: float = 30.0
