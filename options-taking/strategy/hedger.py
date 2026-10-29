"""San Francisco, Options Taking: the team-wide delta hedger.

It holds no options and quotes nothing. It reads the whole team book (every strategy's
option positions, with the exchange's published Greeks) and hedges each underlying's option
delta in SPY or GOOGL stock, keeping the stock a qualifying hedge under Rule 6006. One
process hedges for every strategy, so no fill is hedged twice: run the quoter with
--no-hedge. If the quoter's status file says it is hedging too, this sends nothing.

Hedges cross the stock wall, walking its ladder up to `max_slippage_bp` past the touch;
what the wall does not fill is cancelled by the exchange, and the next grid tick tries
again.

    python -m strategy.hedger --strat-id <registered id> --dry-run   # logs, sends nothing
    python -m strategy.hedger --strat-id <registered id>             # trades
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
from qte_sdk.market_data import Mark, SessionState, as_market_data, subscribe
from qte_sdk.options import (
    OPTION_GREEKS_VALID,
    LatestGreeks,
    greek_to_decimal,
    is_option_symbol,
    listed_contracts,
    option_underlyings,
    parse_option_symbol,
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

from strategy.config import HedgerSettings
from strategy.risk import MULTIPLIER, hedge_shares

log = logging.getLogger("sf_hedger")

STATUS = Path("logs/hedger-status.json")  # read by the dashboard
QUOTER_STATUS = Path("logs/bot-status.json")
CONTROL = Path("control.json")  # written by the dashboard: {"stop": {"hedger": time}}
BUDGET_REJECTS = frozenset({ReasonCodes.MESSAGE_BUDGET_EXCEEDED, ReasonCodes.BURST_CAP_EXCEEDED})
SIDE_NAME = {BUY: "BUY", SELL: "SELL"}


class Hedger:
    def __init__(self, session: ReconnectingSession, strat_id: str, cfg: HedgerSettings, dry_run: bool):
        self.session = session
        self.strat_id = strat_id
        self.cfg = cfg
        self.dry_run = dry_run

        self.books = LatestBooks()
        self.greeks = LatestGreeks()
        self.marks: dict[str, int] = {}
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
        self.pending: dict[str, tuple[str, float]] = {}  # request_ref -> (underlying, sent at)
        self.last_sent: dict[str, float] = {}  # underlying -> monotonic time of last hedge
        self.paused_until = 0.0
        self.blocked_until: dict[str, float] = {}
        self.quoter_hedging = False
        self.last_warn: dict[str, float] = {}
        self.sent_times: deque[float] = deque()
        self.last_status_file = 0.0

        self.last_delta: dict[str, Decimal] = {}  # last valid published delta per contract
        self.view: dict[str, dict] = {}  # per underlying, for the status file
        self.hedges = 0
        self.shares_filled = 0
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
            self.pending.clear()
            log.warning("disconnected: %s; no hedges until the resume", event.error)
            return
        if isinstance(event, ResumeComplete):
            log.info("resume complete")
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
            elif isinstance(item, SessionState):
                self.state = item
                await self.on_grid()
            return
        if is_order_event(event):
            self.on_order_event(event.message)  # type: ignore[attr-defined]

    def on_order_event(self, message: object) -> None:
        ref = request_ref_of(message)
        if isinstance(message, Execution) and message.strat_id == self.strat_id:
            self.shares_filled += message.fill_size
            log.info("HEDGE FILL %s %s %d @ %s, now holding %d", SIDE_NAME.get(message.side),
                     message.instrument, message.fill_size, to_decimal(message.fill_price),
                     self.positions.get(message.instrument, 0))
        if isinstance(message, Reject) and ref in self.pending:
            underlying = self.pending[ref][0]
            reason = message.reason_code
            if reason in BUDGET_REJECTS:
                log.warning("message budget reject (%s): pausing for a minute", reason_code_name(reason))
                self.paused_until = time.monotonic() + 61.0
            else:
                log.warning("hedge reject %s on %s: holding that underlying 10 s",
                            reason_code_name(reason), underlying)
                self.blocked_until[underlying] = time.monotonic() + 10.0
        # Any report on a hedge settles it; the next grid tick works from the new position.
        if ref is not None:
            self.pending.pop(ref, None)

    # ---------- deciding ----------

    def trading(self) -> bool:
        s = self.state
        return (
            s is not None and s.state == MarketSessionPhase.OPEN and not s.outage_active
            and s.close_time - s.grid_time > self.cfg.stop_before_close_seconds * 1000
            and not self.stopping and self.connected and self.account_known
            and not self.resting.incomplete
        )

    def warn(self, key: str, text: str, *args) -> None:
        now = time.monotonic()
        if now - self.last_warn.get(key, 0.0) > 60:
            self.last_warn[key] = now
            log.warning(text, *args)

    def option_delta(self, underlying: str) -> Decimal | None:
        """Dollar delta of the team's options in one underlying, from the published Greeks
        (Rule 6003), or None if any position cannot be measured."""
        spot = self.marks.get(underlying)
        if spot is None:
            return None
        total = Decimal(0)
        for instrument, qty in self.positions.items():
            if qty == 0 or not is_option_symbol(instrument):
                continue
            if parse_option_symbol(instrument).underlying != underlying:
                continue
            g = self.greeks.get(instrument)
            if g is not None and g.status == OPTION_GREEKS_VALID and g.HasField("delta"):
                self.last_delta[instrument] = greek_to_decimal(g.delta)
            # A failed Greek calculation keeps the last Greeks (Rule 6003): so do we.
            delta = self.last_delta.get(instrument)
            if delta is None:
                self.warn(f"greeks-{underlying}", "no Greeks yet for %s: not hedging %s",
                          instrument, underlying)
                return None
            total += qty * MULTIPLIER * delta * to_decimal(spot)
        return total

    def read_quoter(self) -> None:
        """Whether the quoter is running live and hedging too, from its status file."""
        try:
            data = json.loads(QUOTER_STATUS.read_text())
        except (OSError, json.JSONDecodeError):
            self.quoter_hedging = False
            return
        fresh = time.time() - data.get("t", 0) < 10
        self.quoter_hedging = fresh and not data.get("dry_run", True) and data.get("hedging", True)

    # ---------- acting ----------

    async def on_grid(self) -> None:
        now = time.monotonic()
        if self.connected and now - self.last_query > self.cfg.account_query_seconds:
            await self.query_account()
        self.pending = {r: v for r, v in self.pending.items()
                        if now - v[1] < self.cfg.pending_timeout_seconds}
        self.read_quoter()
        for underlying in self.underlyings:
            await self.hedge(underlying, now)
        self.write_status(now)

    async def hedge(self, underlying: str, now: float) -> None:
        spot = self.marks.get(underlying)
        delta = self.option_delta(underlying)
        shares_held = self.positions.get(underlying, 0)
        view = {"option_delta": None if delta is None else float(delta), "stock_shares": shares_held,
                "spot": None if spot is None else float(to_decimal(spot)), "want": 0}
        self.view[underlying] = view
        if spot is None or delta is None:
            return
        spot_d = to_decimal(spot)
        view["stock_notional"] = float(shares_held * spot_d)
        view["net_delta"] = float(delta + shares_held * spot_d)

        lim = self.cfg.limits
        # hedge_shares trades once |options + stock| passes `threshold`, and then hedges
        # only the option delta beyond `buffer`, which leaves net delta at the target.
        shares = hedge_shares(
            delta, shares_held, spot_d,
            threshold=lim.net_delta_per_underlying * self.cfg.trigger_fraction,
            max_order=lim.max_hedge_order,
            tolerance=self.cfg.tolerance,
            buffer=lim.net_delta_per_underlying * self.cfg.target_fraction,
        )
        view["want"] = shares
        if shares == 0 or not self.trading() or now < self.paused_until:
            return
        if self.blocked_until.get(underlying, 0.0) > now:
            return
        if any(u == underlying for u, _ in self.pending.values()):
            return
        if now - self.last_sent.get(underlying, 0.0) < self.cfg.min_interval_seconds:
            return
        if self.quoter_hedging:
            self.warn("quoter", "the quoter is live and hedging too: restart it with --no-hedge. "
                      "Sending nothing so the book is not hedged twice.")
            return

        book = self.books.get(underlying)
        if book is None or underlying in self.books.stale:
            return
        side = BUY if shares > 0 else SELL
        levels = book.ask_levels if side == BUY else book.bid_levels
        if not levels:
            return
        # Walk the wall's ladder to the deepest level within the slippage cap, so one order
        # takes what is shown there; the exchange cancels any remainder.
        touch = levels[0].price
        reach = int(touch * self.cfg.max_slippage_bp / 10_000)
        price, available = touch, 0
        for level in levels:
            if abs(level.price - touch) > reach:
                break
            price, available = level.price, available + level.size
        size = min(abs(shares), available)
        if size <= 0:
            return

        self.last_sent[underlying] = now
        row = {"t": time.time(), "underlying": underlying, "side": SIDE_NAME[side], "size": size,
               "price": float(to_decimal(price)), "option_delta": float(delta),
               "net_before": view["net_delta"]}
        self.recent.appendleft(row)
        if self.dry_run:
            log.info("WOULD HEDGE %s %s %d @ %s (option delta $%s, stock %d shares)",
                     SIDE_NAME[side], underlying, size, to_decimal(price), round(delta), shares_held)
            return
        ref = new_request_ref()
        self.pending[ref] = (underlying, now)
        self.hedges += 1
        log.info("HEDGE %s %s %d @ %s (option delta $%s, stock %d shares)",
                 SIDE_NAME[side], underlying, size, to_decimal(price), round(delta), shares_held)
        self.count_message()
        try:
            await send_new(self.session, strat_id=self.strat_id, instrument=underlying, side=side,
                           order_type=LIMIT, price=price, size=size, request_ref=ref)
        except Exception as error:
            log.warning("hedge send failed: %s", error)
            self.pending.pop(ref, None)

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
            "trading": self.trading(), "quoter_hedging": self.quoter_hedging,
            "messages_last_minute": len(self.sent_times),
            "budget_per_minute": self.cfg.sustained_per_minute,
            "budget_paused": now < self.paused_until,
            "hedges": self.hedges, "shares_filled": self.shares_filled,
            "underlyings": self.view, "recent": list(self.recent),
            "trigger": float(self.cfg.limits.net_delta_per_underlying * self.cfg.trigger_fraction),
            "target": float(self.cfg.limits.net_delta_per_underlying * self.cfg.target_fraction),
        }
        with contextlib.suppress(OSError):
            tmp = STATUS.with_suffix(".tmp")
            tmp.write_text(json.dumps(status))
            tmp.replace(STATUS)


async def run(args: argparse.Namespace) -> int:
    cfg = HedgerSettings()
    pacer = Pacer(Budget(sustained_per_minute=cfg.sustained_per_minute,
                         burst_per_second=cfg.burst_per_second))
    session = ReconnectingSession(pacing=pacer)
    hedger = Hedger(session, args.strat_id, cfg, args.dry_run)

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, task.cancel)
        loop.add_signal_handler(signal.SIGTERM, task.cancel)

    # Stop from the dashboard: it writes {"stop": {"hedger": time}} to control.json, and a
    # request newer than this start shuts down as Ctrl+C does, cancelling our orders first.
    started = time.time()

    async def watch_stop() -> None:
        while True:
            await asyncio.sleep(0.5)
            with contextlib.suppress(OSError, ValueError, AttributeError, TypeError):
                if json.loads(CONTROL.read_text()).get("stop", {}).get("hedger", 0) > started:
                    log.info("stop requested from the dashboard")
                    task.cancel()
                    return

    # A ReconnectingSession can be iterated only once, so a pump task owns it (as in bot.py).
    queue: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        async for event in session:
            await queue.put(event)

    async with session:
        pumping = asyncio.create_task(pump())
        watching = asyncio.create_task(watch_stop())
        try:
            async with asyncio.timeout(args.seconds):
                while True:
                    await hedger.on_event(await queue.get())
        except TimeoutError:
            log.info("time is up")
        except asyncio.CancelledError:
            log.info("interrupted: stopping (Ctrl+C again to quit)")
            task.uncancel()
        hedger.stopping = True
        watching.cancel()
        log.info("hedges sent %d, %d shares filled", hedger.hedges, hedger.shares_filled)
        if not args.dry_run and hedger.own_count():
            # Hedges never rest, but a remainder caught mid-cancel could: clear it.
            for order in list(hedger.resting):
                if order.strat_id == hedger.strat_id:
                    with contextlib.suppress(Exception):
                        await send_cancel(session, instrument=order.key.instrument,
                                          side=order.key.side, price=order.key.price)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(3):
                    while hedger.own_count():
                        await hedger.on_event(await queue.get())
        pumping.cancel()
    log.info("stopped. Nothing hedges the book now unless the quoter runs without --no-hedge.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strat-id", required=True, help="strategy ID registered for the hedger")
    parser.add_argument("--dry-run", action="store_true", help="log what it would hedge; send nothing")
    parser.add_argument("--seconds", type=float, default=None, help="stop after this long")
    args = parser.parse_args(argv)

    Path("logs").mkdir(exist_ok=True)
    qte_sdk.logs.configure()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(f"logs/hedger-{date.today().isoformat()}.log")],
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
