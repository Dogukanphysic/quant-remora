"""Causal 15-minute indicators and explicit Bollinger hypotheses."""
from dataclasses import dataclass, asdict
from statistics import pstdev


BAR_SECONDS = 900
BAR_MS = 900_000
BAR_LABEL = '15M'
POLICY_ID = 'bollinger_15m_v1'
FRESH_WINDOW_SECONDS = 120
MODEL_KEYS = {
    'trend': 'bb15_midtrend_v1',
    'breakout': 'bb15_breakout_v1',
    'reversion': 'bb15_reentry_v1',
}


def rma(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = (result[i - 1] * (period - 1) + values[i]) / period
    return result


def indicators(rows):
    close = [r['close'] for r in rows]
    changes = [close[i] - close[i - 1] for i in range(1, len(close))]
    gains = rma([max(x, 0) for x in changes], 14)
    losses = rma([max(-x, 0) for x in changes], 14)
    rsi = [None]
    for gain, loss in zip(gains, losses):
        rsi.append(None if gain is None else
                   50 if gain == loss == 0 else
                   100 if loss == 0 else 100 - 100 / (1 + gain / loss))
    tr = [max(r['high'] - r['low'],
              abs(r['high'] - rows[i - 1]['close']) if i else 0,
              abs(r['low'] - rows[i - 1]['close']) if i else 0)
          for i, r in enumerate(rows)]
    sma = {n: [None if i < n - 1 else sum(close[i-n+1:i+1]) / n
               for i in range(len(rows))] for n in (20, 50)}
    middle, upper, lower, percent_b, bandwidth = [], [], [], [], []
    for i, price in enumerate(close):
        if i < 19:
            middle.append(None)
            upper.append(None)
            lower.append(None)
            percent_b.append(None)
            bandwidth.append(None)
            continue
        window = close[i-19:i+1]
        mid = sum(window) / 20
        deviation = pstdev(window)
        top, bottom = mid + 2*deviation, mid - 2*deviation
        width = top-bottom
        middle.append(mid)
        upper.append(top)
        lower.append(bottom)
        # A zero-width band is a flat market: %B is neutral rather than undefined.
        percent_b.append((price-bottom)/width if width else .5)
        bandwidth.append(width/mid if mid else None)
    return {'rsi': rsi, 'atr': rma(tr, 14), 'sma20': sma[20], 'sma50': sma[50],
            'bb_middle': middle, 'bb_upper': upper, 'bb_lower': lower,
            'percent_b': percent_b, 'bandwidth': bandwidth,
            # Compatibility aliases for callers that group every Bollinger field.
            'bb_percent_b': percent_b, 'bb_bandwidth': bandwidth}


NAMES = {'trend': 'Bollinger orta bant trend dönüşü',
         'breakout': 'Bollinger üst bant kırılması',
         'reversion': 'Bollinger alt bant dönüşü'}
RULES = {
    'trend': ('Önceki kapanış önceki orta bandın altında/eşitken kapanış orta bandı '
              'yukarı keser, orta bant yükselir ve kapanış üst bandın altında kalırsa '
              'giriş; kapanış orta bandın altına inerse çıkış.'),
    'breakout': ('Önceki kapanış önceki üst bandın altında/eşitken kapanış üst bandı '
                 'yukarı keserse giriş; kapanış orta bandın altına inerse çıkış.'),
    'reversion': ('Önceki kapanış önceki alt bandın altında/eşitken kapanış alt bandı '
                  'yukarı keser ve orta bandın altında kalırsa giriş; kapanış orta '
                  'banda ulaşırsa çıkış.')}


def signal(rows, features, index, strategy):
    """Called at bar index OPEN. Only index-1 and older values are visible."""
    j = index - 1
    if j < 20:
        return False, False
    close, previous_close = rows[j]['close'], rows[j-1]['close']
    middle, previous_middle = features['bb_middle'][j], features['bb_middle'][j-1]
    if strategy == 'trend':
        entry = (previous_close <= previous_middle and close > middle
                 and middle > previous_middle and close < features['bb_upper'][j])
        return entry, close < middle
    if strategy == 'breakout':
        entry = (previous_close <= features['bb_upper'][j-1]
                 and close > features['bb_upper'][j])
        return entry, close < middle
    if strategy == 'reversion':
        entry = (previous_close <= features['bb_lower'][j-1]
                 and close > features['bb_lower'][j] and close < middle)
        return entry, close >= middle
    raise ValueError('Bilinmeyen strateji: ' + strategy)


@dataclass(frozen=True)
class Risk:
    fee: float = 0.001
    slippage: float = 0.0005
    risk_fraction: float = 0.001
    allocation_cap: float = 0.05
    stop_atr: float = 2.0
    reward_risk: float = 2.0

    def validate(self):
        import math
        if not all(math.isfinite(v) for v in asdict(self).values()):
            raise ValueError('Sonlu olmayan risk parametresi.')
        if not (0 <= self.fee < 1 and 0 <= self.slippage < 1
                and 0 < self.risk_fraction <= 1 and 0 < self.allocation_cap <= 1
                and self.stop_atr > 0 and self.reward_risk > 0):
            raise ValueError('Geçersiz risk parametresi.')


def size_position(cash, open_price, atr, risk):
    risk.validate()
    entry = open_price * (1 + risk.slippage)
    distance = risk.stop_atr * atr
    stop = entry - distance
    if distance <= 0 or stop <= 0:
        return None
    unit_cost = entry * (1 + risk.fee)
    planned_loss = unit_cost - stop * (1 - risk.slippage) * (1 - risk.fee)
    quantity = min(cash * risk.risk_fraction / planned_loss,
                   cash * risk.allocation_cap / unit_cost)
    return {'quantity': quantity, 'entry': entry, 'stop': stop,
            'target': entry + distance * risk.reward_risk,
            'cost': quantity * unit_cost}
