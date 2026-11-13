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
from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT, SELL, LimitKind, MarketSessionPhase, ReasonCodes
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

from strategy.bot import no_quickedit
from strategy.bs import black76, implied_vol
from strategy.config import HedgerSettings
from strategy.risk import MULTIPLIER, band_hedge_shares, hedge_shares, ww_band

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
        # After a HEDGE_ONLY refusal: no stock that adds to a hedge, either underlying, until then.
        self.additions_held_until = 0.0
        self.unpriced: dict[str, frozenset] = {}  # underlying -> held contracts with no published delta
        self.quoter_hedging = False
        self.last_warn: dict[str, float] = {}
        self.sent_times: deque[float] = deque()
        self.last_status_file = 0.0

        self.last_delta: dict[str, Decimal] = {}  # last valid published delta per contract
        self.last_gamma: dict[str, Decimal] = {}  # and gamma, per share per $1
        self.forwards: dict[tuple[str, date], float] = {}  # (underlying, expiry) -> forward
        # The exchange's own net delta per underlying (its Rule 6005 limit row), when it
        # last arrived, and from it the exchange's delta for our contracts that have no
        # published Greeks: underlying -> (dollar delta, those contracts, when).
        self.exchange_net: dict[str, Decimal] = {}
        self.exchange_unpriced: dict[str, tuple[Decimal, frozenset, float]] = {}
        # (underlying, expiry) -> {strike: published implied vol, a fraction}
        self.vols: dict[tuple[str, date], dict[float, float]] = {}
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
                    for row in message.summary.limits:
                        if row.kind == LimitKind.LIMIT_NET_DELTA_UNDERLYING and row.HasField("scope"):
                            self.exchange_net[row.scope] = to_decimal(row.used)
                    for underlying in self.underlyings:
                        self.read_exchange_delta(underlying)
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
            if getattr(item, "forward", 0) and is_option_symbol(getattr(item, "instrument", "")):
                symbol = parse_option_symbol(item.instrument)
                self.forwards[(symbol.underlying, symbol.expiry)] = item.forward / 1_000_000
                if getattr(item, "implied_vol", 0) and item.status == OPTION_GREEKS_VALID:
                    # published in millionths of a vol point: 18_000_000 is 18%
                    self.vols.setdefault((symbol.underlying, symbol.expiry), {})[
                        symbol.strike / 1_000_000] = item.implied_vol / 100_000_000
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
            elif reason == ReasonCodes.HEDGE_ONLY:
                # The exchange will not count added stock as a qualifying hedge (Rule 6006).
                # It refuses additions in both underlyings alike and still takes reductions,
                # so hold additions everywhere for a while instead of retrying every 2 s.
                hold = self.cfg.hedge_only_hold_seconds
                self.additions_held_until = time.monotonic() + hold
                unpriced = sorted(set().union(*self.unpriced.values()))
                if unpriced:
                    log.warning("hedge reject HEDGE_ONLY on %s: no stock additions in any underlying "
                                "for %d s (reductions still go). We hold contracts with no published "
                                "Greeks (%s); on 12 Nov every addition was refused until SPY 785P had "
                                "Greeks again, so close them to hedge normally.",
                                underlying, hold, ", ".join(unpriced))
                else:
                    log.warning("hedge reject HEDGE_ONLY on %s: no stock additions in any underlying "
                                "for %d s (reductions still go). Check the cure pill and Limits tab.",
                                underlying, hold)
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

    def split_delta(self, underlying: str) -> tuple[Decimal, Decimal, frozenset] | None:
        """The team's options in one underlying, in dollars of delta: the part with
        published Greeks (Rule 6003; a failed calculation keeps the last ones), our own
        Black-76 value of the part without (see model_greeks), and which contracts those
        are. None if a contract cannot be valued at all."""
        spot = self.marks.get(underlying)
        if spot is None:
            return None
        published, modelled, unpriced = Decimal(0), Decimal(0), set()
        for instrument, qty in self.positions.items():
            if qty == 0 or not is_option_symbol(instrument):
                continue
            if parse_option_symbol(instrument).underlying != underlying:
                continue
            g = self.greeks.get(instrument)
            if g is not None and g.status == OPTION_GREEKS_VALID and g.HasField("delta"):
                self.last_delta[instrument] = greek_to_decimal(g.delta)
            if g is not None and g.status == OPTION_GREEKS_VALID and g.HasField("gamma"):
                self.last_gamma[instrument] = greek_to_decimal(g.gamma)
            delta = self.last_delta.get(instrument)
            if delta is not None:
                published += qty * MULTIPLIER * delta * to_decimal(spot)
                continue
            model = self.model_greeks(instrument)
            if model is None:
                self.warn(f"greeks-{underlying}", "no Greeks and no prices to value %s: not hedging %s",
                          instrument, underlying)
                return None
            self.last_gamma[instrument] = model[1]
            modelled += qty * MULTIPLIER * model[0] * to_decimal(spot)
            unpriced.add(instrument)
        return published, modelled, frozenset(unpriced)

    def read_exchange_delta(self, underlying: str) -> None:
        """When the account summary arrives, work out from the exchange's own net delta for
        the underlying what delta the exchange gives our contracts that have no published
        Greeks: its net delta, less our stock and less the part we value from published
        Greeks. Its net delta may be reported as a magnitude, so of the two signs take
        the one nearer our own Black-76 value. Only that part is taken from the report:
        the rest stays live, since the report is a few seconds old after any fill."""
        used = self.exchange_net.get(underlying)
        split = self.split_delta(underlying)
        spot = self.marks.get(underlying)
        if used is None or split is None or spot is None or not split[2]:
            self.exchange_unpriced.pop(underlying, None)
            return
        published, modelled, unpriced = split
        stock = self.positions.get(underlying, 0) * to_decimal(spot)
        candidates = [used - stock - published] + ([-used - stock - published] if used > 0 else [])
        exchange = min(candidates, key=lambda c: abs(c - modelled))
        self.exchange_unpriced[underlying] = (exchange, unpriced, time.time())
        self.warn(f"exchange-{underlying}", "%s: the exchange's net delta $%s puts our contracts with no "
                  "published Greeks (%s) at $%s of delta; our Black-76 value is $%s",
                  underlying, round(used), ", ".join(sorted(unpriced)), round(exchange), round(modelled))

    def option_delta(self, underlying: str) -> Decimal | None:
        """Dollar delta of the team's options in one underlying, or None if any position
        cannot be measured. Contracts with published Greeks use them; for any without, the
        exchange's own figure is used when its account summary is under a minute old and
        covers the same contracts, and our Black-76 value otherwise."""
        split = self.split_delta(underlying)
        if split is None:
            return None
        published, modelled, unpriced = split
        self.unpriced[underlying] = unpriced
        if not unpriced:
            return published
        exchange = self.exchange_unpriced.get(underlying)
        if exchange is not None and exchange[1] == unpriced and time.time() - exchange[2] < 60:
            return published + exchange[0]
        self.warn(f"model-{underlying}", "no published Greeks for %s: using our Black-76 delta, $%s",
                  ", ".join(sorted(unpriced)), round(modelled))
        return published + modelled

    def model_greeks(self, instrument: str) -> tuple[Decimal, Decimal] | None:
        """Delta and gamma (per share, gamma per $1) of a contract the exchange publishes
        no Greeks for, by Black-76 on the published forward for its expiry (the spot mark
        if none is known), at a volatility from, in order:
          1. its own official mark (the vol that prices it there);
          2. the published implied vol of the nearest strike in the same expiry, or in any
             expiry of that underlying (on 10 Nov the exchange published neither Greeks
             nor a mark for a deep in-the-money SPY put the team held);
        and if neither is there, delta +1 or -1 in the money and 0 out of it. A mark at
        or below intrinsic, where no vol fits, also counts as +1 or -1."""
        if self.state is None:
            return None
        symbol = parse_option_symbol(instrument)
        spot = self.marks.get(symbol.underlying)
        forward = self.forwards.get((symbol.underlying, symbol.expiry))
        if forward is None:
            if spot is None:
                return None
            forward = spot / 1_000_000
        call = symbol.right == "C"
        strike = symbol.strike / 1_000_000
        days = (symbol.expiry - date.fromisoformat(self.state.session_date)).days
        years = max(days, 1) / 365
        itm_delta = Decimal((1 if call else -1) if (forward > strike if call else forward < strike) else 0)

        vol = None
        mark = self.marks.get(instrument)
        if mark is not None:
            vol = implied_vol(call, mark / 1_000_000, forward, strike, years)
            if vol is None:
                return itm_delta, Decimal(0)  # at or below intrinsic
        if vol is None:
            same = self.vols.get((symbol.underlying, symbol.expiry))
            pool = same or {k: v for (u, _), smile in self.vols.items() if u == symbol.underlying
                            for k, v in smile.items()}
            if pool:
                vol = pool[min(pool, key=lambda k: abs(k - strike))]
        if vol is None:
            return itm_delta, Decimal(0)
        value = black76(call, forward, strike, years, vol)
        return Decimal(str(value.delta)), Decimal(str(value.gamma))

    def option_gamma(self, underlying: str) -> Decimal:
        """Net gamma of the team's options in one underlying, in shares per $1 move. A
        contract with no gamma yet counts as zero, which only narrows the band."""
        total = Decimal(0)
        for instrument, qty in self.positions.items():
            if qty == 0 or not is_option_symbol(instrument):
                continue
            if parse_option_symbol(instrument).underlying != underlying:
                continue
            total += qty * MULTIPLIER * self.last_gamma.get(instrument, Decimal(0))
        return total

    def hedge_cost(self, underlying: str) -> Decimal:
        """Cost of hedging as a fraction of notional: half the wall's spread now, half the
        ladder walk we allow, and the taker fee."""
        half_bp = self.cfg.default_half_spread_bp
        book = self.books.get(underlying)
        if book is not None and book.bid_levels and book.ask_levels:
            bid, ask = book.bid_levels[0].price, book.ask_levels[0].price
            if ask > bid > 0:
                half_bp = Decimal(ask - bid) / Decimal(ask + bid) * 10_000
        return (half_bp + self.cfg.max_slippage_bp / 2 + self.cfg.stock_taker_fee_bp) / 10_000

    def band(self, underlying: str, spot: Decimal) -> Decimal:
        """No-trade half-width in dollars of net delta for one underlying."""
        if self.cfg.band_mode == "fixed":
            return self.cfg.limits.net_delta_per_underlying * self.cfg.trigger_fraction
        return ww_band(self.option_gamma(underlying), spot, self.hedge_cost(underlying),
                       risk_aversion=self.cfg.risk_aversion,
                       floor=self.cfg.band_floor, cap=self.cfg.band_cap)

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
        # A held contract can leave the listed window (deep in or out of the money); we
        # still need its mark to value it, so subscribe to every option the team holds.
        held = [i for i, q in self.positions.items()
                if q and is_option_symbol(i) and i not in self.subscribed]
        if held and self.connected:
            self.subscribed.update(held)
            with contextlib.suppress(Exception):
                await subscribe(self.session, held)
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
        band = self.band(underlying, spot_d)
        view["band"] = float(band)
        view["gamma_shares"] = float(self.option_gamma(underlying))
        if self.cfg.band_mode == "fixed":
            # hedge_shares trades once |options + stock| passes `threshold`, and then hedges
            # only the option delta beyond `buffer`, which leaves net delta at the target.
            shares = hedge_shares(
                delta, shares_held, spot_d,
                threshold=band,
                max_order=lim.max_hedge_order,
                tolerance=self.cfg.tolerance,
                buffer=lim.net_delta_per_underlying * self.cfg.target_fraction,
            )
        else:
            # Whalley-Wilmott: nothing inside the band, and back to its edge when outside.
            shares = band_hedge_shares(
                delta, shares_held, spot_d,
                band=band, rehedge_to=self.cfg.rehedge_to,
                max_order=lim.max_hedge_order, tolerance=self.cfg.tolerance,
            )
        if abs(shares_held + shares) > abs(shares_held):
            # Adding to the hedge. Stay inside both hedge allowances (Rule 6005): stock past
            # them is refused RISK_LIMIT_BREACH, and the option delta beyond them cannot be
            # hedged at all, so say so.
            other = sum((abs(q) * to_decimal(self.marks[i]) for i, q in self.positions.items()
                         if i != underlying and not is_option_symbol(i) and i in self.marks), Decimal(0))
            room = min(lim.hedge_allowance_underlying, lim.hedge_allowance_book - other) - self.cfg.allowance_margin
            most = max(0, int(room / spot_d))
            new = max(-most, min(most, shares_held + shares))
            if new != shares_held + shares:
                view["capped"] = True
                self.warn(f"allowance-{underlying}", "%s: stock hedge capped at %d shares ($%s) by the hedge "
                          "allowance (Rule 6005). $%s of option delta cannot be hedged with stock; net "
                          "delta will be $%s. The strategies now only take trades that cut %s option "
                          "delta until it is back under the cap.", underlying, abs(new), round(room),
                          round(abs(delta) - abs(new) * spot_d), round(delta + new * spot_d), underlying)
            shares = new - shares_held if abs(new) > abs(shares_held) else 0
            if shares and now < self.additions_held_until:
                view["additions_held"] = True
                shares = 0
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
            "additions_held_for": max(0.0, self.additions_held_until - now),
            "unpriced": sorted(set().union(*self.unpriced.values())),
            "hedges": self.hedges, "shares_filled": self.shares_filled,
            "underlyings": self.view, "recent": list(self.recent),
            "band_mode": self.cfg.band_mode,
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
                         burst_per_second=cfg.burst_per_second), headroom=cfg.pacer_headroom)
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
    no_quickedit()

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
