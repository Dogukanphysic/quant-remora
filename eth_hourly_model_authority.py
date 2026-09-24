"""Validation-gated ETH Testnet overlay; has no exchange or order API."""
from pathlib import Path

import hourly_model_authority as shared
from strategy_research import ROOT


def apply(signal, path: Path | None = None):
    result = shared.apply(signal, path or ROOT/'state/eth-testnet15m-learning.sqlite3')
    result['feature_schema'] = str(result.get('feature_schema', '')).replace('btc_', 'eth_')
    return result
