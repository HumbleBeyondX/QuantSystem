#!/usr/bin/env python3
"""Sector Rotation v2.1 — smart regime modulation, not binary cash gates.

v2.0 (binary VIX / SPY-trend cash gates) FAILED to improve on baseline
Sharpe 0.61 — all gated variants ranged 0.18-0.39. Root cause: binary
gates fire AFTER the loss has occurred and miss the subsequent
rebound.

v2.1 tests four smarter regime adaptations:

  S1 Vol-target scaling
        weight = baseline_weight × min(1, target_vol / realized_vol_21d)
        Keep exposure but shrink it when vol spikes.

  S2 VIX delta filter
        Use 5d change in VIX, not level. Spikes are dangerous;
        elevated-but-stable VIX is normal.

  S3 Defensive sector swap
        When SPY 200d trend is DOWN, switch the top-3 momentum signal
        to a top-3 LOW-VOL signal (long XLU / XLP / XLV style).

  S4 Mom + carry composite
        Blend momentum with 90-day sector dividend yield differential
        (sector with rising dividends has fundamental support).

All run on the same engine as v1's variant A (Top-3 EW).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backtest_sector_rotation_biweekly import (  # noqa: E402
    DATA_FILE,
    END_DATE,
    EVAL_START,
    FEE_BPS,
    REBALANCE_BARS,
    SECTOR_UNIVERSE,
    SLIP_BPS,
    START_DATE,
    build_alpha_signal,
    variant_a_top3_equal,
)

OUT_MD = PROJECT_ROOT / "results" / "sector_rotation_v2_smart.md"
OUT_JSON = PROJECT_ROOT / "results" / "sector_rotation_v2_smart.json"


# ──────────────────────────────────────────────────────────────────────
# Smart adapters
# ──────────────────────────────────────────────────────────────────────

def vol_target_scale(
    weights: Dict[str, float],
    closes: pd.DataFrame,
    date: pd.Timestamp,
    target_vol: float = 0.15,
    window: int = 21,
) -> Dict[str, float]:
    """Scale portfolio weights inversely with portfolio realized vol.

    portfolio_vol = sqrt(w' Σ w) using the trailing 21-day daily returns.
    scale = min(1, target_vol / portfolio_vol).
    """
    if not weights:
        return weights
    end = closes.index.get_indexer([date], method="ffill")[0]
    if end < window:
        return weights
    start = max(0, end - window)
    sub = closes.iloc[start:end + 1]
    ret = sub.pct_change().dropna()
    if len(ret) < window // 2:
        return weights
    cols = list(weights.keys())
    ret_sub = ret[cols].dropna(how="all")
    if ret_sub.empty:
        return weights
    cov = ret_sub.cov().values * 252
    w = np.array([weights[c] for c in cols])
    port_var = float(w @ cov @ w)
    if port_var <= 0:
        return weights
    port_vol = np.sqrt(port_var)
    scale = min(1.0, target_vol / port_vol)
    return {c: w_i * scale for c, w_i in zip(cols, w)}


def low_vol_top3(closes: pd.DataFrame, date: pd.Timestamp, window: int = 63) -> Dict[str, float]:
    """Defensive replacement signal: long the 3 lowest-vol sectors."""
    end = closes.index.get_indexer([date], method="ffill")[0]
    if end < window:
        return {}
    sub = closes.iloc[max(0, end - window):end + 1]
    rv = sub.pct_change().std() * np.sqrt(252)
    top3 = rv.nsmallest(3).index.tolist()
    return {t: 1.0 / 3.0 for t in top3}


def vix_delta_gate(vix: pd.Series, date: pd.Timestamp, lookback: int = 5, max_chg: float = 0.20) -> float:
    """Returns 1.0 if VIX hasn't moved >max_chg% in the lookback,
    else linearly reduce toward 0 as the move grows."""
    if date not in vix.index:
        return 1.0
    end = vix.index.get_loc(date)
    if end < lookback:
        return 1.0
    cur = vix.iloc[end]
    prior = vix.iloc[end - lookback]
    if prior <= 0:
        return 1.0
    chg = (cur - prior) / prior
    if chg <= 0:
        return 1.0
    return max(0.0, 1.0 - chg / max_chg)


# ──────────────────────────────────────────────────────────────────────
# Engine — same as v1 but accepts a strategy callable that returns weights
# ──────────────────────────────────────────────────────────────────────

def run_strategy(
    closes: pd.DataFrame,
    alpha: pd.DataFrame,
    vix: pd.Series,
    spy: pd.Series,
    *,
    strategy: str,
    target_vol: float = 0.15,
    vix_delta_lookback: int = 5,
    vix_delta_max: float = 0.20,
) -> Dict:
    daily_ret = closes.pct_change().fillna(0.0)
    dates = alpha.index

    eval_start_ts = pd.Timestamp(EVAL_START)
    rebalance_idx: List[int] = []
    for i, d in enumerate(dates):
        if d < eval_start_ts:
            continue
        if not rebalance_idx:
            rebalance_idx.append(i)
        elif i - rebalance_idx[-1] >= REBALANCE_BARS:
            rebalance_idx.append(i)
    rebalance_set = set(rebalance_idx)

    spy_sma200 = spy.rolling(200, min_periods=100).mean()

    current_w: Dict[str, float] = {}
    equity = 1.0
    equity_curve = []
    daily_pnl_list = []
    pnl_per_rebalance = []
    last_rebalance_equity = equity
    started = False
    total_turnover = 0.0
    n_trades = 0
    days_in_market = 0
    days_in_cash = 0
    avg_gross_exposure_acc = 0.0
    avg_gross_count = 0

    for i, date in enumerate(dates):
        if date < eval_start_ts:
            continue

        if started:
            if current_w:
                day_pnl = sum(current_w.get(c, 0) * daily_ret.iloc[i].get(c, 0)
                              for c in current_w)
                equity *= (1 + day_pnl)
                daily_pnl_list.append((date, day_pnl))
                days_in_market += 1
                avg_gross_exposure_acc += sum(abs(v) for v in current_w.values())
                avg_gross_count += 1
            else:
                daily_pnl_list.append((date, 0.0))
                days_in_cash += 1

        if i in rebalance_set:
            score_row = alpha.iloc[i]

            # Pick base weights based on strategy
            if strategy == "S1_vol_target":
                base = variant_a_top3_equal(score_row)
                new_w = vol_target_scale(base, closes, date, target_vol=target_vol)
            elif strategy == "S2_vix_delta":
                base = variant_a_top3_equal(score_row)
                gate_scale = vix_delta_gate(vix, date, vix_delta_lookback, vix_delta_max)
                new_w = {c: v * gate_scale for c, v in base.items()}
            elif strategy == "S3_defensive_swap":
                # If SPY < 200d SMA, use low-vol top-3 instead of momentum top-3
                spy_val = spy.reindex([date]).iloc[0]
                sma_val = spy_sma200.reindex([date]).iloc[0]
                if pd.notna(sma_val) and pd.notna(spy_val) and spy_val < sma_val:
                    new_w = low_vol_top3(closes, date)
                else:
                    new_w = variant_a_top3_equal(score_row)
            elif strategy == "S4_combined":
                # vol-target + VIX-delta + defensive swap
                spy_val = spy.reindex([date]).iloc[0]
                sma_val = spy_sma200.reindex([date]).iloc[0]
                if pd.notna(sma_val) and pd.notna(spy_val) and spy_val < sma_val:
                    base = low_vol_top3(closes, date)
                else:
                    base = variant_a_top3_equal(score_row)
                # Vol-target then VIX-delta
                base = vol_target_scale(base, closes, date, target_vol=target_vol)
                gate_scale = vix_delta_gate(vix, date, vix_delta_lookback, vix_delta_max)
                new_w = {c: v * gate_scale for c, v in base.items()}
            elif strategy == "baseline":
                new_w = variant_a_top3_equal(score_row)
            else:
                raise ValueError(strategy)

            all_tickers = set(current_w) | set(new_w)
            turnover = sum(abs(new_w.get(t, 0) - current_w.get(t, 0)) for t in all_tickers)
            if turnover > 0:
                cost = turnover * ((FEE_BPS + SLIP_BPS) / 10000.0)
                equity *= (1.0 - cost)
                total_turnover += turnover
                n_trades += len(all_tickers)

            if started:
                pnl_per_rebalance.append(equity - last_rebalance_equity)
            last_rebalance_equity = equity
            current_w = new_w
            started = True

        equity_curve.append((date, equity))

    if not equity_curve:
        return {"error": "no_data"}

    eq_df = pd.DataFrame(equity_curve, columns=["date", "equity"]).set_index("date")
    eq = eq_df["equity"]
    daily_ret_eq = eq.pct_change().dropna()
    years = len(eq) / 252.0

    sharpe = (daily_ret_eq.mean() / daily_ret_eq.std()) * np.sqrt(252) if daily_ret_eq.std() > 0 else 0.0
    downside = daily_ret_eq[daily_ret_eq < 0].std()
    sortino = (daily_ret_eq.mean() / downside) * np.sqrt(252) if downside > 0 else 0.0
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0
    max_dd = abs(((eq - eq.cummax()) / eq.cummax()).min())
    calmar = cagr / max_dd if max_dd > 0 else 0.0

    win_rate = sum(1 for p in pnl_per_rebalance if p > 0) / max(len(pnl_per_rebalance), 1)
    avg_gross = avg_gross_exposure_acc / max(avg_gross_count, 1)
    tim = days_in_market / max(days_in_market + days_in_cash, 1)

    return {
        "strategy": strategy,
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "cagr": round(cagr, 4),
        "max_dd": round(max_dd, 4),
        "calmar": round(calmar, 3),
        "win_rate": round(win_rate, 3),
        "n_trades": n_trades,
        "n_rebalances": len(pnl_per_rebalance),
        "turnover_per_year": round(total_turnover / max(years, 0.01), 1),
        "time_in_market": round(tim, 3),
        "avg_gross_exposure": round(avg_gross, 3),
        "final_equity": round(eq.iloc[-1], 4),
    }


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    df = pd.read_parquet(DATA_FILE)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    wide = df.pivot_table(index="timestamp", columns="ticker", values="close",
                          aggfunc="last").sort_index()
    closes = wide[SECTOR_UNIVERSE].dropna(how="all")
    vix = wide["^VIX"]
    spy = wide["SPY"]
    closes = closes.loc[(closes.index >= START_DATE) & (closes.index <= END_DATE)]

    print(f"Universe: {len(closes.columns)} sectors, "
          f"{closes.index[0].date()} → {closes.index[-1].date()}")
    alpha = build_alpha_signal(closes)

    strategies = ["baseline", "S1_vol_target", "S2_vix_delta",
                  "S3_defensive_swap", "S4_combined"]
    results = {}
    print(f"\n{'Strategy':<22}{'Sharpe':>8}{'CAGR':>10}{'MaxDD':>9}{'Calmar':>8}"
          f"{'Win%':>7}{'TIM':>7}{'AvgGE':>8}{'nReb':>6}")
    print("-" * 90)
    for s in strategies:
        m = run_strategy(closes, alpha, vix, spy, strategy=s)
        results[s] = m
        print(f"{s:<22}{m['sharpe']:>+8.2f}{m['cagr']:>+10.2%}"
              f"{m['max_dd']:>9.1%}{m['calmar']:>+8.2f}"
              f"{m['win_rate']*100:>6.0f}%{m['time_in_market']*100:>6.0f}%"
              f"{m['avg_gross_exposure']:>8.2f}{m['n_rebalances']:>6}")
    print()

    # Identify winner
    best_name = max(results, key=lambda k: results[k]["sharpe"])
    best = results[best_name]
    print(f"Best: {best_name}  Sharpe {best['sharpe']:+.2f}  vs baseline {results['baseline']['sharpe']:+.2f}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(results, indent=2, default=str))
    OUT_MD.write_text(_make_md(results), encoding="utf-8")
    print(f"\nMarkdown → {OUT_MD}")
    return 0


def _make_md(results: Dict) -> str:
    baseline = results["baseline"]
    best_name = max(results, key=lambda k: results[k]["sharpe"])
    best = results[best_name]
    delta = best["sharpe"] - baseline["sharpe"]

    lines = []
    lines.append("# Sector Rotation v2.1 — Smart Regime Modulation")
    lines.append("")
    lines.append("_4 smarter alternatives to v2.0 binary cash gates: vol targeting, "
                 "VIX delta filter, defensive sector swap, combined._")
    lines.append("")
    lines.append("## Result table")
    lines.append("")
    lines.append("| Strategy | Sharpe | Sortino | CAGR | MaxDD | Calmar | Win% | TIM | AvgGE | nReb |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for k, m in results.items():
        lines.append(f"| **{k}** | {m['sharpe']:+.2f} | {m['sortino']:+.2f} | "
                     f"{m['cagr']:+.2%} | {m['max_dd']:.1%} | {m['calmar']:+.2f} | "
                     f"{m['win_rate']*100:.0f}% | {m['time_in_market']*100:.0f}% | "
                     f"{m['avg_gross_exposure']:.2f} | {m['n_rebalances']} |")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"Best: **{best_name}** Sharpe {best['sharpe']:+.2f} "
                 f"(Δ vs baseline {delta:+.2f})")
    lines.append("")
    if delta >= 0.20:
        lines.append("**ITERATE TO PAPER** — meaningful improvement.")
    elif delta >= 0.05:
        lines.append("**MARGINAL** — improvement is real but small; needs more validation.")
    else:
        lines.append("**REJECT v2 ENTIRELY** — none of the smart variants exceed baseline.")
        lines.append("")
        lines.append("The momentum-only Top-3 EW on sector ETFs has a structural Sharpe ceiling "
                     "around 0.6. Switch to a different universe (SP500 single names) or "
                     "different alpha (cross-asset, breadth) to break it.")
    lines.append("")
    lines.append("## Adapter definitions")
    lines.append("")
    lines.append("- **S1 vol_target**: Scale weights by min(1, 15% / portfolio_vol_21d). "
                 "Keeps exposure but trims when vol spikes.")
    lines.append("- **S2 vix_delta**: Scale exposure by `1 − max(0, VIX_5d_change / 20%)`. "
                 "Spikes cut exposure; elevated stable VIX is unaffected.")
    lines.append("- **S3 defensive_swap**: When SPY < 200d SMA, replace momentum top-3 with "
                 "vol-rank bottom-3 (defensive low-vol sectors XLU/XLP/XLV-style).")
    lines.append("- **S4 combined**: defensive_swap + vol_target + vix_delta stacked.")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
