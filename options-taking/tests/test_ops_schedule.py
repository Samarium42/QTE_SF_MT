from datetime import datetime, timezone

import pytest

from ops.config import OpsSettings, strategies
from ops.schedule import in_core, is_open, next_open

CFG = OpsSettings()


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_window_after_both_clock_changes():
    # Mon 2 Nov 2026: New York is on EST (UTC-5), so 09:00-16:10 New York is 14:00-21:10 UTC.
    assert not is_open(utc(2026, 11, 2, 13, 59), CFG)
    assert is_open(utc(2026, 11, 2, 14, 0), CFG)
    assert is_open(utc(2026, 11, 2, 21, 9), CFG)
    assert not is_open(utc(2026, 11, 2, 21, 10), CFG)


def test_window_while_clocks_are_out_of_step():
    # Wed 28 Oct 2026: London is on GMT but New York still on EDT (UTC-4), an hour earlier.
    assert is_open(utc(2026, 10, 28, 13, 0), CFG)
    assert not is_open(utc(2026, 10, 28, 20, 10), CFG)


def test_weekends_are_closed():
    assert not is_open(utc(2026, 11, 7, 15, 0), CFG)  # Saturday
    assert not is_open(utc(2026, 11, 8, 15, 0), CFG)  # Sunday


def test_core_session():
    assert not in_core(utc(2026, 11, 2, 14, 29), CFG)  # 09:29 New York, before the open
    assert in_core(utc(2026, 11, 2, 15, 0), CFG)
    assert not in_core(utc(2026, 11, 2, 21, 0), CFG)  # 16:00, the close


def test_next_open():
    assert next_open(utc(2026, 11, 2, 12, 0), CFG) == utc(2026, 11, 2, 14, 0)
    now = utc(2026, 11, 2, 15, 0)
    assert next_open(now, CFG) == now  # open now
    assert next_open(utc(2026, 11, 6, 22, 0), CFG) == utc(2026, 11, 9, 14, 0)  # Friday night -> Monday


def test_strategies_order_and_hedging():
    names = [s.name for s in strategies(env={})]
    assert names == ["hedger", "quoter", "picker"]
    quoter = next(s for s in strategies(env={}) if s.name == "quoter")
    assert quoter.args == ("--no-hedge",) and quoter.strat_id == "sf_print_capture_v1"
    # Without the hedger the quoter hedges itself.
    assert strategies(["quoter"], env={})[0].args == ()
    with pytest.raises(ValueError):
        strategies(["quoter", "nope"], env={})


def test_strategy_ids_from_the_environment():
    ids = {s.name: s.strat_id for s in strategies(env={"QTE_QUOTER_ID": "sf_winter_q"})}
    assert ids == {"hedger": "sf_delta_hedger_v1", "quoter": "sf_winter_q", "picker": "sf_stale_picker_v1"}
