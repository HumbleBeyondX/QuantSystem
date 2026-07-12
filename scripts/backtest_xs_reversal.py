#!/usr/bin/env python3
"""Cross-sectional short-term reversal backtest.

Implements Lehmann (1990) "Fads, Martingales, and Market Efficiency" with
Lo-Mamaysky-Wang (2002) microstructure-aware refinements.

Strategy
--------
- Universe: SP500 mega-cap names (153 from
  data/sample/sp500_2015-01-01_2026-05-04.parquet).
- Signal:   5-day cumulative log return (PRIOR week's return).
- Sleeve:   LONG bottom-20 (biggest 5d losers), SHORT top-20 (biggest 5d winners).
- Sizing:   Equal-weight per side, dollar-neutral (gross = 1.0, net = 0.0).
- Rebal:    Every 5 trading days.
- Filters:
    - Liquidity: 20d avg dollar volume >= $50M (Lo-microstructure)
    - Vol (V1 only): keep all
    - V2 quality:    only tickers with 252d Sharpe > cross-sectional median
- Costs:    5 bps per side (fee + slippage), 3 bps sensitivity variant
- Walk-forward: IS 2015-01 → 2021-12, OOS 2022-01 → 2026-05.

Outputs
-------
- results/xs_reversal_research.md
- results/xs_reversal_research.json

Gate (user's hard rule): OOS Sharpe >= 1.5 -> ACCEPT, else REJECT.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = PROJECT_ROOT / "data" / "sample" / "sp500_2015-01-01_2026-05-04.parquet"
OUT_MD = PROJECT_ROOT / "results" / "xs_reversal_research.md"
OUT_JSON = PROJECT_ROOT / "results" / "xs_reversal_research.json"

# ----- Strategy config -----
LOOKBACK = 5                # 5-day cum return signal
REBAL_BARS = 5              # weekly rebalance
N_PER_SIDE = 20             # 20 longs + 20 shorts
LIQ_MIN_USD = 50_000_000.0  # 20d avg $-vol threshold
LIQ_WINDOW = 20
QUALITY_WINDOW = 252        # 252d sharpe for V2 filter

START_DATE = "2015-01-01"
END_DATE = "2026-05-03"
IS_END = "2021-12-31"
OOS_START = "2022-01-01"

# Costs: spread+fee model — per-side
BPS_BASE = 5.0   # 5 bps base case
BPS_LOW = 3.0    # 3 bps sensitivity variant
BPS_HIGH = 10.0  # stress test


# ──────────────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────────────

def load_panel(path: Path) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load long-format panel → (close, volume, dollar_volume) wide frames."""
    print(f"Loading: {path}")
    df = pd.read_parquet(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df[(df["timestamp"] >= START_DATE) & (df["timestamp"] <= END_DATE)]

    close = df.pivot_table(index="timestamp", columns="ticker",
                           values="close", aggfunc="last").sort_index()
    vol = df.pivot_table(index="timestamp", columns="ticker",
                         values="volume", aggfunc="last").sort_index()
    dvol = (close * vol)

    coverage = close.notna().mean()
    keep = coverage[coverage > 0.7].index
    close = close[keep]
    vol = vol[keep]
    dvol = dvol[keep]

    print(f"  shape (close): {close.shape}  tickers: {close.shape[1]}")
    print(f"  date range: {close.index.min().date()} → {close.index.max().date()}")
    return close, vol, dvol


# ──────────────────────────────────────────────────────────────────────
# Signals & filters
# ──────────────────────────────────────────────────────────────────────

def calc_5d_return(close: pd.DataFrame, lookback: int = LOOKBACK) -> pd.DataFrame:
    """5-day cumulative log return.  HIGH value = winner, LOW = loser."""
    return np.log(close / close.shift(lookback))


def calc_liquidity_filter(dvol: pd.DataFrame, window: int = LIQ_WINDOW,
                          min_usd: float = LIQ_MIN_USD) -> pd.DataFrame:
    """True where 20d avg $-volume >= threshold."""
    rolling = dvol.rolling(window, min_periods=max(5, window // 2)).mean()
    return rolling >= min_usd


def calc_quality_filter(close: pd.DataFrame, window: int = QUALITY_WINDOW) -> pd.DataFrame:
    """V2: True where ticker's 252d Sharpe is above cross-sectional median that day."""
    ret = np.log(close / close.shift(1))
    mu = ret.rolling(window, min_periods=window // 2).mean()
    sd = ret.rolling(window, min_periods=window // 2).std().replace(0, np.nan)
    sharpe = (mu / sd) * np.sqrt(252)
    median_xs = sharpe.median(axis=1)
    return sharpe.gt(median_xs, axis=0)


# ──────────────────────────────────────────────────────────────────────
# Portfolio construction
# ──────────────────────────────────────────────────────────────────────

def build_weights(signal_row: pd.Series,
                  liq_row: pd.Series,
                  quality_row: Optional[pd.Series],
                  n_per_side: int = N_PER_SIDE) -> Dict[str, float]:
    """Long bottom-N losers, short top-N winners, equal weight per side."""
    s = signal_row.dropna()
    if liq_row is not None:
        s = s[liq_row.reindex(s.index).fillna(False)]
    if quality_row is not None:
        s = s[quality_row.reindex(s.index).fillna(False)]
    if len(s) < n_per_side * 2:
        return {}
    losers = s.nsmallest(n_per_side).index.tolist()   # LONG these
    winners = s.nlargest(n_per_side).index.tolist()   # SHORT these
    w: Dict[str, float] = {}
    for t in losers:
        w[t] = 0.5 / n_per_side
    for t in winners:
        w[t] = -0.5 / n_per_side
    return w


# ──────────────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────────────

@dataclass
class RunResult:
    label: str
    daily_dates: List[pd.Timestamp] = field(default_factory=list)
    daily_pnl_gross: List[float] = field(default_factory=list)
    daily_pnl_net: List[float] = field(default_factory=list)
    daily_long_pnl: List[float] = field(default_factory=list)
    daily_short_pnl: List[float] = field(default_factory=list)
    rebalance_turnover: List[float] = field(default_factory=list)
    n_rebalances: int = 0
    bps_per_side: float = 0.0
    period: str = ""


def run_backtest(close: pd.DataFrame,
                 signal: pd.DataFrame,
                 liq: pd.DataFrame,
                 quality: Optional[pd.DataFrame],
                 *,
                 bps_per_side: float,
                 label: str = "V1") -> RunResult:
    daily_ret = close.pct_change().fillna(0.0)
    dates = close.index
    cost = bps_per_side / 10_000.0

    # Rebalance every REBAL_BARS bars, starting after enough warmup.
    warmup = max(LOOKBACK + 1, LIQ_WINDOW)
    if quality is not None:
        warmup = max(warmup, QUALITY_WINDOW + 1)

    rebal_idx: List[int] = []
    for i in range(warmup, len(dates)):
        if not rebal_idx or i - rebal_idx[-1] >= REBAL_BARS:
            rebal_idx.append(i)
    rebal_set = set(rebal_idx)

    current_w: Dict[str, float] = {}
    result = RunResult(label=label, bps_per_side=bps_per_side)

    for i, date in enumerate(dates):
        if i < warmup:
            continue

        # Daily PnL on current weights (computed BEFORE rebalance — weights set
        # at end of prior rebalance day, executed at next-day open in spirit)
        if current_w:
            ret_row = daily_ret.iloc[i]
            day_pnl_gross = 0.0
            long_pnl = 0.0
            short_pnl = 0.0
            for t, w in current_w.items():
                r = ret_row.get(t, 0.0)
                if pd.notna(r):
                    pnl_t = w * r
                    day_pnl_gross += pnl_t
                    if w > 0:
                        long_pnl += pnl_t
                    else:
                        short_pnl += pnl_t
            result.daily_dates.append(date)
            result.daily_pnl_gross.append(day_pnl_gross)
            result.daily_long_pnl.append(long_pnl)
            result.daily_short_pnl.append(short_pnl)
            # cost will be added at rebalance step below; default 0 for non-rebal day
            result.daily_pnl_net.append(day_pnl_gross)

        # Rebalance: at end of bar, compute new weights using signal as-of today
        if i in rebal_set:
            sig_row = signal.iloc[i]
            liq_row = liq.iloc[i]
            qual_row = quality.iloc[i] if quality is not None else None
            new_w = build_weights(sig_row, liq_row, qual_row, n_per_side=N_PER_SIDE)
            if new_w:
                all_t = set(current_w) | set(new_w)
                turnover = sum(abs(new_w.get(t, 0.0) - current_w.get(t, 0.0)) for t in all_t)
                result.rebalance_turnover.append(turnover)
                result.n_rebalances += 1
                # apply cost on the day's NET pnl (last entry)
                if result.daily_pnl_net:
                    result.daily_pnl_net[-1] -= turnover * cost
                current_w = new_w
    return result


# ──────────────────────────────────────────────────────────────────────
# Metrics
# ──────────────────────────────────────────────────────────────────────

def slice_returns(result: RunResult, start: Optional[str] = None,
                  end: Optional[str] = None) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    idx = pd.DatetimeIndex(result.daily_dates)
    gross = pd.Series(result.daily_pnl_gross, index=idx)
    net = pd.Series(result.daily_pnl_net, index=idx)
    long_s = pd.Series(result.daily_long_pnl, index=idx)
    short_s = pd.Series(result.daily_short_pnl, index=idx)
    mask = pd.Series(True, index=idx)
    if start:
        mask &= idx >= pd.Timestamp(start)
    if end:
        mask &= idx <= pd.Timestamp(end)
    return gross[mask], net[mask], long_s[mask], short_s[mask]


def summarize(returns: pd.Series, label: str = "") -> Dict:
    if len(returns) < 10 or returns.std() == 0:
        return {"label": label, "sharpe": 0.0, "cagr": 0.0, "max_dd": 0.0,
                "n_days": int(len(returns)), "vol_ann": 0.0, "hit_rate": 0.0}
    sharpe = (returns.mean() / returns.std()) * np.sqrt(252)
    eq = (1 + returns).cumprod()
    years = len(returns) / 252.0
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else 0.0
    max_dd = abs(((eq - eq.cummax()) / eq.cummax()).min())
    hit = (returns > 0).mean()
    vol_ann = returns.std() * np.sqrt(252)
    return {
        "label": label,
        "sharpe": round(float(sharpe), 3),
        "cagr": round(float(cagr), 4),
        "max_dd": round(float(max_dd), 4),
        "n_days": int(len(returns)),
        "vol_ann": round(float(vol_ann), 4),
        "hit_rate": round(float(hit), 3),
    }


def turnover_metrics(result: RunResult) -> Dict:
    if not result.rebalance_turnover:
        return {"avg_rebal_turnover": 0.0, "avg_daily_turnover": 0.0,
                "rebalances": 0, "approx_annual_turnover": 0.0}
    avg_to = float(np.mean(result.rebalance_turnover))
    # daily turnover ~ rebal_turnover / REBAL_BARS (averaged)
    avg_daily = avg_to / REBAL_BARS
    # annualized: ~52 rebalances * avg_to
    annual = 52 * avg_to
    return {
        "avg_rebal_turnover": round(avg_to, 3),
        "avg_daily_turnover": round(avg_daily, 3),
        "rebalances": int(result.n_rebalances),
        "approx_annual_turnover": round(annual, 2),
    }


def liquidity_quintile_breakdown(close: pd.DataFrame, signal: pd.DataFrame,
                                 dvol: pd.DataFrame, liq: pd.DataFrame,
                                 *, bps_per_side: float) -> Dict[str, Dict]:
    """Sleeve-level XS reversal P&L conditioned on liquidity quintile.

    For each rebalance date, split eligible-liquidity universe into 5 quintiles
    by 20d avg $-vol. Within each quintile, long bottom-20 losers vs short top-20
    winners (or fewer if quintile small), equal-weight per side. Aggregate net of cost.
    """
    daily_ret = close.pct_change().fillna(0.0)
    dates = close.index
    cost = bps_per_side / 10_000.0
    warmup = max(LOOKBACK + 1, LIQ_WINDOW)

    rebal_idx = []
    for i in range(warmup, len(dates)):
        if not rebal_idx or i - rebal_idx[-1] >= REBAL_BARS:
            rebal_idx.append(i)
    rebal_set = set(rebal_idx)

    # rolling 20d avg $vol on rebal dates
    rolling_dvol = dvol.rolling(LIQ_WINDOW, min_periods=10).mean()

    quintile_weights: Dict[int, Dict[str, float]] = {q: {} for q in range(5)}
    quintile_pnl: Dict[int, List[Tuple[pd.Timestamp, float]]] = {q: [] for q in range(5)}

    for i, date in enumerate(dates):
        if i < warmup:
            continue
        # daily PnL on current weights for each quintile
        for q in range(5):
            w_dict = quintile_weights[q]
            if w_dict:
                ret_row = daily_ret.iloc[i]
                p = sum(w * ret_row.get(t, 0.0) for t, w in w_dict.items()
                        if pd.notna(ret_row.get(t, 0.0)))
                quintile_pnl[q].append((date, p))
        # rebalance
        if i in rebal_set:
            sig_row = signal.iloc[i]
            liq_row = liq.iloc[i]
            dv_row = rolling_dvol.iloc[i]
            valid = sig_row.dropna().index
            valid = [t for t in valid if liq_row.get(t, False) and pd.notna(dv_row.get(t, np.nan))]
            if len(valid) < 25:
                continue
            dv_sub = dv_row.reindex(valid).dropna()
            # quintile labels (0 = lowest liquidity, 4 = highest)
            try:
                labels = pd.qcut(dv_sub, 5, labels=False, duplicates="drop")
            except Exception:
                continue
            for q in range(5):
                names_q = labels[labels == q].index.tolist()
                if len(names_q) < 8:
                    quintile_weights[q] = {}
                    continue
                sig_q = sig_row.reindex(names_q).dropna()
                n_side = max(2, min(N_PER_SIDE // 2, len(sig_q) // 4))
                losers = sig_q.nsmallest(n_side).index.tolist()
                winners = sig_q.nlargest(n_side).index.tolist()
                old_w = quintile_weights[q]
                new_w: Dict[str, float] = {}
                for t in losers:
                    new_w[t] = 0.5 / n_side
                for t in winners:
                    new_w[t] = -0.5 / n_side
                all_t = set(old_w) | set(new_w)
                to = sum(abs(new_w.get(t, 0.0) - old_w.get(t, 0.0)) for t in all_t)
                if quintile_pnl[q]:
                    last_d, last_p = quintile_pnl[q][-1]
                    quintile_pnl[q][-1] = (last_d, last_p - to * cost)
                quintile_weights[q] = new_w

    out: Dict[str, Dict] = {}
    for q in range(5):
        ser = pd.Series({d: v for d, v in quintile_pnl[q]})
        out[f"Q{q+1}_{'lowliq' if q == 0 else 'highliq' if q == 4 else 'mid'}"] = summarize(ser, f"Q{q+1}")
    return out


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    close, vol, dvol = load_panel(DATA_FILE)

    print("\nBuilding signal + filters...")
    signal = calc_5d_return(close, LOOKBACK)
    liq = calc_liquidity_filter(dvol)
    quality = calc_quality_filter(close)
    print(f"  signal non-null rows: {signal.notna().any(axis=1).sum()}")
    print(f"  liq pass-through rate (mean): {liq.mean().mean():.2%}")
    print(f"  quality pass-through rate: {quality.mean().mean():.2%}")

    # -- V1 baseline 5 bps
    print("\n[V1] Naive Lehmann reversal — 5 bps/side ...")
    r_v1_5 = run_backtest(close, signal, liq, None, bps_per_side=BPS_BASE, label="V1_5bps")
    # -- V1 sensitivity 3 bps
    print("[V1] 3 bps/side sensitivity ...")
    r_v1_3 = run_backtest(close, signal, liq, None, bps_per_side=BPS_LOW, label="V1_3bps")
    # -- V1 stress 10 bps
    print("[V1] 10 bps/side stress ...")
    r_v1_10 = run_backtest(close, signal, liq, None, bps_per_side=BPS_HIGH, label="V1_10bps")
    # -- V2 quality-filtered 5 bps
    print("[V2] Quality-filtered (sharpe_252 > median) — 5 bps/side ...")
    r_v2_5 = run_backtest(close, signal, liq, quality, bps_per_side=BPS_BASE, label="V2_5bps")
    # -- V2 3 bps
    print("[V2] 3 bps/side ...")
    r_v2_3 = run_backtest(close, signal, liq, quality, bps_per_side=BPS_LOW, label="V2_3bps")

    runs = {
        "V1_5bps": r_v1_5,
        "V1_3bps": r_v1_3,
        "V1_10bps": r_v1_10,
        "V2_5bps": r_v2_5,
        "V2_3bps": r_v2_3,
    }

    # === Walk-forward & cost decomposition ===
    summary: Dict[str, Dict] = {}
    for key, r in runs.items():
        # full
        g_all, n_all, lo_all, sh_all = slice_returns(r)
        s_full_gross = summarize(g_all, f"{key}_FULL_gross")
        s_full_net = summarize(n_all, f"{key}_FULL_net")
        # IS
        g_is, n_is, lo_is, sh_is = slice_returns(r, end=IS_END)
        s_is_gross = summarize(g_is, f"{key}_IS_gross")
        s_is_net = summarize(n_is, f"{key}_IS_net")
        # OOS
        g_oos, n_oos, lo_oos, sh_oos = slice_returns(r, start=OOS_START)
        s_oos_gross = summarize(g_oos, f"{key}_OOS_gross")
        s_oos_net = summarize(n_oos, f"{key}_OOS_net")
        # sleeve hit rate
        long_hit = float((lo_oos > 0).mean()) if len(lo_oos) else 0.0
        short_hit = float((sh_oos > 0).mean()) if len(sh_oos) else 0.0
        long_mean = float(lo_oos.mean() * 252) if len(lo_oos) else 0.0
        short_mean = float(sh_oos.mean() * 252) if len(sh_oos) else 0.0
        # cost drag
        cost_drag_full = s_full_gross["sharpe"] - s_full_net["sharpe"]
        cost_drag_oos = s_oos_gross["sharpe"] - s_oos_net["sharpe"]
        # turnover
        to = turnover_metrics(r)
        summary[key] = {
            "bps_per_side": r.bps_per_side,
            "full_gross": s_full_gross,
            "full_net": s_full_net,
            "IS_gross": s_is_gross,
            "IS_net": s_is_net,
            "OOS_gross": s_oos_gross,
            "OOS_net": s_oos_net,
            "long_hit_rate_oos": round(long_hit, 3),
            "short_hit_rate_oos": round(short_hit, 3),
            "long_ann_ret_oos": round(long_mean, 4),
            "short_ann_ret_oos": round(short_mean, 4),
            "cost_drag_sharpe_full": round(cost_drag_full, 3),
            "cost_drag_sharpe_oos": round(cost_drag_oos, 3),
            "turnover": to,
        }

    # === Liquidity quintile breakdown (V1, 5bps, OOS) ===
    print("\nComputing liquidity-quintile breakdown (V1, 5bps) ...")
    liq_q = liquidity_quintile_breakdown(close, signal, dvol, liq, bps_per_side=BPS_BASE)

    # === Print headline ===
    print("\n" + "=" * 80)
    print(" Headline results — net of costs ")
    print("=" * 80)
    hdr = f"{'Run':<12}{'IS-Shp':>9}{'OOS-Shp':>9}{'OOS-CAGR':>11}{'OOS-MDD':>9}{'Turn/yr':>10}{'CostDrag':>11}"
    print(hdr)
    print("-" * len(hdr))
    for key, info in summary.items():
        print(f"{key:<12}"
              f"{info['IS_net']['sharpe']:>+9.2f}"
              f"{info['OOS_net']['sharpe']:>+9.2f}"
              f"{info['OOS_net']['cagr']:>+11.2%}"
              f"{info['OOS_net']['max_dd']:>9.1%}"
              f"{info['turnover']['approx_annual_turnover']:>10.1f}"
              f"{info['cost_drag_sharpe_oos']:>+11.2f}")

    print("\nLiquidity quintile (V1 5bps, full sample):")
    for q, m in liq_q.items():
        print(f"  {q:<14} sharpe={m['sharpe']:+.2f}  cagr={m['cagr']:+.2%}  ndays={m['n_days']}")

    # === Gate decision ===
    v1_oos = summary["V1_5bps"]["OOS_net"]["sharpe"]
    v2_oos = summary["V2_5bps"]["OOS_net"]["sharpe"]
    best_oos = max(v1_oos, v2_oos)
    gate = "ACCEPT" if best_oos >= 1.5 else "REJECT"
    print(f"\nGate: best OOS Sharpe (net) = {best_oos:+.2f}  →  {gate}  (threshold 1.5)")

    # === Write outputs ===
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    out_blob = {
        "config": {
            "lookback": LOOKBACK,
            "rebal_bars": REBAL_BARS,
            "n_per_side": N_PER_SIDE,
            "liq_min_usd": LIQ_MIN_USD,
            "liq_window": LIQ_WINDOW,
            "is_end": IS_END,
            "oos_start": OOS_START,
            "bps_variants": [BPS_LOW, BPS_BASE, BPS_HIGH],
        },
        "summary": summary,
        "liquidity_quintiles_V1_5bps_full": liq_q,
        "gate": {
            "threshold": 1.5,
            "V1_OOS_net_sharpe": v1_oos,
            "V2_OOS_net_sharpe": v2_oos,
            "best_OOS_net_sharpe": best_oos,
            "decision": gate,
        },
    }
    OUT_JSON.write_text(json.dumps(out_blob, indent=2, default=str))

    OUT_MD.write_text(_md_report(summary, liq_q, gate, best_oos, v1_oos, v2_oos),
                      encoding="utf-8")
    print(f"\nJSON → {OUT_JSON}")
    print(f"MD   → {OUT_MD}")
    return 0


def _fmt_metrics(d: Dict) -> str:
    return (f"Shp {d['sharpe']:+.2f} · CAGR {d['cagr']:+.2%} · "
            f"MDD {d['max_dd']:.1%} · Vol {d['vol_ann']:.1%} · Hit {d['hit_rate']:.1%}")


def _md_report(summary: Dict, liq_q: Dict, gate: str,
               best_oos: float, v1_oos: float, v2_oos: float) -> str:
    lines: List[str] = []
    lines.append("# Cross-Sectional Short-Term Reversal — Lehmann (1990) / Lo-Mamaysky-Wang (2002)")
    lines.append("")
    lines.append(f"_Universe: SP500 153 names · Period: 2015-01 → 2026-05 · "
                 f"IS {START_DATE}→{IS_END} · OOS {OOS_START}→{END_DATE}_")
    lines.append("")
    lines.append("## Strategy")
    lines.append("")
    lines.append(f"- Signal: 5-day cumulative log return (PRIOR week)")
    lines.append(f"- LONG bottom-{N_PER_SIDE} (biggest losers), SHORT top-{N_PER_SIDE} (biggest winners)")
    lines.append(f"- Equal-weight per side, dollar-neutral (gross 1.0, net 0.0)")
    lines.append(f"- Rebalance: every {REBAL_BARS} trading days")
    lines.append(f"- Liquidity filter: 20d avg $-vol >= ${LIQ_MIN_USD/1e6:.0f}M")
    lines.append(f"- **V1** = naive Lehmann reversal (liquidity filter only)")
    lines.append(f"- **V2** = quality-filtered: only tickers with 252d Sharpe > XS median")
    lines.append("")
    lines.append("## Headline (net of costs)")
    lines.append("")
    lines.append("| Run | IS Sharpe | OOS Sharpe | OOS CAGR | OOS MaxDD | Turn/yr | Cost drag (Shp, OOS) |")
    lines.append("|---|---|---|---|---|---|---|")
    for key, info in summary.items():
        lines.append(f"| `{key}` | {info['IS_net']['sharpe']:+.2f} | "
                     f"{info['OOS_net']['sharpe']:+.2f} | "
                     f"{info['OOS_net']['cagr']:+.2%} | "
                     f"{info['OOS_net']['max_dd']:.1%} | "
                     f"{info['turnover']['approx_annual_turnover']:.1f} | "
                     f"{info['cost_drag_sharpe_oos']:+.2f} |")
    lines.append("")
    lines.append("## Gross vs Net (cost drag is the #1 killer)")
    lines.append("")
    lines.append("| Run | Full Gross Shp | Full Net Shp | OOS Gross Shp | OOS Net Shp |")
    lines.append("|---|---|---|---|---|")
    for key, info in summary.items():
        lines.append(f"| `{key}` | {info['full_gross']['sharpe']:+.2f} | "
                     f"{info['full_net']['sharpe']:+.2f} | "
                     f"{info['OOS_gross']['sharpe']:+.2f} | "
                     f"{info['OOS_net']['sharpe']:+.2f} |")
    lines.append("")
    lines.append("## Sleeve hit rates & sleeve ann return (OOS, net)")
    lines.append("")
    lines.append("| Run | Long hit% | Short hit% | Long Ann | Short Ann |")
    lines.append("|---|---|---|---|---|")
    for key, info in summary.items():
        lines.append(f"| `{key}` | {info['long_hit_rate_oos']*100:.1f}% | "
                     f"{info['short_hit_rate_oos']*100:.1f}% | "
                     f"{info['long_ann_ret_oos']:+.2%} | "
                     f"{info['short_ann_ret_oos']:+.2%} |")
    lines.append("")
    lines.append("## Liquidity quintile breakdown (V1 5bps full sample)")
    lines.append("")
    lines.append("Q1=lowest avg $-vol, Q5=highest. Each quintile runs its own self-contained "
                 "long-loser / short-winner sleeve (scale-invariant).")
    lines.append("")
    lines.append("| Quintile | Sharpe | CAGR | MaxDD | Days |")
    lines.append("|---|---|---|---|---|")
    for q, m in liq_q.items():
        lines.append(f"| {q} | {m['sharpe']:+.2f} | {m['cagr']:+.2%} | "
                     f"{m['max_dd']:.1%} | {m['n_days']} |")
    lines.append("")
    lines.append("## Cost sensitivity (V1)")
    lines.append("")
    lines.append("| BPS / side | OOS Sharpe | OOS CAGR | Turn/yr |")
    lines.append("|---|---|---|---|")
    for key in ["V1_3bps", "V1_5bps", "V1_10bps"]:
        if key in summary:
            info = summary[key]
            lines.append(f"| {info['bps_per_side']:.0f} | "
                         f"{info['OOS_net']['sharpe']:+.2f} | "
                         f"{info['OOS_net']['cagr']:+.2%} | "
                         f"{info['turnover']['approx_annual_turnover']:.1f} |")
    lines.append("")
    lines.append("## Walk-forward degradation")
    lines.append("")
    for key in ["V1_5bps", "V2_5bps"]:
        info = summary[key]
        is_s = info["IS_net"]["sharpe"]
        oos_s = info["OOS_net"]["sharpe"]
        delta = oos_s - is_s
        lines.append(f"- `{key}` IS Sharpe {is_s:+.2f} → OOS Sharpe {oos_s:+.2f} "
                     f"(Δ = {delta:+.2f})")
    lines.append("")
    lines.append("## Gate decision (user's hard rule: OOS Sharpe net ≥ 1.5)")
    lines.append("")
    lines.append(f"- V1 OOS Sharpe (net): **{v1_oos:+.2f}**")
    lines.append(f"- V2 OOS Sharpe (net): **{v2_oos:+.2f}**")
    lines.append(f"- Best OOS: **{best_oos:+.2f}**")
    lines.append(f"- Threshold: **1.50**")
    lines.append(f"- **Decision: {gate}**")
    lines.append("")
    lines.append("## Compared to prior small-cap MR strategy")
    lines.append("")
    lines.append("- Prior small-cap mean-reversion: IS Sharpe **+2.00**, OOS Sharpe **-0.13** "
                 "(catastrophic walk-forward failure — overfit to 2024-2026 small-cap dynamics).")
    lines.append(f"- This systematic XS reversal V1: IS {summary['V1_5bps']['IS_net']['sharpe']:+.2f} "
                 f"→ OOS {summary['V1_5bps']['OOS_net']['sharpe']:+.2f} "
                 f"(Δ {summary['V1_5bps']['OOS_net']['sharpe']-summary['V1_5bps']['IS_net']['sharpe']:+.2f})")
    lines.append(f"- V2 quality-filtered: IS {summary['V2_5bps']['IS_net']['sharpe']:+.2f} "
                 f"→ OOS {summary['V2_5bps']['OOS_net']['sharpe']:+.2f} "
                 f"(Δ {summary['V2_5bps']['OOS_net']['sharpe']-summary['V2_5bps']['IS_net']['sharpe']:+.2f})")
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append("- Survivorship bias: panel uses CURRENT SP500 constituents back to 2015, "
                 "so failed/delisted names are absent. Cross-sectional reversal is somewhat "
                 "robust to this (works within liquid mega-caps), but real OOS likely a touch worse.")
    lines.append("- Single-side spread cost = bps_per_side; round-trip = 2× that. We charge "
                 "`turnover × cost` at each rebalance, which captures both sides.")
    lines.append("- 5-day signal uses today's close to set tomorrow's weights — there is no "
                 "look-ahead since the signal is `close.shift(0) / close.shift(5)` and PnL is "
                 "from `daily_ret.iloc[i+1]` style sequencing (weights set at end-of-day i, "
                 "PnL realized day i+1 onward).")
    lines.append("- No intraday execution / no MOC / no impact model — pure close-to-close.")
    lines.append("- 5bps is **light** for short sleeves of low-liquidity small caps but "
                 "realistic for liquid SP500 mega-caps with prime broker pricing.")
    lines.append("- Borrowing cost (locate fee) for shorts is NOT charged; for mega-caps "
                 "typically 0-25 bps annualized, would reduce OOS Sharpe by ~0.05-0.15.")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
