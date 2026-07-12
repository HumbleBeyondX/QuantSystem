# Cross-Sectional Short-Term Reversal — Lehmann (1990) / Lo-Mamaysky-Wang (2002)

_Universe: SP500 153 names · Period: 2015-01 → 2026-05 · IS 2015-01-01→2021-12-31 · OOS 2022-01-01→2026-05-03_

## Strategy

- Signal: 5-day cumulative log return (PRIOR week)
- LONG bottom-20 (biggest losers), SHORT top-20 (biggest winners)
- Equal-weight per side, dollar-neutral (gross 1.0, net 0.0)
- Rebalance: every 5 trading days
- Liquidity filter: 20d avg $-vol >= $50M
- **V1** = naive Lehmann reversal (liquidity filter only)
- **V2** = quality-filtered: only tickers with 252d Sharpe > XS median

## Headline (net of costs)

| Run | IS Sharpe | OOS Sharpe | OOS CAGR | OOS MaxDD | Turn/yr | Cost drag (Shp, OOS) |
|---|---|---|---|---|---|---|
| `V1_5bps` | +0.38 | -0.27 | -3.45% | 22.2% | 87.6 | +0.39 |
| `V1_3bps` | +0.54 | -0.11 | -1.79% | 17.6% | 87.6 | +0.23 |
| `V1_10bps` | -0.03 | -0.66 | -7.47% | 32.7% | 87.6 | +0.78 |
| `V2_5bps` | -0.56 | -0.49 | -4.59% | 18.1% | 77.7 | +0.43 |
| `V2_3bps` | -0.36 | -0.32 | -3.14% | 12.6% | 77.7 | +0.26 |

## Gross vs Net (cost drag is the #1 killer)

| Run | Full Gross Shp | Full Net Shp | OOS Gross Shp | OOS Net Shp |
|---|---|---|---|---|
| `V1_5bps` | +0.53 | +0.12 | +0.12 | -0.27 |
| `V1_3bps` | +0.53 | +0.28 | +0.12 | -0.11 |
| `V1_10bps` | +0.53 | -0.28 | +0.12 | -0.66 |
| `V2_5bps` | -0.06 | -0.52 | -0.06 | -0.49 |
| `V2_3bps` | -0.06 | -0.34 | -0.06 | -0.32 |

## Sleeve hit rates & sleeve ann return (OOS, net)

| Run | Long hit% | Short hit% | Long Ann | Short Ann |
|---|---|---|---|---|
| `V1_5bps` | 54.1% | 46.9% | +9.97% | -8.63% |
| `V1_3bps` | 54.1% | 46.9% | +9.97% | -8.63% |
| `V1_10bps` | 54.1% | 46.9% | +9.97% | -8.63% |
| `V2_5bps` | 53.9% | 45.7% | +8.17% | -8.69% |
| `V2_3bps` | 53.9% | 45.7% | +8.17% | -8.69% |

## Liquidity quintile breakdown (V1 5bps full sample)

Q1=lowest avg $-vol, Q5=highest. Each quintile runs its own self-contained long-loser / short-winner sleeve (scale-invariant).

| Quintile | Sharpe | CAGR | MaxDD | Days |
|---|---|---|---|---|
| Q1_lowliq | -0.06 | -1.13% | 36.9% | 2828 |
| Q2_mid | +0.19 | +1.41% | 25.0% | 2828 |
| Q3_mid | +0.32 | +2.67% | 21.0% | 2828 |
| Q4_mid | -0.02 | -0.69% | 25.8% | 2828 |
| Q5_highliq | -0.08 | -1.69% | 39.5% | 2828 |

## Cost sensitivity (V1)

| BPS / side | OOS Sharpe | OOS CAGR | Turn/yr |
|---|---|---|---|
| 3 | -0.11 | -1.79% | 87.6 |
| 5 | -0.27 | -3.45% | 87.6 |
| 10 | -0.66 | -7.47% | 87.6 |

## Walk-forward degradation

- `V1_5bps` IS Sharpe +0.38 → OOS Sharpe -0.27 (Δ = -0.65)
- `V2_5bps` IS Sharpe -0.56 → OOS Sharpe -0.49 (Δ = +0.07)

## Gate decision (user's hard rule: OOS Sharpe net ≥ 1.5)

- V1 OOS Sharpe (net): **-0.27**
- V2 OOS Sharpe (net): **-0.49**
- Best OOS: **-0.27**
- Threshold: **1.50**
- **Decision: REJECT**

## Compared to prior small-cap MR strategy

- Prior small-cap mean-reversion: IS Sharpe **+2.00**, OOS Sharpe **-0.13** (catastrophic walk-forward failure — overfit to 2024-2026 small-cap dynamics).
- This systematic XS reversal V1: IS +0.38 → OOS -0.27 (Δ -0.65)
- V2 quality-filtered: IS -0.56 → OOS -0.49 (Δ +0.07)

## Caveats

- Survivorship bias: panel uses CURRENT SP500 constituents back to 2015, so failed/delisted names are absent. Cross-sectional reversal is somewhat robust to this (works within liquid mega-caps), but real OOS likely a touch worse.
- Single-side spread cost = bps_per_side; round-trip = 2× that. We charge `turnover × cost` at each rebalance, which captures both sides.
- 5-day signal uses today's close to set tomorrow's weights — there is no look-ahead since the signal is `close.shift(0) / close.shift(5)` and PnL is from `daily_ret.iloc[i+1]` style sequencing (weights set at end-of-day i, PnL realized day i+1 onward).
- No intraday execution / no MOC / no impact model — pure close-to-close.
- 5bps is **light** for short sleeves of low-liquidity small caps but realistic for liquid SP500 mega-caps with prime broker pricing.
- Borrowing cost (locate fee) for shorts is NOT charged; for mega-caps typically 0-25 bps annualized, would reduce OOS Sharpe by ~0.05-0.15.