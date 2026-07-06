# TSMOM Diversified — Research Note

Implementation of *Time Series Momentum* (Moskowitz, Ooi, Pedersen, **JFE 2012**) on
a 10-ETF cross-asset universe.

## 1. Specification

| Parameter            | Value                                                       |
|----------------------|-------------------------------------------------------------|
| Signal               | sign(close[t] / close[t-252] - 1)                           |
| Sizing               | (target_vol / realized_vol_60d) * signal, per asset         |
| Target vol per leg   | 10% annual                                                  |
| Aggregation          | Equal-weight (1/N) across the 10 legs                       |
| Rebalance            | Every 21 trading days (~monthly)                            |
| Costs                | 5 bps fee + 5 bps slippage per side, per leg                |
| Universe             | SPY EFA EEM TLT IEF GLD USO DBC UUP FXE                     |
| IS window            | 2015-01-01 → 2021-12-31                                       |
| OOS window           | 2022-01-01 → 2026-05-04                                     |
| Gate                 | OOS Sharpe ≥ 1.5                              |

## 2. Aggregate portfolio performance (net of costs)

| Block | Sharpe | Ann. return | Ann. vol | Max DD |
|-------|--------|-------------|----------|--------|
| IS  (2015-01-01..2021-12-31)   | 0.07 | +0.22% | 4.97% | -11.92% |
| OOS (2022-01-01..2026-05-04) | 1.01 | +4.74% | 4.72% | -7.74% |
| Full | 0.41 | +1.92% | 4.88% | -11.92% |

Gross (no-cost) comparison: IS gross Sharpe **0.14**, OOS gross
Sharpe **1.08** — i.e. costs cost ~
0.07 Sharpe IS and ~
0.07 OOS.

## 3. SPY buy-and-hold benchmark

| Block | Sharpe | Ann. return | Ann. vol | Max DD |
|-------|--------|-------------|----------|--------|
| IS  | 0.87 | +14.84% | 17.67% | -33.72% |
| OOS | 0.71 | +11.62% | 17.73% | -24.50% |
| Full | 0.81 | +13.59% | 17.69% | -33.72% |

## 4. Per-asset (vol-targeted standalone leg, net)

| Ticker | Description | Sharpe (full) | Sharpe IS | Sharpe OOS | Ann. ret | Ann. vol |
|--------|-------------|---------------|-----------|------------|----------|----------|
| SPY | US large-cap equity | 0.63 | 0.66 | 0.58 | 7.1% | 12.1% |
| EFA | DM ex-US equity | 0.04 | -0.16 | 0.38 | -0.2% | 11.7% |
| EEM | EM equity | 0.38 | 0.21 | 0.67 | 3.7% | 11.3% |
| TLT | 20+ year US Treasuries | -0.22 | -0.26 | -0.13 | -2.8% | 10.6% |
| IEF | 7-10 year US Treasuries | 0.10 | -0.15 | 0.54 | 0.5% | 10.7% |
| GLD | Gold | 0.20 | -0.40 | 1.14 | 1.6% | 10.9% |
| USO | WTI crude oil | 0.12 | 0.05 | 0.23 | 0.7% | 11.8% |
| DBC | Broad commodities | 0.25 | 0.21 | 0.30 | 2.1% | 10.9% |
| UUP | USD bullish | 0.26 | 0.11 | 0.53 | 2.3% | 11.1% |
| FXE | EUR vs USD | -0.02 | -0.08 | 0.08 | -0.8% | 10.5% |

Best OOS asset: **GLD** (1.14).
Worst OOS asset: **TLT** (-0.13).

## 5. Walk-forward equity curve

Portfolio equity starts at 1.00 on 2013-01-02, reaches 1.26x by 2026-05-01. Peak (1.26x) on 2026-04-30; subsequent trough 1.26x on 2026-05-01.

OOS - IS Sharpe degradation: **+1370.3%**.

## 6. Comparison vs Moskowitz et al. original

Paper Sharpe (1985-2009, ~58 futures): ~1.5-1.8 net of costs.
This implementation OOS Sharpe (2022-2026, 10 ETFs): **1.01**.

## 7. GATE CHECK

OOS Sharpe = **1.01**, threshold = 1.5.

**Verdict: REJECT**

### Diagnosis (why OOS Sharpe missed 1.5)

- Moskowitz et al's original 1985-2009 sample used >50 futures across equity index, FX, fixed income and commodities. Our 10-ETF proxy is a small, equity-heavy subset and loses the diversification benefit that drives the headline 1.5+ Sharpe.
- ETF wrappers (especially USO, UUP, DBC) suffer roll yield, expense ratios and tracking drag that futures don't.
- 2022-2026 OOS includes the 2022 bond+commodity whipsaw and the 2023 banking shock, both of which produce regime breaks that hurt 12-month trend signals.
- Vol-targeting at 10% per leg + 1/N aggregation delivers only ~3% portfolio vol, so the Sharpe numerator is small in absolute return terms even when the signal works.
- Monthly rebalance with 12-month look-back is slow; trend-followers like AQR/Man AHL now use multi-horizon ensembles (1m+3m+12m) which substantially raise Sharpe.

