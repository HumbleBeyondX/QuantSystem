# Sector Rotation 2-Week Rebalance — Research Report

_Backtest period: 2014-12-31 → 2026-05-01 (11.3 years). Rebalance: every 10 trading days (~2 weeks)._

## 1. Strategy specification

**Universe:** XLK, XLF, XLV, XLY, XLP, XLE, XLI, XLU, XLB, XLRE, XLC (11 sector ETFs).

**Alpha signal:** equal-weighted cross-sectional z-score blend of four factors selected for their t-stat > 2 performance on the SP500 universe:

- `mom_6_1` — 6-month momentum, skip last 21d (Carhart-style)
- `multi_horizon_mom` — z-score blend of 3m/6m/12m momentum
- `momentum_crash_aware` — Daniel-Moskowitz crash proxy: 12-1 momentum × sign(market 12m)
- `sharpe_252` — 1-year rolling Sharpe ratio (quality proxy)

**Rebalance cadence:** every 10 trading days.
**Costs:** 5 bps fee + 5 bps slippage per side (10 bps per side, charged on weight turnover at rebalance).

## 2. Backtest results

| Variant | Sharpe | Sortino | CAGR | MaxDD | Calmar | Win-rate | Turnover/yr | Final $ |
|---|---|---|---|---|---|---|---|---|
| A — Top-3 Equal-Weight (Long) | 0.61 | 0.77 | 9.75% | 29.61% | 0.33 | 58.5% | 9.46 | $2.86 |
| B — Long-Short Top-2 / Bottom-2 | -0.07 | -0.10 | -3.30% | 43.76% | -0.08 | 47.9% | 22.37 | $0.68 |
| C — Top-3 Risk Parity (Inverse Vol) | 0.61 | 0.77 | 9.51% | 28.94% | 0.33 | 58.1% | 10.73 | $2.79 |

**Benchmarks:**

| Benchmark | Sharpe | CAGR | MaxDD | Total Return |
|---|---|---|---|---|
| Equal-weight ALL sectors (bench) | 0.74 | 11.52% | 36.34% | 242.89% |
| SPY buy-hold | 0.81 | 13.61% | 33.72% | 323.37% |

## 3. Equity curve description (best variant)

Best variant: **A — Top-3 Equal-Weight (Long)** (Sharpe 0.61).

- Start: $1.00 on 2014-12-31
- Peak equity reached on **2025-10-28**
- Maximum drawdown bottomed at **2020-03-23** with a peak-to-trough loss of **29.61%**
- Final equity: **$2.86** (total return 186.47%, 284 rebalances)

## 4. Regime analysis (best variant)

**By vix_bucket:**

| Regime | n_days | Ann. Return | Sharpe |
|---|---|---|---|
| high_vix | 940 | -22.98% | -0.88 |
| low_vix | 938 | 31.55% | 3.26 |
| mid_vix | 971 | 23.71% | 1.81 |

**By trend_bucket:**

| Regime | n_days | Ann. Return | Sharpe |
|---|---|---|---|
| downtrend | 608 | -15.29% | -0.56 |
| uptrend | 2241 | 17.99% | 1.27 |

**Interpretation:** the strategy performs best in `low_vix` (Sharpe 3.26) and worst in `high_vix` (Sharpe -0.88). This is consistent with the momentum-crash literature — pure momentum suffers in vol spikes; the `momentum_crash_aware` factor partially mitigates this.
- In uptrends (SPY > 200d SMA), Sharpe = 1.27.
- In downtrends, Sharpe = -0.56.

## 5. Comparison vs failed v1 (daily rebalance)

| Version | Rebalance | Sharpe | MaxDD | Total Return | Notes |
|---|---|---|---|---|---|
| v1 (failed) | Daily | 0.14 | 30.97% | 144.08% | GMM regime + factor tilt + MV opt; rejected |
| 2-week / A | 10d | 0.61 | 29.61% | 186.47% | top-3 factor-ranked |
| 2-week / B | 10d | -0.07 | 43.76% | -31.59% | top-2L/2S factor-ranked |
| 2-week / C | 10d | 0.61 | 28.94% | 179.48% | top-3 factor-ranked |

The daily v1 used a much more complex pipeline (regime detection, mean-variance optimization, SPY hedge, signal-tilted expected returns) and rebalanced every 10 bars by design — but with daily-frequency factor refresh and a regime-switching weight scheme that overfit on training data. The 2-week variants here use a far simpler signal (static equal-weighted factor blend) and rank-based portfolio construction, which is less prone to overfitting and easier to interpret.

## 6. When the strategy works / fails

**Works when:**
- Persistent sector trends (tech 2020-21, energy 2022, tech 2023-24) — top-3 captures the run-up.
- Low-to-mid VIX regimes where momentum is not punished by reversals.
- Uptrends where the momentum factor and `momentum_crash_aware` align.

**Fails when:**
- Sharp regime flips (Feb-Mar 2020 COVID, Q4 2018, Q1 2022 rotation) — 10-day lag means the portfolio holds last-quarter's winners through the reversal.
- Sector leadership rotation faster than the rebalance cadence.
- Long-short variant (B) suffers from concentrated negative exposure during broad market rallies (short sleeve gets squeezed).

## 7. Recommendation

**VERDICT: ITERATE** — promising but not deploy-ready.

Best variant **A** delivers Sharpe 0.61 — above the failed v1 (0.14) but below the deploy bar (Sharpe > 1.0). MaxDD 29.61%.

**Iteration ideas:**
1. Add a trend filter: only go long when SPY > 200d SMA (skip downtrends).
2. Increase rebalance lookahead — try 21-day (monthly) cadence to reduce turnover further.
3. Replace equal-weight z-score with regime-conditional weights (bull/bear/sideways), fit on a small held-out training window — but be cautious of overfitting.
4. Add a volatility scalar: scale gross exposure by 1/realized_vol of the portfolio.
5. Test on a larger universe (sector + factor ETFs together — MTUM, QUAL, VLUE, USMV).

## 8. Small-account practicality ($5-15K)

- ETF prices range $30-$200; with $10K capital and 3-position portfolio, average position size ≈ $3,300 → 15-100 shares — practical.
- Bi-weekly rebalance × annual turnover ≈ 9.5 means roughly that fraction of capital traded per year. At 10 bps per side, annual transaction cost drag ≈ 0.95% of equity — manageable.
- ~284 rebalance events over 11.3 years = ~25 per year, easy to execute manually or via paper trading.

---

_Generated by `scripts/backtest_sector_rotation_biweekly.py`._