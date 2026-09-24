# Strategy reset research

UTC run: 20260918T165355Z

Previously examined historical data: retrospective diagnostics, not untouched forward proof.
Long/cash spot only; next-open fills, stop-first double touches, costs on both sides.
1000 USD reference equity, 0.25% planned stop risk, 20% allocation cap.

| Candidate | Development net | Selection net | Selection trades | Pass |
|---|---:|---:|---:|---|
| trend_hysteresis_1h_balanced | -30.62% | -15.26% | 268 | False |
| trend_hysteresis_1h_patient | -20.13% | -6.75% | 168 | False |
| channel_breakout_1h_balanced | -19.97% | -12.38% | 178 | False |
| channel_breakout_1h_patient | -11.46% | -7.23% | 113 | False |
| trend_pullback_1h_balanced | -24.33% | -9.11% | 146 | False |
| trend_pullback_1h_patient | -10.48% | -2.69% | 72 | False |
| trend_hysteresis_4h_balanced | 0.07% | 0.75% | 83 | False |
| trend_hysteresis_4h_patient | -0.00% | -0.14% | 68 | False |
| channel_breakout_4h_balanced | 0.55% | -1.20% | 46 | False |
| channel_breakout_4h_patient | 2.09% | -1.81% | 38 | False |
| trend_pullback_4h_balanced | -0.78% | -0.58% | 33 | False |
| trend_pullback_4h_patient | -1.26% | -0.63% | 22 | False |

Result: no_candidate_passed_development_selection

No execution configuration or running worker was modified.
