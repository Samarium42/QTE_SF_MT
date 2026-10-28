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


from dashboard.analytics import (  # noqa: E402
    DayResult,
    Holding,
    Quote,
    SmilePoint,
    fit_smile,
    idle_accrual,
    markout,
    mispricings,
    scenario_grid,
    sharpe,
    smile_iv,
)


def test_sharpe_matches_rule_9001_and_idle_book_scores_zero():
    e0 = 2_500_000.0
    idle = idle_accrual(e0, sofr=0.043, days=1)
    flat = [DayResult(f"d{i}", idle, idle) for i in range(5)]
    assert sharpe(flat, e0)["sharpe"] == 0.0
    days = [DayResult("a", 1000 + idle, idle), DayResult("b", 3000 + idle, idle)]
    r = [1000 / e0, 3000 / e0]
    expected = (252 ** 0.5) * (sum(r) / 2) / (((r[0] - 2000 / e0) ** 2 + (r[1] - 2000 / e0) ** 2) ** 0.5)
    assert sharpe(days, e0)["sharpe"] == pytest.approx(expected)


def _pt(k, iv, f=100.0, t=0.1, vega=0.1):
    return SmilePoint(f"X{k}", True, k, f, t, iv, vega)


def test_smile_fit_recovers_a_quadratic_and_flags_an_outlier():
    pts = [_pt(k, 0) for k in (90, 95, 100, 105, 110)]
    pts = [SmilePoint(p.instrument, p.call, p.strike, p.forward, p.years,
                      20 + 5 * p.x + 30 * p.x * p.x, p.vega) for p in pts]
    fit = fit_smile(pts)
    assert fit == pytest.approx((20, 5, 30), abs=1e-6)
    # A 100 call quoted at an ask well under its fair value at the smile vol is a buy.
    atm = pts[2]
    fair = black76(True, 100.0, 100.0, 0.1, smile_iv(fit, atm.x) / 100).price
    rows = mispricings([Quote(atm, bid=fair - 0.5, ask=fair - 0.2, mark=fair)], fit, fee_per_share=0.003)
    assert rows[0]["side"] == "BUY" and rows[0]["edge"] == pytest.approx((0.2 - 0.003) * 100)


def test_scenario_grid_is_zero_at_the_centre_and_long_call_gains_on_rally_and_vol():
    h = [Holding("SPY", 10, True, strike=100, forward=100, years=0.1, iv=20, spot=100),
         Holding("SPY", -500, None, spot=100)]
    g = scenario_grid(h, [-0.02, 0.0, 0.02], [-2.0, 0.0, 2.0])
    assert g[1][1] == pytest.approx(0.0)
    assert g[2][1] > 0 > g[0][1]  # long vega
    assert g[1][0] > 0 and g[1][2] > 0  # delta hedged long gamma gains both ways


def test_markout_sign():
    assert markout("BUY", 5.00, 5.10, 2, 100) == pytest.approx(20.0)
    assert markout("SELL", 5.00, 5.10, 2, 100) == pytest.approx(-20.0)
