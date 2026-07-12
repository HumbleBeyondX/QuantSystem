# Factor Decorrelation — generated 2026-05-04T12:23:18

## Cluster summary (24 clusters at corr threshold)

### Cluster 1 (2 factors)
- `rel_strength_252`
- `drawdown_252`

### Cluster 2 (6 factors)
- `mom_12_1`
- `resid_mom_252`
- `ts_mom_12`
- `sharpe_252`
- `qmj_proxy`
- `convexity_252`

### Cluster 3 (1 factors)
- `momentum_crash_aware`

### Cluster 4 (3 factors)
- `mom_6_1`
- `pure_trend`
- `multi_horizon_mom`

### Cluster 5 (1 factors)
- `mom_3_1`

### Cluster 6 (1 factors)
- `long_tail_skew_252`

### Cluster 7 (1 factors)
- `ltr_36m`

### Cluster 8 (1 factors)
- `skew_252`

### Cluster 9 (4 factors)
- `bab_proxy`
- `beta_squared_252`
- `multi_horizon_vol`
- `balanced_alpha`

### Cluster 10 (5 factors)
- `realized_vol_252`
- `return_stability_252`
- `max_dd_depth_252`
- `tail_var95_252`
- `covar_proxy_252`

### Cluster 11 (8 factors)
- `realized_vol_21`
- `realized_vol_63`
- `downside_vol_63`
- `idio_vol_63`
- `max_ret_21`
- `parkinson_vol_21`
- `garman_klass_21`
- `lottery_max5_21`

### Cluster 12 (1 factors)
- `vol_of_vol_63`

### Cluster 13 (1 factors)
- `kurt_252`

### Cluster 14 (1 factors)
- `jump_freq_252`

### Cluster 15 (2 factors)
- `dollar_volume_21`
- `amihud_illiq_21`

### Cluster 16 (1 factors)
- `volume_price_corr_21`

### Cluster 17 (2 factors)
- `intraday_mom_21`
- `body_to_wick_21`

### Cluster 18 (3 factors)
- `mom_1m_raw`
- `str_1m`
- `obv_slope_21`

### Cluster 19 (1 factors)
- `str_5d`

### Cluster 20 (1 factors)
- `volume_shock_reversal`

### Cluster 21 (1 factors)
- `overnight_gap_21`

### Cluster 22 (1 factors)
- `str_1d`

### Cluster 23 (1 factors)
- `volume_ratio_5_60`

### Cluster 24 (1 factors)
- `return_autocorr_21`

## Pairwise rank-correlation matrix (top entries)

| Rank | Factor A | Factor B | Corr |
|------|----------|----------|------|
| 1 | `mom_1m_raw` | `str_1m` | -1.000 |
| 2 | `rel_strength_252` | `drawdown_252` | -1.000 |
| 3 | `intraday_mom_21` | `body_to_wick_21` | +1.000 |
| 4 | `bab_proxy` | `beta_squared_252` | +1.000 |
| 5 | `resid_mom_252` | `convexity_252` | +0.995 |
| 6 | `parkinson_vol_21` | `garman_klass_21` | +0.991 |
| 7 | `ts_mom_12` | `sharpe_252` | +0.964 |
| 8 | `realized_vol_21` | `lottery_max5_21` | +0.951 |
| 9 | `max_ret_21` | `lottery_max5_21` | +0.944 |
| 10 | `mom_12_1` | `ts_mom_12` | +0.941 |
| 11 | `realized_vol_252` | `tail_var95_252` | +0.939 |
| 12 | `ts_mom_12` | `convexity_252` | +0.935 |
| 13 | `sharpe_252` | `convexity_252` | +0.932 |
| 14 | `mom_12_1` | `sharpe_252` | +0.911 |
| 15 | `mom_12_1` | `convexity_252` | +0.906 |
| 16 | `realized_vol_252` | `return_stability_252` | +0.906 |
| 17 | `realized_vol_21` | `parkinson_vol_21` | +0.903 |
| 18 | `resid_mom_252` | `qmj_proxy` | +0.888 |
| 19 | `realized_vol_63` | `idio_vol_63` | +0.885 |
| 20 | `mom_6_1` | `pure_trend` | +0.885 |
| 21 | `realized_vol_63` | `downside_vol_63` | +0.883 |
| 22 | `realized_vol_21` | `garman_klass_21` | +0.877 |
| 23 | `qmj_proxy` | `convexity_252` | +0.864 |
| 24 | `return_stability_252` | `tail_var95_252` | +0.859 |
| 25 | `parkinson_vol_21` | `lottery_max5_21` | +0.856 |
| 26 | `dollar_volume_21` | `amihud_illiq_21` | +0.847 |
| 27 | `realized_vol_21` | `max_ret_21` | +0.843 |
| 28 | `realized_vol_252` | `covar_proxy_252` | +0.841 |
| 29 | `resid_mom_252` | `ts_mom_12` | +0.833 |
| 30 | `garman_klass_21` | `lottery_max5_21` | +0.833 |