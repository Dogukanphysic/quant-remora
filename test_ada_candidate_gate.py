"""The historical gate must reject fragile returns and never authorize live use."""

from ada_candidate_gate import assess, research


def fold(ret, drawdown=10, trades=30):
    return {'return_pct': ret, 'max_drawdown_pct': drawdown, 'trades': trades}


def test_gate_requires_all_cost_and_risk_checks():
    good = [fold(5), fold(6), fold(7), fold(8)]
    assert assess(good, good, [1, 2, 3, 10])['historical_screen_passed']
    weak = [fold(5), fold(6), fold(-0.1, drawdown=31, trades=1), fold(8)]
    result = assess(good, weak, [9, 9, 9, 9])
    assert set(result['failed_checks']) == {
        'nonpositive_stress_fold', 'stress_drawdown_above_limit',
        'insufficient_buy_hold_outperformance',
    }
    assert 'insufficient_round_trips' in assess(weak, good, [0] * 4)['failed_checks']


def test_archive_screen_cannot_authorize_live_execution():
    result = research()
    assert result['independent_forward_evidence'] is False
    assert result['live_execution_eligible'] is False
    assert result['automatic_activation_enabled'] is False
    assert len(result['candidates']) >= 1
