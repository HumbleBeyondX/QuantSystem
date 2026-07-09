#!/usr/bin/env python3
"""Sector-Neutral Cross-Sectional Quality+Momentum on SP500.

Reattempts the LS approach that failed at Sharpe 0.04 with sector
neutralization — per Asness-Frazzini-Pedersen "Quality Investing" papers
which report Sharpe ~1.5 for sector-neutralized portfolios in US equity.

The prior naive LS failed because:
  In 2018-2026 bull market, ALL sectors trended up together.
  Short bottom-decile-by-momentum stocks across the whole market =
  shorting cyclically-weak stocks that still rose with broader beta.

The fix:
  Within each GICS sector, long top-N momentum/quality, short bottom-N.
  Each sector sleeve is dollar-neutral.
  Cross-sector exposure cancels → captures only relative-strength alpha
  that is genuine.

Apply hard rule: OOS Sharpe ≥ 1.5 or REJECT.

Walk-forward: IS 2018-01-01 → 2021-12-31 (4y), OOS 2022-01-01 → 2026-05-03 (~4.3y).
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

DATA_FILE = PROJECT_ROOT / "data" / "sample" / "sp500_2015-01-01_2026-05-04.parquet"
OUT_MD = PROJECT_ROOT / "results" / "sp500_sector_neutral_qmom.md"
OUT_JSON = PROJECT_ROOT / "results" / "sp500_sector_neutral_qmom.json"


# GICS sector mapping for the 153 SP500 mega-caps in our panel.
# Approximations OK — small mapping errors don't materially change cross-sectional results.
SECTOR_MAP: Dict[str, str] = {
    # Technology
    "AAPL": "Tech", "MSFT": "Tech", "GOOGL": "Tech", "GOOG": "Tech", "META": "Tech",
    "NVDA": "Tech", "AMD": "Tech", "ORCL": "Tech", "CRM": "Tech", "ADBE": "Tech",
    "AVGO": "Tech", "INTC": "Tech", "CSCO": "Tech", "IBM": "Tech", "NOW": "Tech",
    "PYPL": "Tech", "QCOM": "Tech", "TXN": "Tech", "MU": "Tech", "AMAT": "Tech",
    "LRCX": "Tech", "KLAC": "Tech", "PANW": "Tech", "MDB": "Tech", "ACN": "Tech",
    "INTU": "Tech",

    # Consumer Discretionary
    "AMZN": "ConsDisc", "TSLA": "ConsDisc", "HD": "ConsDisc", "MCD": "ConsDisc",
    "NKE": "ConsDisc", "SBUX": "ConsDisc", "LOW": "ConsDisc", "TGT": "ConsDisc",
    "BKNG": "ConsDisc", "CMG": "ConsDisc", "F": "ConsDisc", "GM": "ConsDisc",

    # Consumer Staples
    "COST": "ConsStap", "WMT": "ConsStap", "PG": "ConsStap", "KO": "ConsStap",
    "PEP": "ConsStap", "MDLZ": "ConsStap", "PM": "ConsStap", "MO": "ConsStap",
    "KHC": "ConsStap", "HSY": "ConsStap", "GIS": "ConsStap",

    # Communication
    "DIS": "Comm", "NFLX": "Comm", "CMCSA": "Comm", "T": "Comm",
    "VZ": "Comm", "TMUS": "Comm",

    # Financials
    "JPM": "Fin", "BAC": "Fin", "WFC": "Fin", "GS": "Fin", "MS": "Fin",
    "C": "Fin", "AXP": "Fin", "USB": "Fin", "PNC": "Fin", "TFC": "Fin",
    "BLK": "Fin", "SCHW": "Fin", "COF": "Fin", "MET": "Fin", "PRU": "Fin",
    "AIG": "Fin", "ALL": "Fin", "TRV": "Fin", "PGR": "Fin", "AON": "Fin",
    "MMC": "Fin", "BX": "Fin", "KKR": "Fin", "CME": "Fin", "V": "Fin",
    "MA": "Fin", "BRK-B": "Fin",

    # Healthcare
    "UNH": "Health", "JNJ": "Health", "LLY": "Health", "PFE": "Health",
    "ABBV": "Health", "MRK": "Health", "ABT": "Health", "TMO": "Health",
    "DHR": "Health", "BMY": "Health", "AMGN": "Health", "GILD": "Health",
    "CVS": "Health", "ISRG": "Health", "VRTX": "Health", "REGN": "Health",
    "ZTS": "Health", "MDT": "Health", "ELV": "Health", "BDX": "Health",
    "SYK": "Health", "BSX": "Health",

    # Industrials
    "HON": "Indu", "RTX": "Indu", "UPS": "Indu", "BA": "Indu", "CAT": "Indu",
    "GE": "Indu", "MMM": "Indu", "DE": "Indu", "UNP": "Indu", "FDX": "Indu",
    "ETN": "Indu", "EMR": "Indu", "ITW": "Indu", "WM": "Indu", "CSX": "Indu",
    "NSC": "Indu", "LMT": "Indu", "NOC": "Indu", "GD": "Indu", "PCAR": "Indu",
    "ROP": "Indu", "PH": "Indu",

    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "EOG": "Energy",
    "SLB": "Energy", "OXY": "Energy", "PSX": "Energy", "VLO": "Energy",
    "MPC": "Energy",

    # Materials
    "LIN": "Mat", "APD": "Mat", "SHW": "Mat", "ECL": "Mat", "FCX": "Mat",
    "NEM": "Mat", "NUE": "Mat", "VMC": "Mat",

    # Real Estate
    "PLD": "RE", "AMT": "RE", "CCI": "RE", "EQIX": "RE", "WELL": "RE", "PSA": "RE",

    # Utilities
    "NEE": "Util", "DUK": "Util", "SO": "Util", "AEP": "Util",
    "EXC": "Util", "D": "Util", "SRE": "Util",
}


# Settings
START_DATE = "2017-01-01"
EVAL_IS_START = "2018-01-01"
EVAL_OOS_START = "2022-01-01"
END_DATE = "2026-05-03"
REBALANCE_BARS = 21
FEE_BPS = 5
SLIP_BPS = 5
COST_PER_SIDE = (FEE_BPS + SLIP_BPS) / 10_000.0

# Within each sector, long top N_PER_SECTOR / short bottom N_PER_SECTOR
N_PER_SECTOR = 2


# ──────────────────────────────────────────────────────────────────────
# Factor calcs (subset — focused on what gives sector-neutral alpha)
# ──────────────────────────────────────────────────────────────────────

def calc_mom_12_1(closes: pd.DataFrame) -> pd.DataFrame:
    return np.log(closes.shift(21) / closes.shift(252))


def calc_sharpe_252(closes: pd.DataFrame) -> pd.DataFrame:
    ret = np.log(closes / closes.shift(1))
    mu = ret.rolling(252, min_periods=126).mean()
    sigma = ret.rolling(252, min_periods=126).std().replace(0, np.nan)
    return mu / sigma * np.sqrt(252)


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


def calc_qmj_proxy(closes: pd.DataFrame) -> pd.DataFrame:
    def _cs_z(x): return x.sub(x.mean(axis=1), axis=0).div(x.std(axis=1).replace(0, np.nan), axis=0)
    ret = np.log(closes / closes.shift(1))
    sharpe = (ret.rolling(252, min_periods=126).mean()
              / ret.rolling(252, min_periods=126).std().replace(0, np.nan)) * np.sqrt(252)
    mom = np.log(closes.shift(21) / closes.shift(252))
    rv = ret.rolling(252, min_periods=126).std() * np.sqrt(252)
    return (_cs_z(sharpe) + _cs_z(mom) + _cs_z(-rv)) / 3.0


def build_alpha(closes: pd.DataFrame) -> pd.DataFrame:
    """Equal-weight composite of 4 top SP500 factors."""
    def _cs_z(x): return x.sub(x.mean(axis=1), axis=0).div(x.std(axis=1).replace(0, np.nan), axis=0)
    mom = _cs_z(calc_mom_12_1(closes)).clip(-3, 3)
    sharpe = _cs_z(calc_sharpe_252(closes)).clip(-3, 3)
    resid = _cs_z(calc_resid_mom_252(closes)).clip(-3, 3)
    qmj = _cs_z(calc_qmj_proxy(closes)).clip(-3, 3)
    return (mom + sharpe + resid + qmj) / 4.0


# ──────────────────────────────────────────────────────────────────────
# Sector-neutral portfolio construction
# ──────────────────────────────────────────────────────────────────────

def sector_neutral_weights(
    alpha_row: pd.Series,
    sector_map: Dict[str, str],
    n_per_sector: int = 2,
) -> Dict[str, float]:
    """For each sector, long top-N, short bottom-N (dollar-neutral per sector).
    Sectors with < 2*N tickers are skipped.
    Final portfolio is sum of dollar-neutral sector sleeves, equal-weight across sectors."""
    by_sec: Dict[str, List[Tuple[str, float]]] = {}
    for ticker, val in alpha_row.dropna().items():
        sec = sector_map.get(ticker)
        if sec is None:
            continue
        by_sec.setdefault(sec, []).append((ticker, val))

    weights: Dict[str, float] = {}
    n_active_sectors = sum(1 for s, lst in by_sec.items() if len(lst) >= 2 * n_per_sector)
    if n_active_sectors == 0:
        return {}

    sleeve_gross = 1.0 / n_active_sectors  # gross exposure per sector
    for sec, lst in by_sec.items():
        if len(lst) < 2 * n_per_sector:
            continue
        lst.sort(key=lambda x: x[1], reverse=True)
        longs = lst[:n_per_sector]
        shorts = lst[-n_per_sector:]
        per_pos = sleeve_gross / (2 * n_per_sector)
        for t, _ in longs:
            weights[t] = weights.get(t, 0) + per_pos
        for t, _ in shorts:
            weights[t] = weights.get(t, 0) - per_pos

    return weights


# ──────────────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────────────

def run_backtest(closes: pd.DataFrame, alpha: pd.DataFrame, start: str) -> Dict:
    daily_ret = closes.pct_change().fillna(0.0)
    dates = alpha.index
    start_ts = pd.Timestamp(start)
    rebalance_idx = []
    for i, d in enumerate(dates):
        if d < start_ts:
            continue
        if not rebalance_idx or i - rebalance_idx[-1] >= REBALANCE_BARS:
            rebalance_idx.append(i)
    rb_set = set(rebalance_idx)

    current_w: Dict[str, float] = {}
    equity = 1.0
    eq_curve = []
    daily_pnl = []
    rb_pnls = []
    started = False
    last_rb_equity = equity
    total_turnover = 0.0
    n_trades = 0

    for i, date in enumerate(dates):
        if date < start_ts:
            continue
        if started and current_w:
            ret_row = daily_ret.iloc[i]
            dp = sum(current_w.get(t, 0) * ret_row.get(t, 0) for t in current_w)
            equity *= (1 + dp)
            daily_pnl.append((date, dp))
        if i in rb_set:
            new_w = sector_neutral_weights(alpha.iloc[i], SECTOR_MAP, N_PER_SECTOR)
            all_t = set(current_w) | set(new_w)
            turnover = sum(abs(new_w.get(t, 0) - current_w.get(t, 0)) for t in all_t)
            if turnover > 0:
                equity *= (1.0 - turnover * COST_PER_SIDE)
                total_turnover += turnover
                n_trades += len(all_t)
            if started:
                rb_pnls.append(equity - last_rb_equity)
            last_rb_equity = equity
            current_w = new_w
            started = True
        eq_curve.append((date, equity))

    if not eq_curve:
        return {"error": "no_data"}

    eq_df = pd.DataFrame(eq_curve, columns=["date", "equity"]).set_index("date")
    eq = eq_df["equity"]
    dr = eq.pct_change().dropna()
    years = len(eq) / 252.0
    sharpe = (dr.mean() / dr.std()) * np.sqrt(252) if dr.std() > 0 else 0.0
    downside = dr[dr < 0].std()
    sortino = (dr.mean() / downside) * np.sqrt(252) if downside > 0 else 0.0
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    max_dd = abs(((eq - eq.cummax()) / eq.cummax()).min())
    calmar = cagr / max_dd if max_dd > 0 else 0.0
    win_rate = sum(1 for p in rb_pnls if p > 0) / max(len(rb_pnls), 1)

    return {
        "n_days": len(eq), "years": round(years, 2),
        "sharpe": round(sharpe, 3), "sortino": round(sortino, 3),
        "cagr": round(cagr, 4), "max_dd": round(max_dd, 4),
        "calmar": round(calmar, 3), "win_rate": round(win_rate, 3),
        "n_rebalances": len(rb_pnls), "n_trades": n_trades,
        "turnover_per_year": round(total_turnover / max(years, 0.01), 1),
        "final_equity": round(eq.iloc[-1], 4),
    }


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"Loading: {DATA_FILE}")
    df = pd.read_parquet(DATA_FILE)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    wide = df.pivot_table(index="timestamp", columns="ticker",
                          values="close", aggfunc="last").sort_index()
    closes = wide.loc[(wide.index >= START_DATE) & (wide.index <= END_DATE)]
    coverage = closes.notna().mean()
    closes = closes.loc[:, coverage > 0.7]

    # Restrict to tickers in our sector map
    mapped = [t for t in closes.columns if t in SECTOR_MAP]
    closes = closes[mapped]
    print(f"  panel: {closes.shape[0]} dates × {closes.shape[1]} mapped tickers")

    # Sectors covered
    sec_counts = pd.Series([SECTOR_MAP[t] for t in mapped]).value_counts()
    print(f"  sectors: {dict(sec_counts)}")

    print("\nBuilding alpha (mom_12_1 + sharpe_252 + resid_mom_252 + qmj_proxy)...")
    alpha = build_alpha(closes)
    print(f"  alpha non-null rows: {alpha.notna().any(axis=1).sum()}")

    # ── Full sample ──
    print("\n=== Full sample (Eval start 2018) ===")
    full = run_backtest(closes, alpha, EVAL_IS_START)
    print(f"  Sharpe={full['sharpe']:+.2f}  CAGR={full['cagr']:+.2%}  MaxDD={full['max_dd']:.1%}  "
          f"Calmar={full['calmar']:+.2f}  Win={full['win_rate']*100:.0f}%")

    # ── IS / OOS walk-forward ──
    print("\n=== In-Sample (2018-2021) ===")
    is_result = run_backtest(closes, alpha.loc[alpha.index <= EVAL_OOS_START], EVAL_IS_START)
    print(f"  Sharpe={is_result['sharpe']:+.2f}  CAGR={is_result['cagr']:+.2%}  MaxDD={is_result['max_dd']:.1%}")

    print("\n=== Out-of-Sample (2022-2026) ===")
    oos_result = run_backtest(closes, alpha, EVAL_OOS_START)
    print(f"  Sharpe={oos_result['sharpe']:+.2f}  CAGR={oos_result['cagr']:+.2%}  MaxDD={oos_result['max_dd']:.1%}")

    # ── Gate ──
    oos_sharpe = oos_result["sharpe"]
    gate_pass = oos_sharpe >= 1.5
    print(f"\n{'='*60}")
    print(f"OOS Sharpe gate (>= 1.5): {'✅ PASS' if gate_pass else '❌ FAIL'}")
    print(f"  OOS Sharpe: {oos_sharpe:+.2f}")
    print('='*60)

    payload = {
        "full_sample": full,
        "in_sample_2018_2021": is_result,
        "out_of_sample_2022_2026": oos_result,
        "gate_pass": gate_pass,
        "user_gate_threshold": 1.5,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str))

    OUT_MD.write_text(_md_report(payload), encoding="utf-8")
    print(f"\nMarkdown → {OUT_MD}")
    return 0 if gate_pass else 1


def _md_report(d: Dict) -> str:
    full = d["full_sample"]
    is_ = d["in_sample_2018_2021"]
    oos = d["out_of_sample_2022_2026"]
    gp = d["gate_pass"]
    lines = []
    lines.append("# SP500 Sector-Neutral Quality+Momentum")
    lines.append("")
    lines.append("_Long top-2 / short bottom-2 WITHIN each GICS sector. "
                 "Dollar-neutral per sector. 10 sectors covered. Monthly rebalance, "
                 "5+5 bps costs. Alpha = z-score blend of mom_12_1 + sharpe_252 + "
                 "resid_mom_252 + qmj_proxy._")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| Period | Sharpe | Sortino | CAGR | MaxDD | Calmar | Win% | nReb |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for label, m in [("Full sample 2018-2026", full),
                     ("In-Sample 2018-2021", is_),
                     ("Out-of-Sample 2022-2026", oos)]:
        lines.append(f"| **{label}** | {m['sharpe']:+.2f} | {m['sortino']:+.2f} | "
                     f"{m['cagr']:+.2%} | {m['max_dd']:.1%} | {m['calmar']:+.2f} | "
                     f"{m['win_rate']*100:.0f}% | {m['n_rebalances']} |")
    lines.append("")
    lines.append("## Walk-forward gate")
    lines.append("")
    lines.append(f"- User rule: OOS Sharpe ≥ 1.5")
    lines.append(f"- OOS Sharpe: **{oos['sharpe']:+.2f}**")
    lines.append(f"- Result: **{'✅ ACCEPT — deploy candidate' if gp else '❌ REJECT'}**")
    lines.append("")
    if not gp:
        lines.append("## Diagnosis")
        lines.append("")
        degrade = (oos["sharpe"] - is_["sharpe"]) / is_["sharpe"] if is_["sharpe"] != 0 else 0
        lines.append(f"- IS Sharpe {is_['sharpe']:+.2f}, OOS Sharpe {oos['sharpe']:+.2f}")
        lines.append(f"- Walk-forward degradation: {degrade*100:.0f}% of IS Sharpe lost")
        lines.append("")
        lines.append("Sector neutralization removes the bull-market beta exposure that "
                     "made the LS strategy fail at 0.04 — but US mega-cap dispersion "
                     "within sectors is also low (e.g. AAPL vs MSFT both rip). The "
                     "factor stack here may not have enough cross-sectional signal "
                     "after removing sector betas.")
        lines.append("")
        lines.append("Next iterations:")
        lines.append("- Expand universe to mid-caps (Russell 1000) for more dispersion")
        lines.append("- Add price-action factors (volume, intraday range)")
        lines.append("- Test on a subset of high-dispersion sectors only (Energy, Health)")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
