"""ETH 15m proxy learner for the Testnet worker; never submits orders."""
import importlib.util
import json
import time

from strategy_research import ROOT


PATH = ROOT / 'state/eth-testnet15m-learning.sqlite3'


def _learner():
    spec = importlib.util.spec_from_file_location('eth_testnet_private_learner', ROOT/'trend4h_learning.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.HOURLY = True
    module.FRAME = '15m'
    module.STEP = 900_000
    module.VERSION = 'ridge-next15m-v1'
    module.PATH = PATH
    return module


def refresh(market):
    learner = _learner()
    try:
        raw = market.klines(interval='15m', limit=1000, symbol='ETHUSDT')
        now = time.time()
        rows = [dict(ts=int(row[0]), open=float(row[1]), high=float(row[2]),
                     low=float(row[3]), close=float(row[4]), volume=float(row[5]))
                for row in raw if int(row[6]) < now * 1000]
        if len(rows) < 205 or any(b['ts'] - a['ts'] != 900_000 for a, b in zip(rows, rows[1:])):
            raise ValueError('Incomplete ETH 15m history')
        result = learner.update(rows, now, seed=not PATH.exists(), path=PATH)
    except Exception as exc:
        result = dict(status='error', error=type(exc).__name__)
    (ROOT/'state/eth-testnet15m-learning-status.json').write_text(
        json.dumps(result, indent=2), encoding='utf-8')
    return result
