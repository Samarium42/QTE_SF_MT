"""Backtest the print-capture quoter on past sessions from the exchange's history service.

    python -m strategy.backtest --from 2026-10-27 --to 2026-10-30
    python -m strategy.backtest --from 2026-10-28 --fill-share 0.3 --no-hedge

Each session's books, prints, marks, Greeks and session state are downloaded once into
logs/history-cache/ (later runs read the cache), merged into one stream in time order and
replayed through the real quoter (`SimBot`) against `SimExchange` (strategy/sim.py, which
says what is and is not modelled). Positions carry from one day to the next, marked at
each day's marks, as the exchange carries them.

The history service needs no setup beyond the team token the SDK already finds; its
address defaults to the documented one and can be overridden with QTE_HISTORY_URL.
A run writes summary.json, days.csv and fills.csv under logs/backtest/<timestamp>/ and
prints a summary. Settings are those in strategy/config.py.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import heapq
import json
import logging
import os
import struct
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean

from qte_sdk.history import HistoryClient, HistoryError, HistoryUnavailable
from qte_sdk.market_data import Book, Mark, OptionChain, OptionGreeks, SessionState, Trades
from qte_sdk.options import (
    OPTION_ROLE_ACTIVE,
    OPTION_ROLE_OBLIGATED,
    chain_contracts,
    expiry_date,
    parse_option_symbol,
)

from dashboard.analytics import DayResult, idle_accrual, sharpe
from strategy.config import Settings
from strategy.sim import MARKOUT_HORIZONS, MICROS, SimBot, SimClock, SimExchange

log = logging.getLogger("sf_backtest")

HISTORY_URL = "https://history.trading.queenstowerexchange.com"
CACHE = Path("logs/history-cache")
TYPES = {1: Book, 2: Trades, 3: Mark, 4: OptionGreeks, 5: SessionState}
CODES = {cls: code for code, cls in TYPES.items()}
STARTING_EQUITY = 2_500_000.0
SOFR = 0.043  # approximate; the idle-cash benchmark (Rule 2009), as on the dashboard
EXTRA_STRIKES = 4  # strikes replayed beyond those the quoter picks, as the forward moves


# ---------- downloading into the cache ----------

def cache_file(day: str, instrument: str, channel: str) -> Path:
    return CACHE / day / f"{instrument}.{channel}.bin"


async def download(client: HistoryClient, day: str, instrument: str | None, channel: str) -> Path:
    """One stream into the cache, as length-prefixed protobuf records. An unavailable
    stream is cached empty (a contract that never traded has no prints)."""
    path = cache_file(day, instrument or "_session", channel)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    stream = (client.fetch_session_state(day) if instrument is None
              else client.fetch(day, instrument, channel))
    try:
        with tmp.open("wb") as out:
            async for item in stream:
                code = CODES.get(type(item))
                if code is None:
                    continue  # a message type the replay does not use
                data = item.SerializeToString()
                out.write(struct.pack("<BI", code, len(data)))
                out.write(data)
    except HistoryUnavailable:
        tmp.write_bytes(b"")
    tmp.replace(path)
    return path


def read_cache(path: Path):
    with path.open("rb") as f:
        while header := f.read(5):
            code, size = struct.unpack("<BI", header)
            yield TYPES[code].FromString(f.read(size))


def time_of(item: object) -> int:
    return getattr(item, "grid_time", 0) or getattr(item, "sampled_at", 0)


def merged(paths: list[Path]):
    """Every cached stream of a day, merged in time order (within one grid point the
    order is unspecified, as live)."""
    streams = [read_cache(p) for p in paths]
    heap = []
    for i, s in enumerate(streams):
        item = next(s, None)
        if item is not None:
            heap.append((time_of(item), i, item))
    heapq.heapify(heap)
    while heap:
        t, i, item = heapq.heappop(heap)
        yield item
        nxt = next(streams[i], None)
        if nxt is not None:
            heapq.heappush(heap, (time_of(nxt), i, nxt))


async def first_live_mark(client: HistoryClient, day: str, instrument: str) -> int | None:
    path = await download(client, day, instrument, "mark")
    for item in read_cache(path):
        if item.value:
            return item.value
    return None


async def prepare_day(client: HistoryClient, day: str, cfg: Settings) -> tuple[OptionChain, list[str], list[Path]]:
    """Download what one day's replay needs: the chain, the underlyings, and for each the
    front expiry's strikes nearest the spot at the open (calls and puts)."""
    chain = None
    async for item in client.fetch_option_chain(day):
        chain = item
    if chain is None:
        raise HistoryUnavailable(f"no option chain for {day}")
    underlyings = sorted({e.underlying for e in chain.expiries})
    paths = [await download(client, day, None, "session_state")]
    contracts: list[str] = []
    for u in underlyings:
        spot = await first_live_mark(client, day, u)
        expiries = [e for e in chain.expiries if e.underlying == u and not e.reducing_only]
        if spot is None or not expiries:
            continue
        nearest = min(expiry_date(e) for e in expiries)
        listed = [c.instrument for c in chain_contracts(chain, underlying=u, expiry=nearest)
                  if c.role in (OPTION_ROLE_ACTIVE, OPTION_ROLE_OBLIGATED)]
        strikes = sorted({parse_option_symbol(c).strike for c in listed}, key=lambda k: abs(k - spot))
        keep = set(strikes[: cfg.strikes_per_underlying + EXTRA_STRIKES])
        contracts += [c for c in listed if parse_option_symbol(c).strike in keep]
        for channel in ("book", "mark"):
            paths.append(await download(client, day, u, channel))
    log.info("%s: %d contracts on %s", day, len(contracts), underlyings)
    for i, c in enumerate(contracts):
        for channel in ("book", "trades", "mark", "option_greeks"):
            paths.append(await download(client, day, c, channel))
        print(f"\r  {day}: downloaded {i + 1}/{len(contracts)} contracts", end="", file=sys.stderr)
    print(file=sys.stderr)
    return chain, underlyings, paths


# ---------- running ----------

async def run_day(day: str, chain: OptionChain, underlyings: list[str], paths: list[Path],
                  sim: SimExchange, clock: SimClock, cfg: Settings, args) -> dict:
    bot = SimBot(sim, cfg, clock=clock, hedging=not args.no_hedge, decide_every=args.decide_every)
    bot.underlyings = list(underlyings)
    bot.on_market_data(chain)
    start_equity, fills_before = sim.equity(), len(sim.fills)
    peak, trough, snapshots, next_snap = 0.0, 0.0, [], None
    started = time.time()
    for item in merged(paths):
        clock.advance(item)
        bot.on_market_data(item)
        if isinstance(item, SessionState):
            await bot.on_grid()
            if bot.market_open():
                if next_snap is None or clock.t >= next_snap:  # 30-minute equity snapshots
                    snapshots.append((clock.t, sim.equity()))
                    next_snap = clock.t + 1800
                pnl = sim.equity() - start_equity
                peak, trough = max(peak, pnl), min(trough, pnl - peak)
    sim.cancel_all()  # the close cancels every order
    day_fills = sim.fills[fills_before:]
    options = [f for f in day_fills if f.kind != "hedge"]
    return {
        "date": day,
        "pnl": round(sim.equity() - start_equity, 2),
        "max_drawdown": round(trough, 2),
        "option_fills": len(options),
        "contracts": sum(f.size for f in options),
        "crossed_team_contracts": sum(f.size for f in options if f.kind == "cross"),
        "fees": round(sum(f.fee for f in day_fills), 2),
        "hedge_shares": sum(f.size for f in day_fills if f.kind == "hedge"),
        "stop": bot.stop,
        "end_option_delta": {u: round(v) for u, v in sim.option_delta().items()},
        "seconds": round(time.time() - started, 1),
        "snapshots": snapshots,
    }


def summarise(days: list[dict], sim: SimExchange, args) -> dict:
    results = [DayResult(d["date"], d["pnl"], idle_accrual(STARTING_EQUITY, SOFR, 1)) for d in days]
    score = sharpe(results, STARTING_EQUITY)
    options = [f for f in sim.fills if f.kind != "hedge"]
    markouts = {}
    for h in MARKOUT_HORIZONS:
        done = [f for f in options if h in f.markouts]
        contracts = sum(f.size for f in done)
        markouts[f"{h}s"] = {
            "per_contract": round(sum(f.markouts[h] for f in done) / contracts, 2) if contracts else None,
            "fills": len(done),
            "negative": sum(1 for f in done if f.markouts[h] < 0),
        }
    return {
        "settings": {"fill_share": args.fill_share, "decide_every": args.decide_every,
                     "hedging": not args.no_hedge, "latency": args.latency,
                     "strikes": args.strikes, "quote_size": args.quote_size,
                     "max_position": args.max_position},
        "days": len(days),
        "net_pnl": round(sum(d["pnl"] for d in days), 2),
        "mean_daily_pnl": round(mean(d["pnl"] for d in days), 2) if days else 0,
        "worst_day": min((d["pnl"] for d in days), default=0),
        "sharpe": round(score["sharpe"], 2),
        "contracts": sum(d["contracts"] for d in days),
        "crossed_team_contracts": sum(d["crossed_team_contracts"] for d in days),
        "fees_paid": round(sum(f.fee for f in sim.fills), 2),
        "markouts": markouts,
        "rule_6006_breaches": sim.violations,
        "stops": {d["date"]: d["stop"] for d in days if d["stop"]},
        "end_positions": {k: v for k, v in sim.positions.items() if v},
    }


def session_dates(start: str, end: str) -> list[str]:
    d, last, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= last:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


async def main_async(args) -> int:
    cfg = Settings()
    if args.strikes:
        cfg = replace(cfg, strikes_per_underlying=args.strikes)
    if args.quote_size:
        cfg = replace(cfg, quote_size=args.quote_size)
    if args.max_position:
        cfg = replace(cfg, max_position=args.max_position)
    client = HistoryClient(url=os.environ.get("QTE_HISTORY_URL", HISTORY_URL))
    out = Path(args.out) / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(out / "run.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    clock = SimClock()
    sim = SimExchange(clock, fill_share=args.fill_share, latency=args.latency)
    days = []
    for day in session_dates(args.date_from, args.date_to or args.date_from):
        try:
            chain, underlyings, paths = await prepare_day(client, day, cfg)
        except HistoryError as error:
            print(f"{day}: skipped ({type(error).__name__})", file=sys.stderr)
            continue
        result = await run_day(day, chain, underlyings, paths, sim, clock, cfg, args)
        days.append(result)
        print(f"{day}: P&L ${result['pnl']:,.0f}, {result['contracts']} contracts, "
              f"drawdown ${result['max_drawdown']:,.0f}, stop {result['stop'] or 'none'} "
              f"({result['seconds']} s)", file=sys.stderr)

    summary = summarise(days, sim, args)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    with (out / "days.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[k for k in (days[0] if days else {}) if k != "snapshots"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(days)
    with (out / "fills.csv").open("w", newline="") as f:
        rows = [fl.as_dict() for fl in sim.fills]
        keys = sorted({k for r in rows for k in r}, key=lambda k: (k.startswith("markout"), k))
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print(json.dumps(summary, indent=2))
    print(f"\nwritten to {out}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="date_from", required=True, help="first session, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="last session (default: --from)")
    parser.add_argument("--fill-share", type=float, default=0.25,
                        help="share of each eligible print that fills us (other teams compete)")
    parser.add_argument("--latency", type=float, default=0.15, help="order delay, seconds")
    parser.add_argument("--decide-every", type=float, default=0.5,
                        help="seconds of market time between quoting decisions (speed)")
    parser.add_argument("--strikes", type=int, help="strikes per underlying (default: config)")
    parser.add_argument("--quote-size", type=int, help="contracts per quote (default: config)")
    parser.add_argument("--max-position", type=int, help="contracts per contract (default: config)")
    parser.add_argument("--no-hedge", action="store_true", help="no delta hedging, as with hedger.py")
    parser.add_argument("--out", default="logs/backtest", help="where results go")
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
