"""Settings for the supervisor (ops/supervisor.py), which keeps the strategies running on an
always-on host: started before the open, restarted when they crash or hang, stopped after
the close, with push alerts so someone is always watching (Rule 1008).

Strategy IDs change each term (Rule 1002), so they come from the environment (deploy/ops.env
on the host) and fall back to the practice-week IDs below.
"""

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import time
from decimal import Decimal

# Start order: the hedger first, because the quoter runs --no-hedge beside it.
ORDER = ("hedger", "quoter", "picker")
MODULES = {"hedger": "strategy.hedger", "quoter": "strategy.bot", "picker": "strategy.stale_bot"}
STATUS = {"hedger": "hedger-status.json", "quoter": "bot-status.json", "picker": "stale-status.json"}
ENV_IDS = {"hedger": "QTE_HEDGER_ID", "quoter": "QTE_QUOTER_ID", "picker": "QTE_PICKER_ID"}
DEFAULT_IDS = {"hedger": "sf_delta_hedger_v1", "quoter": "sf_print_capture_v1",
               "picker": "sf_stale_picker_v1"}


@dataclass(frozen=True)
class Strategy:
    name: str  # quoter, hedger or picker: the key the dashboard and control.json use
    module: str
    strat_id: str
    status: str  # its status file under logs/
    args: tuple[str, ...] = ()


def strat_id(name: str, env: Mapping[str, str] = os.environ) -> str:
    return env.get(ENV_IDS[name]) or DEFAULT_IDS[name]


def strategies(only: Iterable[str] | None = None, env: Mapping[str, str] = os.environ) -> list[Strategy]:
    """The strategies to run, in start order. The quoter leaves hedging to the hedger only
    when the hedger runs too."""
    chosen = set(only or ORDER)
    unknown = chosen - set(ORDER)
    if unknown:
        raise ValueError(f"unknown strategies: {', '.join(sorted(unknown))}")
    out = []
    for name in ORDER:
        if name not in chosen:
            continue
        args = ("--no-hedge",) if name == "quoter" and "hedger" in chosen else ()
        out.append(Strategy(name, MODULES[name], strat_id(name, env), STATUS[name], args))
    return out


@dataclass(frozen=True)
class OpsSettings:
    # When the strategies run: weekdays, New York time. The session is 09:30-16:00; the bots
    # wait for the open themselves and stop quoting before the close, so start early (time
    # to connect and adopt resting orders) and stop after the close has cancelled everything.
    start: time = time(9, 0)
    stop: time = time(16, 10)
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    # Inside this part of the session a bot must be writing its status file, whatever it
    # last said about the market (the hang watchdog).
    core_start: time = time(9, 31)
    core_stop: time = time(15, 59)

    poll_seconds: float = 1.0
    # Stopping a bot: ask through control.json (it cancels its own orders), then SIGTERM,
    # then kill it and mass-cancel.
    stop_grace_seconds: float = 20.0
    term_grace_seconds: float = 10.0

    # Restarts after a crash or hang, waiting each delay in turn (the last repeats). More
    # than max_restarts within restart_window_seconds and the supervisor gives up on it.
    restart_delays: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0)
    max_restarts: int = 5
    restart_window_seconds: float = 15 * 60

    # Hang watchdog: a status file this old means the bot is stuck. A fresh start gets
    # startup_grace_seconds to connect first.
    stale_seconds: float = 60.0
    startup_grace_seconds: float = 120.0
    # At start-up, a status file fresher than this means a copy is already running here.
    orphan_seconds: float = 10.0

    # Push an alert as the day's loss passes each of these.
    loss_alerts: tuple[Decimal, ...] = (Decimal("30_000"), Decimal("60_000"))
    # Ping the dead-man URL (healthchecks.io or similar) this often, if one is set.
    heartbeat_seconds: float = 60.0
    emergency_timeout_seconds: float = 60.0
