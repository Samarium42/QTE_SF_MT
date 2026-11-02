"""Supervisor: keeps the strategies running on an always-on host, with nobody at the keyboard.

Run from the project folder, where .env holds QTE_URL and QTE_TOKEN:

    python -m ops.supervisor                  # live, on the session schedule
    python -m ops.supervisor --dry-run        # the bots read the market and send nothing
    python -m ops.supervisor --now --dry-run  # ignore the schedule (testing)
    python -m ops.supervisor --only quoter    # one strategy (the quoter then hedges itself)

Each weekday it starts the hedger, the quoter (--no-hedge) and the picker before the open and
stops them after the close (ops/schedule.py). Meanwhile:

- A bot that exits unexpectedly is restarted after a delay; it adopts its resting orders on
  start. After too many restarts the supervisor gives up on it and mass-cancels.
- A bot whose status file goes stale while the market is open is stopped and restarted.
- A bot stopped from the dashboard stays stopped until the next session.
- If the hedger is given up on, the quoter and picker are stopped too: nothing hedges them.
- Crashes, restarts, cures, loss levels and a daily summary are pushed through ntfy
  (ops/notify.py), and a dead-man URL is pinged so a dead host is noticed too.

A bot is always asked to stop through control.json, as the dashboard does, so it cancels its
own orders on any OS. Only one that ignores that is terminated, then killed, and then the
team's orders are mass-cancelled (ops/emergency.py): the exchange has no cancel-on-disconnect.
Ctrl+C or SIGTERM (systemd) stops every bot that way before the supervisor exits.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from qte_sdk.errors import QteError
from qte_sdk.session import resolve_token, resolve_url

from ops import schedule
from ops.config import OpsSettings, Strategy, strategies
from ops.notify import Notifier, ping

log = logging.getLogger("ops.supervisor")

# Exit code for a set-up problem a restart cannot fix (systemd: RestartPreventExitStatus=2).
EXIT_SETUP = 2

Emergency = Callable[[], Awaitable[bool]]


@dataclass
class Child:
    strategy: Strategy
    proc: asyncio.subprocess.Process | None = None
    started: float = 0.0  # wall-clock time of the launch, as control.json stop times are
    exited: float = 0.0  # when the last copy we ran ended
    restarts: deque[float] = field(default_factory=deque)  # crash times, for the give-up count
    stuck: int = 0  # restarts after a hang today (not counted towards giving up)
    next_start: float = 0.0
    stopping: bool = False  # the supervisor is stopping it
    held: str | None = None  # why it is not to be started again this session

    @property
    def name(self) -> str:
        return self.strategy.name

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


async def wait_exit(proc: asyncio.subprocess.Process, seconds: float) -> bool:
    try:
        async with asyncio.timeout(seconds):
            await proc.wait()
    except TimeoutError:
        return False
    return True


class Supervisor:
    def __init__(self, strats: list[Strategy], cfg: OpsSettings = OpsSettings(), *,
                 root: Path | None = None, notifier: Notifier | None = None,
                 emergency: Emergency | None = None, dry_run: bool = False,
                 always_open: bool = False, heartbeat_url: str | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.cfg, self.dry_run, self.always_open, self.clock = cfg, dry_run, always_open, clock
        self.root = (root or Path.cwd()).resolve()
        self.logs, self.control = self.root / "logs", self.root / "control.json"
        self.children = [Child(s) for s in strats]
        self.notifier = notifier or Notifier(None)
        self.emergency = emergency or self.run_emergency
        self.heartbeat_url, self.last_heartbeat = heartbeat_url, 0.0
        self.day: date | None = None
        self.was_open = False
        self.flags: dict = {}  # team-level alerts already sent today

    # Files shared with the bots and the dashboard.

    def read_status(self, child: Child) -> dict | None:
        with contextlib.suppress(OSError, ValueError):
            status = json.loads((self.logs / child.strategy.status).read_text())
            if isinstance(status, dict):
                return status
        return None

    def status_age(self, child: Child, now: float) -> float | None:
        status = self.read_status(child)
        t = status.get("t") if status else None
        return now - t if isinstance(t, (int, float)) else None

    def read_control(self) -> dict:
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(self.control.read_text())
            if isinstance(data, dict):
                return data
        return {"paused": []}

    def request_stop(self, name: str) -> None:
        """Ask a bot to stop, as the dashboard's Stop does; keeps the dashboard's other keys.
        Callers repeat it: a bot obeys only a request newer than its own start, and on Windows
        the replace fails while another process has the file open."""
        data = self.read_control()
        stops = data.get("stop") if isinstance(data.get("stop"), dict) else {}
        stops[name] = time.time()
        data["stop"] = stops
        try:
            tmp = self.control.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.control)
        except OSError as error:
            log.warning("could not write control.json: %s", error)

    def stopped_by_hand(self, child: Child) -> bool:
        stop = self.read_control().get("stop")
        when = stop.get(child.name, 0) if isinstance(stop, dict) else 0
        return isinstance(when, (int, float)) and when > child.started

    def write_status(self, open_: bool) -> None:
        status = {
            "t": time.time(), "open": open_, "dry_run": self.dry_run, "day": str(self.day),
            "children": [{
                "name": c.name, "strat_id": c.strategy.strat_id, "running": c.running,
                "pid": c.proc.pid if c.running else None, "held": c.held,
                "restarts": len(c.restarts), "stuck": c.stuck,
                "next_start": c.next_start if not c.running else None,
            } for c in self.children],
        }
        with contextlib.suppress(OSError):
            tmp = self.logs / "supervisor-status.tmp"
            tmp.write_text(json.dumps(status))
            tmp.replace(self.logs / "supervisor-status.json")

    async def alert(self, level: str, message: str, key: str | None = None) -> None:
        await self.notifier.send(level, message, key)

    # Processes.

    def is_open(self) -> bool:
        return self.always_open or schedule.is_open(self.clock(), self.cfg)

    async def launch(self, child: Child) -> None:
        s = child.strategy
        now = time.time()
        age = self.status_age(child, now)
        # A status written since our last copy ended: someone else runs it (a terminal, the
        # dashboard). Two copies would fight over one team book, so leave it to them.
        if age is not None and age < self.cfg.orphan_seconds and now - age > child.exited:
            child.held = "running outside the supervisor"
            await self.alert("warning", f"{s.name} is already running outside the supervisor: "
                             "leaving it alone until the next session")
            return
        args = [sys.executable, "-m", s.module, "--strat-id", s.strat_id, *s.args]
        if self.dry_run:
            args.append("--dry-run")
        self.logs.mkdir(exist_ok=True)
        # The bot writes its own log file; a crash's traceback lands here.
        with open(self.logs / f"{s.name}-console.log", "ab") as console:
            child.proc = await asyncio.create_subprocess_exec(
                *args, cwd=self.root, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=console)
        child.started = now
        log.info("started %s (pid %d): %s", s.name, child.proc.pid, " ".join(args[1:]))

    async def stop_child(self, child: Child) -> bool:
        """Stop one bot. True if it stopped by itself, cancelling its own orders."""
        proc = child.proc
        if proc is None or proc.returncode is not None:
            child.proc = None
            return True
        child.stopping = True
        try:
            deadline = time.monotonic() + self.cfg.stop_grace_seconds
            while time.monotonic() < deadline:
                self.request_stop(child.name)
                if await wait_exit(proc, min(1.0, deadline - time.monotonic())):
                    break
            else:
                log.warning("%s ignored the stop request", child.name)
                if os.name != "nt":  # on Windows terminate() is already a kill
                    proc.terminate()
                if not await wait_exit(proc, self.cfg.term_grace_seconds):
                    proc.kill()
                    await proc.wait()
            if proc.returncode == 0:
                log.info("%s stopped cleanly", child.name)
                return True
            sent = await self.emergency()
            await self.alert("serious", f"{child.name} did not stop cleanly (exit {proc.returncode}), "
                             "so its orders may rest: " + ("mass cancel sent, the other bots requote"
                             if sent else "MASS CANCEL FAILED, cancel on the console now"))
            return False
        finally:
            child.stopping = False
            child.proc = None
            child.exited = time.time()

    async def run_emergency(self) -> bool:
        """Mass-cancel from a separate process with its own connection."""
        self.logs.mkdir(exist_ok=True)
        with open(self.logs / "emergency-console.log", "ab") as console:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "ops.emergency", cwd=self.root,
                stdin=asyncio.subprocess.DEVNULL, stdout=console, stderr=console)
        if not await wait_exit(proc, self.cfg.emergency_timeout_seconds):
            proc.kill()
            return False
        return proc.returncode == 0

    # What happens to them.

    async def on_exit(self, child: Child, now: float) -> None:
        code = child.proc.returncode if child.proc else None
        child.proc, child.exited = None, now
        if self.stopped_by_hand(child):
            child.held = "stopped from the dashboard"
            unhedged = child.name == "hedger" and any(
                c.running and "--no-hedge" in c.strategy.args for c in self.children)
            await self.alert("serious" if unhedged else "warning",
                             f"{child.name} was stopped by hand (exit {code}): it stays down until "
                             "the next session" + (". The quoter is NOT hedged." if unhedged else "."))
            return
        await self.crashed(child, now, f"exited with code {code}")

    async def crashed(self, child: Child, now: float, why: str) -> None:
        child.restarts.append(now)
        while child.restarts and now - child.restarts[0] > self.cfg.restart_window_seconds:
            child.restarts.popleft()
        if len(child.restarts) > self.cfg.max_restarts:
            await self.give_up(child, why)
            return
        delays = self.cfg.restart_delays
        delay = delays[min(len(child.restarts), len(delays)) - 1]
        child.next_start = now + delay
        await self.alert("serious", f"{child.name} {why}: restarting in {delay:.0f} s "
                         f"({len(child.restarts)} of {self.cfg.max_restarts} allowed in "
                         f"{self.cfg.restart_window_seconds / 60:.0f} min)")

    async def give_up(self, child: Child, why: str) -> None:
        child.held = f"gave up: {why}"
        stopped = []
        if child.name == "hedger":  # the others rely on it for their delta
            dependents = [c for c in self.children if c is not child]
            for c in dependents:  # waiting to restart included
                c.held = "the hedger is down"
            running = [c for c in dependents if c.running]
            await asyncio.gather(*(self.stop_child(c) for c in running))
            stopped = [c.name for c in running]
        sent = await self.emergency()
        await self.alert("urgent", f"gave up on {child.name} after {len(child.restarts)} restarts "
                         f"({why}). " + (f"Stopped {', '.join(stopped)}. " if stopped else "")
                         + ("Mass cancel sent." if sent else "MASS CANCEL FAILED: cancel on the console now.")
                         + " Restart the supervisor once fixed.")

    async def check_stuck(self, child: Child, status: dict | None, now: float) -> None:
        if now - child.started < self.cfg.startup_grace_seconds:
            return
        t = status.get("t") if status else None
        age = now - t if isinstance(t, (int, float)) else None
        if age is not None and age < self.cfg.stale_seconds:
            return
        # The quoter reports market_open, the others trading. Before the open a quiet bot is
        # fine; in the core of the session it is stuck whatever it last said.
        active = status.get("market_open", status.get("trading")) if status else None
        if not (active or schedule.in_core(self.clock(), self.cfg)):
            return
        what = "no status file" if age is None else f"status {age:.0f} s old"
        await self.stop_child(child)
        # Not counted towards giving up: an exchange outage silences every bot, and they must
        # come back when it does.
        child.stuck += 1
        child.next_start = time.time() + self.cfg.restart_delays[0]
        await self.alert("serious", f"{child.name} looked stuck ({what}): restarted it", key=f"stuck-{child.name}")

    async def check_risk(self, statuses: list[dict]) -> None:
        cure = any(s.get("in_cure") for s in statuses)
        if cure and not self.flags.get("cure"):
            await self.alert("urgent", "the account is in a cure: only risk-reducing orders for 15 min, "
                             "then the engine reduces positions at our cost. Check Risk on the dashboard.")
        elif self.flags.get("cure") and not cure:
            await self.alert("info", "the cure is over")
        self.flags["cure"] = cure
        pnl = next((s["daily_pnl"] for s in statuses if isinstance(s.get("daily_pnl"), (int, float))), None)
        if pnl is not None:
            self.flags["pnl"] = pnl
            for level in self.cfg.loss_alerts:
                if pnl <= -level and level not in self.flags.setdefault("losses", set()):
                    self.flags["losses"].add(level)
                    await self.alert("serious", f"day P&L is ${pnl:,.0f}, past -${level:,.0f} "
                                     "(the quoter goes reduce-only at -$60k, the exchange halts at -$150k)")
        paused = sorted({s.get("strat_id", "?") for s in statuses if s.get("budget_paused")})
        if paused and not self.flags.get("budget"):
            await self.alert("warning", f"message budget hit, orders paused for a minute: {', '.join(paused)}")
        self.flags["budget"] = bool(paused)

    def new_day(self, day: date) -> None:
        self.day, self.flags = day, {}
        for c in self.children:
            c.held, c.next_start, c.stuck = None, 0.0, 0
            c.restarts.clear()

    async def end_of_session(self) -> None:
        running = [c for c in self.children if c.running]
        await asyncio.gather(*(self.stop_child(c) for c in running))
        pnl = self.flags.get("pnl")
        lines = [f"session over{' (dry run)' if self.dry_run else ''}: day P&L "
                 + (f"${pnl:,.0f}" if pnl is not None else "unknown")]
        for c in self.children:
            notes = [f"{len(c.restarts)} crash restarts"] if c.restarts else []
            notes += [f"{c.stuck} hang restarts"] if c.stuck else []
            notes += [c.held] if c.held else []
            lines.append(f"{c.name}: {', '.join(notes) or 'ran clean'}")
        await self.alert("info", "\n".join(lines))

    # The loop.

    async def take_over(self) -> None:
        """At start-up, stop copies left running here (say by a supervisor that died), so
        exactly one copy of each runs from now on."""
        now = time.time()
        found = [c for c in self.children
                 if (age := self.status_age(c, now)) is not None and age < self.cfg.orphan_seconds]
        if not found:
            return
        names = ", ".join(c.name for c in found)
        await self.alert("warning", f"already running here: {names}. Asking them to stop before taking over.")
        deadline = time.time() + self.cfg.stop_grace_seconds + self.cfg.orphan_seconds
        while time.time() < deadline:
            for c in found:
                self.request_stop(c.name)
            await asyncio.sleep(self.cfg.poll_seconds)
            if all((a := self.status_age(c, time.time())) is None or a >= self.cfg.orphan_seconds for c in found):
                return
        for c in found:
            a = self.status_age(c, time.time())
            if a is not None and a < self.cfg.orphan_seconds:
                c.held = "another copy would not stop"
        await self.alert("urgent", "a copy that was already running would not stop: not starting "
                         f"{', '.join(c.name for c in found if c.held)}. Stop it by hand.")

    async def tick(self) -> None:
        now = time.time()
        open_ = self.is_open()
        day = schedule.session_day(self.clock())
        if open_ and day != self.day:
            self.new_day(day)
        if self.was_open and not open_:
            await self.end_of_session()
        self.was_open = open_
        statuses = []
        for child in self.children:
            if child.proc is not None and child.proc.returncode is not None:
                await self.on_exit(child, now)
            if open_ and child.proc is None and child.held is None and now >= child.next_start:
                await self.launch(child)
            if child.running:
                status = self.read_status(child)
                if status:
                    statuses.append(status)
                await self.check_stuck(child, status, now)
        await self.check_risk(statuses)
        self.write_status(open_)
        if self.heartbeat_url and now - self.last_heartbeat >= self.cfg.heartbeat_seconds:
            self.last_heartbeat = now
            await ping(self.heartbeat_url)

    async def run(self) -> None:
        self.logs.mkdir(exist_ok=True)
        names = ", ".join(f"{c.name} ({c.strategy.strat_id})" for c in self.children)
        await self.alert("info", f"supervisor up{' (dry run)' if self.dry_run else ''}: {names}")
        # Today counts as begun, so the first tick keeps what take_over holds back.
        self.day = schedule.session_day(self.clock())
        await self.take_over()
        try:
            while True:
                try:
                    await self.tick()
                except Exception as error:  # keep supervising whatever one tick does
                    log.exception("tick failed")
                    await self.alert("serious", f"supervisor error: {error!r}", key="tick-error")
                await asyncio.sleep(self.cfg.poll_seconds)
        finally:
            running = [c for c in self.children if c.running]
            log.info("stopping: %s", ", ".join(c.name for c in running) or "nothing running")
            await asyncio.gather(*(self.stop_child(c) for c in running))
            self.write_status(False)
            await self.alert("warning", "supervisor stopped" + (f": stopped {', '.join(c.name for c in running)}"
                                                                 if running else ""))


async def run(sup: Supervisor) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    with contextlib.suppress(NotImplementedError):  # Windows: Ctrl+C cancels the task anyway
        loop.add_signal_handler(signal.SIGINT, task.cancel)
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await sup.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="run every bot with --dry-run (also QTE_DRY_RUN=1)")
    parser.add_argument("--now", action="store_true", help="ignore the schedule: run from now on")
    parser.add_argument("--only", default=None, help="comma-separated: hedger,quoter,picker")
    args = parser.parse_args(argv)

    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.handlers.TimedRotatingFileHandler("logs/supervisor.log", when="midnight", backupCount=30),
        ],
    )
    env = os.environ
    dry_run = args.dry_run or env.get("QTE_DRY_RUN", "").lower() in ("1", "true", "yes")
    notifier = Notifier(env.get("NTFY_TOPIC"), env.get("NTFY_SERVER") or "https://ntfy.sh", env.get("NTFY_TOKEN"))
    if not notifier.topic:
        log.warning("NTFY_TOPIC is not set: alerts are only logged, nobody is told")
    # Every bot would crash on a missing token or address; say so once, not once per restart.
    try:
        resolve_token()
        resolve_url()
    except QteError as error:
        log.error("cannot start: %s", error)
        asyncio.run(notifier.send("urgent", f"supervisor cannot start: {error}"))
        return EXIT_SETUP
    only = [n.strip() for n in args.only.split(",") if n.strip()] if args.only else None
    sup = Supervisor(strategies(only), dry_run=dry_run, always_open=args.now,
                     notifier=notifier, heartbeat_url=env.get("HEARTBEAT_URL"))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(sup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
