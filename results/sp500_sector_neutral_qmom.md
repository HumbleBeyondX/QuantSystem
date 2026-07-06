# SP500 Sector-Neutral Quality+Momentum

_Long top-2 / short bottom-2 WITHIN each GICS sector. Dollar-neutral per sector. 10 sectors covered. Monthly rebalance, 5+5 bps costs. Alpha = z-score blend of mom_12_1 + sharpe_252 + resid_mom_252 + qmj_proxy._

## Results

| Period | Sharpe | Sortino | CAGR | MaxDD | Calmar | Win% | nReb |
|---|---|---|---|---|---|---|---|
| **Full sample 2018-2026** | -0.04 | -0.06 | -0.61% | 25.8% | -0.02 | 50% | 99 |
| **In-Sample 2018-2021** | -0.05 | -0.06 | -0.68% | 17.5% | -0.04 | 57% | 47 |
| **Out-of-Sample 2022-2026** | -0.01 | -0.02 | -0.32% | 13.1% | -0.03 | 45% | 51 |

## Walk-forward gate

- User rule: OOS Sharpe ≥ 1.5
- OOS Sharpe: **-0.01**
- Result: **❌ REJECT**

## Diagnosis

- IS Sharpe -0.05, OOS Sharpe -0.01
- Walk-forward degradation: -74% of IS Sharpe lost

Sector neutralization removes the bull-market beta exposure that made the LS strategy fail at 0.04 — but US mega-cap dispersion within sectors is also low (e.g. AAPL vs MSFT both rip). The factor stack here may not have enough cross-sectional signal after removing sector betas.

Next iterations:
- Expand universe to mid-caps (Russell 1000) for more dispersion
- Add price-action factors (volume, intraday range)
- Test on a subset of high-dispersion sectors only (Energy, Health)