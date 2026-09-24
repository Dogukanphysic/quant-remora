import json
from decimal import Decimal

from ada_pnl_diagnostic import analyze


def test_sale_fee_residual_and_external_cash_are_separated():
    state = dict(initial_mark_usdt='20', external_capital_inflows_usdt='5',
                 marked_equity_usdt='25.09191', usdt='23.07191', ada='1',
                 last_capital_rebase=dict(checked_at=10, ada='10',
                                          reference_bid='2'))
    request = json.dumps(dict(created=11, side='SELL'))
    response = json.dumps(dict(status='FILLED', executedQty='9',
                               cummulativeQuoteQty='18.09',
                               fills=[dict(commission='0.01809',
                                           commissionAsset='USDT')]))
    previous = json.dumps(dict(marked_equity_usdt='20',
                               external_capital_inflows_usdt='0'))
    result = analyze(state, [(request,response)], (10.5,previous,'bollinger_touch_15m_v1'))
    assert result['single_sale_breakdown_available']
    assert result['marked_delta_usdt'] == '0.09191'
    assert result['sold_part_delta_vs_rebase_bid_usdt'] == '0.07191'
    assert Decimal(result['residual_mark_delta_usdt']) == Decimal('0.02')
    assert result['current_strategy']['filled_orders_since_change'] == 1


def test_diagnostic_does_not_invent_decomposition_for_multiple_orders():
    state = dict(initial_mark_usdt='20',external_capital_inflows_usdt='0',
                 marked_equity_usdt='20',usdt='20',ada='0',
                 last_capital_rebase=dict(checked_at=10,ada='10',reference_bid='2'))
    request = json.dumps(dict(created=11,side='SELL'))
    response = json.dumps(dict(status='FILLED',executedQty='5',
                               cummulativeQuoteQty='10',fills=[]))
    result = analyze(state, [(request,response),(request,response)])
    assert result['filled_orders_since_rebase'] == 2
    assert not result['single_sale_breakdown_available']
