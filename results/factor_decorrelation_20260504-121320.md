# Factor Decorrelation — generated 2026-05-04T12:13:20

## Cluster summary (25 clusters at corr threshold)

### Cluster 1 (2 factors)
- `intraday_mom_21`
- `body_to_wick_21`

### Cluster 2 (3 factors)
- `mom_1m_raw`
- `str_1m`
- `obv_slope_21`

### Cluster 3 (1 factors)
- `overnight_gap_21`

### Cluster 4 (1 factors)
- `str_5d`

### Cluster 5 (1 factors)
- `volume_shock_reversal`

### Cluster 6 (1 factors)
- `str_1d`

### Cluster 7 (1 factors)
- `volume_ratio_5_60`

### Cluster 8 (2 factors)
- `dollar_volume_21`
- `amihud_illiq_21`

### Cluster 9 (1 factors)
- `volume_price_corr_21`

### Cluster 10 (1 factors)
- `kurt_252`

### Cluster 11 (1 factors)
- `jump_freq_252`

### Cluster 12 (1 factors)
- `skew_252`

### Cluster 13 (4 factors)
- `bab_proxy`
- `beta_squared_252`
- `multi_horizon_vol`
- `balanced_alpha`

### Cluster 14 (2 factors)
- `rel_strength_252`
- `drawdown_252`

### Cluster 15 (5 factors)
- `realized_vol_252`
- `return_stability_252`
- `max_dd_depth_252`
- `tail_var95_252`
- `covar_proxy_252`

### Cluster 16 (7 factors)
- `realized_vol_21`
- `realized_vol_63`
- `downside_vol_63`
- `max_ret_21`
- `parkinson_vol_21`
- `garman_klass_21`
- `lottery_max5_21`

### Cluster 17 (1 factors)
- `idio_vol_63`

### Cluster 18 (1 factors)
- `vol_of_vol_63`

### Cluster 19 (1 factors)
- `long_tail_skew_252`

### Cluster 20 (4 factors)
- `mom_12_1`
- `ts_mom_12`
- `sharpe_252`
- `momentum_crash_aware`

### Cluster 21 (4 factors)
- `resid_mom_252`
- `qmj_proxy`
- `convexity_252`
- `multi_horizon_mom`

### Cluster 22 (2 factors)
- `mom_6_1`
- `pure_trend`

### Cluster 23 (1 factors)
- `mom_3_1`

### Cluster 24 (1 factors)
- `ltr_36m`

### Cluster 25 (1 factors)
- `return_autocorr_21`

## Pairwise rank-correlation matrix (top entries)

| Rank | Factor A | Factor B | Corr |
|------|----------|----------|------|
| 1 | `mom_1m_raw` | `str_1m` | -1.000 |
| 2 | `rel_strength_252` | `drawdown_252` | -1.000 |
| 3 | `intraday_mom_21` | `body_to_wick_21` | +1.000 |
| 4 | `bab_proxy` | `beta_squared_252` | +1.000 |
| 5 | `resid_mom_252` | `convexity_252` | +0.997 |
| 6 | `parkinson_vol_21` | `garman_klass_21` | +0.992 |
| 7 | `realized_vol_252` | `tail_var95_252` | +0.964 |
| 8 | `max_ret_21` | `lottery_max5_21` | +0.960 |
| 9 | `realized_vol_21` | `lottery_max5_21` | +0.952 |
| 10 | `mom_12_1` | `ts_mom_12` | +0.934 |
| 11 | `realized_vol_63` | `downside_vol_63` | +0.932 |
| 12 | `ts_mom_12` | `sharpe_252` | +0.932 |
| 13 | `realized_vol_252` | `return_stability_252` | +0.930 |
| 14 | `realized_vol_252` | `covar_proxy_252` | +0.924 |
| 15 | `realized_vol_21` | `parkinson_vol_21` | +0.910 |
| 16 | `return_stability_252` | `tail_var95_252` | +0.909 |
| 17 | `dollar_volume_21` | `amihud_illiq_21` | +0.905 |
| 18 | `return_stability_252` | `max_dd_depth_252` | +0.900 |
| 19 | `qmj_proxy` | `convexity_252` | +0.898 |
| 20 | `tail_var95_252` | `covar_proxy_252` | +0.892 |
| 21 | `resid_mom_252` | `qmj_proxy` | +0.885 |
| 22 | `max_dd_depth_252` | `covar_proxy_252` | +0.884 |
| 23 | `realized_vol_21` | `garman_klass_21` | +0.883 |
| 24 | `mom_12_1` | `sharpe_252` | +0.877 |
| 25 | `realized_vol_21` | `max_ret_21` | +0.873 |
| 26 | `mom_6_1` | `pure_trend` | +0.873 |
| 27 | `realized_vol_252` | `max_dd_depth_252` | +0.865 |
| 28 | `parkinson_vol_21` | `lottery_max5_21` | +0.862 |
| 29 | `sharpe_252` | `convexity_252` | +0.858 |
| 30 | `max_dd_depth_252` | `tail_var95_252` | +0.850 |