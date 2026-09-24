"""Read-only decomposition of ADA's current marked PnL since capital rebase."""

from decimal import Decimal
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'state/ada-live.sqlite3'


def dec(value):
    return Decimal(str(value))


def analyze(state, orders, strategy_change=None):
    baseline = dec(state['initial_mark_usdt'])
    external = dec(state.get('external_capital_inflows_usdt', '0'))
    equity = dec(state['marked_equity_usdt'])
    report = {
        'basis': 'external_rebase_mark_not_tax_cost_basis',
        'baseline_usdt': str(baseline),
        'external_inflows_usdt': str(external),
        'current_marked_equity_usdt': str(equity),
        'marked_delta_usdt': str(equity - baseline - external),
        'filled_orders_since_rebase': 0,
        'single_sale_breakdown_available': False,
    }
    if strategy_change:
        changed_at, previous_json, strategy = strategy_change
        previous = json.loads(previous_json)
        since_orders = 0
        for request, response in orders:
            intent = json.loads(request)
            fill = json.loads(response) if response else {}
            if dec(intent.get('created', 0)) > dec(changed_at) and fill.get('status') == 'FILLED':
                since_orders += 1
        old_external = dec(previous.get('external_capital_inflows_usdt', '0'))
        old_mark = dec(previous['marked_equity_usdt'])
        report['current_strategy'] = {
            'name': strategy,
            'filled_orders_since_change': since_orders,
            'marked_delta_since_change_usdt': str(equity - old_mark - (external - old_external)),
            'comparison_basis': 'previous_poll_mark_before_strategy_switch',
        }
    rebase = state.get('last_capital_rebase')
    if not rebase:
        return report
    after = []
    for request, response in orders:
        intent = json.loads(request)
        fill = json.loads(response) if response else {}
        if dec(intent.get('created', 0)) > dec(rebase['checked_at']) and fill.get('status') == 'FILLED':
            after.append((intent, fill))
    report['filled_orders_since_rebase'] = len(after)
    if len(after) != 1 or after[0][0].get('side') != 'SELL':
        return report
    intent, fill = after[0]
    if any(f.get('commissionAsset') not in ('USDT',) for f in fill.get('fills', [])):
        return report
    start_ada = dec(rebase['ada'])
    sold = dec(fill['executedQty'])
    remaining = dec(state['ada'])
    if sold <= 0 or start_ada - sold != remaining:
        return report
    start_bid = dec(rebase['reference_bid'])
    gross = dec(fill['cummulativeQuoteQty'])
    fee = sum((dec(f['commission']) for f in fill['fills']), Decimal(0))
    net = gross - fee
    if abs(dec(state['usdt']) - external - net) > Decimal('0.00000001'):
        return report
    sold_delta = net - sold * start_bid
    residual_delta = equity - dec(state['usdt']) - remaining * start_bid
    if abs(sold_delta + residual_delta - (equity - baseline - external)) > Decimal('0.00000001'):
        return report
    report.update(single_sale_breakdown_available=True,
                  sale_quantity_ada=str(sold), sale_gross_usdt=str(gross),
                  sale_fee_usdt=str(fee), sale_net_usdt=str(net),
                  sold_part_delta_vs_rebase_bid_usdt=str(sold_delta),
                  residual_ada=str(remaining),
                  residual_mark_delta_usdt=str(residual_delta))
    return report


def main():
    db = sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True)
    try:
        state = json.loads(db.execute('SELECT value FROM state WHERE id=1').fetchone()[0])
        orders = db.execute('SELECT request,response FROM orders WHERE applied=1 ORDER BY id').fetchall()
        table_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_changes'").fetchone()
        strategy_change = (db.execute('SELECT ts,previous_state,new_strategy FROM strategy_changes ORDER BY id DESC LIMIT 1').fetchone()
                           if table_exists else None)
    finally:
        db.close()
    print(json.dumps(analyze(state, orders, strategy_change), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
