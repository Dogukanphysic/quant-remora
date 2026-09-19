"""Run only in the user's credential-bearing local session."""
import json
import os
import subprocess
import sys
import binance_testnet_worker as old


def main():
    if old.HOURLY_MODE:
        raise ValueError('Switch must start in daily mode')
    api = old.execution.Client()
    old.validate_bound_account(client=api)
    api.account()
    env = dict(os.environ, BINANCE_TESTNET_POLICY_MODE='hourly')
    subprocess.run([sys.executable,'-c',
                    'import binance_testnet_worker as w; w._signal(w.execution.PublicMarketDataClient())'],
                   env=env,check=True,cwd=old.ROOT)
    # Verify public hourly data before stopping the daily position manager.
    old.execution.PublicMarketDataClient().klines(interval='1h',limit=26)
    print('Stopping daily worker; settling its tracked Testnet position before switching.',flush=True)
    old.control('stop',client=api)
    with old._control_mutex(), old._process_lock(old.ACCOUNT_LOCK_PATH), old._process_lock(old.LOCK_PATH):
        result = old.run_once(client=api,exit_only=True)
        state = old.status_snapshot()
        if state.get('halted') or state.get('pending_client_id') or state.get('tracked_position_qty') not in ('0','0.00000000'):
            from decimal import Decimal
            if state.get('halted') or state.get('pending_client_id') or Decimal(str(state.get('tracked_position_qty'))) != 0:
                raise RuntimeError('Daily position did not settle; hourly worker NOT started. Inspect daily status.')
        print(json.dumps({'daily_close_result':result},default=str),flush=True)
    subprocess.run([sys.executable,str(old.ROOT/'binance_testnet_worker.py'),'start'],env=env,check=True)


if __name__ == '__main__':
    main()
