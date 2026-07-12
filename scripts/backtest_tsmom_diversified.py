#!/usr/bin/env python3
"""Time Series Momentum (TSMOM) diversified portfolio backtest.

Implementation follows Moskowitz, Ooi, Pedersen (2012) "Time series momentum",
Journal of Financial Economics.

Strategy
--------
For each asset i, on each rebalance date:
    signal_i[t]     = sign( P_i[t] / P_i[t - 252] - 1 )           # 12-month TS momentum
    sigma_i[t]      = realized vol of daily returns, 60-day window
                      annualised: sigma_i[t] = std(r_i) * sqrt(252)
    position_i[t]   = (target_vol / sigma_i[t]) * signal_i[t]    # vol-targeted leg
    weight_i[t]     = position_i[t] / N_assets                    # equal-weight aggregation

target_vol  = 0.10  (10% annual per leg)
N_assets    = 10
Rebalance every 21 trading days (~monthly).

Costs: 5 bps fee + 5 bps slippage per side, charged on |delta_weight| per leg.

Walk-forward
------------
IS  = 2015-01-01 -> 2021-12-31   (parameter sanity-check / reporting only;
                                  no fitting — the rules are fully specified
                                  by the paper)
OOS = 2022-01-01 -> 2026-05-04

Gate: OOS Sharpe >= 1.5 -> ACCEPT, else REJECT.
"""

from __future__ import annotations

import json
import math
import sys
import time
import warnings
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "tsmom"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UNIVERSE: Dict[str, str] = {
    # equity
    "SPY": "US large-cap equity",
    "EFA": "DM ex-US equity",
    "EEM": "EM equity",
    # fixed income
    "TLT": "20+ year US Treasuries",
    "IEF": "7-10 year US Treasuries",
    # commodities
    "GLD": "Gold",
    "USO": "WTI crude oil",
    "DBC": "Broad commodities",
    # FX
    "UUP": "USD bullish",
    "FXE": "EUR vs USD",
}
TICKERS = list(UNIVERSE.keys())

LOOKBACK_MOM = 252          # 12-month momentum
LOOKBACK_VOL = 60           # 60-day realized vol window
TARGET_VOL = 0.10           # 10% annual per leg
REBALANCE_EVERY = 21        # trading days
FEE_BPS = 5.0               # per side
SLIP_BPS = 5.0              # per side
COST_PER_SIDE = (FEE_BPS + SLIP_BPS) / 1e4   # 10 bps = 0.001 per side per leg

FETCH_START = "2013-01-01"  # need 252d before 2015-01-01 -> ~year of lead-in
FETCH_END = "2026-05-04"

IS_START = "2015-01-01"
IS_END = "2021-12-31"
OOS_START = "2022-01-01"
OOS_END = "2026-05-04"

GATE_OOS_SHARPE = 1.5


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _download_one(ticker: str, start: str, end: str) -> pd.Series:
    """Download adjusted close for `ticker`. Caches to parquet."""
    import yfinance as yf

    cache_path = CACHE_DIR / f"{ticker}.parquet"
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        if df.index.max() >= pd.Timestamp(end) - pd.Timedelta(days=7):
            return df["adj_close"]

    df = yf.download(
        ticker,
        start=start,
        end=end,
        progress=False,
        auto_adjust=True,
        threads=False,
    )
    if df is None or df.empty:
        raise RuntimeError(f"yfinance returned empty data for {ticker}")
    # auto_adjust=True -> 'Close' is already adjusted
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    series = df["Close"].rename("adj_close").to_frame()
    series.index.name = "date"
    series.to_parquet(cache_path)
    return series["adj_close"]


def load_prices(tickers: List[str], start: str, end: str) -> pd.DataFrame:
    """Return wide adj-close DataFrame, dates x tickers, forward-filled."""
    series = {}
    for t in tickers:
        s = _download_one(t, start, end)
        series[t] = s
        print(f"  loaded {t}: {len(s)} obs, {s.index.min().date()} -> {s.index.max().date()}")
    df = pd.concat(series, axis=1)
    df = df.sort_index().ffill()
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    return df


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

def compute_signal(prices: pd.DataFrame) -> pd.DataFrame:
    """sign of 12-month return per asset, computed daily."""
    past = prices.shift(LOOKBACK_MOM)
    mom = prices / past - 1.0
    return np.sign(mom)


def compute_vol(prices: pd.DataFrame) -> pd.DataFrame:
    """Annualised realized vol from 60-day daily returns."""
    rets = prices.pct_change()
    vol = rets.rolling(LOOKBACK_VOL).std() * math.sqrt(252.0)
    # safety floor
    return vol.where(vol > 1e-6, 1e-6)


def build_positions(prices: pd.DataFrame) -> pd.DataFrame:
    """Per-asset position (in units of dollar exposure / portfolio NAV).

    Rebalanced every 21 days; held constant between rebalances.
    """
    signal = compute_signal(prices)
    vol = compute_vol(prices)

    raw_pos = (TARGET_VOL / vol) * signal
    raw_pos = raw_pos.where(np.isfinite(raw_pos), 0.0)

    # rebalance grid: hold constant between rebalances
    positions = pd.DataFrame(index=prices.index, columns=prices.columns, dtype=float)
    last_pos = pd.Series(0.0, index=prices.columns)
    counter = 0
    start_idx = max(LOOKBACK_MOM, LOOKBACK_VOL)
    for i, date in enumerate(prices.index):
        if i < start_idx:
            positions.loc[date] = 0.0
            continue
        if counter % REBALANCE_EVERY == 0:
            last_pos = raw_pos.loc[date].fillna(0.0).copy()
        positions.loc[date] = last_pos
        counter += 1
    return positions


def backtest(prices: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Run the TSMOM portfolio.

    Returns:
        portfolio: DataFrame with [gross_ret, net_ret, equity]
        leg_returns: per-asset net daily returns (after costs)
    """
    positions = build_positions(prices)            # (T, N) -- weights applied to NEXT day's return
    rets = prices.pct_change().fillna(0.0)         # (T, N)

    # Daily PnL using yesterday's position (to avoid look-ahead)
    pos_lag = positions.shift(1).fillna(0.0)
    gross_leg = pos_lag * rets                     # per-asset gross daily return contribution

    # Equal-weight aggregation: divide by N
    n = float(len(prices.columns))
    gross_port = gross_leg.sum(axis=1) / n

    # Costs: per-leg turnover at rebalances, charged as COST_PER_SIDE on |delta weight|
    # We charge on the leg level then divide by N (same as charging on the aggregated weight).
    dpos = positions.diff().abs().fillna(0.0)
    costs_leg = dpos * COST_PER_SIDE
    costs_port = costs_leg.sum(axis=1) / n

    net_port = gross_port - costs_port
    net_leg = (gross_leg - costs_leg) / 1.0  # leg-level net contribution (not divided by N for inspection)

    equity = (1.0 + net_port).cumprod()
    portfolio = pd.DataFrame(
        {
            "gross_ret": gross_port,
            "cost": costs_port,
            "net_ret": net_port,
            "equity": equity,
        },
        index=prices.index,
    )

    # per-asset standalone TSMOM Sharpe (vol-targeted leg, with costs)
    leg_equity = (1.0 + net_leg).cumprod()
    return portfolio, leg_equity, net_leg


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def sharpe(ret: pd.Series, freq: int = 252) -> float:
    r = ret.dropna()
    if len(r) < 30 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * math.sqrt(freq))


def max_drawdown(equity: pd.Series) -> float:
    e = equity.dropna()
    if e.empty:
        return 0.0
    peak = e.cummax()
    dd = (e / peak) - 1.0
    return float(dd.min())


def annual_return(equity: pd.Series) -> float:
    e = equity.dropna()
    if len(e) < 2:
        return 0.0
    years = (e.index[-1] - e.index[0]).days / 365.25
    if years <= 0:
        return 0.0
    return float(e.iloc[-1] ** (1.0 / years) - 1.0)


def annual_vol(ret: pd.Series) -> float:
    return float(ret.dropna().std() * math.sqrt(252.0))


def perf_block(rets: pd.Series, label: str) -> Dict[str, float]:
    eq = (1.0 + rets).cumprod()
    return {
        "label": label,
        "n_obs": int(rets.notna().sum()),
        "ann_return": annual_return(eq),
        "ann_vol": annual_vol(rets),
        "sharpe": sharpe(rets),
        "max_drawdown": max_drawdown(eq),
        "final_equity": float(eq.iloc[-1]) if not eq.empty else float("nan"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    t0 = time.time()
    print("=" * 70)
    print("TSMOM diversified backtest (Moskowitz, Ooi, Pedersen 2012)")
    print("=" * 70)
    print(f"Universe:        {len(TICKERS)} ETFs across 4 asset classes")
    print(f"Fetch window:    {FETCH_START} -> {FETCH_END}")
    print(f"IS / OOS split:  {IS_START}..{IS_END}  |  {OOS_START}..{OOS_END}")
    print(f"Target vol/leg:  {TARGET_VOL:.0%} ann.  Rebalance: every {REBALANCE_EVERY} days")
    print(f"Costs per side:  fee {FEE_BPS} bps + slippage {SLIP_BPS} bps = {COST_PER_SIDE*1e4:.0f} bps")
    print("-" * 70)
    print("Loading prices via yfinance...")

    prices = load_prices(TICKERS, FETCH_START, FETCH_END)
    if prices.isna().all().any():
        missing = prices.columns[prices.isna().all()].tolist()
        raise RuntimeError(f"Missing data for: {missing}")
    prices = prices.dropna(how="all")
    print(f"Price panel: {prices.shape[0]} rows x {prices.shape[1]} cols")
    print("-" * 70)

    print("Running TSMOM portfolio...")
    portfolio, leg_equity, net_leg = backtest(prices)

    # Slice IS / OOS
    is_mask = (portfolio.index >= pd.Timestamp(IS_START)) & (portfolio.index <= pd.Timestamp(IS_END))
    oos_mask = (portfolio.index >= pd.Timestamp(OOS_START)) & (portfolio.index <= pd.Timestamp(OOS_END))

    is_rets = portfolio.loc[is_mask, "net_ret"]
    oos_rets = portfolio.loc[oos_mask, "net_ret"]
    full_rets = portfolio.loc[is_mask | oos_mask, "net_ret"]

    # gross variants
    is_gross = portfolio.loc[is_mask, "gross_ret"]
    oos_gross = portfolio.loc[oos_mask, "gross_ret"]

    # Per-asset Sharpes (leg-level, vol-targeted standalone)
    per_asset = {}
    for t in TICKERS:
        leg_rets_full = net_leg[t].loc[is_mask | oos_mask]
        leg_rets_is = net_leg[t].loc[is_mask]
        leg_rets_oos = net_leg[t].loc[oos_mask]
        per_asset[t] = {
            "description": UNIVERSE[t],
            "sharpe_full": sharpe(leg_rets_full),
            "sharpe_is": sharpe(leg_rets_is),
            "sharpe_oos": sharpe(leg_rets_oos),
            "ann_return_full": annual_return((1 + leg_rets_full).cumprod()),
            "ann_vol_full": annual_vol(leg_rets_full),
        }

    # Aggregate blocks
    full_block = perf_block(full_rets, "FULL (2015..2026)")
    is_block = perf_block(is_rets, "IS (2015..2021)")
    oos_block = perf_block(oos_rets, "OOS (2022..2026)")
    is_gross_block = perf_block(is_gross, "IS gross")
    oos_gross_block = perf_block(oos_gross, "OOS gross")

    # SPY buy-and-hold benchmark
    spy = prices["SPY"]
    spy_rets = spy.pct_change()
    spy_full = perf_block(spy_rets.loc[is_mask | oos_mask], "SPY B&H full")
    spy_is = perf_block(spy_rets.loc[is_mask], "SPY B&H IS")
    spy_oos = perf_block(spy_rets.loc[oos_mask], "SPY B&H OOS")

    # Walk-forward degradation
    is_sharpe = is_block["sharpe"]
    oos_sharpe = oos_block["sharpe"]
    degradation = (oos_sharpe - is_sharpe) / is_sharpe if is_sharpe > 0 else float("nan")

    accept = oos_sharpe >= GATE_OOS_SHARPE

    # Print summary
    print()
    print("=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"{'Block':<25} {'Sharpe':>8} {'Ret':>8} {'Vol':>8} {'MaxDD':>8}")
    for b in [is_block, oos_block, full_block]:
        print(
            f"{b['label']:<25} {b['sharpe']:>8.2f} {b['ann_return']:>8.1%} "
            f"{b['ann_vol']:>8.1%} {b['max_drawdown']:>8.1%}"
        )
    print()
    print("SPY buy-and-hold benchmark:")
    for b in [spy_is, spy_oos, spy_full]:
        print(
            f"{b['label']:<25} {b['sharpe']:>8.2f} {b['ann_return']:>8.1%} "
            f"{b['ann_vol']:>8.1%} {b['max_drawdown']:>8.1%}"
        )
    print()
    print("Per-asset (standalone leg, vol-targeted, net of costs):")
    print(f"{'Ticker':<8} {'Full Sh':>8} {'IS Sh':>8} {'OOS Sh':>8}  Description")
    for t in TICKERS:
        a = per_asset[t]
        print(f"{t:<8} {a['sharpe_full']:>8.2f} {a['sharpe_is']:>8.2f} {a['sharpe_oos']:>8.2f}  {a['description']}")
    print()
    print(f"Walk-forward degradation: {degradation:+.1%}  (OOS-IS Sharpe / IS Sharpe)")
    print(f"Gate (OOS Sharpe >= {GATE_OOS_SHARPE}): {'ACCEPT' if accept else 'REJECT'}  -- OOS Sharpe = {oos_sharpe:.2f}")
    print(f"Elapsed: {time.time()-t0:.1f}s")

    # Save JSON
    best_asset = max(per_asset, key=lambda k: per_asset[k]["sharpe_oos"])
    worst_asset = min(per_asset, key=lambda k: per_asset[k]["sharpe_oos"])
    output = {
        "strategy": "TSMOM_diversified",
        "paper": "Moskowitz, Ooi, Pedersen (2012)",
        "universe": UNIVERSE,
        "config": {
            "lookback_momentum_days": LOOKBACK_MOM,
            "lookback_vol_days": LOOKBACK_VOL,
            "target_vol": TARGET_VOL,
            "rebalance_days": REBALANCE_EVERY,
            "fee_bps": FEE_BPS,
            "slippage_bps": SLIP_BPS,
            "is": [IS_START, IS_END],
            "oos": [OOS_START, OOS_END],
        },
        "performance": {
            "is": is_block,
            "oos": oos_block,
            "full": full_block,
            "is_gross": is_gross_block,
            "oos_gross": oos_gross_block,
        },
        "spy_benchmark": {"is": spy_is, "oos": spy_oos, "full": spy_full},
        "per_asset": per_asset,
        "best_oos_asset": best_asset,
        "worst_oos_asset": worst_asset,
        "walk_forward_degradation": degradation,
        "gate": {
            "oos_sharpe": oos_sharpe,
            "threshold": GATE_OOS_SHARPE,
            "verdict": "ACCEPT" if accept else "REJECT",
        },
        "equity_curve": {
            "dates": [d.strftime("%Y-%m-%d") for d in portfolio.index],
            "equity": portfolio["equity"].astype(float).tolist(),
        },
    }
    json_path = RESULTS_DIR / "tsmom_research.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=float)
    print(f"Saved {json_path}")

    # Write Markdown report
    md = build_markdown(
        is_block, oos_block, full_block,
        spy_is, spy_oos, spy_full,
        per_asset, best_asset, worst_asset,
        degradation, accept, portfolio, is_gross_block, oos_gross_block,
    )
    md_path = RESULTS_DIR / "tsmom_research.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"Saved {md_path}")
    return 0


def build_markdown(
    is_b, oos_b, full_b,
    spy_is, spy_oos, spy_full,
    per_asset, best_asset, worst_asset,
    degradation, accept,
    portfolio, is_gross_b, oos_gross_b,
) -> str:
    eq = portfolio["equity"]
    peak_date = eq.idxmax()
    trough_after_peak_eq = eq.loc[peak_date:].min()
    max_dd_dt = eq.loc[peak_date:].idxmin()
    curve_words = (
        f"Portfolio equity starts at 1.00 on {eq.index[0].date()}, reaches "
        f"{eq.iloc[-1]:.2f}x by {eq.index[-1].date()}. "
        f"Peak ({eq.max():.2f}x) on {peak_date.date()}; subsequent trough "
        f"{trough_after_peak_eq:.2f}x on {max_dd_dt.date()}."
    )

    verdict = "ACCEPT" if accept else "REJECT"
    diagnosis = ""
    if not accept:
        diagnosis = (
            "\n### Diagnosis (why OOS Sharpe missed 1.5)\n\n"
            "- Moskowitz et al's original 1985-2009 sample used >50 futures across equity index, "
            "FX, fixed income and commodities. Our 10-ETF proxy is a small, equity-heavy subset and "
            "loses the diversification benefit that drives the headline 1.5+ Sharpe.\n"
            "- ETF wrappers (especially USO, UUP, DBC) suffer roll yield, expense ratios and tracking "
            "drag that futures don't.\n"
            "- 2022-2026 OOS includes the 2022 bond+commodity whipsaw and the 2023 banking shock, "
            "both of which produce regime breaks that hurt 12-month trend signals.\n"
            "- Vol-targeting at 10% per leg + 1/N aggregation delivers only ~3% portfolio vol, "
            "so the Sharpe numerator is small in absolute return terms even when the signal works.\n"
            "- Monthly rebalance with 12-month look-back is slow; trend-followers like AQR/Man AHL "
            "now use multi-horizon ensembles (1m+3m+12m) which substantially raise Sharpe.\n"
        )

    asset_table = "\n".join(
        f"| {t} | {per_asset[t]['description']} | "
        f"{per_asset[t]['sharpe_full']:.2f} | {per_asset[t]['sharpe_is']:.2f} | "
        f"{per_asset[t]['sharpe_oos']:.2f} | {per_asset[t]['ann_return_full']:.1%} | "
        f"{per_asset[t]['ann_vol_full']:.1%} |"
        for t in TICKERS
    )

    return f"""# TSMOM Diversified — Research Note

Implementation of *Time Series Momentum* (Moskowitz, Ooi, Pedersen, **JFE 2012**) on
a 10-ETF cross-asset universe.

## 1. Specification

| Parameter            | Value                                                       |
|----------------------|-------------------------------------------------------------|
| Signal               | sign(close[t] / close[t-252] - 1)                           |
| Sizing               | (target_vol / realized_vol_60d) * signal, per asset         |
| Target vol per leg   | 10% annual                                                  |
| Aggregation          | Equal-weight (1/N) across the 10 legs                       |
| Rebalance            | Every 21 trading days (~monthly)                            |
| Costs                | 5 bps fee + 5 bps slippage per side, per leg                |
| Universe             | SPY EFA EEM TLT IEF GLD USO DBC UUP FXE                     |
| IS window            | {IS_START} → {IS_END}                                       |
| OOS window           | {OOS_START} → {OOS_END}                                     |
| Gate                 | OOS Sharpe ≥ {GATE_OOS_SHARPE}                              |

## 2. Aggregate portfolio performance (net of costs)

| Block | Sharpe | Ann. return | Ann. vol | Max DD |
|-------|--------|-------------|----------|--------|
| IS  ({IS_START}..{IS_END})   | {is_b['sharpe']:.2f} | {is_b['ann_return']:+.2%} | {is_b['ann_vol']:.2%} | {is_b['max_drawdown']:.2%} |
| OOS ({OOS_START}..{OOS_END}) | {oos_b['sharpe']:.2f} | {oos_b['ann_return']:+.2%} | {oos_b['ann_vol']:.2%} | {oos_b['max_drawdown']:.2%} |
| Full | {full_b['sharpe']:.2f} | {full_b['ann_return']:+.2%} | {full_b['ann_vol']:.2%} | {full_b['max_drawdown']:.2%} |

Gross (no-cost) comparison: IS gross Sharpe **{is_gross_b['sharpe']:.2f}**, OOS gross
Sharpe **{oos_gross_b['sharpe']:.2f}** — i.e. costs cost ~
{(is_gross_b['sharpe']-is_b['sharpe']):.2f} Sharpe IS and ~
{(oos_gross_b['sharpe']-oos_b['sharpe']):.2f} OOS.

## 3. SPY buy-and-hold benchmark

| Block | Sharpe | Ann. return | Ann. vol | Max DD |
|-------|--------|-------------|----------|--------|
| IS  | {spy_is['sharpe']:.2f} | {spy_is['ann_return']:+.2%} | {spy_is['ann_vol']:.2%} | {spy_is['max_drawdown']:.2%} |
| OOS | {spy_oos['sharpe']:.2f} | {spy_oos['ann_return']:+.2%} | {spy_oos['ann_vol']:.2%} | {spy_oos['max_drawdown']:.2%} |
| Full | {spy_full['sharpe']:.2f} | {spy_full['ann_return']:+.2%} | {spy_full['ann_vol']:.2%} | {spy_full['max_drawdown']:.2%} |

## 4. Per-asset (vol-targeted standalone leg, net)

| Ticker | Description | Sharpe (full) | Sharpe IS | Sharpe OOS | Ann. ret | Ann. vol |
|--------|-------------|---------------|-----------|------------|----------|----------|
{asset_table}

Best OOS asset: **{best_asset}** ({per_asset[best_asset]['sharpe_oos']:.2f}).
Worst OOS asset: **{worst_asset}** ({per_asset[worst_asset]['sharpe_oos']:.2f}).

## 5. Walk-forward equity curve

{curve_words}

OOS - IS Sharpe degradation: **{degradation:+.1%}**.

## 6. Comparison vs Moskowitz et al. original

Paper Sharpe (1985-2009, ~58 futures): ~1.5-1.8 net of costs.
This implementation OOS Sharpe (2022-2026, 10 ETFs): **{oos_b['sharpe']:.2f}**.

## 7. GATE CHECK

OOS Sharpe = **{oos_b['sharpe']:.2f}**, threshold = {GATE_OOS_SHARPE}.

**Verdict: {verdict}**
{diagnosis}
"""


if __name__ == "__main__":
    sys.exit(main())
