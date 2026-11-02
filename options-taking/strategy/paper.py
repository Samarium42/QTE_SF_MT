"""Paper-trade the print-capture quoter on the live market: real data, simulated fills.

    python -m strategy.paper                 # quoter with its own hedging
    python -m strategy.paper --no-hedge      # as it runs next to hedger.py
    python -m strategy.paper --reset         # start a fresh paper book

The quoter's own code decides every quote and hedge, exactly as live; its orders go to
`SimExchange` (strategy/sim.py), which fills them from the live tape. Nothing is ever sent
to the exchange: the session is used only to read market data, so paper trading needs no
strategy ID and can run next to the live bots.

The paper book (cash and positions) is kept in logs/paper-book.json and carries across
restarts and days, marked at live marks. Fills go to logs/paper-fills.jsonl, each day's
result to logs/paper-days.json, the bot's log to logs/paper-YYYY-MM-DD.log and its status
to logs/paper-status.json. Ctrl+C saves the book and exits.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
import time
from datetime import date
from pathlib import Path

import qte_sdk.logs
from qte_sdk.reconnect import ReconnectingSession

from strategy.config import Settings
from strategy.sim import SimBot, SimExchange

log = logging.getLogger("sf_paper")

BOOK = Path("logs/paper-book.json")
FILLS = Path("logs/paper-fills.jsonl")
DAYS = Path("logs/paper-days.json")
STATUS = Path("logs/paper-status.json")


class WallClock:
    """Real time for the simulated exchange and the bot alike."""

    @property
    def t(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.time()

    def time(self) -> float:
        return time.time()

    def advance(self, item: object) -> None:
        pass


def load_book(sim: SimExchange) -> None:
    with contextlib.suppress(OSError, ValueError, KeyError):
        saved = json.loads(BOOK.read_text())
        sim.cash = float(saved["cash"])
        sim.positions = {k: int(v) for k, v in saved["positions"].items()}
        log.info("paper book loaded: cash $%.0f, %d positions", sim.cash,
                 sum(1 for v in sim.positions.values() if v))


def save_book(sim: SimExchange, saved_fills: int) -> int:
    tmp = BOOK.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cash": sim.cash, "positions": sim.positions, "t": time.time()}))
    tmp.replace(BOOK)
    with FILLS.open("a") as f:
        for fill in sim.fills[saved_fills:]:
            f.write(json.dumps(fill.as_dict()) + "\n")
    return len(sim.fills)


def record_day(bot: SimBot) -> None:
    if bot.state is None:
        return
    days = {}
    with contextlib.suppress(OSError, ValueError):
        days = json.loads(DAYS.read_text())
    days[bot.state.session_date] = {"pnl": float(bot.daily_pnl), "stop": bot.stop,
                                    "equity": round(bot.sim.equity(), 2)}
    DAYS.write_text(json.dumps(days, indent=1))


async def run(args: argparse.Namespace) -> int:
    cfg = Settings()
    clock = WallClock()
    sim = SimExchange(clock, fill_share=args.fill_share)  # type: ignore[arg-type]
    if args.reset:
        BOOK.unlink(missing_ok=True)
    load_book(sim)
    session = ReconnectingSession()
    bot = SimBot(sim, cfg, session=session, clock=clock, strat_id="paper",
                 hedging=not args.no_hedge, persist=True)
    bot.status_path = STATUS
    log.info("paper trading: fill share %.2f, hedging %s; nothing is sent to the exchange",
             args.fill_share, not args.no_hedge)

    task = asyncio.current_task()
    assert task is not None
    with contextlib.suppress(NotImplementedError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, task.cancel)
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)

    saved, last_save, last_day = 0, time.monotonic(), None
    try:
        async with session:
            async for event in session:
                await bot.on_event(event)
                if bot.state is not None and bot.state.session_date != last_day:
                    if last_day is not None:
                        record_day(bot)
                    last_day = bot.state.session_date
                if time.monotonic() - last_save > 10:
                    saved, last_save = save_book(sim, saved), time.monotonic()
                    record_day(bot)
    except asyncio.CancelledError:
        log.info("stopping paper trading")
    finally:
        sim.cancel_all()
        save_book(sim, saved)
        record_day(bot)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fill-share", type=float, default=0.25,
                        help="share of each eligible print that fills us (other teams compete)")
    parser.add_argument("--no-hedge", action="store_true", help="no delta hedging, as with hedger.py")
    parser.add_argument("--reset", action="store_true", help="start a fresh paper book")
    args = parser.parse_args(argv)

    Path("logs").mkdir(exist_ok=True)
    qte_sdk.logs.configure()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(Path("logs") / f"paper-{date.today().isoformat()}.log")],
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
