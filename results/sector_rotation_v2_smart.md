# Sector Rotation v2.1 — Smart Regime Modulation

_4 smarter alternatives to v2.0 binary cash gates: vol targeting, VIX delta filter, defensive sector swap, combined._

## Result table

| Strategy | Sharpe | Sortino | CAGR | MaxDD | Calmar | Win% | TIM | AvgGE | nReb |
|---|---|---|---|---|---|---|---|---|---|
| **baseline** | +0.61 | +0.77 | +9.62% | 29.6% | +0.33 | 58% | 100% | 1.00 | 284 |
| **S1_vol_target** | +0.53 | +0.68 | +6.89% | 18.8% | +0.37 | 58% | 100% | 0.92 | 284 |
| **S2_vix_delta** | +0.52 | +0.61 | +6.05% | 24.0% | +0.25 | 49% | 100% | 0.74 | 284 |
| **S3_defensive_swap** | +0.55 | +0.69 | +8.01% | 30.1% | +0.27 | 58% | 100% | 1.00 | 284 |
| **S4_combined** | +0.35 | +0.41 | +3.37% | 22.9% | +0.15 | 50% | 100% | 0.69 | 284 |

## Verdict

Best: **baseline** Sharpe +0.61 (Δ vs baseline +0.00)

**REJECT v2 ENTIRELY** — none of the smart variants exceed baseline.

The momentum-only Top-3 EW on sector ETFs has a structural Sharpe ceiling around 0.6. Switch to a different universe (SP500 single names) or different alpha (cross-asset, breadth) to break it.

## Adapter definitions

- **S1 vol_target**: Scale weights by min(1, 15% / portfolio_vol_21d). Keeps exposure but trims when vol spikes.
- **S2 vix_delta**: Scale exposure by `1 − max(0, VIX_5d_change / 20%)`. Spikes cut exposure; elevated stable VIX is unaffected.
- **S3 defensive_swap**: When SPY < 200d SMA, replace momentum top-3 with vol-rank bottom-3 (defensive low-vol sectors XLU/XLP/XLV-style).
- **S4 combined**: defensive_swap + vol_target + vix_delta stacked.