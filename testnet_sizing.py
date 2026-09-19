"""Explicit, stopped-worker Testnet sizing change; never sends orders."""
import argparse
import json
import os
from pathlib import Path
import tempfile

import binance_testnet_worker as worker


def configure(quote_usdt):
    if quote_usdt not in ('10', '15'):
        raise ValueError('Supported Testnet entry sizes: 10 or 15 USDT.')
    with worker._control_mutex(worker.LOCK_PATH):
        state = worker.status_snapshot()
        if (state.get('status_available') is not True
                or state.get('execution_state_known') is not True
                or state.get('running') or state.get('desired_running')
                or state.get('pending_client_id')
                or worker._lock_active(worker.ACCOUNT_LOCK_PATH)):
            raise ValueError('Sizing change requires a stopped, known worker without pending orders.')
        value = worker._load_policy_config()
        if quote_usdt == '15':
            value['testnet_sizing_override'] = {
                'quote_usdt': '15', 'scope': 'next_entries_only',
                'authorization': 'user_requested_testnet_risk_increase',
            }
        else:
            value.pop('testnet_sizing_override', None)
        path = worker.POLICY_CONFIG_PATH
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                json.dump(value, output, indent=2, ensure_ascii=False)
                output.write('\n')
            worker._load_policy_config(temporary)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return {'next_entry_quote_usdt': quote_usdt, 'restart_required': True,
            'existing_position_unchanged': True, 'orders_submitted': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quote-usdt', required=True, choices=('10', '15'))
    print(json.dumps(configure(parser.parse_args().quote_usdt), indent=2))
