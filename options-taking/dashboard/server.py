"""San Francisco trading dashboard: risk, P&L, positions, orders, alerts, controls, structures.

One SDK connection keeps the whole picture (account, limits, books, Greeks, the team's
resting orders and fills) and a local web page shows it. Run from the project folder,
where .env holds QTE_URL and QTE_TOKEN:

    python -m dashboard.server                    # view only, controls for the bot
    python -m dashboard.server --strat-id <ID>    # also lets the page send structures

then open http://127.0.0.1:8050. The page is served on localhost only.

Controls: pausing an underlying writes control.json, which the bot reads each grid step
and then quotes nothing on that underlying (it still hedges). "Cancel all" sends one mass
cancel, which removes every resting order of the team, every strategy, on every
instrument. Structures send their legs one at a time, the biggest first, each crossing at
the wall, and stop at the first leg that does not fill completely.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from qte_sdk.account import AccountReports, ValuationBasis, is_account_state
from qte_sdk.books import LatestBooks
from qte_sdk.connection import Connected, Disconnected, ResumeComplete
from qte_sdk.contract.v1.common_pb2 import (
    BUY,
    LIMIT,
    SELL,
    LimitKind,
    LossLevel,
    MarketSessionPhase,
    RiskNoticeKind,
)
from qte_sdk.contract.v1.market_data_pb2 import LIVE
from qte_sdk.contract.v1.order_events_pb2 import (
    Execution,
    OrderCancelled,
    Reject,
    RiskNotice,
)
from qte_sdk.market_data import Mark, SessionState, as_market_data, subscribe
from qte_sdk.options import (
    OPTION_GREEKS_VALID,
    OPTION_TRADING,
    LatestGreeks,
    OptionChain,
    chain_contracts,
    expiry_date,
    greek_to_decimal,
    is_option_symbol,
    listed_contracts,
    option_symbol,
    option_underlyings,
    parse_option_symbol,
    trading_state,
    vol_to_decimal,
)
from qte_sdk.orders import (
    is_order_event,
    new_request_ref,
    reason_code_name,
    request_ref_of,
    send_cancel,
    send_mass_cancel,
    send_new,
)
from qte_sdk.pacing import Budget, Pacer
from qte_sdk.reconnect import ReconnectingSession
from qte_sdk.resting import RestingOrders
from qte_sdk.units import to_decimal

from dashboard.structures import LegSpec, execution_order, legs
from strategy.bs import black76
from strategy.config import Settings
from strategy.risk import MULTIPLIER

log = logging.getLogger("sf_dashboard")

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
CONTROL = ROOT / "control.json"
STATIC = Path(__file__).resolve().parent / "static"
NEW_YORK = ZoneInfo("America/New_York")
SIDE = {BUY: "BUY", SELL: "SELL"}
ACCOUNT_EVERY = 5.0  # seconds; an account query is free (not an order message)
EQUITY_EVERY = 30.0
LEG_WAIT = 2.0  # seconds to wait for a leg's fills before giving up on its remainder


def d(micros: int | None) -> float | None:
    return None if micros is None else float(to_decimal(micros))


def underlying_of(instrument: str) -> str:
    return parse_option_symbol(instrument).underlying if is_option_symbol(instrument) else instrument


def multiplier(instrument: str) -> int:
    return MULTIPLIER if is_option_symbol(instrument) else 1


def years_to(expiry: date) -> float:
    close = datetime(expiry.year, expiry.month, expiry.day, 16, tzinfo=NEW_YORK)
    return max(0.0, (close - datetime.now(timezone.utc)).total_seconds() / (365.0 * 86400))


def read_control() -> dict:
    try:
        return json.loads(CONTROL.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"paused": []}


def write_control(data: dict) -> None:
    tmp = CONTROL.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(CONTROL)


class LegWatch:
    """Fills and the end of one leg order, matched by strategy, instrument, side, price."""

    def __init__(self, ref: str, size: int):
        self.ref, self.size = ref, size
        self.filled = 0
        self.ended = asyncio.Event()
        self.reason: str | None = None


class Hub:
    def __init__(self, session: ReconnectingSession, strat_id: str | None, cfg: Settings):
        self.session = session
        self.strat_id = strat_id
        self.cfg = cfg
        self.books = LatestBooks()
        self.greeks = LatestGreeks()
        self.marks: dict[str, int] = {}
        self.chain: OptionChain | None = None
        self.state: SessionState | None = None
        self.resting = RestingOrders()
        self.account = AccountReports()
        self.summary = None
        self.positions: dict[str, int] = {}
        self.position_prices: dict[str, int] = {}
        self.valuation_basis = ""
        self.connected = False
        self.subscribed: set[str] = set()
        self.table_seen: object | None = None
        self.underlyings: list[str] = []

        self.alerts: deque[dict] = deque(maxlen=200)
        self.fills: deque[dict] = deque(maxlen=2000)
        self.fill_ids: set[str] = set()
        self.rejects: deque[dict] = deque(maxlen=200)
        self.bot_events: deque[dict] = deque(maxlen=300)
        self.equity: list[dict] = []
        self.jobs: deque[dict] = deque(maxlen=20)
        self.watches: dict[tuple[str, int, int], LegWatch] = {}
        self.busy = False  # a structure is being sent
        self.last_query = 0.0
        self.last_equity = 0.0
        self.bot_log: Path | None = None
        self.bot_log_pos = 0
        self.bot_last_line = 0.0
        self.bot_status = ""
        self.started = time.time()
        self.load_history()

    # ---------- history on disk ----------

    def load_history(self) -> None:
        path = LOGS / "fills.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                with contextlib.suppress(json.JSONDecodeError):
                    f = json.loads(line)
                    if f["exec_id"] not in self.fill_ids:
                        self.fill_ids.add(f["exec_id"])
                        self.fills.append(f)
        path = LOGS / f"equity-{date.today().isoformat()}.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                with contextlib.suppress(json.JSONDecodeError):
                    self.equity.append(json.loads(line))

    def append(self, name: str, row: dict) -> None:
        LOGS.mkdir(exist_ok=True)
        with (LOGS / name).open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def alert(self, level: str, text: str) -> None:
        log.info("alert %s: %s", level, text)
        self.alerts.appendleft({"t": time.time(), "level": level, "text": text})

    # ---------- reading the exchange ----------

    async def on_event(self, event: object) -> None:
        self.resting.apply(event)
        for report in self.account.update(event):
            self.on_account_report(report)

        if isinstance(event, Connected):
            self.connected = True
            self.alert("good" if not event.reconnected else "warning",
                       "reconnected to the exchange" if event.reconnected
                       else "connected to the exchange")
            await self.query_account()
            return
        if isinstance(event, Disconnected):
            self.connected = False
            self.alert("critical", f"disconnected from the exchange: {event.error}")
            return
        if isinstance(event, ResumeComplete):
            return

        table = self.session.instrument_table
        if table is not None and table is not self.table_seen:
            self.table_seen = table
            await self.subscribe_listed()

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
            return
        if is_order_event(event):
            self.on_order_event(event.message)  # type: ignore[attr-defined]

    async def subscribe_listed(self) -> None:
        table = self.session.instrument_table
        assert table is not None
        self.underlyings = list(option_underlyings(table))
        listed = [c for u in self.underlyings for c in listed_contracts(table, u)]
        new = [i for i in self.underlyings + listed if i not in self.subscribed]
        if new:
            self.subscribed.update(new)
            await subscribe(self.session, new)

    def on_account_report(self, report: object) -> None:
        message = report.message  # type: ignore[attr-defined]
        if is_account_state(report):
            self.positions = {p.instrument: p.quantity for p in message.positions}
            self.position_prices = {p.instrument: p.price for p in message.positions}
            self.valuation_basis = ValuationBasis.Name(message.valuation_basis)
            if message.HasField("summary"):
                before = self.summary
                self.summary = message.summary
                if before is not None and before.loss_level != self.summary.loss_level:
                    level = LossLevel.Name(self.summary.loss_level)
                    self.alert("critical" if "HALT" in level else "warning",
                               f"loss level is now {level}")
                if before is not None and self.summary.in_cure and not before.in_cure:
                    self.alert("critical", "in the 15-minute exposure cure: reduce only")
                self.check_limits()
        elif isinstance(message, Execution):
            self.on_fill(message)
            sign = 1 if message.side == BUY else -1
            self.positions[message.instrument] = (
                self.positions.get(message.instrument, 0) + sign * message.fill_size)
        elif isinstance(message, RiskNotice):
            kind = RiskNoticeKind.Name(message.kind)
            self.alert("critical", f"risk notice from the exchange: {kind}")

    def check_limits(self) -> None:
        """Warn once a limit passes 80% of the exchange cap."""
        for row in self.summary.limits:
            if row.cap > 0 and row.used / row.cap >= 0.8:
                key = f"{LimitKind.Name(row.kind)} {row.scope}"
                recent = [a for a in self.alerts if key in a["text"] and time.time() - a["t"] < 300]
                if not recent:
                    self.alert("serious", f"{key} at {row.used / row.cap:.0%} of the exchange cap")

    def on_fill(self, m: Execution) -> None:
        if m.exec_id in self.fill_ids:
            return
        self.fill_ids.add(m.exec_id)
        row = {
            "exec_id": m.exec_id,
            "t": m.timestamp / 1e3,
            "strat": m.strat_id if m.HasField("strat_id") else "(none)",
            "instrument": m.instrument,
            "side": SIDE.get(m.side, "?"),
            "size": m.fill_size,
            "price": d(m.fill_price),
            "fee": d(m.fee),
            "kind": int(m.fill_kind),
            "liquidity": int(m.liquidity),
        }
        self.fills.append(row)
        self.append("fills.jsonl", row)
        if m.HasField("order_price"):
            watch = self.watches.get((m.instrument, m.side, m.order_price))
            if watch is not None and (not m.HasField("strat_id") or m.strat_id == self.strat_id):
                watch.filled += m.fill_size
                if m.remaining_size == 0 or watch.filled >= watch.size:
                    watch.ended.set()

    def on_order_event(self, message: object) -> None:
        if isinstance(message, Reject):
            ref = request_ref_of(message)
            name = reason_code_name(message.reason_code)
            self.rejects.appendleft({"t": time.time(), "source": "dashboard", "text": name})
            for watch in self.watches.values():
                if watch.ref == ref:
                    watch.reason = name
                    watch.ended.set()
        elif isinstance(message, OrderCancelled) and message.HasField("price"):
            watch = self.watches.get((message.instrument, message.side, message.price))
            if watch is not None:
                watch.reason = watch.reason or reason_code_name(message.reason_code)
                watch.ended.set()

    async def query_account(self) -> None:
        self.last_query = time.monotonic()
        with contextlib.suppress(Exception):
            await self.account.query(self.session)

    async def ticker(self) -> None:
        """Account, equity samples and the bot's log on a timer: market data is sparse
        while the market is closed."""
        while True:
            await asyncio.sleep(1.0)
            with contextlib.suppress(Exception):
                await self.on_tick()

    async def on_tick(self) -> None:
        now = time.monotonic()
        if self.connected and now - self.last_query > ACCOUNT_EVERY:
            await self.query_account()
        if self.summary is not None and now - self.last_equity > EQUITY_EVERY:
            self.last_equity = now
            row = {"t": time.time(), "equity": d(self.summary.equity),
                   "daily_pnl": d(self.summary.daily_pnl)}
            self.equity.append(row)
            self.append(f"equity-{date.today().isoformat()}.jsonl", row)
        self.tail_bot_log()

    # ---------- the bot's log ----------

    def tail_bot_log(self) -> None:
        logs = sorted(LOGS.glob("bot-*.log"))
        if not logs:
            return
        if logs[-1] != self.bot_log:
            self.bot_log, self.bot_log_pos = logs[-1], 0
            self.bot_log_pos = max(0, logs[-1].stat().st_size - 20_000)
        with self.bot_log.open() as fh:
            fh.seek(self.bot_log_pos)
            lines = fh.readlines()
            self.bot_log_pos = fh.tell()
        for line in lines:
            parts = line.rstrip("\n").split(" ", 4)
            if len(parts) < 5:
                continue
            stamp, level, text = f"{parts[0]} {parts[1]}", parts[2], parts[4]
            with contextlib.suppress(ValueError):
                self.bot_last_line = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S").timestamp()
            if text.startswith("status:"):
                self.bot_status = text
            elif text.startswith("reject"):
                self.rejects.appendleft({"t": self.bot_last_line, "source": "bot", "text": text})
            elif level in ("WARNING", "ERROR") or text.startswith(("FILL", "HEDGE", "PAPER FILL")):
                self.bot_events.appendleft({"t": self.bot_last_line, "level": level, "text": text})
                fresh = self.bot_last_line >= self.started - 5
                if fresh and ("budget" in text or level == "ERROR" or "disconnected" in text):
                    self.alert("serious", f"bot: {text}")

    # ---------- views ----------

    def spot(self, underlying: str) -> float | None:
        m = self.marks.get(underlying) or self.position_prices.get(underlying)
        return d(m)

    def mark(self, instrument: str) -> float | None:
        m = self.marks.get(instrument) or self.position_prices.get(instrument)
        return d(m)

    def greeks_of(self, instrument: str) -> dict | None:
        g = self.greeks.get(instrument)
        if g is None or g.status != OPTION_GREEKS_VALID:
            return None
        return {
            "delta": float(greek_to_decimal(g.delta)),
            "gamma": float(greek_to_decimal(g.gamma)),
            "vega": d(g.vega),
            "theta": d(g.theta),
            "iv": float(vol_to_decimal(g.implied_vol)),
            "forward": d(g.forward),
        }

    def snapshot(self) -> dict:
        s, st = self.summary, self.state
        frac = float(self.cfg.risk_fraction)
        out: dict = {
            "now": time.time(),
            "connected": self.connected,
            "strat_id": self.strat_id,
            "session": None if st is None else {
                "phase": MarketSessionPhase.Name(st.state),
                "open": st.state == MarketSessionPhase.OPEN and not st.outage_active,
                "date": st.session_date,
                "close_time": st.close_time / 1e3,
            },
            "account": None if s is None else {
                "equity": d(s.equity), "cash": d(s.cash), "daily_pnl": d(s.daily_pnl),
                "previous_close_equity": d(s.previous_close_equity),
                "loss_warning": d(s.loss_warning_amount) if s.HasField("loss_warning_amount") else None,
                "loss_halt": d(s.loss_halt_amount) if s.HasField("loss_halt_amount") else None,
                "soft_loss": float(self.cfg.soft_loss),
                "loss_level": LossLevel.Name(s.loss_level),
                "in_cure": s.in_cure,
                "cure_deadline": s.cure_deadline / 1e3 if s.HasField("cure_deadline") else None,
                "valuation": self.valuation_basis,
            },
            "limits": [] if s is None else [
                {"kind": LimitKind.Name(r.kind).removeprefix("LIMIT_"),
                 "scope": r.scope if r.HasField("scope") else "book",
                 "used": d(r.used), "cap": d(r.cap), "ours": d(r.cap) * frac if r.cap else None}
                for r in s.limits
            ],
            "risk_fraction": frac,
        }
        out.update(self.risk_view())
        out["pnl"] = self.pnl_view()
        out["resting"] = sorted(
            ({"strat": o.strat_id, "instrument": o.key.instrument, "side": SIDE.get(o.key.side),
              "price": d(o.key.price), "size": o.remaining_size,
              "stale": o.stale_since is not None} for o in self.resting),
            key=lambda r: (r["instrument"], r["side"]))
        out["fills"] = list(self.fills)[-100:][::-1]
        out["rejects"] = list(self.rejects)[:100]
        out["alerts"] = list(self.alerts)[:100]
        out["bot"] = {"events": list(self.bot_events)[:100], "status": self.bot_status,
                      "last_line": self.bot_last_line,
                      "alive": time.time() - self.bot_last_line < 90}
        out["equity"] = self.equity[-2000:]
        out["paused"] = read_control().get("paused", [])
        out["underlyings"] = self.underlyings
        out["jobs"] = list(self.jobs)
        out["expiries"] = self.expiries()
        return out

    def risk_view(self) -> dict:
        per: dict[str, dict] = {}
        rows = []
        gross = 0.0
        for instrument, qty in sorted(self.positions.items()):
            if qty == 0:
                continue
            u = underlying_of(instrument)
            spot = self.spot(u)
            mark = self.mark(instrument)
            mult = multiplier(instrument)
            agg = per.setdefault(u, {"underlying": u, "option_delta": 0.0, "stock_delta": 0.0,
                                     "gamma": 0.0, "vega": 0.0, "theta": 0.0})
            row = {"instrument": instrument, "underlying": u, "qty": qty, "mark": mark,
                   "value": None if mark is None else qty * mult * mark,
                   "option": is_option_symbol(instrument)}
            if is_option_symbol(instrument):
                g = self.greeks_of(instrument)
                if mark is not None:
                    gross += abs(qty) * mult * mark
                if g is not None and spot is not None:
                    row.update({k: g[k] for k in ("delta", "gamma", "vega", "theta", "iv")})
                    agg["option_delta"] += qty * mult * g["delta"] * spot
                    agg["gamma"] += 0.01 * spot * spot * qty * mult * g["gamma"]
                    agg["vega"] += qty * mult * g["vega"]
                    agg["theta"] += qty * mult * g["theta"]
                else:
                    row["greeks_missing"] = True
            elif spot is not None:
                agg["stock_delta"] += qty * spot
                row["delta"] = 1.0
            rows.append(row)
        for agg in per.values():
            agg["net_delta"] = agg["option_delta"] + agg["stock_delta"]
        total = {k: sum(a[k] for a in per.values())
                 for k in ("option_delta", "stock_delta", "net_delta", "gamma", "vega", "theta")}
        total["abs_gamma"] = sum(abs(a["gamma"]) for a in per.values())
        lim = self.cfg.limits
        return {"positions": rows, "greeks": list(per.values()), "greeks_total": total,
                "gross": gross,
                "own_limits": {"gross": float(lim.gross), "net_delta": float(lim.net_delta_per_underlying),
                               "gamma": float(lim.gamma), "v0": float(lim.v0)}}

    def pnl_view(self) -> dict:
        """P&L from the fills this dashboard has recorded (logs/fills.jsonl), marked now:
        cash flows plus what is still held at the mark, by strategy and by underlying."""
        books: dict[tuple[str, str], dict] = {}
        for f in self.fills:
            mult = multiplier(f["instrument"])
            sign = 1 if f["side"] == "BUY" else -1
            b = books.setdefault((f["strat"], f["instrument"]), {"qty": 0, "cash": 0.0, "fees": 0.0})
            b["qty"] += sign * f["size"]
            b["cash"] += -sign * f["size"] * mult * f["price"] + (f["fee"] or 0.0)
            b["fees"] += f["fee"] or 0.0
        by_strat: dict[str, dict] = {}
        by_und: dict[str, dict] = {}
        for (strat, instrument), b in books.items():
            mark = self.mark(instrument)
            value = 0.0 if b["qty"] == 0 else (None if mark is None else b["qty"] * multiplier(instrument) * mark)
            pnl = None if value is None else b["cash"] + value
            for key, table in ((strat, by_strat), (underlying_of(instrument), by_und)):
                r = table.setdefault(key, {"name": key, "pnl": 0.0, "fees": 0.0, "unmarked": 0})
                r["fees"] += b["fees"]
                if pnl is None:
                    r["unmarked"] += 1
                else:
                    r["pnl"] += pnl
        since = min((f["t"] for f in self.fills), default=None)
        return {"by_strategy": sorted(by_strat.values(), key=lambda r: r["name"]),
                "by_underlying": sorted(by_und.values(), key=lambda r: r["name"]),
                "since": since}

    def expiries(self) -> dict:
        out: dict[str, list] = {}
        if self.chain is None:
            return out
        for e in self.chain.expiries:
            out.setdefault(e.underlying, []).append(
                {"expiry": expiry_date(e).isoformat(), "reducing_only": e.reducing_only})
        return out

    def strikes(self, underlying: str, expiry: date) -> list[float]:
        if self.chain is None:
            return []
        return sorted({d(parse_option_symbol(c.instrument).strike)
                       for c in chain_contracts(self.chain, underlying=underlying, expiry=expiry)})

    # ---------- structures ----------

    def price_structure(self, q: dict) -> dict:
        underlying, expiry = q["underlying"], date.fromisoformat(q["expiry"])
        strikes = [int(round(float(k) * 1_000_000)) for k in q["strikes"]]
        spec = legs(q["structure"], strikes, int(q["direction"]), int(q["qty"]))
        vol_override = float(q["vol"]) / 100 if q.get("vol") not in (None, "") else None
        fee = float(self.cfg.taker_fee_per_contract)
        t = years_to(expiry)
        rows, total = [], {"wall": 0.0, "mark": 0.0, "fair": 0.0, "fees": 0.0,
                           "delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
        complete = True
        for leg in spec:
            contract = option_symbol(underlying, expiry, leg.right, leg.strike)
            book = self.books.get(contract)
            bid = d(book.bid_levels[0].price) if book and book.bid_levels else None
            ask = d(book.ask_levels[0].price) if book and book.ask_levels else None
            mark = self.mark(contract)
            g = self.greeks_of(contract)
            cross = ask if leg.qty > 0 else bid
            n = leg.qty * MULTIPLIER
            row = {"contract": contract, "qty": leg.qty, "bid": bid, "ask": ask, "mark": mark,
                   "cross": cross, "iv": g["iv"] if g else None,
                   "tradable": bool(book) and trading_state(book) == OPTION_TRADING}
            if g is None or cross is None or mark is None:
                complete = False
            else:
                vol = vol_override if vol_override is not None else g["iv"] / 100
                v = black76(leg.right == "C", g["forward"], leg.strike / 1e6, t, vol)
                row["fair"] = v.price
                total["wall"] += n * cross
                total["mark"] += n * mark
                total["fair"] += n * v.price
                total["fees"] += abs(leg.qty) * fee
                for k in ("delta", "gamma", "vega", "theta"):
                    total[k] += n * g[k]
            rows.append(row)
        if complete:
            # Positive edge: we pay less than fair (buying) or get more than fair (selling).
            total["edge_vs_fair"] = total["fair"] - total["wall"] - total["fees"]
            total["edge_vs_mark"] = total["mark"] - total["wall"] - total["fees"]
        return {"legs": rows, "total": total, "complete": complete, "years": t}

    async def execute_structure(self, q: dict) -> dict:
        if not self.strat_id:
            return {"error": "start the dashboard with --strat-id to send orders"}
        if self.busy:
            return {"error": "a structure is already being sent"}
        priced = self.price_structure(q)
        if not priced["complete"]:
            return {"error": "a leg has no quote, mark or Greeks: nothing sent"}
        if any(not r["tradable"] for r in priced["legs"]):
            return {"error": "a leg is not trading: nothing sent"}
        underlying, expiry = q["underlying"], date.fromisoformat(q["expiry"])
        strikes = [int(round(float(k) * 1_000_000)) for k in q["strikes"]]
        spec = legs(q["structure"], strikes, int(q["direction"]), int(q["qty"]))
        crosses = {r["contract"]: r["cross"] for r in priced["legs"]}
        order = execution_order([
            (leg, leg.qty * crosses[option_symbol(underlying, expiry, leg.right, leg.strike)])
            for leg in spec])
        job = {"t": time.time(), "what": f"{'BUY' if int(q['direction']) > 0 else 'SELL'} "
               f"{q['qty']} {q['structure']} {underlying} {q['expiry']} {q['strikes']}",
               "status": "running", "log": []}
        self.jobs.appendleft(job)
        self.busy = True
        try:
            for leg in order:
                ok = await self.send_leg(underlying, expiry, leg, job)
                if not ok:
                    job["status"] = "stopped: a leg did not fill; later legs not sent"
                    self.alert("serious", f"structure stopped part way: {job['what']}")
                    return job
            job["status"] = "done"
            return job
        finally:
            self.busy = False

    async def send_leg(self, underlying: str, expiry: date, leg: LegSpec, job: dict) -> bool:
        contract = option_symbol(underlying, expiry, leg.right, leg.strike)
        side = BUY if leg.qty > 0 else SELL
        remaining = abs(leg.qty)
        while remaining > 0:
            book = self.books.get(contract)
            levels = (book.ask_levels if side == BUY else book.bid_levels) if book else None
            if not levels or trading_state(book) != OPTION_TRADING:
                job["log"].append(f"{contract}: no wall to cross")
                return False
            price = levels[0].price
            size = min(remaining, self.cfg.limits.max_option_order)
            ref = new_request_ref()
            watch = LegWatch(ref, size)
            key = (contract, side, price)
            self.watches[key] = watch
            try:
                await send_new(self.session, strat_id=self.strat_id, instrument=contract, side=side,
                               order_type=LIMIT, price=price, size=size, request_ref=ref)
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(LEG_WAIT):
                        await watch.ended.wait()
                if watch.filled < size and watch.reason is None:
                    with contextlib.suppress(Exception):
                        await send_cancel(self.session, instrument=contract, side=side, price=price)
            finally:
                self.watches.pop(key, None)
            job["log"].append(f"{SIDE[side]} {contract} {watch.filled}/{size} @ {d(price)}"
                              + (f" ({watch.reason})" if watch.reason else ""))
            remaining -= watch.filled
            if watch.filled == 0:
                return False
        return True

    async def cancel_all(self) -> None:
        await send_mass_cancel(self.session)
        self.alert("warning", "mass cancel sent: every resting order of the team")


# ---------- web ----------

def same_origin(request: web.Request) -> bool:
    """Refuse cross-site posts: another page in the browser must not drive the controls."""
    origin = request.headers.get("Origin")
    return request.headers.get("X-SF-Dashboard") == "1" and (
        origin is None or origin == f"http://{request.host}")


def make_app(hub: Hub) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    @routes.get("/api/state")
    async def state(_: web.Request) -> web.Response:
        return web.json_response(hub.snapshot())

    @routes.post("/api/control")
    async def control(request: web.Request) -> web.Response:
        if not same_origin(request):
            raise web.HTTPForbidden()
        body = await request.json()
        data = read_control()
        paused = set(data.get("paused", []))
        targets = hub.underlyings if body.get("underlying") == "*" else [body["underlying"]]
        for u in targets:
            (paused.add if body.get("paused") else paused.discard)(u)
        data["paused"] = sorted(paused)
        data["updated"] = time.time()
        write_control(data)
        hub.alert("warning", f"{'paused' if body.get('paused') else 'resumed'} quoting on "
                  f"{', '.join(targets)}")
        return web.json_response(data)

    @routes.post("/api/cancel-all")
    async def cancel_all(request: web.Request) -> web.Response:
        if not same_origin(request):
            raise web.HTTPForbidden()
        await hub.cancel_all()
        return web.json_response({"ok": True})

    @routes.post("/api/structure/price")
    async def price(request: web.Request) -> web.Response:
        if not same_origin(request):
            raise web.HTTPForbidden()
        try:
            return web.json_response(hub.price_structure(await request.json()))
        except (KeyError, ValueError) as error:
            return web.json_response({"error": str(error)}, status=400)

    @routes.get("/api/strikes")
    async def strikes(request: web.Request) -> web.Response:
        try:
            expiry = date.fromisoformat(request.query["expiry"])
        except (KeyError, ValueError):
            return web.json_response({"strikes": []})
        return web.json_response({"strikes": hub.strikes(request.query.get("underlying", ""), expiry)})

    @routes.post("/api/structure/execute")
    async def execute(request: web.Request) -> web.Response:
        if not same_origin(request):
            raise web.HTTPForbidden()
        body = await request.json()
        if body.get("confirm") is not True:
            return web.json_response({"error": "not confirmed"}, status=400)
        try:
            return web.json_response(await hub.execute_structure(body))
        except (KeyError, ValueError) as error:
            return web.json_response({"error": str(error)}, status=400)

    app = web.Application()
    app.add_routes(routes)
    return app


async def run(args: argparse.Namespace) -> None:
    cfg = Settings()
    # A small share of the team's message budget: the bot has the rest.
    session = ReconnectingSession(pacing=Pacer(Budget(sustained_per_minute=240, burst_per_second=40)))
    hub = Hub(session, args.strat_id, cfg)
    runner = web.AppRunner(make_app(hub), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", args.port).start()
    log.info("dashboard on http://127.0.0.1:%d", args.port)
    ticker = asyncio.create_task(hub.ticker())
    try:
        async with session:
            async for event in session:
                await hub.on_event(event)
    finally:
        ticker.cancel()
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strat-id", default=None,
                        help="registered strategy ID for structures sent from the page")
    parser.add_argument("--port", type=int, default=8050)
    args = parser.parse_args()
    LOGS.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(LOGS / f"dashboard-{date.today().isoformat()}.log")])
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
