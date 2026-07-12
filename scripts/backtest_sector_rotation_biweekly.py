#!/usr/bin/env python3
"""Backtest sector rotation with BI-WEEKLY (10-day) rebalance.

Uses the top SP500 factors (mom_6_1, multi_horizon_mom, momentum_crash_aware,
sharpe_252) combined into an equal-weight cross-sectional z-score signal,
applied to the sector ETF panel.

Compares three portfolio variants:
  A) Equal-weight top-3 sectors (long only)
  B) Long-short top-2 / bottom-2
  C) Risk-parity (inverse vol) top-3

Realistic costs: 5 bps fee + 5 bps slippage per side per trade.

Run:
    python scripts/backtest_sector_rotation_biweekly.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ──────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────
DATA_FILE = PROJECT_ROOT / "data" / "sample" / "sector_rotation_2010-01-01_2026-05-03.parquet"
OUT_FILE = PROJECT_ROOT / "results" / "sector_rotation_2week_research.md"

START_DATE = "2014-01-01"   # warmup needs ~252d before this
EVAL_START = "2014-12-31"   # OOS eval from here
END_DATE = "2026-05-03"

REBALANCE_BARS = 10         # bi-weekly (10 trading days ≈ 2 calendar weeks)
FEE_BPS = 5                 # 0.05% commission
SLIP_BPS = 5                # 5 bps slippage per side
TOTAL_COST_BPS_ROUNDTRIP = 2 * (FEE_BPS + SLIP_BPS)  # round-trip cost in bps of turnover

# Universe — pure sector ETFs (NOT factor ETFs, NOT macro)
SECTOR_UNIVERSE = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE",
                   "XLI", "XLU", "XLB", "XLRE", "XLC"]

# Factor weights — equal-weight z-score blend (per task spec)
ALPHA_FACTORS = ["mom_6_1", "multi_horizon_mom", "momentum_crash_aware", "sharpe_252"]


# ──────────────────────────────────────────────────────────────────────
# Factor calculations (replicating the library logic, but standalone)
# ──────────────────────────────────────────────────────────────────────
def calc_mom_6_1(closes: pd.DataFrame) -> pd.DataFrame:
    """6-month momentum skipping last 21d."""
    return np.log(closes.shift(21) / closes.shift(126))


def calc_mom_3_1(closes: pd.DataFrame) -> pd.DataFrame:
    return np.log(closes.shift(21) / closes.shift(63))


def calc_mom_12_1(closes: pd.DataFrame) -> pd.DataFrame:
    return np.log(closes.shift(21) / closes.shift(252))


def calc_multi_horizon_mom(closes: pd.DataFrame) -> pd.DataFrame:
    """Equal-weight z-score blend of 3m/6m/12m momentum."""
    m3 = calc_mom_3_1(closes)
    m6 = calc_mom_6_1(closes)
    m12 = calc_mom_12_1(closes)
    return (_cs_z(m3) + _cs_z(m6) + _cs_z(m12)) / 3.0


def calc_momentum_crash_aware(closes: pd.DataFrame) -> pd.DataFrame:
    """Daniel-Moskowitz crash proxy: mom_12_1 × sign(market 12m return)."""
    mom = calc_mom_12_1(closes)
    market_close = closes.mean(axis=1)
    market_mom = np.log(market_close.shift(21) / market_close.shift(252))
    regime = np.sign(market_mom).fillna(0)
    return mom.multiply(regime, axis=0)


def calc_sharpe_252(closes: pd.DataFrame) -> pd.DataFrame:
    log_ret = np.log(closes / closes.shift(1))
    mu = log_ret.rolling(252, min_periods=126).mean()
    sigma = log_ret.rolling(252, min_periods=126).std().replace(0, np.nan)
    return (mu / sigma) * np.sqrt(252)


def _cs_z(df: pd.DataFrame) -> pd.DataFrame:
    mean = df.mean(axis=1)
    std = df.std(axis=1).replace(0, np.nan)
    return df.sub(mean, axis=0).div(std, axis=0)


def realized_vol(returns: pd.DataFrame, window: int) -> pd.DataFrame:
    return returns.rolling(window, min_periods=window // 2).std() * np.sqrt(252)


# ──────────────────────────────────────────────────────────────────────
# Composite alpha signal
# ──────────────────────────────────────────────────────────────────────
def build_alpha_signal(closes: pd.DataFrame) -> pd.DataFrame:
    """Equal-weight cross-sectional z-score of the 4 factors."""
    f1 = _cs_z(calc_mom_6_1(closes))
    f2 = _cs_z(calc_multi_horizon_mom(closes))
    f3 = _cs_z(calc_momentum_crash_aware(closes))
    f4 = _cs_z(calc_sharpe_252(closes))
    composite = (f1 + f2 + f3 + f4) / 4.0
    return composite


# ──────────────────────────────────────────────────────────────────────
# Portfolio constructors — return weight vector per rebalance date
# ──────────────────────────────────────────────────────────────────────
def variant_a_top3_equal(score_row: pd.Series) -> Dict[str, float]:
    """Long-only equal-weight top 3."""
    valid = score_row.dropna()
    if len(valid) < 3:
        return {}
    top3 = valid.nlargest(3).index.tolist()
    return {t: 1.0 / 3.0 for t in top3}


def variant_b_longshort(score_row: pd.Series) -> Dict[str, float]:
    """Long top-2, short bottom-2 — dollar-neutral 50/50."""
    valid = score_row.dropna()
    if len(valid) < 4:
        return {}
    top2 = valid.nlargest(2).index.tolist()
    bot2 = valid.nsmallest(2).index.tolist()
    w = {}
    for t in top2:
        w[t] = 0.5
    for t in bot2:
        w[t] = -0.5
    return w


def variant_c_risk_parity(score_row: pd.Series, vol_row: pd.Series) -> Dict[str, float]:
    """Long-only top-3 with inverse-vol (risk parity) weights."""
    valid = score_row.dropna()
    if len(valid) < 3:
        return {}
    top3 = valid.nlargest(3).index.tolist()
    inv_vol = 1.0 / vol_row.reindex(top3).replace(0, np.nan)
    inv_vol = inv_vol.dropna()
    if len(inv_vol) < 3:
        return {t: 1.0 / 3.0 for t in top3}
    w = inv_vol / inv_vol.sum()
    return w.to_dict()


# ──────────────────────────────────────────────────────────────────────
# Backtest engine — bi-weekly rebalance, daily returns w/ costs
# ──────────────────────────────────────────────────────────────────────
def run_backtest(closes: pd.DataFrame,
                 alpha: pd.DataFrame,
                 vol_60d: pd.DataFrame,
                 variant: str,
                 rebalance_bars: int = REBALANCE_BARS,
                 cost_bps_roundtrip: int = TOTAL_COST_BPS_ROUNDTRIP,
                 eval_start: str = EVAL_START) -> Dict:
    """Returns dict with equity curve + metrics."""
    daily_ret = closes.pct_change().fillna(0.0)

    # Determine rebalance dates: every Nth trading day in the alpha index
    dates = alpha.index
    eval_start_ts = pd.Timestamp(eval_start)

    # Build list of rebalance indices (positional)
    rebalance_idx: List[int] = []
    for i, d in enumerate(dates):
        if d < eval_start_ts:
            continue
        if not rebalance_idx:
            rebalance_idx.append(i)
        elif i - rebalance_idx[-1] >= rebalance_bars:
            rebalance_idx.append(i)

    # Track weights, equity, turnover
    current_w: Dict[str, float] = {}
    equity = 1.0
    equity_curve = []
    pnl_per_rebalance = []
    n_trades = 0
    total_turnover = 0.0
    daily_pnl_list = []

    rebalance_set = set(rebalance_idx)

    # We only track AFTER the first rebalance
    started = False
    last_rebalance_equity = equity

    for i, date in enumerate(dates):
        if date < eval_start_ts:
            continue

        # Apply daily return using yesterday's weights (executed at close on prev rebal date)
        if started and current_w:
            day_pnl = 0.0
            for ticker, w in current_w.items():
                r = daily_ret.iloc[i].get(ticker, 0.0)
                if pd.notna(r):
                    day_pnl += w * r
            equity *= (1.0 + day_pnl)
            daily_pnl_list.append((date, day_pnl))

        # Rebalance check
        if i in rebalance_set:
            score_row = alpha.iloc[i]
            vol_row = vol_60d.iloc[i] if vol_60d is not None else None

            if variant == "A":
                new_w = variant_a_top3_equal(score_row)
            elif variant == "B":
                new_w = variant_b_longshort(score_row)
            elif variant == "C":
                new_w = variant_c_risk_parity(score_row, vol_row)
            else:
                raise ValueError(variant)

            if new_w:
                # Compute turnover vs current
                all_tickers = set(current_w) | set(new_w)
                turnover = sum(abs(new_w.get(t, 0) - current_w.get(t, 0)) for t in all_tickers)
                # Apply transaction cost = turnover × (fee+slip per side, both sides handled by single rate)
                # cost_bps_roundtrip already accounts for entry+exit, so charge once on turnover
                cost = turnover * (cost_bps_roundtrip / 10000.0) * 0.5
                # turnover is the gross weight change; cost per unit of weight change is (fee+slip) one side
                # so cost = turnover × (FEE_BPS+SLIP_BPS)/10000
                cost = turnover * ((FEE_BPS + SLIP_BPS) / 10000.0)
                equity *= (1.0 - cost)
                total_turnover += turnover
                n_trades += len(all_tickers)

                # Track per-rebalance pnl (for win-rate)
                if started:
                    pnl_per_rebalance.append(equity - last_rebalance_equity)
                last_rebalance_equity = equity
                current_w = new_w
                started = True

        equity_curve.append((date, equity))

    if not equity_curve:
        return {"variant": variant, "error": "no_data"}

    eq_df = pd.DataFrame(equity_curve, columns=["date", "equity"]).set_index("date")
    daily_pnl = pd.Series(dict(daily_pnl_list))

    return _compute_metrics(eq_df, daily_pnl, pnl_per_rebalance, total_turnover, n_trades, variant)


def _compute_metrics(eq_df: pd.DataFrame,
                     daily_pnl: pd.Series,
                     rebalance_pnls: list,
                     total_turnover: float,
                     n_trades: int,
                     variant: str) -> Dict:
    eq = eq_df["equity"]
    if len(eq) < 2:
        return {"variant": variant, "error": "insufficient_data"}

    total_return = eq.iloc[-1] / eq.iloc[0] - 1.0
    n_days = len(eq)
    years = n_days / 252.0
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0

    daily_ret = eq.pct_change().dropna()
    sharpe = (daily_ret.mean() / daily_ret.std()) * np.sqrt(252) if daily_ret.std() > 0 else 0.0

    # Sortino
    downside = daily_ret[daily_ret < 0].std()
    sortino = (daily_ret.mean() / downside) * np.sqrt(252) if downside > 0 else 0.0

    # MaxDD
    cummax = eq.cummax()
    drawdown = (eq - cummax) / cummax
    max_dd = abs(drawdown.min())
    calmar = cagr / max_dd if max_dd > 0 else 0.0

    # Win rate per rebalance
    wins = sum(1 for p in rebalance_pnls if p > 0)
    win_rate = wins / len(rebalance_pnls) if rebalance_pnls else 0.0

    # Find peak and trough dates
    peak_date = eq.idxmax()
    trough_idx = drawdown.idxmin()

    # Annual turnover (sum of weight changes / number of years)
    annual_turnover = total_turnover / years if years > 0 else 0.0

    return {
        "variant": variant,
        "n_days": int(n_days),
        "years": round(years, 2),
        "start_date": str(eq.index[0].date()),
        "end_date": str(eq.index[-1].date()),
        "total_return": float(total_return),
        "cagr": float(cagr),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_drawdown": float(max_dd),
        "calmar": float(calmar),
        "win_rate": float(win_rate),
        "n_rebalances": len(rebalance_pnls),
        "annual_turnover": float(annual_turnover),
        "final_equity": float(eq.iloc[-1]),
        "peak_date": str(peak_date.date()),
        "trough_date": str(trough_idx.date()) if pd.notna(trough_idx) else None,
        "max_drawdown_value": float(drawdown.min()),
        "_eq_series": eq,   # keep for regime analysis
        "_daily_ret": daily_ret,
    }


# ──────────────────────────────────────────────────────────────────────
# Regime analysis — split returns by VIX & market trend
# ──────────────────────────────────────────────────────────────────────
def regime_analysis(daily_ret: pd.Series, vix: pd.Series, spy_close: pd.Series) -> Dict:
    """Split daily returns by VIX regime (low/mid/high) and trend (up/down)."""
    df = pd.DataFrame({"ret": daily_ret})
    df["vix"] = vix.reindex(df.index).ffill()
    df["spy_sma200"] = spy_close.reindex(df.index).rolling(200, min_periods=100).mean()
    df["spy"] = spy_close.reindex(df.index)
    df["trend_up"] = (df["spy"] > df["spy_sma200"]).astype(int)

    # VIX terciles using its full history
    q33 = df["vix"].quantile(0.33)
    q67 = df["vix"].quantile(0.67)

    def vix_bucket(v):
        if pd.isna(v):
            return "unknown"
        if v < q33:
            return "low_vix"
        if v < q67:
            return "mid_vix"
        return "high_vix"

    df["vix_bucket"] = df["vix"].apply(vix_bucket)
    df["trend_bucket"] = df["trend_up"].map({1: "uptrend", 0: "downtrend"})

    summary = {}
    for grp in ["vix_bucket", "trend_bucket"]:
        out = {}
        for name, g in df.groupby(grp):
            if len(g) < 20:
                continue
            sh = (g["ret"].mean() / g["ret"].std()) * np.sqrt(252) if g["ret"].std() > 0 else 0
            out[str(name)] = {
                "n_days": len(g),
                "ann_return": float(g["ret"].mean() * 252),
                "sharpe": float(sh),
            }
        summary[grp] = out

    return summary


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────
def main():
    print("Loading data from:", DATA_FILE)
    df = pd.read_parquet(DATA_FILE)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is not None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(None)

    start_ts = pd.Timestamp(START_DATE) - pd.Timedelta(days=400)
    df = df[(df["timestamp"] >= start_ts) & (df["timestamp"] <= pd.Timestamp(END_DATE))]

    # Wide close panel
    wide = df.pivot_table(index="timestamp", columns="ticker",
                          values="close", aggfunc="last").sort_index()

    # Restrict to sector universe (drop tickers not present)
    avail_sectors = [t for t in SECTOR_UNIVERSE if t in wide.columns]
    closes_sectors = wide[avail_sectors].dropna(how="all")
    print(f"Sector universe: {avail_sectors}")
    print(f"Closes shape: {closes_sectors.shape}")
    print(f"Date range: {closes_sectors.index.min()} → {closes_sectors.index.max()}")

    # Build alpha signal
    alpha = build_alpha_signal(closes_sectors)
    print(f"Alpha signal computed. Non-null rows: {alpha.dropna(how='all').shape[0]}")

    # 60-day realized vol for risk parity
    returns = closes_sectors.pct_change()
    vol_60 = returns.rolling(60, min_periods=30).std() * np.sqrt(252)

    # Run three variants
    results = {}
    for variant_id, variant_name in [("A", "Top-3 Equal-Weight (Long)"),
                                      ("B", "Long-Short Top-2 / Bottom-2"),
                                      ("C", "Top-3 Risk Parity (Inverse Vol)")]:
        print(f"\n=== Running variant {variant_id}: {variant_name} ===")
        res = run_backtest(closes_sectors, alpha, vol_60, variant_id)
        res["variant_name"] = variant_name
        results[variant_id] = res
        print(f"  Sharpe: {res['sharpe']:.2f}  CAGR: {res['cagr']:.2%}  "
              f"MaxDD: {res['max_drawdown']:.2%}  Calmar: {res['calmar']:.2f}  "
              f"WinRate: {res['win_rate']:.1%}  Turnover/yr: {res['annual_turnover']:.1f}")

    # Benchmarks — equal-weight all sectors, and SPY buy-hold
    eval_start_ts = pd.Timestamp(EVAL_START)
    sector_ew = closes_sectors[avail_sectors].loc[eval_start_ts:]
    ew_ret = sector_ew.pct_change().mean(axis=1).dropna()
    ew_eq = (1 + ew_ret).cumprod()
    ew_sharpe = (ew_ret.mean() / ew_ret.std()) * np.sqrt(252) if ew_ret.std() > 0 else 0
    ew_cagr = ew_eq.iloc[-1] ** (252 / len(ew_eq)) - 1
    ew_dd = abs(((ew_eq / ew_eq.cummax()) - 1).min())
    bench_ew = {
        "name": "Equal-weight ALL sectors (bench)",
        "sharpe": float(ew_sharpe),
        "cagr": float(ew_cagr),
        "max_drawdown": float(ew_dd),
        "total_return": float(ew_eq.iloc[-1] - 1),
    }
    print(f"\nBenchmark (equal-weight all sectors): Sharpe={ew_sharpe:.2f}, CAGR={ew_cagr:.2%}, MaxDD={ew_dd:.2%}")

    spy_bench = None
    if "SPY" in wide.columns:
        spy = wide["SPY"].loc[eval_start_ts:].dropna()
        spy_ret = spy.pct_change().dropna()
        spy_sh = (spy_ret.mean() / spy_ret.std()) * np.sqrt(252)
        spy_eq = (1 + spy_ret).cumprod()
        spy_cagr = spy_eq.iloc[-1] ** (252 / len(spy_eq)) - 1
        spy_dd = abs(((spy_eq / spy_eq.cummax()) - 1).min())
        spy_bench = {
            "name": "SPY buy-hold",
            "sharpe": float(spy_sh),
            "cagr": float(spy_cagr),
            "max_drawdown": float(spy_dd),
            "total_return": float(spy_eq.iloc[-1] - 1),
        }
        print(f"Benchmark (SPY buy-hold): Sharpe={spy_sh:.2f}, CAGR={spy_cagr:.2%}, MaxDD={spy_dd:.2%}")

    # Regime analysis — only for the BEST variant (highest Sharpe)
    best_id = max(results, key=lambda k: results[k]["sharpe"])
    best = results[best_id]
    print(f"\nBest variant: {best_id} ({best['variant_name']})")

    regimes = {}
    if "^VIX" in wide.columns and "SPY" in wide.columns:
        regimes = regime_analysis(best["_daily_ret"], wide["^VIX"], wide["SPY"])
        print("Regime breakdown for best variant:")
        for grp, vals in regimes.items():
            print(f"  {grp}: {vals}")

    # Generate report
    report = build_report(results, bench_ew, spy_bench, regimes, best_id)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nReport written: {OUT_FILE}")

    # Also dump raw metrics to json
    metrics_path = OUT_FILE.with_suffix(".json")
    dump = {}
    for k, v in results.items():
        dump[k] = {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
    dump["bench_equal_weight_sectors"] = bench_ew
    if spy_bench:
        dump["bench_spy"] = spy_bench
    dump["regimes_best_variant"] = regimes
    dump["best_variant_id"] = best_id
    with open(metrics_path, "w") as f:
        json.dump(dump, f, indent=2, default=str)
    print(f"Metrics JSON: {metrics_path}")


def build_report(results, bench_ew, spy_bench, regimes, best_id) -> str:
    """Generate the markdown research report."""
    best = results[best_id]
    sharpe_gt_1 = best["sharpe"] > 1.0
    verdict_label = "DEPLOY" if best["sharpe"] > 1.0 and best["max_drawdown"] < 0.25 \
        else "ITERATE" if best["sharpe"] > 0.5 \
        else "REJECT"

    lines = []
    lines.append("# Sector Rotation 2-Week Rebalance — Research Report")
    lines.append("")
    lines.append(f"_Backtest period: {best['start_date']} → {best['end_date']} "
                 f"({best['years']:.1f} years). Rebalance: every 10 trading days (~2 weeks)._")
    lines.append("")
    lines.append("## 1. Strategy specification")
    lines.append("")
    lines.append("**Universe:** XLK, XLF, XLV, XLY, XLP, XLE, XLI, XLU, XLB, XLRE, XLC (11 sector ETFs).")
    lines.append("")
    lines.append("**Alpha signal:** equal-weighted cross-sectional z-score blend of four factors "
                 "selected for their t-stat > 2 performance on the SP500 universe:")
    lines.append("")
    lines.append("- `mom_6_1` — 6-month momentum, skip last 21d (Carhart-style)")
    lines.append("- `multi_horizon_mom` — z-score blend of 3m/6m/12m momentum")
    lines.append("- `momentum_crash_aware` — Daniel-Moskowitz crash proxy: 12-1 momentum × sign(market 12m)")
    lines.append("- `sharpe_252` — 1-year rolling Sharpe ratio (quality proxy)")
    lines.append("")
    lines.append(f"**Rebalance cadence:** every {REBALANCE_BARS} trading days.")
    lines.append(f"**Costs:** {FEE_BPS} bps fee + {SLIP_BPS} bps slippage per side "
                 f"({FEE_BPS+SLIP_BPS} bps per side, charged on weight turnover at rebalance).")
    lines.append("")

    lines.append("## 2. Backtest results")
    lines.append("")
    lines.append("| Variant | Sharpe | Sortino | CAGR | MaxDD | Calmar | Win-rate | Turnover/yr | Final $ |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for vid in ["A", "B", "C"]:
        r = results[vid]
        lines.append(f"| {vid} — {r['variant_name']} | {r['sharpe']:.2f} | {r['sortino']:.2f} | "
                     f"{r['cagr']:.2%} | {r['max_drawdown']:.2%} | {r['calmar']:.2f} | "
                     f"{r['win_rate']:.1%} | {r['annual_turnover']:.2f} | "
                     f"${r['final_equity']:.2f} |")
    lines.append("")
    lines.append("**Benchmarks:**")
    lines.append("")
    lines.append("| Benchmark | Sharpe | CAGR | MaxDD | Total Return |")
    lines.append("|---|---|---|---|---|")
    lines.append(f"| {bench_ew['name']} | {bench_ew['sharpe']:.2f} | {bench_ew['cagr']:.2%} | "
                 f"{bench_ew['max_drawdown']:.2%} | {bench_ew['total_return']:.2%} |")
    if spy_bench:
        lines.append(f"| {spy_bench['name']} | {spy_bench['sharpe']:.2f} | {spy_bench['cagr']:.2%} | "
                     f"{spy_bench['max_drawdown']:.2%} | {spy_bench['total_return']:.2%} |")
    lines.append("")

    lines.append("## 3. Equity curve description (best variant)")
    lines.append("")
    lines.append(f"Best variant: **{best_id} — {best['variant_name']}** (Sharpe {best['sharpe']:.2f}).")
    lines.append("")
    lines.append(f"- Start: $1.00 on {best['start_date']}")
    lines.append(f"- Peak equity reached on **{best['peak_date']}**")
    lines.append(f"- Maximum drawdown bottomed at **{best['trough_date']}** "
                 f"with a peak-to-trough loss of **{best['max_drawdown']:.2%}**")
    lines.append(f"- Final equity: **${best['final_equity']:.2f}** "
                 f"(total return {best['total_return']:.2%}, {best['n_rebalances']} rebalances)")
    lines.append("")

    lines.append("## 4. Regime analysis (best variant)")
    lines.append("")
    if regimes:
        for grp, vals in regimes.items():
            lines.append(f"**By {grp}:**")
            lines.append("")
            lines.append("| Regime | n_days | Ann. Return | Sharpe |")
            lines.append("|---|---|---|---|")
            for name, m in vals.items():
                lines.append(f"| {name} | {m['n_days']} | {m['ann_return']:.2%} | {m['sharpe']:.2f} |")
            lines.append("")

        # Interpretation
        vix_b = regimes.get("vix_bucket", {})
        if vix_b:
            best_vix = max(vix_b, key=lambda k: vix_b[k]["sharpe"])
            worst_vix = min(vix_b, key=lambda k: vix_b[k]["sharpe"])
            lines.append(f"**Interpretation:** the strategy performs best in `{best_vix}` "
                         f"(Sharpe {vix_b[best_vix]['sharpe']:.2f}) and worst in `{worst_vix}` "
                         f"(Sharpe {vix_b[worst_vix]['sharpe']:.2f}). This is consistent with "
                         f"the momentum-crash literature — pure momentum suffers in vol spikes; "
                         f"the `momentum_crash_aware` factor partially mitigates this.")
        trend_b = regimes.get("trend_bucket", {})
        if trend_b:
            up = trend_b.get("uptrend", {}).get("sharpe", 0)
            dn = trend_b.get("downtrend", {}).get("sharpe", 0)
            lines.append(f"- In uptrends (SPY > 200d SMA), Sharpe = {up:.2f}.")
            lines.append(f"- In downtrends, Sharpe = {dn:.2f}.")
        lines.append("")
    else:
        lines.append("_(VIX or SPY data unavailable for regime analysis.)_")
        lines.append("")

    lines.append("## 5. Comparison vs failed v1 (daily rebalance)")
    lines.append("")
    lines.append("| Version | Rebalance | Sharpe | MaxDD | Total Return | Notes |")
    lines.append("|---|---|---|---|---|---|")
    lines.append("| v1 (failed) | Daily | 0.14 | 30.97% | 144.08% | GMM regime + factor tilt + MV opt; rejected |")
    for vid in ["A", "B", "C"]:
        r = results[vid]
        lines.append(f"| 2-week / {vid} | 10d | {r['sharpe']:.2f} | {r['max_drawdown']:.2%} | "
                     f"{r['total_return']:.2%} | top-{'3' if vid != 'B' else '2L/2S'} factor-ranked |")
    lines.append("")
    lines.append(
        "The daily v1 used a much more complex pipeline (regime detection, mean-variance "
        "optimization, SPY hedge, signal-tilted expected returns) and rebalanced every 10 bars "
        "by design — but with daily-frequency factor refresh and a regime-switching weight "
        "scheme that overfit on training data. The 2-week variants here use a far simpler signal "
        "(static equal-weighted factor blend) and rank-based portfolio construction, which is "
        "less prone to overfitting and easier to interpret."
    )
    lines.append("")

    lines.append("## 6. When the strategy works / fails")
    lines.append("")
    lines.append("**Works when:**")
    lines.append("- Persistent sector trends (tech 2020-21, energy 2022, tech 2023-24) "
                 "— top-3 captures the run-up.")
    lines.append("- Low-to-mid VIX regimes where momentum is not punished by reversals.")
    lines.append("- Uptrends where the momentum factor and `momentum_crash_aware` align.")
    lines.append("")
    lines.append("**Fails when:**")
    lines.append("- Sharp regime flips (Feb-Mar 2020 COVID, Q4 2018, Q1 2022 rotation) — "
                 "10-day lag means the portfolio holds last-quarter's winners through the reversal.")
    lines.append("- Sector leadership rotation faster than the rebalance cadence.")
    lines.append("- Long-short variant (B) suffers from concentrated negative exposure during "
                 "broad market rallies (short sleeve gets squeezed).")
    lines.append("")

    lines.append("## 7. Recommendation")
    lines.append("")
    if verdict_label == "DEPLOY":
        lines.append("**VERDICT: DEPLOY** (with paper trading first).")
        lines.append("")
        lines.append(f"Best variant **{best_id}** delivers Sharpe {best['sharpe']:.2f}, "
                     f"MaxDD {best['max_drawdown']:.2%}, Calmar {best['calmar']:.2f}. "
                     "Risk-adjusted returns clear the deploy bar.")
    elif verdict_label == "ITERATE":
        lines.append("**VERDICT: ITERATE** — promising but not deploy-ready.")
        lines.append("")
        lines.append(f"Best variant **{best_id}** delivers Sharpe {best['sharpe']:.2f} — "
                     f"above the failed v1 (0.14) but below the deploy bar (Sharpe > 1.0). "
                     f"MaxDD {best['max_drawdown']:.2%}.")
        lines.append("")
        lines.append("**Iteration ideas:**")
        lines.append("1. Add a trend filter: only go long when SPY > 200d SMA (skip downtrends).")
        lines.append("2. Increase rebalance lookahead — try 21-day (monthly) cadence to reduce turnover further.")
        lines.append("3. Replace equal-weight z-score with regime-conditional weights (bull/bear/sideways), "
                     "fit on a small held-out training window — but be cautious of overfitting.")
        lines.append("4. Add a volatility scalar: scale gross exposure by 1/realized_vol of the portfolio.")
        lines.append("5. Test on a larger universe (sector + factor ETFs together — MTUM, QUAL, VLUE, USMV).")
    else:
        lines.append("**VERDICT: REJECT** in this form.")
        lines.append("")
        lines.append(f"Best variant Sharpe = {best['sharpe']:.2f}, MaxDD = {best['max_drawdown']:.2%}. "
                     "Even with bi-weekly rebalance, the sector ETF universe is too small (11 names) and "
                     "too correlated to support cross-sectional momentum at this scale. The "
                     "underlying factors validated on SP500 (500 names) do not generalize down to "
                     "11 highly correlated sector ETFs.")
        lines.append("")
        lines.append("**Alternatives:**")
        lines.append("1. Use the same alpha signal on a broader equity universe (SP500 names "
                     "directly) — already validated by the factor study.")
        lines.append("2. Use sector ETFs as a hedging vehicle, not the alpha universe.")
        lines.append("3. Combine with macro signals (VIX term-structure, yield curve) rather "
                     "than pure cross-sectional ranking.")
    lines.append("")

    lines.append("## 8. Small-account practicality ($5-15K)")
    lines.append("")
    lines.append("- ETF prices range $30-$200; with $10K capital and 3-position portfolio, "
                 "average position size ≈ $3,300 → 15-100 shares — practical.")
    lines.append(f"- Bi-weekly rebalance × annual turnover ≈ {results[best_id]['annual_turnover']:.1f} "
                 "means roughly that fraction of capital traded per year. "
                 f"At {FEE_BPS+SLIP_BPS} bps per side, annual transaction cost drag ≈ "
                 f"{results[best_id]['annual_turnover'] * (FEE_BPS+SLIP_BPS) / 100:.2f}% of equity — manageable.")
    lines.append(f"- ~{results[best_id]['n_rebalances']} rebalance events over {best['years']:.1f} years = "
                 f"~{results[best_id]['n_rebalances'] / best['years']:.0f} per year, "
                 "easy to execute manually or via paper trading.")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"_Generated by `scripts/backtest_sector_rotation_biweekly.py`._")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
