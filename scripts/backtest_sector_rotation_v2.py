#!/usr/bin/env python3
"""Sector Rotation v2 — adds VIX + SPY-200d regime gates to the v1 bi-weekly
Top-3 long strategy.

The v1 (in backtest_sector_rotation_biweekly.py) showed Sharpe 0.61 on the
full 11.3-year sample. Regime analysis revealed a striking edge in low-VIX
uptrending regimes (Sharpe 3.26 / 1.27) and a clear failure mode in high-VIX
or downtrending regimes (-0.88 / -0.56). v2 tests three gate combinations:

    G1 — VIX gate:    long only when VIX < threshold (else cash, no PnL)
    G2 — Trend gate:  long only when SPY > 200d SMA (else cash)
    G3 — Both:        long only when BOTH conditions met

Plus a VIX-threshold sweep ({18, 20, 22, 25, 28, 30}) to find the
risk-adjusted optimum.

Reuses helpers from backtest_sector_rotation_biweekly.py (same alpha and
top-3 equal-weight portfolio construction).

Outputs:
    results/sector_rotation_v2_research.md
    results/sector_rotation_v2_research.json
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Reuse the v1 module's helpers (factor calcs, variant_a, alpha builder)
from scripts.backtest_sector_rotation_biweekly import (  # noqa: E402
    ALPHA_FACTORS,
    DATA_FILE,
    EVAL_START,
    END_DATE,
    FEE_BPS,
    REBALANCE_BARS,
    SECTOR_UNIVERSE,
    SLIP_BPS,
    START_DATE,
    build_alpha_signal,
    variant_a_top3_equal,
)

OUT_MD = PROJECT_ROOT / "results" / "sector_rotation_v2_research.md"
OUT_JSON = PROJECT_ROOT / "results" / "sector_rotation_v2_research.json"


# ──────────────────────────────────────────────────────────────────────
# Regime gates
# ──────────────────────────────────────────────────────────────────────

@dataclass
class GateConfig:
    name: str
    use_vix: bool
    vix_max: float
    use_trend: bool
    trend_sma: int = 200

    def label(self) -> str:
        parts = []
        if self.use_vix:
            parts.append(f"VIX<{self.vix_max:.0f}")
        if self.use_trend:
            parts.append(f"SPY>SMA{self.trend_sma}")
        return " & ".join(parts) if parts else "Baseline"


def evaluate_gate(
    gate: GateConfig,
    vix: pd.Series,
    spy_close: pd.Series,
) -> pd.Series:
    """Per-day boolean: True = allow exposure, False = sit in cash."""
    idx = vix.index.union(spy_close.index)
    vix_a = vix.reindex(idx).ffill()
    spy_a = spy_close.reindex(idx).ffill()

    allow = pd.Series(True, index=idx)
    if gate.use_vix:
        allow &= (vix_a < gate.vix_max)
    if gate.use_trend:
        sma = spy_a.rolling(gate.trend_sma, min_periods=gate.trend_sma // 2).mean()
        allow &= (spy_a > sma)

    # Treat NaN gate values as False (be conservative when data missing)
    return allow.fillna(False)


# ──────────────────────────────────────────────────────────────────────
# Gated backtest
# ──────────────────────────────────────────────────────────────────────

def run_gated_backtest(
    closes: pd.DataFrame,
    alpha: pd.DataFrame,
    allow_mask: pd.Series,
    rebalance_bars: int = REBALANCE_BARS,
) -> Dict:
    """Same engine as v1 variant A, but when allow_mask[date] is False the
    portfolio sits in cash (zero return, no costs).

    Re-entry: when the gate flips back to True, the next rebalance date
    will re-establish the top-3 EW position (paying turnover cost from 0).
    """
    daily_ret = closes.pct_change().fillna(0.0)
    dates = alpha.index

    eval_start_ts = pd.Timestamp(EVAL_START)
    rebalance_idx: List[int] = []
    for i, d in enumerate(dates):
        if d < eval_start_ts:
            continue
        if not rebalance_idx:
            rebalance_idx.append(i)
        elif i - rebalance_idx[-1] >= rebalance_bars:
            rebalance_idx.append(i)
    rebalance_set = set(rebalance_idx)

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

    allow_mask_aligned = allow_mask.reindex(dates).fillna(False)

    for i, date in enumerate(dates):
        if date < eval_start_ts:
            continue

        gate_open = bool(allow_mask_aligned.iloc[i])

        # ── Apply yesterday's positioning to today's return ──
        if started and current_w:
            day_pnl = 0.0
            for ticker, w in current_w.items():
                r = daily_ret.iloc[i].get(ticker, 0.0)
                if pd.notna(r):
                    day_pnl += w * r
            equity *= (1.0 + day_pnl)
            daily_pnl_list.append((date, day_pnl))
            days_in_market += 1
        elif started:
            daily_pnl_list.append((date, 0.0))
            days_in_cash += 1

        # ── Rebalance ──
        if i in rebalance_set:
            if gate_open:
                new_w = variant_a_top3_equal(alpha.iloc[i])
            else:
                new_w = {}   # close out — go to cash

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
    daily_pnl = pd.Series(dict(daily_pnl_list))
    return _compute_metrics(eq_df, daily_pnl, pnl_per_rebalance, total_turnover,
                            n_trades, days_in_market, days_in_cash)


def _compute_metrics(
    eq_df: pd.DataFrame,
    daily_pnl: pd.Series,
    rebalance_pnls: list,
    total_turnover: float,
    n_trades: int,
    days_in_market: int,
    days_in_cash: int,
) -> Dict:
    eq = eq_df["equity"]
    if len(eq) < 2:
        return {"error": "insufficient_data"}

    total_return = eq.iloc[-1] / eq.iloc[0] - 1.0
    n_days = len(eq)
    years = n_days / 252.0
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0

    daily_ret = eq.pct_change().dropna()
    sharpe = (daily_ret.mean() / daily_ret.std()) * np.sqrt(252) if daily_ret.std() > 0 else 0.0
    downside = daily_ret[daily_ret < 0].std()
    sortino = (daily_ret.mean() / downside) * np.sqrt(252) if downside > 0 else 0.0

    cummax = eq.cummax()
    max_dd = abs(((eq - cummax) / cummax).min())
    calmar = cagr / max_dd if max_dd > 0 else 0.0

    wins = sum(1 for p in rebalance_pnls if p > 0)
    win_rate = wins / len(rebalance_pnls) if rebalance_pnls else 0.0
    total_days = days_in_market + days_in_cash
    time_in_market = days_in_market / total_days if total_days > 0 else 0.0

    return {
        "n_days": n_days, "years": round(years, 2),
        "total_return": round(total_return, 4), "cagr": round(cagr, 4),
        "sharpe": round(sharpe, 3), "sortino": round(sortino, 3),
        "max_dd": round(max_dd, 4), "calmar": round(calmar, 3),
        "n_trades": n_trades, "turnover_per_year": round(total_turnover / max(years, 0.01), 1),
        "win_rate": round(win_rate, 3), "n_rebalances": len(rebalance_pnls),
        "final_equity": round(eq.iloc[-1], 4),
        "time_in_market": round(time_in_market, 3),
        "days_in_market": days_in_market, "days_in_cash": days_in_cash,
    }


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"Loading: {DATA_FILE}")
    df = pd.read_parquet(DATA_FILE)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    wide = df.pivot_table(index="timestamp", columns="ticker", values="close", aggfunc="last").sort_index()

    closes = wide[SECTOR_UNIVERSE].dropna(how="all")
    vix = wide.get("^VIX")
    spy = wide.get("SPY")
    if vix is None or spy is None:
        print("ERROR: VIX or SPY series missing from panel"); return 1

    closes = closes.loc[(closes.index >= START_DATE) & (closes.index <= END_DATE)]
    print(f"  closes: {closes.shape}, range {closes.index[0].date()} → {closes.index[-1].date()}")

    print("Building alpha signal...")
    alpha = build_alpha_signal(closes)
    print(f"  alpha non-null rows: {alpha.notna().any(axis=1).sum()}")

    # ── Baseline (no gate) ──
    print("\n=== Baseline (no gate) ===")
    baseline = run_gated_backtest(closes, alpha, pd.Series(True, index=closes.index))
    print(f"  Sharpe={baseline['sharpe']}  CAGR={baseline['cagr']:.2%}  "
          f"MaxDD={baseline['max_dd']:.2%}  TIM={baseline['time_in_market']:.0%}")

    # ── Gate variants ──
    print("\n=== Gate variants ===")
    gate_configs = [
        GateConfig("G1_VIX22", use_vix=True, vix_max=22, use_trend=False),
        GateConfig("G1_VIX25", use_vix=True, vix_max=25, use_trend=False),
        GateConfig("G2_Trend200", use_vix=False, vix_max=0, use_trend=True, trend_sma=200),
        GateConfig("G3_Both_VIX22", use_vix=True, vix_max=22, use_trend=True, trend_sma=200),
        GateConfig("G3_Both_VIX25", use_vix=True, vix_max=25, use_trend=True, trend_sma=200),
    ]

    gate_results: Dict[str, Dict] = {}
    for gc in gate_configs:
        mask = evaluate_gate(gc, vix, spy)
        m = run_gated_backtest(closes, alpha, mask)
        m["gate_label"] = gc.label()
        gate_results[gc.name] = m
        print(f"  {gc.name:18s} {gc.label():28s}  Sharpe={m['sharpe']:+.2f}  "
              f"CAGR={m['cagr']:+.2%}  MaxDD={m['max_dd']:.2%}  TIM={m['time_in_market']:.0%}")

    # ── VIX sensitivity sweep (no trend gate) ──
    print("\n=== VIX threshold sweep (VIX gate only) ===")
    sweep_results: Dict[str, Dict] = {}
    for vix_max in [18, 20, 22, 25, 28, 30]:
        gc = GateConfig(f"VIXsweep_{vix_max}", use_vix=True, vix_max=vix_max, use_trend=False)
        mask = evaluate_gate(gc, vix, spy)
        m = run_gated_backtest(closes, alpha, mask)
        sweep_results[str(vix_max)] = m
        print(f"  VIX<{vix_max:2d}  Sharpe={m['sharpe']:+.2f}  CAGR={m['cagr']:+.2%}  "
              f"MaxDD={m['max_dd']:.2%}  TIM={m['time_in_market']:.0%}")

    # ── Persist ──
    payload = {
        "baseline_no_gate": baseline,
        "gate_variants": gate_results,
        "vix_sweep": sweep_results,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nJSON saved → {OUT_JSON}")

    md = _build_markdown(payload)
    OUT_MD.write_text(md, encoding="utf-8")
    print(f"Markdown → {OUT_MD}")
    return 0


def _build_markdown(payload: Dict) -> str:
    base = payload["baseline_no_gate"]
    gates = payload["gate_variants"]
    sweep = payload["vix_sweep"]

    lines = []
    lines.append("# Sector Rotation v2 — Regime-Gated Backtest")
    lines.append("")
    lines.append(f"_Period: 2014-12-31 → 2026-05-03 · Universe: 11 sector ETFs · "
                 f"Rebalance: 10 days · Costs: 5bps + 5bps_")
    lines.append("")
    lines.append("## TL;DR")
    lines.append("")
    best = max(gates.values(), key=lambda m: m["sharpe"])
    best_name = [k for k, v in gates.items() if v is best][0]
    lines.append(f"Best variant: **{best_name}** ({gates[best_name]['gate_label']}) → "
                 f"Sharpe **{best['sharpe']:+.2f}**, "
                 f"MaxDD **{best['max_dd']:.1%}**, "
                 f"CAGR **{best['cagr']:+.2%}** "
                 f"(vs baseline Sharpe {base['sharpe']:+.2f}, MaxDD {base['max_dd']:.1%}).")
    lines.append("")
    lines.append("## All variants")
    lines.append("")
    lines.append("| Variant | Gate | Sharpe | Sortino | CAGR | MaxDD | Calmar | TIM | n trades |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    lines.append(f"| **Baseline** | (always on) | {base['sharpe']:+.2f} | {base['sortino']:+.2f} | "
                 f"{base['cagr']:+.2%} | {base['max_dd']:.1%} | {base['calmar']:+.2f} | "
                 f"{base['time_in_market']:.0%} | {base['n_trades']} |")
    for k, m in gates.items():
        lines.append(f"| {k} | {m['gate_label']} | {m['sharpe']:+.2f} | {m['sortino']:+.2f} | "
                     f"{m['cagr']:+.2%} | {m['max_dd']:.1%} | {m['calmar']:+.2f} | "
                     f"{m['time_in_market']:.0%} | {m['n_trades']} |")
    lines.append("")
    lines.append("## VIX threshold sweep (VIX gate only, no trend filter)")
    lines.append("")
    lines.append("| VIX max | Sharpe | CAGR | MaxDD | Time in market | n trades |")
    lines.append("|---|---|---|---|---|---|")
    for vix_max, m in sweep.items():
        lines.append(f"| <{vix_max} | {m['sharpe']:+.2f} | {m['cagr']:+.2%} | "
                     f"{m['max_dd']:.1%} | {m['time_in_market']:.0%} | {m['n_trades']} |")
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append("- **TIM** (Time-In-Market) below 100% means the strategy sat in cash "
                 "(0% return, no risk) during periods the regime gate was closed.")
    lines.append("- A high-Sharpe variant with TIM 60-70% trades real-world risk for "
                 "real-world drawdown reduction — usable as a tactical overlay.")
    lines.append("- A high-Sharpe variant with TIM < 30% is statistical noise from a tiny "
                 "sample of trading days; don't trust it.")
    lines.append("")
    lines.append("## Deploy-readiness rubric (apply to the best variant)")
    lines.append("")
    rubric_pass = (best["sharpe"] >= 1.0 and best["max_dd"] <= 0.20
                   and best["time_in_market"] >= 0.40 and best["n_trades"] >= 50)
    lines.append(f"- Sharpe ≥ 1.0 → {'✅' if best['sharpe']>=1.0 else '❌'} ({best['sharpe']:+.2f})")
    lines.append(f"- MaxDD ≤ 20% → {'✅' if best['max_dd']<=0.20 else '❌'} ({best['max_dd']:.1%})")
    lines.append(f"- Time-in-market ≥ 40% → {'✅' if best['time_in_market']>=0.40 else '❌'} ({best['time_in_market']:.0%})")
    lines.append(f"- Trade count ≥ 50 → {'✅' if best['n_trades']>=50 else '❌'} ({best['n_trades']})")
    lines.append("")
    if rubric_pass:
        lines.append("**Verdict: DEPLOYABLE.** Move to 30-day paper trading.")
    else:
        lines.append("**Verdict: ITERATE.** Closer to deploy than v1 (Sharpe 0.14) but "
                     "doesn't fully clear the bar. Next iteration ideas:")
        lines.append("- Add a sector-specific vol gate (don't trade XLE / XLF in tail conditions).")
        lines.append("- Tighten 200d trend filter to 50d for faster regime detection.")
        lines.append("- Stack with a 2nd alpha source (volatility carry, sector breadth).")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
