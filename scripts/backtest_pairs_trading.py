#!/usr/bin/env python3
"""Cointegration Pairs Trading (Gatev-Goetzmann-Rouwenhorst 2006 + Avellaneda-Lee 2010).

Strategy
--------
- Universe: SP500 mega-cap names, filtered to top-80 by 252-day dollar volume.
- Pair selection: refreshed every 63 trading days (quarterly).
    * For each candidate pair (a, b), fit OLS log(P_a) = alpha + beta * log(P_b)
      on a trailing 252-day window. Run statsmodels ADF on the residual spread.
    * Keep top 30 pairs ranked by ADF p-value (lowest = strongest cointegration).
    * Require p < 0.05 and reasonable hedge ratio (0.05 < beta < 20).
- Spread:  s_t = log(P_a) - beta * log(P_b)
  z-score:  (s - rolling_mean_60) / rolling_std_60
- Entry:  |z| > 2 (long underperformer, short outperformer)
          Position legs vol-matched: long leg dollar size = short leg dollar size.
- Exits:
    * Mean reversion: |z| < 0.5
    * Broken pair stop: |z| > 4
    * Time stop: 30 trading days
- Capital: equal weight across active pairs (each pair gets gross_cap / N_open notional).
  Gross exposure ceiling: 100% of equity (notional sum across both legs).
- Costs: 5bps fee + 5bps slippage per side, applied per leg on open and close.

Walk-forward
------------
- IS  : 2017-01-01 -> 2021-12-31
- OOS : 2022-01-01 -> 2026-05-04

Gate
----
- OOS Sharpe >= 1.5 -> ACCEPT; else REJECT.

Outputs:
    results/pairs_trading_research.md
    results/pairs_trading_research.json
"""

from __future__ import annotations

import json
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DATA_FILE = PROJECT_ROOT / "data" / "sample" / "sp500_2015-01-01_2026-05-04.parquet"
OUT_MD = PROJECT_ROOT / "results" / "pairs_trading_research.md"
OUT_JSON = PROJECT_ROOT / "results" / "pairs_trading_research.json"

# Date ranges
WARMUP_START = "2015-01-01"
IS_START = "2017-01-01"
IS_END = "2021-12-31"
OOS_START = "2022-01-01"
OOS_END = "2026-05-04"

# Universe
TOP_LIQUID = 80                  # top-N by dollar volume
N_PAIRS_KEEP = 30                # pairs to trade
REFRESH_BARS = 63                # quarterly pair re-selection
SELECTION_WINDOW = 252           # trailing window for ADF / OLS
ADF_PVAL_MAX = 0.05
HEDGE_MIN = 0.05
HEDGE_MAX = 20.0

# Trading parameters
ZWIN = 60                        # z-score rolling window
ZENTRY = 2.0
ZEXIT = 0.5
ZSTOP = 4.0
MAX_HOLD = 30                    # bars

# Costs
COST_BPS_PER_SIDE = 10.0 / 10_000.0  # 5bps fee + 5bps slip per side

# Capital
GROSS_CAP = 1.0                  # 100% gross exposure target


# ──────────────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────────────

def load_panel() -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Return (closes_wide, dollar_vol_wide) covering WARMUP_START..OOS_END."""
    print(f"Loading: {DATA_FILE}")
    df = pd.read_parquet(DATA_FILE)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df[(df["timestamp"] >= WARMUP_START) & (df["timestamp"] <= OOS_END)]

    closes = df.pivot_table(index="timestamp", columns="ticker",
                            values="close", aggfunc="last").sort_index()
    vols = df.pivot_table(index="timestamp", columns="ticker",
                          values="volume", aggfunc="last").sort_index()

    # Drop tickers with poor coverage
    coverage = closes.notna().mean()
    keep_cols = coverage[coverage > 0.95].index
    closes = closes[keep_cols]
    vols = vols.reindex(columns=keep_cols)

    # Forward-fill gaps to keep series usable, but drop any rows that
    # remain entirely NaN (early bars before listing).
    closes = closes.ffill(limit=5)
    vols = vols.ffill(limit=5)
    print(f"  closes shape: {closes.shape}")
    return closes, vols


# ──────────────────────────────────────────────────────────────────────
# Pair selection
# ──────────────────────────────────────────────────────────────────────

from statsmodels.tsa.stattools import adfuller


def _ols_hedge(log_a: np.ndarray, log_b: np.ndarray) -> Tuple[float, float]:
    """OLS log(P_a) = alpha + beta * log(P_b). Returns (alpha, beta)."""
    X = np.column_stack([np.ones_like(log_b), log_b])
    beta_vec, *_ = np.linalg.lstsq(X, log_a, rcond=None)
    return float(beta_vec[0]), float(beta_vec[1])


def _adf_pvalue(series: np.ndarray) -> float:
    try:
        # maxlag=1 keeps it fast; autolag=None to skip BIC search
        res = adfuller(series, maxlag=1, autolag=None, regression="c")
        return float(res[1])
    except Exception:
        return 1.0


def _half_life(spread: np.ndarray) -> float:
    """OU half-life via lag-1 regression: ds = lambda*(mu - s) + eps."""
    s = spread[~np.isnan(spread)]
    if len(s) < 30:
        return np.nan
    s_lag = s[:-1]
    ds = np.diff(s)
    X = np.column_stack([np.ones_like(s_lag), s_lag])
    try:
        coef, *_ = np.linalg.lstsq(X, ds, rcond=None)
        lam = coef[1]
        if lam >= 0:
            return np.nan
        hl = -np.log(2) / lam
        return float(hl)
    except Exception:
        return np.nan


def select_top_liquid(closes: pd.DataFrame, vols: pd.DataFrame,
                      asof_idx: int, lookback: int = 252) -> List[str]:
    start = max(0, asof_idx - lookback)
    win_c = closes.iloc[start:asof_idx]
    win_v = vols.iloc[start:asof_idx]
    dv = (win_c * win_v).mean(axis=0)
    dv = dv.dropna()
    if dv.empty:
        return []
    top = dv.nlargest(TOP_LIQUID).index.tolist()
    # Need full data in the selection window
    valid = []
    for t in top:
        col = closes.iloc[start:asof_idx][t]
        if col.notna().sum() >= lookback * 0.95:
            valid.append(t)
    return valid


@dataclass
class Pair:
    a: str
    b: str
    beta: float
    alpha: float
    pval: float
    half_life: float


def select_pairs(closes: pd.DataFrame, vols: pd.DataFrame, asof_idx: int) -> List[Pair]:
    universe = select_top_liquid(closes, vols, asof_idx, SELECTION_WINDOW)
    if len(universe) < 10:
        return []
    start = max(0, asof_idx - SELECTION_WINDOW)
    win = closes.iloc[start:asof_idx][universe]
    log_win = np.log(win)

    # Drop columns with any NaN in window for clean cointegration test
    log_win = log_win.dropna(axis=1, how="any")
    cols = log_win.columns.tolist()
    arr = log_win.values  # (T, N)

    candidates: List[Pair] = []

    # Quick screen: rank correlation pre-filter — only test pairs with
    # |corr| > 0.5 to keep runtime manageable.
    ret = np.diff(arr, axis=0)
    if ret.shape[0] < 30:
        return []
    rc = np.corrcoef(ret.T)
    np.fill_diagonal(rc, 0.0)

    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            if abs(rc[i, j]) < 0.5:
                continue
            la = arr[:, i]
            lb = arr[:, j]
            alpha, beta = _ols_hedge(la, lb)
            if not (HEDGE_MIN <= beta <= HEDGE_MAX):
                continue
            spread = la - beta * lb
            pval = _adf_pvalue(spread)
            if pval > ADF_PVAL_MAX:
                continue
            hl = _half_life(spread)
            if not (1 < hl < 60):
                continue
            candidates.append(Pair(cols[i], cols[j], beta, alpha, pval, hl))

    candidates.sort(key=lambda p: p.pval)
    return candidates[:N_PAIRS_KEEP]


# ──────────────────────────────────────────────────────────────────────
# Backtest engine
# ──────────────────────────────────────────────────────────────────────

@dataclass
class Position:
    pair: Pair
    direction: int          # +1: long A short B  ; -1: long B short A
    entry_idx: int
    entry_z: float
    # notionals locked at entry (as fraction of equity)
    notional_a: float       # signed; long positive
    notional_b: float       # signed; short negative
    entry_price_a: float
    entry_price_b: float
    cost_open: float        # total dollar cost (as fraction of equity)


def run_backtest(
    closes: pd.DataFrame,
    vols: pd.DataFrame,
    dates: pd.DatetimeIndex,
    start_idx: int,
    end_idx: int,
    label: str,
) -> Dict:
    """Run pairs trading between start_idx..end_idx (inclusive).

    Pairs are refreshed every REFRESH_BARS using the trailing window
    available up to that bar. The first selection happens at start_idx.
    """
    print(f"\n=== {label}: bars {start_idx}..{end_idx} "
          f"({dates[start_idx].date()} → {dates[end_idx].date()}) ===")

    log_closes = np.log(closes)

    pairs: List[Pair] = []
    last_refresh = -10**9
    open_positions: Dict[Tuple[str, str], Position] = {}

    equity = 1.0
    equity_curve: List[float] = [equity]
    daily_rets: List[float] = []

    # bookkeeping
    closed_trades: List[Dict] = []
    refresh_records: List[Dict] = []
    bars_pairs_active: List[int] = []

    # Track per-pair P&L for top/bottom ranking
    pair_stats: Dict[Tuple[str, str], Dict[str, float]] = {}

    for t in range(start_idx, end_idx + 1):
        bar_date = dates[t]

        # Pair refresh
        if t - last_refresh >= REFRESH_BARS:
            # Close any open positions before refresh? -> keep them open;
            # they'll exit by their own rules.
            new_pairs = select_pairs(closes, vols, t)
            pairs = new_pairs
            last_refresh = t
            refresh_records.append({
                "date": str(bar_date.date()),
                "n_pairs": len(pairs),
                "avg_pval": float(np.mean([p.pval for p in pairs])) if pairs else None,
                "avg_half_life": float(np.mean([p.half_life for p in pairs])) if pairs else None,
            })
            print(f"  [refresh] {bar_date.date()}  pairs={len(pairs)}"
                  + (f"  avg_pval={refresh_records[-1]['avg_pval']:.3f}"
                     f"  avg_hl={refresh_records[-1]['avg_half_life']:.1f}"
                     if pairs else ""))

        bars_pairs_active.append(len(pairs))

        if t == 0 or not pairs:
            equity_curve.append(equity)
            daily_rets.append(0.0)
            continue

        # ── 1. Compute spread + z for each pair using prices through t-1 ──
        # We use info available at close of bar t-1 to act at bar t open.
        # Approximation: use close-to-close PnL, entries/exits at close of t.
        # Build z based on prior ZWIN bars including up to t.
        z_now: Dict[Tuple[str, str], Tuple[float, float, float]] = {}
        # (z, price_a, price_b)
        for p in pairs:
            key = (p.a, p.b)
            window_start = max(0, t - ZWIN + 1)
            la = log_closes.iloc[window_start:t + 1][p.a].values
            lb = log_closes.iloc[window_start:t + 1][p.b].values
            if len(la) < 20 or np.isnan(la[-1]) or np.isnan(lb[-1]):
                continue
            spread = la - p.beta * lb
            mu = np.nanmean(spread)
            sd = np.nanstd(spread)
            if sd <= 1e-9:
                continue
            z = (spread[-1] - mu) / sd
            pa = closes.iloc[t][p.a]
            pb = closes.iloc[t][p.b]
            if not (np.isfinite(pa) and np.isfinite(pb) and pa > 0 and pb > 0):
                continue
            z_now[key] = (float(z), float(pa), float(pb))

        # ── 2. Mark-to-market open positions (apply daily return to equity) ──
        # Compute return contribution from holding period since previous close.
        ret_today = 0.0
        for key, pos in list(open_positions.items()):
            pa_today = closes.iloc[t][pos.pair.a]
            pb_today = closes.iloc[t][pos.pair.b]
            pa_prev = closes.iloc[t - 1][pos.pair.a]
            pb_prev = closes.iloc[t - 1][pos.pair.b]
            if not all(np.isfinite([pa_today, pb_today, pa_prev, pb_prev])):
                # Mark to NaN — assume zero return this bar
                continue
            # notional_a and notional_b are signed fractions of equity at entry.
            # We don't rebalance intraperiod — fixed share count style.
            # daily P&L on leg = notional * (P_today / P_prev - 1)
            r_a = pa_today / pa_prev - 1.0
            r_b = pb_today / pb_prev - 1.0
            pnl_frac = pos.notional_a * r_a + pos.notional_b * r_b
            ret_today += pnl_frac

        # Update equity from mark-to-market
        equity *= (1.0 + ret_today)
        equity_curve.append(equity)
        daily_rets.append(ret_today)

        # ── 3. Check exits for open positions ──
        for key, pos in list(open_positions.items()):
            z_info = z_now.get(key)
            held = t - pos.entry_idx
            should_exit = False
            reason = ""
            if z_info is None:
                # data missing — force exit
                should_exit = True
                reason = "data_missing"
            else:
                z, _, _ = z_info
                if abs(z) < ZEXIT:
                    should_exit = True
                    reason = "mean_revert"
                elif abs(z) > ZSTOP:
                    should_exit = True
                    reason = "blowout_stop"
                elif held >= MAX_HOLD:
                    should_exit = True
                    reason = "time_stop"

            if should_exit:
                # Apply close cost on both legs (gross notional * cost)
                gross = abs(pos.notional_a) + abs(pos.notional_b)
                cost_close = gross * COST_BPS_PER_SIDE
                equity *= (1.0 - cost_close)
                daily_rets[-1] -= cost_close

                # Track trade P&L (approx): total return on position since entry
                # = (notional_a * (Pa_now/Pa_entry - 1) + notional_b * (Pb_now/Pb_entry - 1))
                #   - cost_open - cost_close
                pa = closes.iloc[t][pos.pair.a]
                pb = closes.iloc[t][pos.pair.b]
                if np.isfinite(pa) and np.isfinite(pb):
                    pnl_total = (
                        pos.notional_a * (pa / pos.entry_price_a - 1.0)
                        + pos.notional_b * (pb / pos.entry_price_b - 1.0)
                        - pos.cost_open - cost_close
                    )
                else:
                    pnl_total = -pos.cost_open - cost_close
                closed_trades.append({
                    "pair": f"{pos.pair.a}-{pos.pair.b}",
                    "entry_date": str(dates[pos.entry_idx].date()),
                    "exit_date": str(bar_date.date()),
                    "direction": pos.direction,
                    "entry_z": pos.entry_z,
                    "held_bars": held,
                    "reason": reason,
                    "pnl_frac": float(pnl_total),
                })
                s = pair_stats.setdefault(key, {"pnl": 0.0, "n": 0, "wins": 0})
                s["pnl"] += pnl_total
                s["n"] += 1
                if pnl_total > 0:
                    s["wins"] += 1
                del open_positions[key]

        # ── 4. Check entries on pairs not currently open ──
        # Slots available
        available = N_PAIRS_KEEP - len(open_positions)
        if available > 0:
            # Rank candidate entries by |z| (strongest signal first)
            candidates = []
            for p in pairs:
                key = (p.a, p.b)
                if key in open_positions:
                    continue
                z_info = z_now.get(key)
                if z_info is None:
                    continue
                z, pa, pb = z_info
                if abs(z) < ZENTRY or abs(z) > ZSTOP:
                    continue
                candidates.append((abs(z), p, z, pa, pb))
            candidates.sort(reverse=True, key=lambda x: x[0])

            for _, p, z, pa, pb in candidates[:available]:
                # Equal weight per pair: each pair gets GROSS_CAP / N_PAIRS_KEEP
                # split equally between long and short leg (notional).
                per_pair_gross = GROSS_CAP / N_PAIRS_KEEP
                leg_size = per_pair_gross / 2.0  # half on each leg
                # Direction: z < -2 means spread too low → A underperforming
                #   → LONG A, SHORT B → direction +1
                #   z > +2 means spread too high → A overperforming → SHORT A, LONG B → direction -1
                if z < 0:
                    direction = +1
                    notional_a = +leg_size
                    notional_b = -leg_size
                else:
                    direction = -1
                    notional_a = -leg_size
                    notional_b = +leg_size

                gross = abs(notional_a) + abs(notional_b)
                cost_open = gross * COST_BPS_PER_SIDE
                equity *= (1.0 - cost_open)
                daily_rets[-1] -= cost_open

                open_positions[(p.a, p.b)] = Position(
                    pair=p,
                    direction=direction,
                    entry_idx=t,
                    entry_z=z,
                    notional_a=notional_a,
                    notional_b=notional_b,
                    entry_price_a=pa,
                    entry_price_b=pb,
                    cost_open=cost_open,
                )

    # Close out any still-open positions at the end (force exit, no cost double-count)
    final_t = end_idx
    for key, pos in list(open_positions.items()):
        gross = abs(pos.notional_a) + abs(pos.notional_b)
        cost_close = gross * COST_BPS_PER_SIDE
        equity *= (1.0 - cost_close)
        if daily_rets:
            daily_rets[-1] -= cost_close
        pa = closes.iloc[final_t][pos.pair.a]
        pb = closes.iloc[final_t][pos.pair.b]
        if np.isfinite(pa) and np.isfinite(pb):
            pnl_total = (
                pos.notional_a * (pa / pos.entry_price_a - 1.0)
                + pos.notional_b * (pb / pos.entry_price_b - 1.0)
                - pos.cost_open - cost_close
            )
        else:
            pnl_total = -pos.cost_open - cost_close
        closed_trades.append({
            "pair": f"{pos.pair.a}-{pos.pair.b}",
            "entry_date": str(dates[pos.entry_idx].date()),
            "exit_date": str(dates[final_t].date()),
            "direction": pos.direction,
            "entry_z": pos.entry_z,
            "held_bars": final_t - pos.entry_idx,
            "reason": "eod_close",
            "pnl_frac": float(pnl_total),
        })
        s = pair_stats.setdefault(key, {"pnl": 0.0, "n": 0, "wins": 0})
        s["pnl"] += pnl_total
        s["n"] += 1
        if pnl_total > 0:
            s["wins"] += 1

    # ── Metrics ──
    rets = np.array(daily_rets)
    if rets.size == 0:
        return {"sharpe": 0.0, "cagr": 0.0, "max_dd": 0.0,
                "equity_end": equity, "trades": closed_trades,
                "pair_stats": pair_stats,
                "refresh_records": refresh_records,
                "avg_pairs_active": 0.0,
                "n_trades": 0, "win_rate": 0.0, "label": label}

    mu = rets.mean()
    sd = rets.std(ddof=1) if rets.size > 1 else 0.0
    sharpe = (mu / sd) * np.sqrt(252) if sd > 1e-12 else 0.0
    n_years = max(rets.size / 252.0, 1e-9)
    cagr = equity ** (1.0 / n_years) - 1.0

    eq_arr = np.array(equity_curve)
    peaks = np.maximum.accumulate(eq_arr)
    dd = (eq_arr / peaks) - 1.0
    max_dd = float(dd.min()) if dd.size else 0.0

    n_trades = len(closed_trades)
    wins = sum(1 for c in closed_trades if c["pnl_frac"] > 0)
    win_rate = wins / n_trades if n_trades else 0.0

    return {
        "label": label,
        "sharpe": float(sharpe),
        "cagr": float(cagr),
        "max_dd": max_dd,
        "equity_end": float(equity),
        "n_trades": n_trades,
        "win_rate": float(win_rate),
        "avg_pairs_active": float(np.mean(bars_pairs_active)) if bars_pairs_active else 0.0,
        "trades": closed_trades,
        "pair_stats": pair_stats,
        "refresh_records": refresh_records,
        "daily_rets": rets.tolist(),
        "equity_curve": eq_arr.tolist(),
    }


# ──────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────

def top_pairs(stats: Dict[Tuple[str, str], Dict[str, float]], k: int, reverse: bool) -> List[Dict]:
    rows = []
    for (a, b), s in stats.items():
        if s["n"] == 0:
            continue
        rows.append({
            "pair": f"{a}-{b}",
            "pnl_frac": s["pnl"],
            "n_trades": s["n"],
            "win_rate": s["wins"] / s["n"],
        })
    rows.sort(key=lambda r: r["pnl_frac"], reverse=reverse)
    return rows[:k]


def build_report(is_res: Dict, oos_res: Dict) -> str:
    gate_pass = oos_res["sharpe"] >= 1.5
    verdict = "ACCEPT" if gate_pass else "REJECT"

    def fmt_pair_list(plist: List[Dict]) -> str:
        if not plist:
            return "  (none)\n"
        out = ""
        for r in plist:
            out += (f"  - {r['pair']:<14}  P&L={r['pnl_frac']*100:+6.2f}%  "
                    f"n={r['n_trades']:>3}  win={r['win_rate']*100:>5.1f}%\n")
        return out

    top_oos = top_pairs(oos_res["pair_stats"], 5, reverse=True)
    bot_oos = top_pairs(oos_res["pair_stats"], 5, reverse=False)

    # Avg half-life across refreshes (OOS)
    hl_vals = [r["avg_half_life"] for r in oos_res["refresh_records"]
               if r.get("avg_half_life") is not None]
    avg_hl_oos = float(np.mean(hl_vals)) if hl_vals else float("nan")

    npairs_vals = [r["n_pairs"] for r in oos_res["refresh_records"]]
    avg_npairs_oos = float(np.mean(npairs_vals)) if npairs_vals else 0.0

    diagnosis = ""
    if not gate_pass:
        cost_drag_per_trade = 2.0 * COST_BPS_PER_SIDE  # roundtrip both legs / leg
        diagnosis = (
            "\n## Diagnosis (REJECTED)\n\n"
            f"- OOS Sharpe ({oos_res['sharpe']:.2f}) fell short of the 1.5 hurdle.\n"
            f"- Per-roundtrip cost drag ≈ {cost_drag_per_trade*100:.2f}% of notional.\n"
            f"- With {oos_res['n_trades']} trades over the OOS window, "
            f"gross-of-cost edge is being eaten by the 5+5bps assumption.\n"
            "- Likely causes: (a) cointegration regime breaks in OOS "
            "(many pairs were 'broken' by the 2022 rate shock / 2023 AI bubble), "
            "(b) z-score crossings cluster post-cost — high turnover, low edge, "
            "(c) hedge ratios fitted on 252d are stale during regime change.\n"
            "- Mitigations to try next iteration: Kalman-filtered hedge ratios, "
            "sector-constrained pair pool, longer z-window (90-120d), entry threshold |z|>2.5, "
            "and a wider blowout stop (5-6 sigma) to avoid stopping at the worst time.\n"
        )

    md = f"""# Cointegration Pairs Trading (Gatev/GGR 2006 + Avellaneda-Lee 2010)

## Verdict: **{verdict}**  (OOS Sharpe = {oos_res['sharpe']:.2f}, gate ≥ 1.5)

## Setup
- Universe: SP500 mega-caps, top {TOP_LIQUID} by trailing 252d dollar volume.
- Pair selection: ADF on OLS residual log-spread (p<{ADF_PVAL_MAX}), {N_PAIRS_KEEP} pairs.
- Refresh: every {REFRESH_BARS} trading days.
- z-score window: {ZWIN} bars. Entry |z|>{ZENTRY}, exit |z|<{ZEXIT}, stop |z|>{ZSTOP}, time stop {MAX_HOLD} bars.
- Costs: 5bps + 5bps per side per leg.
- Gross cap: {GROSS_CAP*100:.0f}% of equity, equal-weight across {N_PAIRS_KEEP} pair slots.

## Aggregate Results

| Period | Sharpe | CAGR | MaxDD | Trades | WinRate | AvgPairsActive |
|--------|-------:|-----:|------:|-------:|--------:|---------------:|
| IS  (2017-2021) | {is_res['sharpe']:+.2f} | {is_res['cagr']:+.2%} | {is_res['max_dd']:.1%} | {is_res['n_trades']} | {is_res['win_rate']*100:.1f}% | {is_res['avg_pairs_active']:.1f} |
| OOS (2022-2026) | {oos_res['sharpe']:+.2f} | {oos_res['cagr']:+.2%} | {oos_res['max_dd']:.1%} | {oos_res['n_trades']} | {oos_res['win_rate']*100:.1f}% | {oos_res['avg_pairs_active']:.1f} |

## Pair Selection Health (OOS)
- Quarterly refresh count: {len(oos_res['refresh_records'])}
- Avg pairs identified per refresh: {avg_npairs_oos:.1f} / {N_PAIRS_KEEP}
- Avg pair half-life: {avg_hl_oos:.1f} bars

## Top 5 OOS Pairs (by total P&L)
{fmt_pair_list(top_oos)}
## Worst 5 OOS Pairs (broken cointegrations)
{fmt_pair_list(bot_oos)}
{diagnosis}
"""
    return md


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    t0 = time.time()
    closes, vols = load_panel()
    dates = closes.index

    def _idx_of(d: str) -> int:
        ts = pd.Timestamp(d)
        # Find first idx with date >= ts
        for i, x in enumerate(dates):
            if x >= ts:
                return i
        return len(dates) - 1

    is_start = _idx_of(IS_START)
    is_end = _idx_of(IS_END)
    oos_start = _idx_of(OOS_START)
    oos_end = min(_idx_of(OOS_END), len(dates) - 1)

    print(f"IS:  idx {is_start}..{is_end}  ({dates[is_start].date()} → {dates[is_end].date()})")
    print(f"OOS: idx {oos_start}..{oos_end}  ({dates[oos_start].date()} → {dates[oos_end].date()})")

    is_res = run_backtest(closes, vols, dates, is_start, is_end, "IS")
    oos_res = run_backtest(closes, vols, dates, oos_start, oos_end, "OOS")

    # Build report
    md = build_report(is_res, oos_res)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(md, encoding="utf-8")

    # JSON dump (strip heavy arrays for compactness, keep summary + trades)
    def _light(r: Dict) -> Dict:
        return {
            "label": r["label"],
            "sharpe": r["sharpe"],
            "cagr": r["cagr"],
            "max_dd": r["max_dd"],
            "equity_end": r["equity_end"],
            "n_trades": r["n_trades"],
            "win_rate": r["win_rate"],
            "avg_pairs_active": r["avg_pairs_active"],
            "refresh_records": r["refresh_records"],
            "pair_stats": {f"{a}__{b}": v for (a, b), v in r["pair_stats"].items()},
            "n_first_50_trades": r["trades"][:50],
        }

    out = {
        "config": {
            "TOP_LIQUID": TOP_LIQUID,
            "N_PAIRS_KEEP": N_PAIRS_KEEP,
            "REFRESH_BARS": REFRESH_BARS,
            "SELECTION_WINDOW": SELECTION_WINDOW,
            "ZWIN": ZWIN, "ZENTRY": ZENTRY, "ZEXIT": ZEXIT, "ZSTOP": ZSTOP,
            "MAX_HOLD": MAX_HOLD,
            "COST_BPS_PER_SIDE": COST_BPS_PER_SIDE,
            "GROSS_CAP": GROSS_CAP,
            "IS_START": IS_START, "IS_END": IS_END,
            "OOS_START": OOS_START, "OOS_END": OOS_END,
        },
        "is": _light(is_res),
        "oos": _light(oos_res),
        "verdict": "ACCEPT" if oos_res["sharpe"] >= 1.5 else "REJECT",
        "runtime_sec": time.time() - t0,
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    print(f"\n[done] runtime {time.time() - t0:.1f}s")
    print(f"  IS  Sharpe={is_res['sharpe']:+.2f}  CAGR={is_res['cagr']:+.2%}"
          f"  MaxDD={is_res['max_dd']:.1%}  Trades={is_res['n_trades']}"
          f"  Win={is_res['win_rate']*100:.1f}%")
    print(f"  OOS Sharpe={oos_res['sharpe']:+.2f}  CAGR={oos_res['cagr']:+.2%}"
          f"  MaxDD={oos_res['max_dd']:.1%}  Trades={oos_res['n_trades']}"
          f"  Win={oos_res['win_rate']*100:.1f}%")
    print(f"  Verdict: {out['verdict']}")
    print(f"  Wrote: {OUT_MD}")
    print(f"         {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
