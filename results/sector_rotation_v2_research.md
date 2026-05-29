# Sector Rotation v2 — Regime-Gated Backtest

_Period: 2014-12-31 → 2026-05-03 · Universe: 11 sector ETFs · Rebalance: 10 days · Costs: 5bps + 5bps_

## TL;DR

Best variant: **G2_Trend200** (SPY>SMA200) → Sharpe **+0.39**, MaxDD **22.7%**, CAGR **+4.25%** (vs baseline Sharpe +0.61, MaxDD 29.6%).

## All variants

| Variant | Gate | Sharpe | Sortino | CAGR | MaxDD | Calmar | TIM | n trades |
|---|---|---|---|---|---|---|---|---|
| **Baseline** | (always on) | +0.61 | +0.77 | +9.62% | 29.6% | +0.33 | 100% | 609 |
| G1_VIX22 | VIX<22 | +0.21 | +0.24 | +1.89% | 24.6% | +0.08 | 78% | 540 |
| G1_VIX25 | VIX<25 | +0.38 | +0.46 | +4.30% | 17.3% | +0.25 | 86% | 569 |
| G2_Trend200 | SPY>SMA200 | +0.39 | +0.45 | +4.25% | 22.7% | +0.19 | 81% | 540 |
| G3_Both_VIX22 | VIX<22 & SPY>SMA200 | +0.18 | +0.20 | +1.37% | 25.2% | +0.05 | 72% | 507 |
| G3_Both_VIX25 | VIX<25 & SPY>SMA200 | +0.25 | +0.29 | +2.32% | 22.7% | +0.10 | 76% | 518 |

## VIX threshold sweep (VIX gate only, no trend filter)

| VIX max | Sharpe | CAGR | MaxDD | Time in market | n trades |
|---|---|---|---|---|---|
| <18 | +0.17 | +1.20% | 17.4% | 58% | 450 |
| <20 | -0.03 | -1.00% | 30.2% | 67% | 519 |
| <22 | +0.21 | +1.89% | 24.6% | 78% | 540 |
| <25 | +0.38 | +4.30% | 17.3% | 86% | 569 |
| <28 | +0.37 | +4.35% | 27.1% | 90% | 597 |
| <30 | +0.38 | +4.60% | 22.5% | 93% | 607 |

## Interpretation

- **TIM** (Time-In-Market) below 100% means the strategy sat in cash (0% return, no risk) during periods the regime gate was closed.
- A high-Sharpe variant with TIM 60-70% trades real-world risk for real-world drawdown reduction — usable as a tactical overlay.
- A high-Sharpe variant with TIM < 30% is statistical noise from a tiny sample of trading days; don't trust it.

## Deploy-readiness rubric (apply to the best variant)

- Sharpe ≥ 1.0 → ❌ (+0.39)
- MaxDD ≤ 20% → ❌ (22.7%)
- Time-in-market ≥ 40% → ✅ (81%)
- Trade count ≥ 50 → ✅ (540)

**Verdict: ITERATE.** Closer to deploy than v1 (Sharpe 0.14) but doesn't fully clear the bar. Next iteration ideas:
- Add a sector-specific vol gate (don't trade XLE / XLF in tail conditions).
- Tighten 200d trend filter to 50d for faster regime detection.
- Stack with a 2nd alpha source (volatility carry, sector breadth).
