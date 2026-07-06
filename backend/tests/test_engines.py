import math

import pytest

from app.engines import odds_math as om
from app.engines.elo import EloEngine, win_draw_loss_probs


def test_american_decimal_roundtrip():
    assert om.american_to_decimal(100) == 2.0
    assert om.american_to_decimal(-110) == pytest.approx(1.9091, abs=1e-4)
    assert om.american_to_decimal(250) == 3.5
    assert om.american_to_decimal(-200) == 1.5
    assert om.decimal_to_american(2.0) == 100
    assert om.decimal_to_american(1.9091) == -110
    assert om.decimal_to_american(1.5) == -200


def test_implied_and_vig():
    imp = [om.implied_prob(1.9091)] * 2  # -110 both sides
    assert om.overround(imp) == pytest.approx(0.0476, abs=1e-3)
    fair = om.remove_vig(imp)
    assert fair == pytest.approx([0.5, 0.5])
    assert sum(om.remove_vig([0.55, 0.28, 0.25])) == pytest.approx(1.0)


def test_ev():
    # p=0.55 at even money -> +10%
    assert om.expected_value(0.55, 2.0) == pytest.approx(0.10)
    # book-fair price -> EV 0
    assert om.expected_value(0.5, 2.0) == pytest.approx(0.0)


def test_kelly():
    # p=0.55, dec 2.0: f* = (0.55*1 - 0.45)/1 = 0.10
    assert om.kelly_fraction(0.55, 2.0) == pytest.approx(0.10)
    # no edge -> 0
    assert om.kelly_fraction(0.5, 2.0) == 0.0
    assert om.kelly_fraction(0.4, 2.0) == 0.0


def test_clv():
    # placed 2.10, closed 2.00 -> +5%
    assert om.clv_pct(2.10, 2.00) == pytest.approx(0.05)
    assert om.clv_pct(1.90, 2.00) == pytest.approx(-0.05)


def test_davidson_probs_sum_and_symmetry():
    p_h, p_d, p_a = win_draw_loss_probs(1500, 1500, nu=0.63)
    assert p_h + p_d + p_a == pytest.approx(1.0)
    assert p_h == pytest.approx(p_a)
    assert p_d == pytest.approx(0.63 / 2.63, abs=1e-6)  # nu/(2+nu)
    # stronger home player -> higher p_home
    q_h, _, q_a = win_draw_loss_probs(1600, 1500)
    assert q_h > p_h and q_a < p_a


def test_elo_update_direction_and_zero_sum():
    e = EloEngine(k=32)
    hb, ha, ab, aa = e.update(1, 2, 3, 0)  # home wins big
    assert ha > hb and aa < ab
    assert (ha - hb) == pytest.approx(-(aa - ab))  # zero-sum
    # upset moves more than expected win
    e2 = EloEngine(k=32)
    e2.ratings = {1: 1700, 2: 1300}
    _, after_upset, _, _ = e2.update(1, 2, 0, 1)  # heavy favorite loses
    assert 1700 - after_upset > 16  # loses more than half K


def test_mov_scaling():
    e_small = EloEngine(k=32)
    _, a1, _, _ = e_small.update(1, 2, 1, 0)
    e_big = EloEngine(k=32)
    _, a2, _, _ = e_big.update(1, 2, 5, 0)
    assert (a2 - 1500) > (a1 - 1500)


def test_suggested_stakes_capped():
    s = om.suggested_stakes(0.6, 2.5, bankroll=1000, unit_size=10,
                            kelly_mult=0.25, max_bet=50)
    assert s["flat"] == 10
    assert 0 < s["kelly"] <= 50
