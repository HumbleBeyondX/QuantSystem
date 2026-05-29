# SP500 153-Name Long-Short — 10-Factor Composite

_Period: 2018-2026 (8.3y) · Universe: 153 SP500 names · Monthly rebalance · 5bps fee + 5bps slip per side_

## Alpha construction

Equal-weighted or IC-weighted z-score blend of 10 factors with t-stat > 2 from the SP500 factor catalog:

- `resid_mom_252` (+)  RankIC IR +0.068
- `mom_12_1` (+)  RankIC IR +0.066
- `sharpe_252` (+)  RankIC IR +0.064
- `ts_mom_12` (+)  RankIC IR +0.063
- `momentum_crash_aware` (+)  RankIC IR +0.056
- `ltr_36m` (−)  RankIC IR -0.053
- `multi_horizon_mom` (+)  RankIC IR +0.052
- `mom_6_1` (+)  RankIC IR +0.051
- `qmj_proxy` (+)  RankIC IR +0.048
- `pure_trend` (+)  RankIC IR +0.044

## Portfolio variants
- **L1** — Long-only top-N, equal weight
- **LS** — Long top-N, short bottom-N, dollar-neutral
- **LS-RP** — Same names as LS, inverse-vol within each sleeve

## Results — best variant: **IC_L1_n10** Sharpe +0.94

| Run | Sharpe | Sortino | CAGR | MaxDD | Calmar | Win% | Turn/yr |
|---|---|---|---|---|---|---|---|
| `IC_L1_n10` | +0.94 | +1.24 | +25.33% | 37.0% | +0.69 | 65% | 7.5 |
| `IC_L1_n15` | +0.94 | +1.21 | +23.24% | 35.0% | +0.66 | 69% | 6.6 |
| `IC_L1_n25` | +0.94 | +1.19 | +20.63% | 33.1% | +0.62 | 69% | 5.8 |
| `EW_L1_n10` | +0.92 | +1.21 | +24.60% | 37.0% | +0.67 | 65% | 7.8 |
| `EW_L1_n15` | +0.90 | +1.16 | +21.93% | 35.0% | +0.63 | 69% | 7.1 |
| `EW_L1_n25` | +0.88 | +1.11 | +19.31% | 33.7% | +0.57 | 70% | 6.2 |
| `EW_LS-RP_n10` | +0.23 | +0.29 | +2.35% | 37.0% | +0.06 | 52% | 9.0 |
| `IC_LS-RP_n10` | +0.22 | +0.28 | +2.27% | 39.2% | +0.06 | 52% | 8.7 |
| `IC_LS-RP_n15` | +0.18 | +0.24 | +1.60% | 29.3% | +0.06 | 52% | 8.0 |
| `IC_LS_n10` | +0.14 | +0.18 | +0.99% | 43.1% | +0.02 | 56% | 7.2 |
| `IC_LS-RP_n25` | +0.13 | +0.16 | +0.85% | 22.9% | +0.04 | 54% | 7.1 |
| `EW_LS_n10` | +0.13 | +0.17 | +0.80% | 42.2% | +0.02 | 55% | 7.6 |
| `IC_LS_n25` | +0.12 | +0.15 | +0.70% | 26.2% | +0.03 | 52% | 5.8 |
| `EW_LS-RP_n15` | +0.11 | +0.14 | +0.59% | 32.7% | +0.02 | 52% | 8.2 |
| `IC_LS_n15` | +0.11 | +0.14 | +0.52% | 34.2% | +0.01 | 54% | 6.6 |
| `EW_LS-RP_n25` | +0.07 | +0.08 | +0.13% | 23.7% | +0.01 | 55% | 7.4 |
| `EW_LS_n25` | +0.05 | +0.07 | -0.08% | 27.2% | -0.00 | 55% | 6.2 |
| `EW_LS_n15` | +0.04 | +0.05 | -0.44% | 37.7% | -0.01 | 52% | 6.9 |

## Benchmark

Equal-weight 153-name basket: Sharpe +0.91, CAGR +16.81%, MaxDD 36.0%

## Deploy verdict

- Sharpe ≥ 1.0  → ❌ (+0.94)
- MaxDD ≤ 20% → ❌ (37.0%)
- Calmar ≥ 0.5 → ✅ (+0.69)

**Verdict: CLOSE — iterate one round.** Add risk model / sector neutralization / better factor selection.