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
    loss_halt: Decimal = Decimal("150_000")
    sustained_per_minute: int = 2_400
    burst_per_second: int = 800


@dataclass(frozen=True)
class Settings:
    limits: ExchangeLimits = ExchangeLimits()
    # Fraction of each exchange risk limit the bot keeps to.
    risk_fraction: Decimal = Decimal("0.5")
    # Stop opening risk once the day's loss reaches this (the exchange halts at $150k).
    soft_loss: Decimal = Decimal("60_000")

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
    trigger_fraction: Decimal = Decimal("0.25")  # $375k
    target_fraction: Decimal = Decimal("0.15")  # $225k
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
    # Messages: a small slice of the team's 2,400 per minute (see StaleSettings for the sum).
    sustained_per_minute: int = 100
    burst_per_second: int = 20
    account_query_seconds: float = 5.0
