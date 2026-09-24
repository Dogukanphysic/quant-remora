# BTC Futures loss review — 2026-09-23

## Observed live result

The reviewed completed mainnet round trips had negative account-wallet changes
before the entry gate migration. Actual account amounts are omitted from this
public copy; the original is retained locally under ignored `state/private-reports/`.
Wallet changes are proxies and have not been reconciled with individual fills,
commissions, funding or external transfers. They are not verified net trade PnL.

## Signal weakness found in the review

The former reclaim rule accepted `close > previous close OR RSI <= 45`.  A low
RSI is evidence of weakness, not proof of a reversal.  The `OR` branch therefore
allowed long entries while price was still falling.  The mainnet worker is
long-only, so it cannot benefit from the downtrend that produces those signals.

## Validation result

The Binance USD-M BTCUSDT 15-minute five-year file was split chronologically:
the last year was held out from development.  Simulations used next-bar entry,
conservative same-bar stop precedence, an estimated `0.10%` round-trip execution
cost, a four-bar cooldown and minimum sample requirements.

- Lower-band reclaim search: no tested trend, momentum, RSI, stop, target and
  time-exit combination was net positive in both development and holdout.
- Trend breakout/pullback search: no tested Bollinger breakout, Donchian
  breakout, middle-band reclaim, EMA20 pullback or EMA50 pullback combination
  was net positive in both development and holdout.

This is evidence against enabling another hand-tuned live entry immediately. It
is not proof that no profitable BTC strategy exists.

## Applied control

- Oversold is no longer treated as a reversal. A candidate reclaim requires a
  bullish close, rising MACD histogram and positive EMA50/EMA200 regime.
- Mainnet new entries are blocked by
  `blocked_no_robust_out_of_sample_edge` while the active position continues to
  use its existing exchange-side stop and target.
- A completed trade starts a four-candle cooldown.
- Future close records include the filled protection type when Binance returns
  it, so stop and target outcomes can be separated.

The gate must only be changed after a candidate is positive after costs in an
untouched chronological holdout and passes execution-level paper/testnet checks.
