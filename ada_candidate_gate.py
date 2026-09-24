"""Read-only historical screen for ADA execution candidates.

The archive has been inspected in earlier experiments. Passing this screen is
therefore never treated as independent forward evidence or live authorization.
"""

from datetime import datetime, timezone
import json

from ada_bb_execution_audit import atr14, entry_variants, simulate
from dual_market_strategy_audit import ROOT, load


BASE_FEE = 0.0015  # 0.1% commission plus 0.05% execution allowance per side.
STRESS_FEE = 0.0025
MAX_DRAWDOWN_PCT = 30.0
MIN_ROUND_TRIPS_PER_FOLD = 20


def buy_and_hold(prices, start, end, fee):
    return 100 * (prices['close'][end - 1] / prices['open'][start]
                  * (1 - fee) ** 2 - 1)


def assess(base, stress, benchmark):
    """All folds must survive normal and stressed costs; 3/4 beat holding."""
    failures = []
    if any(f['trades'] < MIN_ROUND_TRIPS_PER_FOLD for f in base):
        failures.append('insufficient_round_trips')
    if any(f['return_pct'] <= 0 for f in base):
        failures.append('nonpositive_base_fold')
    if any(f['return_pct'] <= 0 for f in stress):
        failures.append('nonpositive_stress_fold')
    if any(f['max_drawdown_pct'] > MAX_DRAWDOWN_PCT for f in stress):
        failures.append('stress_drawdown_above_limit')
    if sum(f['return_pct'] > hold for f, hold in zip(stress, benchmark)) < 3:
        failures.append('insufficient_buy_hold_outperformance')
    return {'historical_screen_passed': not failures, 'failed_checks': failures}


def research():
    ts, prices = load('adausdt')
    entries, exits = entry_variants(prices)
    atr = atr14(prices)
    cuts = [int(len(ts) * fraction) for fraction in (0, .25, .5, .75, 1)]
    benchmarks = [buy_and_hold(prices, cuts[i], cuts[i + 1], STRESS_FEE)
                  for i in range(4)]
    candidates = {}
    for name, entry in entries.items():
        base = [simulate(prices, entry, exits, atr, cuts[i], cuts[i + 1], BASE_FEE)
                for i in range(4)]
        stress = [simulate(prices, entry, exits, atr, cuts[i], cuts[i + 1], STRESS_FEE)
                  for i in range(4)]
        candidates[name] = {
            'base_folds': base,
            'stress_folds': stress,
            **assess(base, stress, benchmarks),
        }
    return {
        'as_of_utc': datetime.now(timezone.utc).isoformat(),
        'symbol': 'ADAUSDT',
        'candles': len(ts),
        'historical_archive_previously_inspected': True,
        'independent_forward_evidence': False,
        'live_execution_eligible': False,
        'automatic_activation_enabled': False,
        'normal_cost_per_side': BASE_FEE,
        'stress_cost_per_side': STRESS_FEE,
        'minimum_round_trips_per_fold': MIN_ROUND_TRIPS_PER_FOLD,
        'maximum_stress_drawdown_pct': MAX_DRAWDOWN_PCT,
        'minimum_stress_folds_beating_buy_hold': 3,
        'buy_hold_stress_return_pct': benchmarks,
        'candidates': candidates,
    }


def main():
    report = research()
    output = ROOT / 'reports/ada-candidate-gate.json'
    output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    for name, candidate in report['candidates'].items():
        print(name, 'PASS' if candidate['historical_screen_passed'] else 'REJECT',
              ', '.join(candidate['failed_checks']) or 'historical screen only')
    print('independent_forward_evidence=false; live_execution_eligible=false')


if __name__ == '__main__':
    main()
