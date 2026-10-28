import pytest

from dashboard.structures import execution_order, legs
from strategy.bs import black76, implied_vol

M = 1_000_000


def test_put_call_parity_on_the_forward():
    c = black76(True, 100.0, 95.0, 0.25, 0.2, rate=0.04)
    p = black76(False, 100.0, 95.0, 0.25, 0.2, rate=0.04)
    df = 2.718281828 ** (-0.04 * 0.25)
    assert c.price - p.price == pytest.approx(df * (100.0 - 95.0), abs=1e-6)
    assert c.delta - p.delta == pytest.approx(df, abs=1e-6)


def test_atm_vega_and_implied_vol_round_trip():
    v = black76(True, 100.0, 100.0, 1.0, 0.2)
    assert v.vega == pytest.approx(100.0 * 0.3969 / 100, rel=1e-3)  # F * pdf(0.1) * sqrt(T) / 100
    assert implied_vol(True, v.price, 100.0, 100.0, 1.0) == pytest.approx(0.2, abs=1e-6)


def test_structure_legs_and_signs():
    rr = legs("risk_reversal", [110 * M, 90 * M], direction=1, qty=3)
    assert [(leg.right, leg.strike, leg.qty) for leg in rr] == [("P", 90 * M, -3), ("C", 110 * M, 3)]
    fly = legs("call_butterfly", [95 * M, 100 * M, 105 * M], direction=-1, qty=2)
    assert [leg.qty for leg in fly] == [-2, 4, -2]
    with pytest.raises(ValueError):
        legs("straddle", [100 * M, 105 * M], 1, 1)


def test_biggest_leg_goes_first():
    a, b = legs("strangle", [90 * M, 110 * M], 1, 1)
    assert execution_order([(a, 120.0), (b, -480.0)]) == [b, a]
