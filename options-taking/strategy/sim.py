"""A simulated exchange for the print-capture quoter, shared by the backtest
(strategy/backtest.py, replaying the history service) and paper trading (strategy/paper.py,
on the live feed). The quoter's own code decides every quote and hedge: `SimBot` is the
real `Bot` with its order sending pointed at `SimExchange` instead of the exchange.

What the simulated exchange models (Rules 2007, 6002, 6003):
- Trade-based fills. An eligible tape print at P fills our bid when bid >= P - kappa*padding
  (a sell print) or our offer when offer <= P + kappa*padding (a buy print), at our own
  limit price, at most 25 contracts per print. Eligible prints are the live-print kinds
  (TRADE_BASED_MATCH and RESIDUAL) that are not feed-only; student-to-student and
  student-to-wall trades are not live prints. Other teams compete for the same prints, so
  only `fill_share` of each print's size is ours, with the fraction carried per contract.
- Order delay: an order or amend rests `latency` seconds after it is sent.
- Stale orders: an order the wall touches is purged, as the exchange purges stale orders.
- Crossing other teams: a bid at or above another team's best offer (or an offer at or
  below their best bid) trades with it at once as a taker, paying the taker fee.
- Hedges take the wall's level-one size at the touch; the rest is cancelled.
- Fees: maker rebate $0.20 per contract, taker $0.30, stock 0.30 bp of notional.
- Rule 6006: every time hedge stock stops qualifying (same sign as the option delta, or
  bigger than |D| + $100k) is counted, since live that starts a cure.

What it cannot model: queue position against other teams at the same price (fill_share
stands in for it), other teams reacting to our quotes, the exchange's own price guards
beyond the wall, cures and forced reductions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from qte_sdk.contract.v1.common_pb2 import BUY, SELL, RESIDUAL, TRADE_BASED_MATCH
from qte_sdk.contract.v1.market_data_pb2 import LIVE
from qte_sdk.market_data import Book, Mark, OptionGreeks, SessionState, Trades
from qte_sdk.options import (
    OPTION_GREEKS_VALID,
    greek_to_decimal,
    is_feed_only,
    is_option_symbol,
    parse_option_symbol,
)
from qte_sdk.orders import send_amend, send_cancel, send_new

from strategy.bot import Bot, Slot
from strategy.config import Settings
from strategy.pricing import padding, reach
from strategy.risk import MULTIPLIER

log = logging.getLogger("sf_sim")

MICROS = 1_000_000
PRINT_CAP = 25  # contracts per original print that may fill students (Rule 6002)
LIVE_PRINT_KINDS = (TRADE_BASED_MATCH, RESIDUAL)
MARKOUT_HORIZONS = (1, 10, 60, 300)  # seconds, as on the dashboard


class SimClock:
    """The time a replay has reached, in seconds; `monotonic()` and `time()` both read it,
    so the bot's cooldowns run on market time, however fast the replay goes."""

    def __init__(self, t: float = 0.0):
        self.t = t

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return self.t

    def advance(self, item: object) -> None:
        ms = getattr(item, "grid_time", 0) or getattr(item, "sampled_at", 0)
        if ms and ms / 1000 > self.t:
            self.t = ms / 1000


@dataclass
class SimOrder:
    instrument: str
    side: int
    price: int  # micro-dollars
    size: int
    active_at: float  # seconds: rests from then on (order delay)


@dataclass
class Fill:
    t: float
    instrument: str
    side: int
    size: int
    price: int  # micro-dollars
    kind: str  # "print" (maker), "cross" (taker against a team), "hedge"
    fee: float  # dollars, positive = paid
    mark: float | None  # dollars per share at the fill
    markouts: dict[int, float] = field(default_factory=dict)  # horizon -> dollars

    def as_dict(self) -> dict:
        return {"t": self.t, "instrument": self.instrument,
                "side": "BUY" if self.side == BUY else "SELL", "size": self.size,
                "price": self.price / MICROS, "kind": self.kind, "fee": round(self.fee, 4),
                "mark": self.mark, **{f"markout_{h}s": round(v, 2) for h, v in self.markouts.items()}}


@dataclass
class _Wall:
    bid: int | None = None
    ask: int | None = None
    bid_size: int = 0
    ask_size: int = 0
    team_bid: int | None = None  # other teams' best orders
    team_ask: int | None = None


class SimExchange:
    """The quoter's orders, fills, positions and cash, against replayed or live market data."""

    def __init__(self, clock: SimClock, *, fill_share: float = 0.25, latency: float = 0.15,
                 starting_cash: float = 2_500_000.0, rebate: float = 0.20,
                 taker_fee: float = 0.30, stock_fee_bp: float = 0.30,
                 hedge_tolerance: float = 100_000.0):
        self.clock = clock
        self.fill_share = fill_share
        self.latency = latency
        self.rebate, self.taker_fee, self.stock_fee_bp = rebate, taker_fee, stock_fee_bp
        self.hedge_tolerance = hedge_tolerance
        self.cash = starting_cash
        self.positions: dict[str, int] = {}
        self.orders: dict[tuple[str, int], SimOrder] = {}
        self.walls: dict[str, _Wall] = {}
        self.marks: dict[str, float] = {}  # dollars per share (LIVE marks only)
        self.vega: dict[str, int] = {}  # micro-dollars per vol point, for the padding
        self.delta: dict[str, Decimal] = {}  # per share
        self.carry: dict[tuple[str, int], float] = {}  # fractional print volume per side
        self.fills: list[Fill] = []
        self._open_markouts: list[Fill] = []
        self.violations = 0  # times hedge stock stopped qualifying (a cure, live)
        self._violating: set[str] = set()
        self.messages = 0

    # ---------- the market ----------

    def on_market(self, item: object) -> None:
        if isinstance(item, Book):
            self._on_book(item)
        elif isinstance(item, Trades):
            self._on_trades(item)
        elif isinstance(item, Mark):
            if item.condition == LIVE:
                self.marks[item.instrument] = item.value / MICROS
                self._settle_markouts(item.instrument)
        elif isinstance(item, OptionGreeks):
            if item.status == OPTION_GREEKS_VALID:
                if item.HasField("vega"):
                    self.vega[item.instrument] = item.vega
                if item.HasField("delta"):
                    self.delta[item.instrument] = greek_to_decimal(item.delta)
        elif isinstance(item, SessionState):
            pass

    def _on_book(self, book: Book) -> None:
        w = self.walls.setdefault(book.instrument, _Wall())
        if book.bid_levels:
            w.bid, w.bid_size = book.bid_levels[0].price, book.bid_levels[0].size
        if book.ask_levels:
            w.ask, w.ask_size = book.ask_levels[0].price, book.ask_levels[0].size
        w.team_bid = book.student_bid_levels[0].price if book.student_bid_levels else None
        w.team_ask = book.student_ask_levels[0].price if book.student_ask_levels else None
        # Stale: an order the wall has reached is purged.
        for side in (BUY, SELL):
            o = self.orders.get((book.instrument, side))
            if o is None:
                continue
            if (side == BUY and w.ask is not None and o.price >= w.ask) or (
                    side == SELL and w.bid is not None and o.price <= w.bid):
                del self.orders[(book.instrument, side)]

    def _on_trades(self, trades: Trades) -> None:
        if not is_option_symbol(trades.instrument):
            return
        vega = self.vega.get(trades.instrument)
        if vega is None:
            return
        out = reach(padding(vega))
        for p in trades.prints:
            if p.kind not in LIVE_PRINT_KINDS or is_feed_only(p):
                continue
            side = BUY if p.aggressor_side == SELL else SELL  # the resting side it can fill
            # Our share of the print, in whole contracts; only the fraction carries over.
            key = (trades.instrument, side)
            share = min(p.size, PRINT_CAP) * self.fill_share + self.carry.get(key, 0.0)
            available = int(share)
            self.carry[key] = share - available
            o = self.orders.get(key)
            if o is None or self.clock.t < o.active_at or available <= 0:
                continue
            eligible = o.price >= p.price - out if side == BUY else o.price <= p.price + out
            if eligible:
                size = min(o.size, available)
                self._fill(o, size, o.price, "print", fee=-self.rebate * size)

    # ---------- our orders ----------

    def new(self, instrument: str, side: int, price: int, size: int) -> None:
        self.messages += 1
        self.orders[(instrument, side)] = SimOrder(instrument, side, price, size,
                                                   self.clock.t + self.latency)
        self._cross_team(instrument, side)

    def amend(self, instrument: str, side: int, price: int, size: int) -> None:
        self.messages += 1
        o = self.orders.get((instrument, side))
        if o is None:
            return  # gone meanwhile (filled or stale): the bot sends a new one next tick
        o.price, o.size, o.active_at = price, size, self.clock.t + self.latency
        self._cross_team(instrument, side)

    def cancel(self, instrument: str, side: int) -> None:
        self.messages += 1
        self.orders.pop((instrument, side), None)

    def cancel_all(self) -> None:
        self.orders.clear()

    def _cross_team(self, instrument: str, side: int) -> None:
        """An order priced through another team's best order trades with it at once, as a
        taker, at their price (their size is unknown: assume it covers ours)."""
        o, w = self.orders.get((instrument, side)), self.walls.get(instrument)
        if o is None or w is None:
            return
        theirs = w.team_ask if side == BUY else w.team_bid
        if theirs is not None and (o.price >= theirs if side == BUY else o.price <= theirs):
            self._fill(o, o.size, theirs, "cross", fee=self.taker_fee * o.size)

    def hedge(self, underlying: str, side: int, price: int, shares: int) -> int:
        """Take the wall's level one at the touch; returns the shares filled."""
        self.messages += 1
        w = self.walls.get(underlying)
        if w is None:
            return 0
        touch, size = (w.ask, w.ask_size) if side == BUY else (w.bid, w.bid_size)
        if touch is None or (touch > price if side == BUY else touch < price):
            return 0
        filled = min(shares, size)
        if filled > 0:
            fee = filled * touch / MICROS * self.stock_fee_bp / 10_000
            o = SimOrder(underlying, side, touch, filled, self.clock.t)
            self._fill(o, filled, touch, "hedge", fee=fee)
        return filled

    def _fill(self, o: SimOrder, size: int, price: int, kind: str, fee: float) -> None:
        mult = MULTIPLIER if is_option_symbol(o.instrument) else 1
        sign = 1 if o.side == BUY else -1
        self.cash -= sign * size * price / MICROS * mult + fee
        self.positions[o.instrument] = self.positions.get(o.instrument, 0) + sign * size
        o.size -= size
        if o.size <= 0:
            self.orders.pop((o.instrument, o.side), None)
        fill = Fill(self.clock.t, o.instrument, o.side, size, price, kind, fee,
                    self.marks.get(o.instrument))
        self.fills.append(fill)
        if kind != "hedge":
            self._open_markouts.append(fill)
        log.info("SIM FILL %s %s %d @ %.2f (%s, fee %.2f)", "BUY" if o.side == BUY else "SELL",
                 o.instrument, size, price / MICROS, kind, fee)
        self._check_hedge_rule()

    def _settle_markouts(self, instrument: str) -> None:
        mark = self.marks[instrument]
        still = []
        for f in self._open_markouts:
            if f.instrument == instrument:
                for h in MARKOUT_HORIZONS:
                    if h not in f.markouts and self.clock.t >= f.t + h:
                        sign = 1 if f.side == BUY else -1
                        f.markouts[h] = sign * (mark - f.price / MICROS) * f.size * MULTIPLIER
            if len(f.markouts) < len(MARKOUT_HORIZONS):
                still.append(f)
        self._open_markouts = still

    # ---------- the book ----------

    def option_delta(self) -> dict[str, float]:
        """Dollar delta of the options held, per underlying."""
        out: dict[str, float] = {}
        for instrument, qty in self.positions.items():
            if qty and is_option_symbol(instrument):
                u = parse_option_symbol(instrument).underlying
                spot, d = self.marks.get(u), self.delta.get(instrument)
                if spot is not None and d is not None:
                    out[u] = out.get(u, 0.0) + float(d) * qty * MULTIPLIER * spot
        return out

    def _check_hedge_rule(self) -> None:
        deltas = self.option_delta()
        for u in {parse_option_symbol(i).underlying for i in self.positions if is_option_symbol(i)}:
            shares, spot = self.positions.get(u, 0), self.marks.get(u)
            if not shares or spot is None:
                self._violating.discard(u)
                continue
            d, h = deltas.get(u, 0.0), shares * spot
            bad = (d == 0 or (d > 0) == (h > 0)) or abs(h) > abs(d) + self.hedge_tolerance
            if bad and u not in self._violating:
                self.violations += 1
                log.info("SIM RULE 6006: %s stock $%.0f against option delta $%.0f", u, h, d)
            (self._violating.add if bad else self._violating.discard)(u)

    def all_marked(self) -> bool:
        return all(i in self.marks for i, q in self.positions.items() if q)

    def equity(self) -> float:
        value = 0.0
        for instrument, qty in self.positions.items():
            if qty:
                mark = self.marks.get(instrument)
                if mark is None:
                    continue  # not marked yet: valued when it is
                value += qty * mark * (MULTIPLIER if is_option_symbol(instrument) else 1)
        return self.cash + value


class _SimResting:
    """What the bot reads as its resting orders: the simulated ones."""

    incomplete = False

    def __init__(self, sim: SimExchange, strat_id: str):
        self.sim, self.strat_id = sim, strat_id

    def apply(self, event: object) -> None:
        pass

    def __iter__(self):
        for o in list(self.sim.orders.values()):
            yield _OrderView(o, self.strat_id)

    def __len__(self) -> int:
        return len(self.sim.orders)


class _Key:
    def __init__(self, o: SimOrder):
        self.instrument, self.side, self.price = o.instrument, o.side, o.price


class _OrderView:
    def __init__(self, o: SimOrder, strat_id: str):
        self.key, self.strat_id, self.remaining_size = _Key(o), strat_id, o.size


class _NoAccount:
    def update(self, event: object) -> list:
        return []

    async def query(self, session: object) -> None:
        pass


class _NoSession:
    instrument_table = None

    async def send(self, *args, **kwargs) -> None:
        raise RuntimeError("a simulated bot must not send to an exchange")


DEFAULT_TICK = 10_000  # $0.01 in micro-dollars


class SimBot(Bot):
    """The quoter, trading against `SimExchange`. Market data goes in through
    `on_market_data` and `on_grid` (a backtest calls them; paper trading gets them from a
    live session through `Bot.on_event`)."""

    def __init__(self, sim: SimExchange, cfg: Settings, *, session=None, clock=None,
                 strat_id: str = "sim", hedging: bool = True, decide_every: float = 0.0,
                 ticks: dict[str, int] | None = None, persist: bool = False):
        super().__init__(session or _NoSession(), strat_id, cfg, dry_run=False, hedging=hedging,
                         clock=clock or sim.clock)
        self.sim = sim
        self.resting = _SimResting(sim, strat_id)  # type: ignore[assignment]
        self.account = _NoAccount()  # type: ignore[assignment]
        self.connected = True
        self.account_known = True
        self.decide_every = decide_every
        self.last_decision = float("-inf")
        self.ticks = ticks or {}
        self.persist_state = persist
        self.day_start_equity: float | None = None  # set once every holding has a mark

    # Market data reaches the simulated exchange first, so fills land before the bot reacts.
    def on_market_data(self, item: object) -> None:
        fills = len(self.sim.fills)
        self.sim.on_market(item)
        if isinstance(item, SessionState) and (self.state is None
                                               or self.state.session_date != item.session_date):
            self.day_start_equity = None
        super().on_market_data(item)
        if len(self.sim.fills) != fills:
            self.sync()  # only after a fill: valuing the book on every message is slow

    def sync(self) -> None:
        self.positions = dict(self.sim.positions)
        if self.day_start_equity is None and self.sim.all_marked():
            self.day_start_equity = self.sim.equity()
        start = self.day_start_equity
        self.daily_pnl = Decimal(0) if start is None else Decimal(str(round(self.sim.equity() - start, 2)))

    async def on_grid(self) -> None:
        if self.clock.monotonic() - self.last_decision < self.decide_every:
            return
        self.last_decision = self.clock.monotonic()
        self.sync()
        await super().on_grid()

    def tick_of(self, instrument: str) -> int | None:
        if self.session.instrument_table is not None:
            return super().tick_of(instrument)
        return self.ticks.get(instrument, DEFAULT_TICK)

    async def send(self, slot: Slot, now: float, fn, **kwargs) -> None:
        self.count_message()
        slot.sent_at = now
        slot.pending = None  # the simulated exchange answers at once
        if fn is send_new:
            self.sim.new(kwargs["instrument"], kwargs["side"], kwargs["price"], kwargs["size"])
        elif fn is send_amend:
            self.sim.amend(kwargs["instrument"], kwargs["side"], kwargs["new_price"],
                           kwargs["new_size"])
        elif fn is send_cancel:
            self.sim.cancel(kwargs["instrument"], kwargs["side"])
        self.sync()

    async def send_hedge(self, ref: str, underlying: str, side: int, price: int,
                         shares: int) -> None:
        self.count_message()
        self.sim.hedge(underlying, side, price, shares)
        self.sync()

    async def query_account(self) -> None:
        self.last_query = self.clock.monotonic()
        self.account_known = True  # the simulated book is always known
        self.sync()

    async def cancel_all(self) -> None:
        self.sim.cancel_all()

    def read_control(self) -> None:
        if self.persist_state:
            super().read_control()

    def write_status(self, now: float) -> None:
        if self.persist_state:
            super().write_status(now)
