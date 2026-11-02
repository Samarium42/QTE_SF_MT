"""The supervisor against fake bots: real child processes that crash, hang, obey or ignore a
stop, so restarts, give-ups and stops are exercised as on the host."""

import asyncio
import json
import time

import pytest

from ops.config import OpsSettings, Strategy
from ops.supervisor import Supervisor

# A fake bot. Its strat ID says what to do: "<behaviour>:<name>:<status file>".
FAKE_BOT = '''
import json, sys, time
from pathlib import Path
behaviour, name, status = sys.argv[sys.argv.index("--strat-id") + 1].split(":")
started = time.time()
Path("logs").mkdir(exist_ok=True)
def write(**extra):
    try:  # as the bots do: on Windows the replace fails while a reader has the file open
        tmp = Path("logs") / (status + ".tmp")
        tmp.write_text(json.dumps({"t": time.time(), "strat_id": name, "market_open": True,
                                   "argv": sys.argv[1:], **extra}))
        tmp.replace(Path("logs") / status)
    except OSError:
        pass
def stop_requested():
    try:
        return json.loads(Path("control.json").read_text()).get("stop", {}).get(name, 0) > started
    except (OSError, ValueError):
        return False
if behaviour == "crash":
    time.sleep(0.1)
    sys.exit(1)
write()
while True:
    if behaviour != "hang" and stop_requested():
        sys.exit(0)
    if behaviour == "obey":
        write()
    elif behaviour == "cure":
        write(in_cure=True, daily_pnl=-35_000.0)
    time.sleep(0.05)
'''

CFG = OpsSettings(poll_seconds=0.05, stop_grace_seconds=3.0, term_grace_seconds=0.5,
                  restart_delays=(0.1,), max_restarts=2, restart_window_seconds=60,
                  startup_grace_seconds=1.0, stale_seconds=0.5, orphan_seconds=0.3)


class Alerts:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, level, message, key=None):
        self.sent.append((level, message))
        return True

    def levels(self, level: str) -> list[str]:
        return [m for lv, m in self.sent if lv == level]


class Emergency:
    def __init__(self):
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return True


def fake(name: str, behaviour: str, args: tuple[str, ...] = ()) -> Strategy:
    status = f"{name}-status.json"
    return Strategy(name, "fakebot", f"{behaviour}:{name}:{status}", status, args)


def make(tmp_path, strats) -> tuple[Supervisor, Alerts, Emergency]:
    (tmp_path / "fakebot.py").write_text(FAKE_BOT)
    alerts, emergency = Alerts(), Emergency()
    sup = Supervisor(strats, CFG, root=tmp_path, notifier=alerts, emergency=emergency, always_open=True)
    return sup, alerts, emergency


async def run_until(sup: Supervisor, done, timeout: float = 15.0) -> None:
    """Run the supervisor until done() holds, then stop it as Ctrl+C would."""
    task = asyncio.create_task(sup.run())
    try:
        deadline = time.monotonic() + timeout
        while not done():
            assert time.monotonic() < deadline, "timed out"
            assert not task.done(), task.exception()
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def child(sup: Supervisor, name: str):
    return next(c for c in sup.children if c.name == name)


def status(tmp_path, name: str) -> dict:
    return json.loads((tmp_path / "logs" / f"{name}-status.json").read_text())


def test_starts_bots_and_stops_them_cleanly(tmp_path):
    sup, alerts, emergency = make(tmp_path, [fake("hedger", "obey"), fake("quoter", "obey", ("--no-hedge",))])
    procs = []

    def both_up():
        if all(c.running for c in sup.children) and all((tmp_path / "logs" / c.strategy.status).exists()
                                                       for c in sup.children):
            procs[:] = [c.proc for c in sup.children]
            return True
        return False

    asyncio.run(run_until(sup, both_up))
    assert "--no-hedge" in status(tmp_path, "quoter")["argv"]
    assert [p.returncode for p in procs] == [0, 0]  # stopped through control.json
    assert set(json.loads((tmp_path / "control.json").read_text())["stop"]) == {"hedger", "quoter"}
    assert emergency.calls == 0
    assert json.loads((tmp_path / "logs" / "supervisor-status.json").read_text())["open"] is False


def test_crash_is_restarted_then_given_up_on(tmp_path):
    sup, alerts, emergency = make(tmp_path, [fake("picker", "crash")])
    asyncio.run(run_until(sup, lambda: alerts.levels("urgent")))
    picker = child(sup, "picker")
    assert picker.held.startswith("gave up")
    assert len(alerts.levels("serious")) == CFG.max_restarts  # one per restart
    assert any("gave up on picker" in m for m in alerts.levels("urgent"))
    assert emergency.calls == 1


def test_hedger_down_stops_the_bots_it_hedges(tmp_path):
    sup, alerts, emergency = make(tmp_path, [fake("hedger", "crash"), fake("quoter", "obey", ("--no-hedge",))])
    asyncio.run(run_until(sup, lambda: alerts.levels("urgent")))
    quoter = child(sup, "quoter")
    assert not quoter.running and quoter.held == "the hedger is down"
    assert any("Stopped quoter" in m for m in alerts.levels("urgent"))
    assert emergency.calls == 1


def test_stopped_from_the_dashboard_stays_down(tmp_path):
    sup, alerts, emergency = make(tmp_path, [fake("quoter", "obey")])

    def stop_by_hand():
        q = child(sup, "quoter")
        if q.running and (tmp_path / "logs" / "quoter-status.json").exists() and q.held is None:
            (tmp_path / "control.json").write_text(json.dumps({"stop": {"quoter": time.time()}}))
        return q.held is not None

    asyncio.run(run_until(sup, stop_by_hand))
    assert child(sup, "quoter").held == "stopped from the dashboard"
    assert any("stopped by hand" in m for m in alerts.levels("warning"))
    assert emergency.calls == 0


def test_stuck_bot_is_restarted(tmp_path):
    sup, alerts, emergency = make(tmp_path, [fake("quoter", "stale")])
    first = []

    def restarted():
        q = child(sup, "quoter")
        if q.running and not first:
            first.append(q.proc.pid)
        return q.stuck >= 1 and q.running and q.proc.pid != first[0]

    asyncio.run(run_until(sup, restarted))
    assert any("looked stuck" in m for m in alerts.levels("serious"))
    assert child(sup, "quoter").held is None  # hangs do not count towards giving up
    assert emergency.calls == 0  # it stopped cleanly when asked


def test_bot_that_ignores_stop_is_killed_and_orders_mass_cancelled(tmp_path):
    sup, alerts, emergency = make(tmp_path, [fake("quoter", "hang")])
    asyncio.run(run_until(sup, lambda: (tmp_path / "logs" / "quoter-status.json").exists()))
    assert emergency.calls == 1
    assert any("did not stop cleanly" in m for m in alerts.levels("serious"))


def test_cure_and_loss_alerts_once(tmp_path):
    sup, alerts, emergency = make(tmp_path, [fake("quoter", "cure")])
    asyncio.run(run_until(sup, lambda: sup.flags.get("cure") and len(alerts.sent) >= 3))
    assert sum("in a cure" in m for m in alerts.levels("urgent")) == 1
    assert sum("past -$30,000" in m for m in alerts.levels("serious")) == 1
    assert not any("60,000" in m for _, m in alerts.sent if "past" in m)


def test_copy_running_elsewhere_is_not_doubled(tmp_path):
    # A status file kept fresh by someone else (a terminal, the dashboard) that ignores stop.
    sup, alerts, emergency = make(tmp_path, [fake("quoter", "obey")])
    (tmp_path / "logs").mkdir()

    async def go():
        async def other_copy():
            while True:
                (tmp_path / "logs" / "quoter-status.json").write_text(json.dumps({"t": time.time()}))
                await asyncio.sleep(0.05)

        writer = asyncio.create_task(other_copy())
        try:
            await run_until(sup, lambda: child(sup, "quoter").held is not None)
        finally:
            writer.cancel()

    asyncio.run(go())
    q = child(sup, "quoter")
    assert q.held == "another copy would not stop" and q.proc is None
    assert any("would not stop" in m for m in alerts.levels("urgent"))
