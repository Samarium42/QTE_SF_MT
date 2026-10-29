"""San Francisco, Options Taking: the stale-quote picker on SPY and GOOGL options.

On every option book update it compares the student quotes (other teams' resting orders)
with the wall mid, the contract's current fair mid (see stale.py). Where a student offer
is below it, or a student bid above it, by more than the taker fee plus `min_edge`, it
crosses that quote with a limit order at the quote's price, then cancels whatever is left
of its order. The fill is at a price better than the mark straight away.

It is its own strategy with its own strat_id and its own slice of the risk limits and the
message budget. It does not hedge: run it alongside the quoter (strategy/bot.py), which
hedges the whole team book's option delta, or keep its position caps small.

    python -m strategy.stale_bot --strat-id <registered id> --dry-run   # logs, sends nothing
    python -m strategy.stale_bot --strat-id <registered id>             # trades
"""

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
import time
from collections import deque
from datetime import date
from decimal import Decimal
from pathlib import Path

import qte_sdk.logs
from qte_sdk.account import AccountReports, is_account_state
from qte_sdk.books import LatestBooks
from qte_sdk.connection import Connected, Disconnected, ResumeComplete
from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT, SELL, MarketSessionPhase, ReasonCodes
from qte_sdk.contract.v1.market_data_pb2 import LIVE
from qte_sdk.contract.v1.order_events_pb2 import Execution, Reject
from qte_sdk.market_data import Book, Mark, SessionState, as_market_data, subscribe
from qte_sdk.options import (
    OPTION_GREEKS_VALID,
    OPTION_TRADING,
    LatestGreeks,
    OptionChain,
    expiry_date,
    greek_to_decimal,
    is_option_symbol,
    listed_contracts,
    option_underlyings,
    parse_option_symbol,
    trading_state,
)
from qte_sdk.orders import (
    is_order_event,
    new_request_ref,
    reason_code_name,
    request_ref_of,
    send_cancel,
    send_new,
)
from qte_sdk.pacing import Budget, Pacer
from qte_sdk.reconnect import ReconnectingSession
from qte_sdk.resting import RestingOrders
from qte_sdk.units import to_decimal

from strategy.config import StaleSettings
from strategy.risk import Leg, SoftLimits, adds_risk, measure
from strategy.stale import find_takes

log = logging.getLogger("sf_stale")

STATUS = Path("logs/stale-status.json")  # read by the dashboard
BUDGET_REJECTS = frozenset({ReasonCodes.MESSAGE_BUDGET_EXCEEDED, ReasonCodes.BURST_CAP_EXCEEDED})
SIDE_NAME = {BUY: "BUY", SELL: "SELL"}
STOP_BEFORE_CLOSE_MS = 10 * 60 * 1000


class Picker:
    def __init__(self, session: ReconnectingSession, strat_id: str, cfg: StaleSettings, dry_run: bool):
        self.session = session
        self.strat_id = strat_id
        self.cfg = cfg
        self.dry_run = dry_run

        self.books = LatestBooks()
        self.greeks = LatestGreeks()
        self.marks: dict[str, int] = {}
        self.chain: OptionChain | None = None
        self.state: SessionState | None = None
        self.resting = RestingOrders()
        self.account = AccountReports()
        self.positions: dict[str, int] = {}
        self.account_known = False
        self.daily_pnl = Decimal(0)
        self.in_cure = False
        self.last_query = 0.0
        self.connected = False
        self.stopping = False

        self.subscribed: set[str] = set()
        self.table_seen: object | None = None
        self.underlyings: list[str] = []
        self.last_take: dict[str, float] = {}  # contract -> monotonic time of our last take
        self.blocked_until: dict[str, float] = {}
        self.paused_until = 0.0
        self.sent_times: deque[float] = deque()
        self.last_status_file = 0.0
        self.cancel_sent: dict[tuple[str, int, int], float] = {}  # level -> when we cancelled it

        # Counters for the log and the dashboard.
        self.seen = 0  # takes found (live or dry run)
        self.sent = 0
        self.filled = 0  # contracts
        self.fill_edge = Decimal(0)  # dollars against the wall mid at the time, before fees
        self.recent: deque[dict] = deque(maxlen=20)

    # ---------- reading ----------

    async def on_event(self, event: object) -> None:
        self.resting.apply(event)
        for report in self.account.update(event):
            message = report.message  # type: ignore[attr-defined]
            if is_account_state(report):
                self.positions = {p.instrument: p.quantity for p in message.positions}
                if message.HasField("summary"):
                    self.daily_pnl = to_decimal(message.summary.daily_pnl)
                    self.in_cure = message.summary.in_cure
                self.account_known = True
            elif isinstance(message, Execution):
                signed = message.fill_size if message.side == BUY else -message.fill_size
                self.positions[message.instrument] = self.positions.get(message.instrument, 0) + signed

        if isinstance(event, Connected):
            self.connected = True
            self.account_known = False
            log.info("connected (reconnected=%s)", event.reconnected)
            await self.query_account()
            return
        if isinstance(event, Disconnected):
            self.connected = False
            log.warning("disconnected: %s; no takes until the resume", event.error)
            return
        if isinstance(event, ResumeComplete):
            log.info("resume complete; %d team orders resting", len(self.resting))
            return

        table = self.session.instrument_table
        if table is not None and table is not self.table_seen:
            self.table_seen = table
            self.underlyings = list(option_underlyings(table))
            listed = [c for u in self.underlyings for c in listed_contracts(table, u)]
            new = [i for i in self.underlyings + listed if i not in self.subscribed]
            if new:
                self.subscribed.update(new)
                await subscribe(self.session, new)

        item = as_market_data(event)
        if item is not None:
            self.books.update(item)
            self.greeks.update(item)
            if isinstance(item, Mark):
                if item.condition == LIVE:
                    self.marks[item.instrument] = item.value
                else:
                    self.marks.pop(item.instrument, None)
            elif isinstance(item, OptionChain):
                self.chain = item
            elif isinstance(item, SessionState):
                self.state = item
                await self.on_grid()
            elif isinstance(item, Book) and is_option_symbol(item.instrument):
                await self.on_book(item)
            return
        if is_order_event(event):
            self.on_order_event(event.message)  # type: ignore[attr-defined]

    def on_order_event(self, message: object) -> None:
        if isinstance(message, Execution) and message.strat_id == self.strat_id:
            book = self.books.get(message.instrument)
            fair = None
            if book is not None and book.bid_levels and book.ask_levels:
                fair = (book.bid_levels[0].price + book.ask_levels[0].price) / 2
            edge = Decimal(0)
            if fair is not None:
                sign = 1 if message.side == BUY else -1
                edge = to_decimal(int(sign * (fair - message.fill_price))) * 100 * message.fill_size
            self.filled += message.fill_size
            self.fill_edge += edge
            log.info("FILL %s %s %d @ %s (%s), edge vs wall mid $%s, position now %d",
                     SIDE_NAME.get(message.side), message.instrument, message.fill_size,
                     to_decimal(message.fill_price), message.fill_kind, edge,
                     self.positions.get(message.instrument, 0))
        if isinstance(message, Reject) and request_ref_of(message) is not None:
            reason = message.reason_code
            if reason in BUDGET_REJECTS:
                log.warning("message budget reject (%s): pausing for a minute", reason_code_name(reason))
                self.paused_until = time.monotonic() + 61.0
            elif reason not in (ReasonCodes.MIN_REST_VIOLATION, ReasonCodes.NO_ORDER_AT_LEVEL):
                instrument = getattr(message, "instrument", None)
                log.warning("reject %s on %s", reason_code_name(reason), instrument)
                if instrument:
                    self.blocked_until[instrument] = time.monotonic() + 30.0

    # ---------- deciding ----------

    def trading(self) -> bool:
        s = self.state
        return (
            s is not None and s.state == MarketSessionPhase.OPEN and not s.outage_active
            and s.close_time - s.grid_time > STOP_BEFORE_CLOSE_MS and not self.stopping
            and self.connected and self.account_known and not self.resting.incomplete
        )

    def leg(self, instrument: str, qty: int) -> Leg | None:
        g = self.greeks.get(instrument)
        mark = self.marks.get(instrument)
        if (g is None or g.status != OPTION_GREEKS_VALID or mark is None
                or not (g.HasField("delta") and g.HasField("gamma") and g.HasField("vega"))):
            return None
        symbol = parse_option_symbol(instrument)
        return Leg(instrument, symbol.underlying, symbol.expiry, qty, to_decimal(mark),
                   greek_to_decimal(g.delta), greek_to_decimal(g.gamma), to_decimal(g.vega))

    async def on_book(self, book: Book) -> None:
        contract = book.instrument
        now = time.monotonic()
        if not self.trading() or now < self.paused_until:
            return
        if self.blocked_until.get(contract, 0.0) > now:
            return
        if now - self.last_take.get(contract, 0.0) < self.cfg.cooldown_seconds:
            return
        if trading_state(book) != OPTION_TRADING or book.condition != LIVE or contract in self.books.stale:
            return
        if not book.bid_levels or not book.ask_levels:
            return
        if not book.student_bid_levels and not book.student_ask_levels:
            return
        if self.chain is not None and any(
            e.reducing_only for e in self.chain.expiries
            if e.underlying == parse_option_symbol(contract).underlying
            and expiry_date(e) == parse_option_symbol(contract).expiry
        ):
            return

        own_bids: dict[int, int] = {}
        own_asks: dict[int, int] = {}
        for order in self.resting:
            if order.key.instrument == contract:
                side_map = own_bids if order.key.side == BUY else own_asks
                side_map[order.key.price] = side_map.get(order.key.price, 0) + order.remaining_size

        takes = find_takes(
            wall_bid=book.bid_levels[0].price,
            wall_ask=book.ask_levels[0].price,
            student_bids=[(lv.price, lv.size) for lv in book.student_bid_levels],
            student_asks=[(lv.price, lv.size) for lv in book.student_ask_levels],
            own_bids=own_bids,
            own_asks=own_asks,
            fee=int(self.cfg.taker_fee_per_contract * 1_000_000) // 100,
            min_edge=int(self.cfg.min_edge * 1_000_000),
        )
        if not takes:
            return

        pos = self.positions.get(contract, 0)
        reduce_only = self.in_cure or self.daily_pnl <= -self.cfg.soft_loss
        lim, f = self.cfg.limits, self.cfg.risk_fraction
        assert self.state is not None
        limits = SoftLimits(gross=lim.gross * f, net_delta=lim.net_delta_per_underlying * f,
                            v0=lim.v0 * f, gamma=lim.gamma * f,
                            today=date.fromisoformat(self.state.session_date))
        leg = self.leg(contract, pos)
        if leg is None:
            return
        legs = [x for x in (self.leg(i, q) for i, q in self.positions.items()
                            if q and is_option_symbol(i)) if x is not None]
        spots = {u: to_decimal(self.marks[u]) for u in self.underlyings if u in self.marks}
        exposure = measure(legs, spots)
        stock = {u: self.positions.get(u, 0) * s for u, s in spots.items()}

        for take in takes:
            self.seen += 1
            room = self.cfg.max_position - take.side * pos
            if reduce_only:
                room = max(0, -take.side * pos)
            size = min(take.size, self.cfg.max_order, lim.max_option_order, room)
            if size <= 0:
                continue
            if not reduce_only and adds_risk(exposure, leg, take.side * size, spots, limits, stock):
                continue
            side = BUY if take.side > 0 else SELL
            row = {"t": time.time(), "contract": contract, "side": SIDE_NAME[side],
                   "price": float(to_decimal(take.price)), "size": size,
                   "fair": float(to_decimal(take.fair)), "edge": float(to_decimal(take.edge))}
            self.recent.appendleft(row)
            self.last_take[contract] = now
            if self.dry_run:
                log.info("WOULD TAKE %s %s %d @ %s (wall mid %s, edge $%s/share after fee)",
                         SIDE_NAME[side], contract, size, to_decimal(take.price),
                         to_decimal(take.fair), to_decimal(take.edge))
                continue
            log.info("TAKE %s %s %d @ %s (wall mid %s, edge $%s/share after fee)",
                     SIDE_NAME[side], contract, size, to_decimal(take.price),
                     to_decimal(take.fair), to_decimal(take.edge))
            self.count_message()
            self.sent += 1
            try:
                await send_new(self.session, strat_id=self.strat_id, instrument=contract,
                               side=side, order_type=LIMIT, price=take.price, size=size,
                               request_ref=new_request_ref())
            except Exception as error:
                log.warning("send failed: %s", error)
            break  # one take per contract per book update: the position has changed

    # ---------- acting ----------

    async def on_grid(self) -> None:
        now = time.monotonic()
        self.write_status(now)
        if self.connected and now - self.last_query > self.cfg.account_query_seconds:
            await self.query_account()
        if self.dry_run or not self.connected or self.resting.incomplete:
            return
        # Whatever is left of a take rests at the stale quote's price: cancel it. A cancel
        # inside the 50 ms minimum rest is rejected; the next grid tries again.
        for order in list(self.resting):
            if order.strat_id == self.strat_id and is_option_symbol(order.key.instrument):
                level = (order.key.instrument, order.key.side, order.key.price)
                if now - self.cancel_sent.get(level, 0.0) < 0.5:
                    continue  # a cancel is already on its way
                self.cancel_sent[level] = now
                self.count_message()
                with contextlib.suppress(Exception):
                    await send_cancel(self.session, instrument=order.key.instrument,
                                      side=order.key.side, price=order.key.price)

    async def query_account(self) -> None:
        self.last_query = time.monotonic()
        with contextlib.suppress(Exception):
            await self.account.query(self.session)

    def count_message(self) -> None:
        now = time.monotonic()
        self.sent_times.append(now)
        while self.sent_times and now - self.sent_times[0] > 60:
            self.sent_times.popleft()

    def own_count(self) -> int:
        return sum(1 for o in self.resting if o.strat_id == self.strat_id)

    def write_status(self, now: float) -> None:
        if now - self.last_status_file < 2:
            return
        self.last_status_file = now
        while self.sent_times and now - self.sent_times[0] > 60:
            self.sent_times.popleft()
        status = {
            "t": time.time(), "strat_id": self.strat_id, "dry_run": self.dry_run,
            "trading": self.trading(), "messages_last_minute": len(self.sent_times),
            "budget_per_minute": self.cfg.sustained_per_minute,
            "budget_paused": now < self.paused_until,
            "seen": self.seen, "sent": self.sent, "filled": self.filled,
            "fill_edge": float(self.fill_edge), "daily_pnl": float(self.daily_pnl),
            "in_cure": self.in_cure, "recent": list(self.recent),
        }
        with contextlib.suppress(OSError):
            tmp = STATUS.with_suffix(".tmp")
            tmp.write_text(json.dumps(status))
            tmp.replace(STATUS)


async def run(args: argparse.Namespace) -> int:
    cfg = StaleSettings()
    pacer = Pacer(Budget(sustained_per_minute=cfg.sustained_per_minute,
                         burst_per_second=cfg.burst_per_second))
    session = ReconnectingSession(pacing=pacer)
    picker = Picker(session, args.strat_id, cfg, args.dry_run)

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, task.cancel)
        loop.add_signal_handler(signal.SIGTERM, task.cancel)

    async with session:
        try:
            async with asyncio.timeout(args.seconds):
                async for event in session:
                    await picker.on_event(event)
        except TimeoutError:
            log.info("time is up")
        except asyncio.CancelledError:
            log.info("interrupted: cancelling this strategy's orders (Ctrl+C again to quit)")
            task.uncancel()
        picker.stopping = True
        log.info("takes found %d, sent %d, filled %d contracts, edge vs wall mid $%s",
                 picker.seen, picker.sent, picker.filled, picker.fill_edge)
        if not args.dry_run:
            for order in list(picker.resting):
                if order.strat_id == picker.strat_id:
                    with contextlib.suppress(Exception):
                        await send_cancel(session, instrument=order.key.instrument,
                                          side=order.key.side, price=order.key.price)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(5):
                    async for event in session:
                        await picker.on_event(event)
                        if picker.own_count() == 0:
                            break
            if picker.own_count():
                log.error("%d orders may still rest: check the console and cancel them", picker.own_count())
                return 1
    log.info("stopped cleanly")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strat-id", required=True, help="strategy ID registered for the picker")
    parser.add_argument("--dry-run", action="store_true", help="log what it would take; send nothing")
    parser.add_argument("--seconds", type=float, default=None, help="stop after this long")
    args = parser.parse_args(argv)

    Path("logs").mkdir(exist_ok=True)
    qte_sdk.logs.configure()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(f"logs/stale-{date.today().isoformat()}.log")],
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
