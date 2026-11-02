"""When the strategies should be running: a window each weekday, in New York time.

New York time follows the exchange through both daylight-saving changes, including the weeks
when London and New York are an hour out of step (Rule 1005).
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ops.config import OpsSettings

NEW_YORK = ZoneInfo("America/New_York")


def _between(now: datetime, cfg: OpsSettings, start: time, stop: time) -> bool:
    local = now.astimezone(NEW_YORK)
    if local.weekday() not in cfg.weekdays:
        return False
    return start <= local.time() < stop


def is_open(now: datetime, cfg: OpsSettings) -> bool:
    """Whether the strategies should be running now."""
    return _between(now, cfg, cfg.start, cfg.stop)


def in_core(now: datetime, cfg: OpsSettings) -> bool:
    """Whether the market is surely open now, so a silent bot is a stuck one."""
    return _between(now, cfg, cfg.core_start, cfg.core_stop)


def session_day(now: datetime) -> date:
    return now.astimezone(NEW_YORK).date()


def next_open(now: datetime, cfg: OpsSettings) -> datetime:
    """The start of the next window, or now if one is open."""
    local = now.astimezone(NEW_YORK)
    for days in range(8):
        day = local.date() + timedelta(days=days)
        if day.weekday() not in cfg.weekdays:
            continue
        start = datetime.combine(day, cfg.start, NEW_YORK)
        stop = datetime.combine(day, cfg.stop, NEW_YORK)
        if local < stop:
            return max(start, local)
    raise ValueError("no weekday in OpsSettings.weekdays")
