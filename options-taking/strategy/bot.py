"""San Francisco, Options Taking: passive print capture on SPY and GOOGL options.

The bot rests a bid and an offer on the contracts nearest the forward, priced so that live
prints fill them below the mark on the bid and above it on the offer (see pricing.py). It
keeps each contract's position and the book's vega, gamma, gross and net delta inside a
fraction of the Rule 6005 limits, hedges option delta in the underlying (a qualifying hedge
under Rule 6006), stops opening risk after a soft daily loss, and cancels its quotes
before the close and when it stops.

Run from the project folder, where .env holds QTE_URL and QTE_TOKEN:

    python -m strategy.bot --strat-id <registered id> --dry-run   # sends nothing
    python -m strategy.bot --strat-id <registered id>             # trades

The strategy ID must be registered by the Team Lead, with SPY and GOOGL options and the
SPY and GOOGL stock (for hedging) in its declared scope.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
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
from qte_sdk.instruments import can_trade, instrument_info
from qte_sdk.market_data import Book, Mark, SessionState, Trades, as_market_data, subscribe
from qte_sdk.options import (
    OPTION_GREEKS_VALID,
    OPTION_ROLE_ACTIVE,
    OPTION_ROLE_OBLIGATED,
    OPTION_TRADING,
    LatestGreeks,
    OptionChain,
    chain_contracts,
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
    send_amend,
    send_cancel,
    send_new,
)
from qte_sdk.pacing import Budget, Pacer
from qte_sdk.reconnect import ReconnectingSession
from qte_sdk.resting import RestingOrders
from qte_sdk.units import to_decimal

from strategy.arb import Strike, scan
from strategy.config import Settings
from strategy.pricing import is_inside, padding, passive_quotes, reach
from strategy.risk import MULTIPLIER, Exposure, Leg, SoftLimits, adds_risk, hedge_shares, measure

log = logging.getLogger("sf_bot")

BUDGET_REJECTS = frozenset(
    {ReasonCodes.MESSAGE_BUDGET_EXCEEDED, ReasonCodes.BURST_CAP_EXCEEDED}
)
STATUS = Path("logs/bot-status.json")  # read by the dashboard
CONTROL = Path("control.json")  # written by the dashboard: {"paused": ["SPY", ...]}
PENDING_TIMEOUT = 3.0  # seconds before an unanswered message no longer blocks its quote
BUDGET_PAUSE = 61.0
SIDE_NAME = {BUY: "BUY", SELL: "SELL"}


@dataclass
class Slot:
    """One quote: a contract and side. What rests there comes from RestingOrders."""

    pending: str | None = None  # request_ref of the message in flight
    sent_at: float = 0.0


class Bot:
    def __init__(self, session: ReconnectingSession, strat_id: str, cfg: Settings, dry_run: bool,
                 hedging: bool = True):
        self.session = session
        self.hedging = hedging  # False when strategy/hedger.py hedges the book instead
        self.strat_id = strat_id
        self.cfg = cfg
        self.dry_run = dry_run

        self.books = LatestBooks()
        self.greeks = LatestGreeks()
        self.marks: dict[str, int] = {}  # LIVE marks only
        self.chain: OptionChain | None = None
        self.state: SessionState | None = None
        self.resting = RestingOrders()
        self.account = AccountReports()
        self.positions: dict[str, int] = {}
        self.account_known = False
        self.daily_pnl = Decimal(0)
        self.in_cure = False
        self.last_query = 0.0

        self.subscribed: set[str] = set()
        self.table_seen: object | None = None
        self.underlyings: list[str] = []
        self.quoting: list[str] = []  # contracts the bot quotes this session
        self.slots: dict[tuple[str, int], Slot] = {}
        self.hedge_pending: dict[str, tuple[str, float]] = {}  # ref -> (underlying, sent at)
        self.paused_until = 0.0  # after a budget reject
        self.blocked_until: dict[str, float] = {}  # a contract after an unexpected reject
        self.connected = False
        self.stopping = False
        self.last_status = 0.0
        self.dry_quotes: dict[tuple[str, int], tuple[int, int] | None] = {}
        self.paper_fills = 0
        self.last_arb_scan = 0.0
        self.paused: set[str] = set()  # underlyings paused from the dashboard
        self.sent_times: deque[float] = deque()  # order messages in the last minute
        self.last_status_file = 0.0
        self.control_mtime = 0.0
        self.arb_seen: set[tuple[str, str]] = set()
        self.paper_edge = Decimal(0)  # dollars against the mark, before the maker rebate
        self.last_grid_wall = 0.0  # wall clock of the last grid tick, to spot a stall
        self.hold_until = 0.0  # no quoting until then, after a stall

    # ---------- reading ----------

    async def on_event(self, event: object) -> None:
        self.resting.apply(event)
        for report in self.account.update(event):
            self.on_account_report(report)

        if isinstance(event, Connected):
            self.connected = True
            log.info("connected (reconnected=%s)", event.reconnected)
            self.account_known = False
            await self.query_account()
            return
        if isinstance(event, Disconnected):
            self.connected = False
            log.warning("disconnected: %s; quotes are uncertain until the resume", event.error)
            for slot in self.slots.values():
                slot.pending = None
            return
        if isinstance(event, ResumeComplete):
            log.info("resume complete; %d resting orders", len(self.resting))
            return

        table = self.session.instrument_table
        if table is not None and table is not self.table_seen:
            self.table_seen = table
            await self.subscribe_listed()

        item = as_market_data(event)
        if item is not None:
            self.on_market_data(item)
            if self.dry_run and isinstance(item, Trades):
                self.paper_fill(item)
            if isinstance(item, SessionState):
                await self.on_grid()
            return
        if is_order_event(event):
            self.on_order_event(event.message)  # type: ignore[attr-defined]

    async def subscribe_listed(self) -> None:
        """Subscribe to the underlyings and every listed contract not yet subscribed. The
        exchange sends the whole table again whenever a listing changes, such as at the
        open, when the day's contracts are listed."""
        table = self.session.instrument_table
        assert table is not None
        self.underlyings = list(option_underlyings(table))
        listed = [c for u in self.underlyings for c in listed_contracts(table, u)]
        new = [i for i in self.underlyings + listed if i not in self.subscribed]
        if new:
            log.info("underlyings %s, %d listed contracts; subscribing to %d new",
                     self.underlyings, len(listed), len(new))
            self.subscribed.update(new)
            await subscribe(self.session, new)

    def on_market_data(self, item: object) -> None:
        self.books.update(item)
        self.greeks.update(item)
        if isinstance(item, Mark):
            if item.condition == LIVE:
                self.marks[item.instrument] = item.value
            else:
                self.marks.pop(item.instrument, None)
        elif isinstance(item, OptionChain):
            self.chain = item
            self.quoting = []  # choose again from the new chain
        elif isinstance(item, SessionState):
            if self.state is None or self.state.session_date != item.session_date:
                self.quoting = []
            self.state = item

    def on_account_report(self, report: object) -> None:
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

    def on_order_event(self, message: object) -> None:
        ref = request_ref_of(message)
        if isinstance(message, Execution) and message.strat_id == self.strat_id:
            log.info(
                "FILL %s %s %d @ %s (%s, fee %s), position now %d",
                SIDE_NAME.get(message.side), message.instrument, message.fill_size,
                to_decimal(message.fill_price), message.fill_kind, to_decimal(message.fee),
                self.positions.get(message.instrument, 0),
            )
        if isinstance(message, Reject) and ref is not None:
            self.on_reject(message, ref)
        # Any report about one of our quotes settles the message in flight on it.
        instrument = getattr(message, "instrument", None)
        side = getattr(message, "side", None)
        slot = self.slots.get((instrument, side)) if instrument else None
        if slot is not None and not isinstance(message, Reject):
            slot.pending = None

    def on_reject(self, message: Reject, ref: str) -> None:
        for (instrument, side), slot in self.slots.items():
            if slot.pending == ref:
                slot.pending = None
                break
        else:
            instrument, side = None, None
        if any(ref == r for r in self.hedge_pending):
            self.hedge_pending.pop(ref, None)
        reason = message.reason_code
        name = reason_code_name(reason)
        if reason in BUDGET_REJECTS:
            log.warning("message budget reject (%s): pausing order messages for a minute", name)
            self.paused_until = time.monotonic() + BUDGET_PAUSE
        elif reason in (ReasonCodes.MIN_REST_VIOLATION, ReasonCodes.NO_ORDER_AT_LEVEL,
                        ReasonCodes.AMEND_PRICE_AT_OR_BEYOND_WALL):
            # The wall moved while the amend was in flight: the next tick requotes from the
            # fresh book, so there is nothing to block.
            log.debug("reject %s on %s %s", name, instrument, SIDE_NAME.get(side))
        else:
            log.warning("reject %s on %s %s", name, instrument, SIDE_NAME.get(side))
            if instrument:
                self.blocked_until[instrument] = time.monotonic() + 30.0

    def paper_fill(self, trades: Trades) -> None:
        """In a dry run, count the prints that would have filled the quotes we would rest: a
        sell print at P fills a bid at or above P - kappa*padding, and a buy print an offer."""
        g = self.greeks.get(trades.instrument)
        mark = self.marks.get(trades.instrument)
        if g is None or not g.HasField("vega") or mark is None:
            return
        out = reach(padding(g.vega))
        for p in trades.prints:
            log.info("PRINT %s %s %d @ %s kind=%s", trades.instrument,
                      SIDE_NAME.get(p.aggressor_side), p.size, to_decimal(p.price), p.kind)
            side = BUY if p.aggressor_side == SELL else SELL  # the side a print would fill
            quote = self.dry_quotes.get((trades.instrument, side))
            if quote is None:
                continue
            price, size = quote
            if (side == BUY and price >= p.price - out) or (side == SELL and price <= p.price + out):
                contracts = min(size, p.size)
                edge = to_decimal((mark - price) if side == BUY else (price - mark)) * 100 * contracts
                self.paper_fills += contracts
                self.paper_edge += edge
                log.info("PAPER FILL %s %s %d @ %s (print %s, mark %s, edge $%s); total %d contracts, $%s",
                         SIDE_NAME[side], trades.instrument, contracts, to_decimal(price),
                         to_decimal(p.price), to_decimal(mark), edge, self.paper_fills,
                         self.paper_edge)

    def read_control(self) -> None:
        """Pick up pauses set on the dashboard, when control.json changes."""
        try:
            mtime = CONTROL.stat().st_mtime
        except FileNotFoundError:
            mtime = 0.0
        if mtime == self.control_mtime:
            return
        self.control_mtime = mtime
        try:
            paused = set(json.loads(CONTROL.read_text()).get("paused", [])) if mtime else set()
        except (OSError, json.JSONDecodeError) as error:
            log.warning("control.json unreadable, keeping %s paused: %s", sorted(self.paused), error)
            return
        if paused != self.paused:
            log.warning("paused underlyings now %s", sorted(paused) or "none")
            self.paused = paused

    # ---------- deciding ----------

    def market_open(self) -> bool:
        s = self.state
        return s is not None and s.state == MarketSessionPhase.OPEN and not s.outage_active

    def winding_down(self) -> bool:
        s = self.state
        return self.stopping or (
            s is not None and s.close_time - s.grid_time < self.cfg.stop_before_close_seconds * 1000
        )

    def tick_of(self, instrument: str) -> int | None:
        table = self.session.instrument_table
        info = instrument_info(table, instrument) if table is not None else None
        if info is None or not can_trade(info) or info.tick_size <= 0:
            return None
        return info.tick_size

    def choose_contracts(self) -> list[str]:
        """Per underlying, the strikes nearest the forward in the nearest expiry open to new
        positions, with both the call and the put."""
        if self.chain is None:
            return []
        chosen: list[str] = []
        for underlying in self.underlyings:
            expiries = [
                e for e in self.chain.expiries if e.underlying == underlying and not e.reducing_only
            ]
            if not expiries:
                continue
            nearest = min(expiry_date(e) for e in expiries)
            contracts = [
                c.instrument
                for c in chain_contracts(self.chain, underlying=underlying, expiry=nearest)
                if c.role in (OPTION_ROLE_ACTIVE, OPTION_ROLE_OBLIGATED)
            ]
            forward = None
            for c in contracts:
                g = self.greeks.get(c)
                if g is not None and g.status == OPTION_GREEKS_VALID and g.HasField("forward"):
                    forward = g.forward
                    break
            if forward is None:
                continue
            strikes = sorted({parse_option_symbol(c).strike for c in contracts},
                             key=lambda k: abs(k - forward))
            keep = set(strikes[: self.cfg.strikes_per_underlying])
            chosen += [c for c in contracts if parse_option_symbol(c).strike in keep]
        return chosen

    def leg(self, instrument: str, qty: int) -> Leg | None:
        g = self.greeks.get(instrument)
        if g is None or g.status != OPTION_GREEKS_VALID:
            return None
        if not (g.HasField("delta") and g.HasField("gamma") and g.HasField("vega")):
            return None
        mark = self.marks.get(instrument)
        if mark is None:
            return None
        symbol = parse_option_symbol(instrument)
        return Leg(
            instrument=instrument,
            underlying=symbol.underlying,
            expiry=symbol.expiry,
            qty=qty,
            mark=to_decimal(mark),
            delta=greek_to_decimal(g.delta),
            gamma=greek_to_decimal(g.gamma),
            vega=to_decimal(g.vega),
        )

    def spots(self) -> dict[str, Decimal]:
        return {u: to_decimal(self.marks[u]) for u in self.underlyings if u in self.marks}

    def exposure(self) -> tuple[Exposure, bool]:
        """The book's exposure, and whether every option position could be measured."""
        legs, complete = [], True
        for instrument, qty in self.positions.items():
            if qty == 0 or not is_option_symbol(instrument):
                continue
            leg = self.leg(instrument, qty)
            if leg is None:
                complete = False
            else:
                legs.append(leg)
        return measure(legs, self.spots()), complete

    def soft_limits(self) -> SoftLimits:
        lim, f = self.cfg.limits, self.cfg.risk_fraction
        assert self.state is not None
        return SoftLimits(
            gross=lim.gross * f,
            net_delta=lim.net_delta_per_underlying * f,
            v0=lim.v0 * f,
            gamma=lim.gamma * f,
            today=date.fromisoformat(self.state.session_date),
        )

    def targets(self, contract: str, exposure: Exposure, limits: SoftLimits,
                complete: bool) -> dict[int, tuple[int, int] | None]:
        """For each side, the (price, size) to rest, or None to rest nothing."""
        none: dict[int, tuple[int, int] | None] = {BUY: None, SELL: None}
        if self.blocked_until.get(contract, 0.0) > time.monotonic():
            return none
        tick = self.tick_of(contract)
        book = self.books.get(contract)
        mark = self.marks.get(contract)
        if tick is None or book is None or mark is None or contract in self.books.stale:
            return none
        if trading_state(book) != OPTION_TRADING or book.condition != LIVE:
            return none
        if not book.bid_levels or not book.ask_levels:
            return none
        g = self.greeks.get(contract)
        if g is None or g.status != OPTION_GREEKS_VALID or not g.HasField("vega"):
            return none
        quote = passive_quotes(
            wall_bid=book.bid_levels[0].price,
            wall_ask=book.ask_levels[0].price,
            mark=mark,
            vega=g.vega,
            tick=tick,
            min_edge=int(self.cfg.min_edge * 1_000_000),
            improve_ticks=self.cfg.improve_ticks,
        )
        pos = self.positions.get(contract, 0)
        back_off = round(self.cfg.inventory_skew_ticks * abs(pos) / self.cfg.max_position) * tick
        bid, ask = quote.bid, quote.ask
        if pos > 0 and bid is not None:
            bid = bid - back_off if bid - back_off > book.bid_levels[0].price else None
        if pos < 0 and ask is not None:
            ask = ask + back_off if ask + back_off < book.ask_levels[0].price else None
        reduce_only = (
            not complete
            or self.in_cure
            or self.daily_pnl <= -self.cfg.soft_loss
        )
        leg = self.leg(contract, pos)
        if leg is None:
            return none
        stock = {u: self.positions.get(u, 0) * s for u, s in self.spots().items()}
        out = dict(none)
        max_order = min(self.cfg.quote_size, self.cfg.limits.max_option_order)
        for side, price in ((BUY, bid), (SELL, ask)):
            if price is None:
                continue
            sign = 1 if side == BUY else -1
            room = self.cfg.max_position - sign * pos  # contracts before the position cap
            if reduce_only:
                room = max(0, -sign * pos)  # only what brings the position to zero
            size = min(max_order, room)
            if size <= 0:
                continue
            if not reduce_only and adds_risk(exposure, leg, sign * size, self.spots(), limits, stock):
                continue
            out[side] = (price, size)
        return out

    # ---------- acting ----------

    async def on_grid(self) -> None:
        now = time.monotonic()
        if now - self.last_status > 30:
            self.last_status = now
            self.log_status()
        self.write_status(now)
        if not self.market_open() or not self.connected or self.resting.incomplete:
            return
        await self.check_stall(now)
        if now - self.last_query > self.cfg.account_query_seconds:
            await self.query_account()
        if not self.account_known:
            return
        if now - self.last_arb_scan > self.cfg.arb_scan_seconds:
            self.last_arb_scan = now
            self.scan_arbitrage()
        if not self.quoting:
            self.quoting = self.choose_contracts()
            if self.quoting:
                log.info("quoting %d contracts: %s", len(self.quoting), " ".join(self.quoting))
        if now < self.paused_until or now < self.hold_until:
            return

        self.read_control()
        down = self.winding_down()
        exposure, complete = self.exposure()
        limits = self.soft_limits()
        wanted: dict[tuple[str, int], tuple[int, int] | None] = {}
        for contract in self.quoting:
            paused = parse_option_symbol(contract).underlying in self.paused
            sides = (
                {BUY: None, SELL: None} if down or paused
                else self.targets(contract, exposure, limits, complete)
            )
            for side, target in sides.items():
                wanted[(contract, side)] = target
        # Our own orders on contracts no longer quoted are cancelled too.
        for order in self.resting:
            if order.strat_id == self.strat_id:
                wanted.setdefault((order.key.instrument, order.key.side), None)
        for (contract, side), target in wanted.items():
            if is_option_symbol(contract):
                await self.reconcile(contract, side, target, now)
        if not down and self.hedging:
            await self.hedge(exposure, now)

    async def check_stall(self, now: float) -> None:
        """Dead-man on wake: after a gap in grid ticks (the machine slept, or the link stalled)
        our quotes were unmanaged, so cancel them all, refresh the account, and hold quoting a
        moment before quoting again from fresh state."""
        wall = time.time()
        gap = wall - self.last_grid_wall if self.last_grid_wall else 0.0
        self.last_grid_wall = wall
        if gap < self.cfg.stall_seconds or self.dry_run:
            return
        log.warning("no grid ticks for %.0f s (asleep or link stalled): cancelling our quotes, "
                    "holding %.0f s", gap, self.cfg.stall_hold_seconds)
        await self.cancel_all()
        self.hold_until = now + self.cfg.stall_hold_seconds
        self.account_known = False
        await self.query_account()

    def scan_arbitrage(self) -> None:
        """Log no-arbitrage violations in each underlying's front expiry, priced at the walls
        (once per spread per session). Logged only: the bot does not trade them yet."""
        if self.chain is None:
            return
        fee = int(self.cfg.taker_fee_per_contract * 1_000_000) // 100
        for underlying in self.underlyings:
            expiries = [e for e in self.chain.expiries
                        if e.underlying == underlying and not e.reducing_only]
            if not expiries:
                continue
            nearest = min(expiry_date(e) for e in expiries)
            by_strike: dict[int, dict[str, tuple[int | None, int | None]]] = {}
            for c in chain_contracts(self.chain, underlying=underlying, expiry=nearest):
                book = self.books.get(c.instrument)
                if (book is None or c.instrument in self.books.stale or book.condition != LIVE
                        or trading_state(book) != OPTION_TRADING):
                    continue
                sym = parse_option_symbol(c.instrument)
                bid = book.bid_levels[0].price if book.bid_levels else None
                ask = book.ask_levels[0].price if book.ask_levels else None
                by_strike.setdefault(sym.strike, {})[sym.right] = (bid, ask)
            strikes = [
                Strike(k, *(v.get("C", (None, None))), *(v.get("P", (None, None))))
                for k, v in by_strike.items()
            ]
            for opp in scan(strikes, fee):
                key = (underlying, opp.legs)
                if key not in self.arb_seen:
                    self.arb_seen.add(key)
                    log.info("ARB %s %s %s: $%s per share after fees", underlying, opp.kind,
                             opp.legs, to_decimal(opp.profit))

    def own_order(self, instrument: str, side: int):
        for order in self.resting:
            if (order.strat_id == self.strat_id and order.key.instrument == instrument
                    and order.key.side == side):
                return order
        return None

    async def reconcile(self, contract: str, side: int, target: tuple[int, int] | None,
                        now: float) -> None:
        slot = self.slots.setdefault((contract, side), Slot())
        if self.dry_run:
            if self.dry_quotes.get((contract, side)) != target:
                self.dry_quotes[(contract, side)] = target
                log.info("WOULD QUOTE %s %s %s", contract, SIDE_NAME[side],
                         "nothing" if target is None
                         else f"{target[1]} @ {to_decimal(target[0])}")
            return
        if slot.pending is not None and now - slot.sent_at < PENDING_TIMEOUT:
            return
        resting = self.own_order(contract, side)
        if target is None:
            if resting is not None:
                await self.send(slot, now, send_cancel, instrument=contract, side=side,
                                price=resting.key.price)
            return
        price, size = target
        if resting is None:
            await self.send(slot, now, send_new, strat_id=self.strat_id, instrument=contract,
                            side=side, order_type=LIMIT, price=price, size=size)
            return
        if now - slot.sent_at < self.cfg.requote_seconds:
            return
        tick = self.tick_of(contract) or 1
        book = self.books.get(contract)
        endangered = book is not None and book.bid_levels and book.ask_levels and not is_inside(
            resting.key.price, book.bid_levels[0].price, book.ask_levels[0].price)
        moved = abs(resting.key.price - price) >= self.cfg.requote_ticks * tick
        refill = resting.remaining_size * 2 < size
        if endangered or moved or refill:
            await self.send(slot, now, send_amend, instrument=contract, side=side,
                            price=resting.key.price, new_size=size, new_price=price)

    def count_message(self) -> None:
        now = time.monotonic()
        self.sent_times.append(now)
        while self.sent_times and now - self.sent_times[0] > 60:
            self.sent_times.popleft()

    def write_status(self, now: float) -> None:
        """A small status file for the dashboard, every couple of seconds."""
        if now - self.last_status_file < 2:
            return
        self.last_status_file = now
        while self.sent_times and now - self.sent_times[0] > 60:
            self.sent_times.popleft()
        status = {
            "t": time.time(),
            "strat_id": self.strat_id,
            "dry_run": self.dry_run,
            "market_open": self.market_open(),
            "messages_last_minute": len(self.sent_times),
            "budget_per_minute": self.cfg.limits.sustained_per_minute,
            "budget_paused": now < self.paused_until,
            "paused": sorted(self.paused),
            "quoting": len(self.quoting),
            "resting": self.own_count(),
            "paper_fills": self.paper_fills,
            "paper_edge": float(self.paper_edge),
            "daily_pnl": float(self.daily_pnl),
            "in_cure": self.in_cure,
            "hedging": self.hedging,
        }
        with contextlib.suppress(OSError):
            tmp = STATUS.with_suffix(".tmp")
            tmp.write_text(json.dumps(status))
            tmp.replace(STATUS)

    async def send(self, slot: Slot, now: float, fn, **kwargs) -> None:
        self.count_message()
        ref = new_request_ref()
        slot.pending, slot.sent_at = ref, now
        try:
            await fn(self.session, request_ref=ref, **kwargs)
        except Exception as error:  # a send cut short by a drop: the resume settles it
            log.warning("send failed: %s", error)
            slot.pending = None

    async def hedge(self, exposure: Exposure, now: float) -> None:
        lim = self.cfg.limits
        cooldown = self.cfg.hedge_cooldown_seconds
        self.hedge_pending = {r: v for r, v in self.hedge_pending.items() if now - v[1] < cooldown}
        for underlying in self.underlyings:
            spot = self.marks.get(underlying)
            if spot is None:
                continue
            if any(u == underlying for u, _ in self.hedge_pending.values()):
                continue
            shares = hedge_shares(
                exposure.option_delta.get(underlying, Decimal(0)),
                self.positions.get(underlying, 0),
                to_decimal(spot),
                threshold=self.cfg.hedge_threshold,
                max_order=lim.max_hedge_order,
                tolerance=lim.hedge_tolerance,
                buffer=(self.cfg.hedge_buffer_fills * self.cfg.quote_size * MULTIPLIER
                        * self.cfg.hedge_buffer_delta * to_decimal(spot)),
            )
            book = self.books.get(underlying)
            if shares == 0 or book is None or not book.bid_levels or not book.ask_levels:
                continue
            side = BUY if shares > 0 else SELL
            # At the wall's touch: fills what level one shows, the rest is cancelled.
            price = book.ask_levels[0].price if side == BUY else book.bid_levels[0].price
            if self.dry_run:
                log.info("WOULD HEDGE %s %s %d @ %s", SIDE_NAME[side], underlying, abs(shares),
                         to_decimal(price))
                self.hedge_pending[f"dry-{underlying}"] = (underlying, now)
                continue
            ref = new_request_ref()
            self.hedge_pending[ref] = (underlying, now)
            log.info("HEDGE %s %s %d @ %s", SIDE_NAME[side], underlying, abs(shares),
                     to_decimal(price))
            try:
                self.count_message()
                await send_new(self.session, strat_id=self.strat_id, instrument=underlying,
                               side=side, order_type=LIMIT, price=price, size=abs(shares),
                               request_ref=ref)
            except Exception as error:
                log.warning("hedge send failed: %s", error)

    async def query_account(self) -> None:
        self.last_query = time.monotonic()
        with contextlib.suppress(Exception):
            await self.account.query(self.session)

    async def cancel_all(self) -> None:
        """Cancel each of this strategy's resting orders, level by level."""
        for order in list(self.resting):
            if order.strat_id == self.strat_id:
                with contextlib.suppress(Exception):
                    await send_cancel(self.session, instrument=order.key.instrument,
                                      side=order.key.side, price=order.key.price)

    def own_count(self) -> int:
        return sum(1 for o in self.resting if o.strat_id == self.strat_id)

    def log_status(self) -> None:
        held = {k: v for k, v in self.positions.items() if v}
        exposure, _ = self.exposure() if self.state else (None, True)
        log.info(
            "status: open=%s pnl=%s cure=%s resting=%d positions=%s delta=%s gamma=%s paper=%d/$%s",
            self.market_open(), self.daily_pnl, self.in_cure, self.own_count(), held,
            {k: round(v) for k, v in exposure.option_delta.items()} if exposure else {},
            {k: round(v) for k, v in exposure.gamma.items()} if exposure else {},
            self.paper_fills, self.paper_edge,
        )


def keep_awake() -> None:
    """On a Mac, hold off idle and system sleep for as long as this process lives: asleep, the
    bot cannot manage its quotes but they stay live on the exchange. A closed lid still
    sleeps, and on battery `caffeinate -s` does not apply, so warn about that."""
    if sys.platform != "darwin":
        return
    with contextlib.suppress(OSError):
        subprocess.Popen(["caffeinate", "-is", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        power = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
        if "Battery Power" in power:
            log.warning("on battery: plug in, or the Mac may sleep with our quotes live")


async def run(args: argparse.Namespace) -> int:
    cfg = Settings()
    lim = cfg.limits
    pacer = Pacer(Budget(sustained_per_minute=lim.sustained_per_minute,
                         burst_per_second=lim.burst_per_second))
    session = ReconnectingSession(pacing=pacer)
    bot = Bot(session, args.strat_id, cfg, args.dry_run, hedging=not args.no_hedge)
    if not args.dry_run:
        keep_awake()

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, task.cancel)
        loop.add_signal_handler(signal.SIGTERM, task.cancel)

    # A ReconnectingSession can be iterated only once, and cancelling the loop that iterates
    # it ends the iteration; so a pump task owns it and the shutdown keeps reading the queue.
    queue: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        async for event in session:
            await queue.put(event)

    async with session:
        pumping = asyncio.create_task(pump())
        try:
            async with asyncio.timeout(args.seconds):
                while True:
                    await bot.on_event(await queue.get())
        except TimeoutError:
            log.info("time is up: cancelling this strategy's quotes")
        except asyncio.CancelledError:
            log.info("interrupted: cancelling this strategy's quotes (Ctrl+C again to quit)")
            task.uncancel()
        bot.stopping = True
        if not args.dry_run:
            await bot.cancel_all()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(5):
                    while bot.own_count():
                        await bot.on_event(await queue.get())
        pumping.cancel()
        left = bot.own_count() if not args.dry_run else 0
        if left:
            log.error("%d orders may still rest: check the console and cancel them", left)
            return 1
    log.info("stopped cleanly")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strat-id", required=True, help="strategy ID registered for the team")
    parser.add_argument("--dry-run", action="store_true",
                        help="read the market and log what it would send; send nothing")
    parser.add_argument("--no-hedge", action="store_true",
                        help="leave delta hedging to strategy/hedger.py")
    parser.add_argument("--seconds", type=float, default=None,
                        help="stop after this long (default: run until Ctrl+C)")
    args = parser.parse_args(argv)

    Path("logs").mkdir(exist_ok=True)
    qte_sdk.logs.configure()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(f"logs/bot-{date.today().isoformat()}.log"),
        ],
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
