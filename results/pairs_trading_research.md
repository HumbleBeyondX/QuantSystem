# Cointegration Pairs Trading (Gatev/GGR 2006 + Avellaneda-Lee 2010)

## Verdict: **REJECT**  (OOS Sharpe = -0.30, gate ≥ 1.5)

## Setup
- Universe: SP500 mega-caps, top 80 by trailing 252d dollar volume.
- Pair selection: ADF on OLS residual log-spread (p<0.05), 30 pairs.
- Refresh: every 63 trading days.
- z-score window: 60 bars. Entry |z|>2.0, exit |z|<0.5, stop |z|>4.0, time stop 30 bars.
- Costs: 5bps + 5bps per side per leg.
- Gross cap: 100% of equity, equal-weight across 30 pair slots.

## Aggregate Results

| Period | Sharpe | CAGR | MaxDD | Trades | WinRate | AvgPairsActive |
|--------|-------:|-----:|------:|-------:|--------:|---------------:|
| IS  (2017-2021) | -0.49 | -1.50% | -12.6% | 889 | 54.7% | 28.9 |
| OOS (2022-2026) | -0.30 | -0.76% | -6.4% | 665 | 54.4% | 24.8 |

## Pair Selection Health (OOS)
- Quarterly refresh count: 18
- Avg pairs identified per refresh: 24.5 / 30
- Avg pair half-life: 9.2 bars

## Top 5 OOS Pairs (by total P&L)
  - MSFT-NOW        P&L= +0.73%  n=  6  win=100.0%
  - WFC-AXP         P&L= +0.57%  n=  9  win= 88.9%
  - GOOGL-NOW       P&L= +0.57%  n=  5  win=100.0%
  - AMZN-META       P&L= +0.56%  n=  6  win= 66.7%
  - MA-BKNG         P&L= +0.52%  n=  7  win= 85.7%

## Worst 5 OOS Pairs (broken cointegrations)
  - NVDA-ADBE       P&L= -1.38%  n=  2  win=  0.0%
  - INTC-TXN        P&L= -0.96%  n=  5  win= 20.0%
  - ADBE-AMAT       P&L= -0.76%  n=  2  win=  0.0%
  - TMO-LRCX        P&L= -0.69%  n=  3  win= 33.3%
  - MU-TXN          P&L= -0.63%  n=  2  win= 50.0%


## Diagnosis (REJECTED)

- OOS Sharpe (-0.30) fell short of the 1.5 hurdle.
- Per-roundtrip cost drag ≈ 0.20% of notional.
- With 665 trades over the OOS window, gross-of-cost edge is being eaten by the 5+5bps assumption.
- Likely causes: (a) cointegration regime breaks in OOS (many pairs were 'broken' by the 2022 rate shock / 2023 AI bubble), (b) z-score crossings cluster post-cost — high turnover, low edge, (c) hedge ratios fitted on 252d are stale during regime change.
- Mitigations to try next iteration: Kalman-filtered hedge ratios, sector-constrained pair pool, longer z-window (90-120d), entry threshold |z|>2.5, and a wider blowout stop (5-6 sigma) to avoid stopping at the worst time.

