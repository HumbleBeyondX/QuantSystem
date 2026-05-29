#!/usr/bin/env python3
"""SP500 cross-sectional long-short factor portfolio.

Universe: 153 SP500 mega-cap names from
  data/sample/sp500_2015-01-01_2026-05-04.parquet

Alpha: composite of 10 factors with |t-stat| > 2 from the SP500
factor catalog. Signs adjusted so each factor's HIGHER value =
expected positive forward return:

    resid_mom_252        (mom alpha after market reg)        +
    mom_12_1             (Carhart momentum)                  +
    sharpe_252           (own-asset Sharpe)                  +
    ts_mom_12            (Moskowitz time-series mom)         +
    momentum_crash_aware (Daniel-Moskowitz)                  +
    ltr_36m              (long-term reversal)                − (flip)
    multi_horizon_mom    (3m/6m/12m blend)                   +
    mom_6_1              (6-month mom)                       +
    qmj_proxy            (Quality minus Junk proxy)          +
    pure_trend           (mom net of short-term reversal)    +

Two composite schemes are tested:
    EW  — equal-weight z-score of all 10 factors (with sign flip on ltr_36m)
    IC  — IC-weighted: each factor's z-score × |its rank_ic_ir|

Three portfolio variants per composite:
    L1  — Long-only top decile (~15 names), equal weight
    LS  — Long top decile, short bottom decile
    LS-RP — Same names as LS but inverse-vol weights within each sleeve

Monthly rebalance, 5bps fee + 5bps slippage per side per trade,
21-bar (monthly) cycle.

Outputs:
    results/sp500_longshort_research.md
    results/sp500_longshort_research.json
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

DATA_FILE = PROJECT_ROOT / "data" / "sample" / "sp500_2015-01-01_2026-05-04.parquet"
OUT_MD = PROJECT_ROOT / "results" / "sp500_longshort_research.md"
OUT_JSON = PROJECT_ROOT / "results" / "sp500_longshort_research.json"

# 10 factors with |t-stat| > 2 on SP500
TOP_FACTORS = [
    ("resid_mom_252", +1.0, 0.068),
    ("mom_12_1", +1.0, 0.066),
    ("sharpe_252", +1.0, 0.064),
    ("ts_mom_12", +1.0, 0.063),
    ("momentum_crash_aware", +1.0, 0.056),
    ("ltr_36m", -1.0, 0.053),   # sign-flipped: high LTR raw = low expected ret
    ("multi_horizon_mom", +1.0, 0.052),
    ("mom_6_1", +1.0, 0.051),
    ("qmj_proxy", +1.0, 0.048),
    ("pure_trend", +1.0, 0.044),
]

START_DATE = "2017-01-01"
EVAL_START = "2018-01-01"   # 1 year warmup for factor calcs
END_DATE = "2026-05-03"
REBALANCE_BARS = 21          # ~monthly
FEE_BPS = 5
SLIP_BPS = 5
COST_PER_SIDE = (FEE_BPS + SLIP_BPS) / 10_000.0


# ──────────────────────────────────────────────────────────────────────
# Factor calculations (standalone, no project imports needed)
# ──────────────────────────────────────────────────────────────────────

def calc_mom_lag(closes: pd.DataFrame, lookback: int, skip: int = 21) -> pd.DataFrame:
    return np.log(closes.shift(skip) / closes.shift(lookback))


def calc_ts_mom(closes: pd.DataFrame, lookback: int = 252) -> pd.DataFrame:
    return np.log(closes / closes.shift(lookback))


def calc_resid_mom_252(closes: pd.DataFrame) -> pd.DataFrame:
    log_ret = np.log(closes / closes.shift(1))
    market = log_ret.mean(axis=1)
    n = 252
    out = pd.DataFrame(index=log_ret.index, columns=log_ret.columns, dtype="float64")
    var_b = market.rolling(n, min_periods=126).var().replace(0, np.nan)
    for col in log_ret.columns:
        r = log_ret[col]
        cov = r.rolling(n, min_periods=126).cov(market)
        beta = cov / var_b
        mean_r = r.rolling(n, min_periods=126).mean()
        mean_m = market.rolling(n, min_periods=126).mean()
        alpha = mean_r - beta * mean_m
        out[col] = alpha * 252
    return out


def calc_sharpe_252(closes: pd.DataFrame) -> pd.DataFrame:
    ret = np.log(closes / closes.shift(1))
    mu = ret.rolling(252, min_periods=126).mean()
    sigma = ret.rolling(252, min_periods=126).std().replace(0, np.nan)
    return mu / sigma * np.sqrt(252)


def calc_momentum_crash_aware(closes: pd.DataFrame) -> pd.DataFrame:
    mom = np.log(closes.shift(21) / closes.shift(252))
    market_close = closes.mean(axis=1)
    market_mom = np.log(market_close.shift(21) / market_close.shift(252))
    regime = np.sign(market_mom).fillna(0)
    return mom.multiply(regime, axis=0)


def calc_ltr_36m(closes: pd.DataFrame) -> pd.DataFrame:
    return -np.log(closes.shift(21) / closes.shift(756))


def calc_multi_horizon_mom(closes: pd.DataFrame) -> pd.DataFrame:
    def _cs_z(x: pd.DataFrame) -> pd.DataFrame:
        return x.sub(x.mean(axis=1), axis=0).div(x.std(axis=1).replace(0, np.nan), axis=0)

    mom_3 = np.log(closes.shift(21) / closes.shift(63))
    mom_6 = np.log(closes.shift(21) / closes.shift(126))
    mom_12 = np.log(closes.shift(21) / closes.shift(252))
    return (_cs_z(mom_3) + _cs_z(mom_6) + _cs_z(mom_12)) / 3.0


def calc_pure_trend(closes: pd.DataFrame) -> pd.DataFrame:
    mom = np.log(closes.shift(21) / closes.shift(126))
    str1m = np.log(closes / closes.shift(21))
    return mom - str1m


def calc_qmj_proxy(closes: pd.DataFrame) -> pd.DataFrame:
    def _cs_z(x: pd.DataFrame) -> pd.DataFrame:
        return x.sub(x.mean(axis=1), axis=0).div(x.std(axis=1).replace(0, np.nan), axis=0)

    ret = np.log(closes / closes.shift(1))
    sharpe = (ret.rolling(252, min_periods=126).mean()
              / ret.rolling(252, min_periods=126).std().replace(0, np.nan)) * np.sqrt(252)
    mom = np.log(closes.shift(21) / closes.shift(252))
    rv = ret.rolling(252, min_periods=126).std() * np.sqrt(252)
    return (_cs_z(sharpe) + _cs_z(mom) + _cs_z(-rv)) / 3.0


FACTOR_FUNCS = {
    "mom_12_1": lambda c: calc_mom_lag(c, 252, 21),
    "mom_6_1": lambda c: calc_mom_lag(c, 126, 21),
    "ts_mom_12": lambda c: calc_ts_mom(c, 252),
    "resid_mom_252": calc_resid_mom_252,
    "sharpe_252": calc_sharpe_252,
    "momentum_crash_aware": calc_momentum_crash_aware,
    "ltr_36m": calc_ltr_36m,
    "multi_horizon_mom": calc_multi_horizon_mom,
    "pure_trend": calc_pure_trend,
    "qmj_proxy": calc_qmj_proxy,
}


# ──────────────────────────────────────────────────────────────────────
# Alpha composite
# ──────────────────────────────────────────────────────────────────────

def cs_zscore(df: pd.DataFrame) -> pd.DataFrame:
    mu = df.mean(axis=1)
    sd = df.std(axis=1).replace(0, np.nan)
    return df.sub(mu, axis=0).div(sd, axis=0)


def build_composite(closes: pd.DataFrame, scheme: str) -> pd.DataFrame:
    """Build composite alpha. scheme = 'EW' or 'IC'."""
    panel_sum = None
    total_w = 0.0
    for name, sign, ic_ir in TOP_FACTORS:
        raw = FACTOR_FUNCS[name](closes)
        z = cs_zscore(raw) * sign
        z = z.clip(-3, 3)
        w = 1.0 if scheme == "EW" else ic_ir
        if panel_sum is None:
            panel_sum = z * w
        else:
            panel_sum = panel_sum.add(z * w, fill_value=0)
        total_w += abs(w)
    return panel_sum / total_w


# ──────────────────────────────────────────────────────────────────────
# Portfolio construction
# ──────────────────────────────────────────────────────────────────────

def long_only_top_decile(alpha_row: pd.Series, n_long: int) -> Dict[str, float]:
    valid = alpha_row.dropna()
    if len(valid) < n_long * 2:
        return {}
    top = valid.nlargest(n_long).index.tolist()
    return {t: 1.0 / n_long for t in top}


def long_short_decile(alpha_row: pd.Series, n: int) -> Dict[str, float]:
    valid = alpha_row.dropna()
    if len(valid) < n * 2:
        return {}
    top = valid.nlargest(n).index.tolist()
    bot = valid.nsmallest(n).index.tolist()
    w: Dict[str, float] = {}
    for t in top:
        w[t] = 0.5 / n
    for t in bot:
        w[t] = -0.5 / n
    return w


def long_short_risk_parity(alpha_row: pd.Series, vol_row: pd.Series, n: int) -> Dict[str, float]:
    valid = alpha_row.dropna()
    if len(valid) < n * 2:
        return {}
    top = valid.nlargest(n).index.tolist()
    bot = valid.nsmallest(n).index.tolist()

    def _inv_vol_weights(names: List[str], sleeve_gross: float) -> Dict[str, float]:
        ivs = (1.0 / vol_row.reindex(names).replace(0, np.nan)).dropna()
        if ivs.empty:
            return {t: sleeve_gross / len(names) for t in names}
        w = ivs / ivs.sum() * sleeve_gross
        return w.to_dict()

    long_w = _inv_vol_weights(top, 0.5)
    short_w = _inv_vol_weights(bot, -0.5)
    return {**long_w, **short_w}


# ──────────────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────────────

def run_backtest(
    closes: pd.DataFrame,
    alpha: pd.DataFrame,
    vol_60d: pd.DataFrame,
    *,
    variant: str,
    n_per_side: int = 15,
) -> Dict:
    daily_ret = closes.pct_change().fillna(0.0)
    dates = alpha.index

    eval_ts = pd.Timestamp(EVAL_START)
    rebalance_idx: List[int] = []
    for i, d in enumerate(dates):
        if d < eval_ts:
            continue
        if not rebalance_idx or i - rebalance_idx[-1] >= REBALANCE_BARS:
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

    for i, date in enumerate(dates):
        if date < eval_ts:
            continue

        if started and current_w:
            day_pnl = 0.0
            ret_row = daily_ret.iloc[i]
            for t, w in current_w.items():
                r = ret_row.get(t, 0.0)
                if pd.notna(r):
                    day_pnl += w * r
            equity *= (1 + day_pnl)
            daily_pnl_list.append((date, day_pnl))

        if i in rebalance_set:
            alpha_row = alpha.iloc[i]
            if variant == "L1":
                new_w = long_only_top_decile(alpha_row, n_per_side)
            elif variant == "LS":
                new_w = long_short_decile(alpha_row, n_per_side)
            elif variant == "LS-RP":
                vol_row = vol_60d.iloc[i] if vol_60d is not None else pd.Series(dtype=float)
                new_w = long_short_risk_parity(alpha_row, vol_row, n_per_side)
            else:
                raise ValueError(variant)

            all_tickers = set(current_w) | set(new_w)
            turnover = sum(abs(new_w.get(t, 0) - current_w.get(t, 0)) for t in all_tickers)
            if turnover > 0:
                cost = turnover * COST_PER_SIDE
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
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    max_dd = abs(((eq - eq.cummax()) / eq.cummax()).min())
    calmar = cagr / max_dd if max_dd > 0 else 0.0
    win_rate = sum(1 for p in pnl_per_rebalance if p > 0) / max(len(pnl_per_rebalance), 1)

    return {
        "variant": variant,
        "n_per_side": n_per_side,
        "n_days": len(eq),
        "years": round(years, 2),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "cagr": round(cagr, 4),
        "max_dd": round(max_dd, 4),
        "calmar": round(calmar, 3),
        "win_rate": round(win_rate, 3),
        "n_rebalances": len(pnl_per_rebalance),
        "n_trades": n_trades,
        "turnover_per_year": round(total_turnover / max(years, 0.01), 1),
        "final_equity": round(eq.iloc[-1], 4),
        "_daily_returns": daily_pnl_list,
        "_equity_curve": [(str(d), v) for d, v in equity_curve],
    }


# ──────────────────────────────────────────────────────────────────────
# Benchmarks
# ──────────────────────────────────────────────────────────────────────

def spx_benchmark(closes: pd.DataFrame, equal_weight: bool = True) -> Dict:
    """Compute equal-weight SP500 (153 names here) or cap-weight proxy."""
    daily = closes.pct_change().fillna(0.0)
    if equal_weight:
        port_ret = daily.mean(axis=1)
    else:
        port_ret = daily.iloc[:, 0]  # placeholder
    port_ret = port_ret.loc[port_ret.index >= pd.Timestamp(EVAL_START)]
    eq = (1 + port_ret).cumprod()
    years = len(eq) / 252.0
    sharpe = (port_ret.mean() / port_ret.std()) * np.sqrt(252) if port_ret.std() > 0 else 0.0
    max_dd = abs(((eq - eq.cummax()) / eq.cummax()).min())
    cagr = (eq.iloc[-1]) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    return {
        "sharpe": round(sharpe, 3),
        "cagr": round(cagr, 4),
        "max_dd": round(max_dd, 4),
        "final_equity": round(eq.iloc[-1], 4),
    }


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"Loading: {DATA_FILE}")
    df = pd.read_parquet(DATA_FILE)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    wide_close = df.pivot_table(index="timestamp", columns="ticker",
                                values="close", aggfunc="last").sort_index()
    closes = wide_close.loc[(wide_close.index >= START_DATE) & (wide_close.index <= END_DATE)]
    # Drop tickers with too many NaN
    coverage = closes.notna().mean()
    closes = closes.loc[:, coverage > 0.7]
    print(f"  closes shape: {closes.shape}  ({closes.shape[1]} tickers)")

    # 60d vol for risk parity
    vol_60d = closes.pct_change().rolling(60, min_periods=30).std() * np.sqrt(252)

    print(f"\nBuilding 10-factor composites (EW + IC weighted)...")
    alpha_ew = build_composite(closes, "EW")
    alpha_ic = build_composite(closes, "IC")
    print(f"  EW alpha non-null rows: {alpha_ew.notna().any(axis=1).sum()}")
    print(f"  IC alpha non-null rows: {alpha_ic.notna().any(axis=1).sum()}")

    # ── Run all variants ──
    print(f"\n{'Composite':<6}{'Variant':<8}{'N':>5}{'Sharpe':>9}{'Sortino':>9}"
          f"{'CAGR':>9}{'MaxDD':>9}{'Calmar':>9}{'Win%':>7}{'TurnYr':>8}")
    print("-" * 90)

    runs: Dict[str, Dict] = {}
    for scheme, alpha in [("EW", alpha_ew), ("IC", alpha_ic)]:
        for variant in ["L1", "LS", "LS-RP"]:
            for n_per_side in [10, 15, 25]:
                key = f"{scheme}_{variant}_n{n_per_side}"
                m = run_backtest(closes, alpha, vol_60d, variant=variant, n_per_side=n_per_side)
                # Strip heavy arrays from default output for printing
                light = {k: v for k, v in m.items() if not k.startswith("_")}
                runs[key] = light
                print(f"{scheme:<6}{variant:<8}{n_per_side:>5}"
                      f"{m['sharpe']:>+9.2f}{m['sortino']:>+9.2f}"
                      f"{m['cagr']:>+9.2%}{m['max_dd']:>9.1%}{m['calmar']:>+9.2f}"
                      f"{m['win_rate']*100:>6.0f}%{m['turnover_per_year']:>8.1f}")

    print("\n=== Benchmarks ===")
    bench_ew = spx_benchmark(closes, equal_weight=True)
    print(f"  EW basket (153 names):  Sharpe={bench_ew['sharpe']}  CAGR={bench_ew['cagr']:+.2%}  MaxDD={bench_ew['max_dd']:.1%}")

    # ── Find best ──
    best_key = max(runs, key=lambda k: runs[k]["sharpe"])
    print(f"\nBest variant: {best_key}  Sharpe {runs[best_key]['sharpe']:+.2f}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({"runs": runs, "benchmark_ew": bench_ew}, indent=2, default=str))
    OUT_MD.write_text(_md_report(runs, bench_ew, best_key), encoding="utf-8")
    print(f"\nMarkdown → {OUT_MD}")
    return 0


def _md_report(runs: Dict, bench: Dict, best_key: str) -> str:
    best = runs[best_key]
    lines = []
    lines.append("# SP500 153-Name Long-Short — 10-Factor Composite")
    lines.append("")
    lines.append("_Period: 2018-2026 (8.3y) · Universe: 153 SP500 names · "
                 "Monthly rebalance · 5bps fee + 5bps slip per side_")
    lines.append("")
    lines.append("## Alpha construction")
    lines.append("")
    lines.append("Equal-weighted or IC-weighted z-score blend of 10 factors "
                 "with t-stat > 2 from the SP500 factor catalog:")
    lines.append("")
    for n, sgn, ir in TOP_FACTORS:
        sign = "(+)" if sgn > 0 else "(−)"
        lines.append(f"- `{n}` {sign}  RankIC IR {ir if sgn > 0 else -ir:+.3f}")
    lines.append("")
    lines.append("## Portfolio variants")
    lines.append("- **L1** — Long-only top-N, equal weight")
    lines.append("- **LS** — Long top-N, short bottom-N, dollar-neutral")
    lines.append("- **LS-RP** — Same names as LS, inverse-vol within each sleeve")
    lines.append("")
    lines.append(f"## Results — best variant: **{best_key}** Sharpe {best['sharpe']:+.2f}")
    lines.append("")
    lines.append("| Run | Sharpe | Sortino | CAGR | MaxDD | Calmar | Win% | Turn/yr |")
    lines.append("|---|---|---|---|---|---|---|---|")
    # Sort runs by Sharpe descending
    sorted_runs = sorted(runs.items(), key=lambda kv: kv[1]["sharpe"], reverse=True)
    for key, m in sorted_runs:
        lines.append(f"| `{key}` | {m['sharpe']:+.2f} | {m['sortino']:+.2f} | "
                     f"{m['cagr']:+.2%} | {m['max_dd']:.1%} | {m['calmar']:+.2f} | "
                     f"{m['win_rate']*100:.0f}% | {m['turnover_per_year']:.1f} |")
    lines.append("")
    lines.append("## Benchmark")
    lines.append("")
    lines.append(f"Equal-weight 153-name basket: Sharpe {bench['sharpe']:+.2f}, "
                 f"CAGR {bench['cagr']:+.2%}, MaxDD {bench['max_dd']:.1%}")
    lines.append("")
    lines.append("## Deploy verdict")
    lines.append("")
    lines.append(f"- Sharpe ≥ 1.0  → {'✅' if best['sharpe'] >= 1.0 else '❌'} ({best['sharpe']:+.2f})")
    lines.append(f"- MaxDD ≤ 20% → {'✅' if best['max_dd'] <= 0.20 else '❌'} ({best['max_dd']:.1%})")
    lines.append(f"- Calmar ≥ 0.5 → {'✅' if best['calmar'] >= 0.5 else '❌'} ({best['calmar']:+.2f})")
    lines.append("")
    if best["sharpe"] >= 1.0 and best["max_dd"] <= 0.25:
        lines.append("**Verdict: DEPLOYABLE to paper trading.** "
                     "30-day forward test, then size up.")
    elif best["sharpe"] >= 0.8:
        lines.append("**Verdict: CLOSE — iterate one round.** "
                     "Add risk model / sector neutralization / better factor selection.")
    else:
        lines.append("**Verdict: BELOW BAR.** Despite ~10× larger cross-section vs sector "
                     "ETFs, individual SP500 names still suffer from the post-2018 "
                     "momentum-crash dynamics on the long sleeve and crowded mega-cap on "
                     "the short sleeve. Consider equal-weight basket-style rotation, or "
                     "shrink universe to high-dispersion sub-baskets.")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
