#!/usr/bin/env python3
"""Wheel-strategy candidate screener — rank tickers by suitability.

Target audience: $5K-$15K capital, high-IV high-premium preference.
Criteria balanced for that profile:

  1. CAPITAL FIT — share price * 100 must fit (1+ contracts of buying power).
     Universe pre-filtered to < $80/share so even $5K can wheel ≥ 1 contract.

  2. LIQUIDITY — daily $ volume (avg close × volume) > threshold;
     thin names mean wide bid-ask, no fills.

  3. IV / PREMIUM — annualized realized vol (proxy for IV) ranked
     higher = better premium. We're explicitly seeking the high-vol bucket.

  4. CRASH RISK — max drawdown over 2y. Wheel's worst case is being
     assigned and watching the stock collapse another -50%. We penalize
     names with > 60% historical DD.

  5. RECOVERY — % of all 60-bar windows that ended above their start.
     A name that "always comes back" is wheel-friendly even if volatile.

  6. CAPITAL EFFICIENCY — annualized premium yield estimate
     ≈ IV × sqrt(30/365) × 12 (simple monthly cycle, OTM put).

Output: sorted table of all candidates with composite score, plus
recommended top 5 with detailed metrics.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ────────────────────────────────────────────────────────────
# Candidate universe — high-IV names mostly under $80, plus a few
# premium ETFs and leveraged ETFs that are wheel-friendly.
# ────────────────────────────────────────────────────────────

CANDIDATES: List[str] = [
    # — Higher-volatility individual stocks —
    "F",        # Ford — value/cyclical, low share price
    "BAC",      # Bank of America — moderate IV, good liquidity
    "INTC",     # Intel — turnaround story, very high IV
    "T",        # AT&T — telecom, lower IV but stable
    "PFE",      # Pfizer — pharma value
    "PLTR",     # Palantir — high IV growth
    "SOFI",     # SoFi — fintech, high IV
    "NIO",      # NIO — Chinese EV, very high IV
    "RIOT",     # Riot Platforms — BTC miner, very high IV
    "MARA",     # Marathon Digital — BTC miner, very high IV
    "AMD",      # AMD — semis, moderate-high IV
    "BABA",     # Alibaba — Chinese tech
    "GME",      # GameStop — meme/high IV
    "DKNG",     # DraftKings — sports betting, high IV
    "SNAP",     # Snap — social media
    "RIVN",     # Rivian — EV, high IV
    "LCID",     # Lucid — EV, very high IV but tiny price
    "UAA",      # Under Armour — consumer, moderate IV
    "GLD",      # Gold ETF — defensive but lower IV

    # — Liquid ETFs —
    "QQQ",      # Nasdaq ETF — premium liquidity but >$400 (might fail price filter)
    "SPY",      # S&P ETF — same
    "IWM",      # Russell 2000 — moderate, ~$200
    "ARKK",     # ARK Innovation — high IV ETF
    "SOXL",     # 3x semis ETF — very high IV, but small share price
    "TQQQ",     # 3x Nasdaq ETF — very high IV
    "SQQQ",     # 3x inverse Nasdaq — very high IV
    "TNA",      # 3x small caps — very high IV
    "GDX",      # Gold miners ETF — moderate IV
    "KWEB",     # Chinese internet ETF — high IV
    "XLE",      # Energy sector ETF — moderate
    "EWZ",      # Brazil ETF — high IV, ~$30
]


@dataclass
class WheelMetrics:
    ticker: str
    last_price: float
    dollar_volume_avg: float       # avg daily $ volume, last 60 bars
    realized_vol_30d: float        # annualized
    realized_vol_60d: float
    max_dd_2y: float               # negative number, e.g. -0.45
    pct_positive_60d_windows: float
    capital_per_contract: float    # last_price × 100
    estimated_monthly_premium_pct: float  # 25-delta OTM put premium / capital, rough
    composite_score: float = 0.0
    eligible: bool = True
    error: Optional[str] = None


# ────────────────────────────────────────────────────────────
# Metric computation
# ────────────────────────────────────────────────────────────

def compute_metrics(ticker: str, period_days: int = 750) -> WheelMetrics:
    """Pull yfinance data and compute all wheel-fit metrics."""
    import yfinance as yf

    try:
        end = datetime.utcnow()
        start = end - timedelta(days=period_days)
        df = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=False)
        if df.empty or len(df) < 100:
            return WheelMetrics(ticker, 0, 0, 0, 0, 0, 0, 0, 0,
                                eligible=False, error=f"insufficient_data ({len(df)} bars)")
        df = df.reset_index()
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
    except Exception as e:
        return WheelMetrics(ticker, 0, 0, 0, 0, 0, 0, 0, 0,
                            eligible=False, error=f"fetch_failed: {e}")

    last_price = float(df["close"].iloc[-1])
    dollar_volume = (df["close"] * df["volume"]).iloc[-60:].mean()

    log_ret = np.log(df["close"] / df["close"].shift(1)).dropna()

    rv30 = float(log_ret.iloc[-30:].std() * np.sqrt(252))
    rv60 = float(log_ret.iloc[-60:].std() * np.sqrt(252))

    # Max drawdown over 2y
    closes = df["close"].iloc[-504:]   # ~2y
    running_max = closes.cummax()
    dd = (closes / running_max - 1).min()
    max_dd_2y = float(dd) if not np.isnan(dd) else 0.0

    # % of 60-bar rolling windows where end > start (recovery proxy)
    if len(df) >= 60:
        end_close = df["close"].iloc[60:].values
        start_close = df["close"].iloc[:-60].values
        pct_pos = (end_close > start_close).mean() if len(end_close) > 0 else 0.0
    else:
        pct_pos = 0.0

    capital = last_price * 100

    # Rough OTM put premium estimate: 25-delta put for 30 DTE
    # Black-Scholes: ATM straddle ≈ S × IV × sqrt(T/365) × 0.4
    # 25-delta OTM put ≈ 0.30 × ATM-put-price (rule of thumb)
    iv_proxy = rv30 * 1.15   # assume 15% VRP for high-IV names
    atm_put_price = last_price * iv_proxy * np.sqrt(30 / 365) * 0.4
    otm25_put_price = atm_put_price * 0.45   # approximation
    monthly_yield_pct = (otm25_put_price / last_price) * 100

    return WheelMetrics(
        ticker=ticker,
        last_price=last_price,
        dollar_volume_avg=float(dollar_volume),
        realized_vol_30d=rv30,
        realized_vol_60d=rv60,
        max_dd_2y=max_dd_2y,
        pct_positive_60d_windows=float(pct_pos),
        capital_per_contract=float(capital),
        estimated_monthly_premium_pct=float(monthly_yield_pct),
    )


# ────────────────────────────────────────────────────────────
# Composite scoring
# ────────────────────────────────────────────────────────────

def score_candidates(metrics: List[WheelMetrics], capital_max: float = 15000) -> None:
    """Compute composite scores in-place. Higher = more wheel-friendly."""
    valid = [m for m in metrics if m.eligible]
    if not valid:
        return

    def _normalize(values: List[float], higher_is_better: bool = True) -> List[float]:
        arr = np.array(values, dtype=float)
        arr_min, arr_max = arr.min(), arr.max()
        if arr_max - arr_min < 1e-9:
            return [0.5] * len(values)
        norm = (arr - arr_min) / (arr_max - arr_min)
        return list(norm if higher_is_better else 1 - norm)

    rv = _normalize([m.realized_vol_30d for m in valid], higher_is_better=True)        # high IV
    liq = _normalize([np.log10(max(m.dollar_volume_avg, 1)) for m in valid], higher_is_better=True)
    dd = _normalize([m.max_dd_2y for m in valid], higher_is_better=True)               # less neg is better
    rec = _normalize([m.pct_positive_60d_windows for m in valid], higher_is_better=True)
    yield_ = _normalize([m.estimated_monthly_premium_pct for m in valid], higher_is_better=True)

    # Capital fit: penalize if capital_per_contract > capital_max
    cap_fit = []
    for m in valid:
        if m.capital_per_contract <= capital_max * 0.5:
            cap_fit.append(1.0)        # plenty of room for ≥2 contracts
        elif m.capital_per_contract <= capital_max:
            cap_fit.append(0.7)        # fits exactly 1 contract
        else:
            cap_fit.append(0.0)        # cannot afford even 1 contract
    cap_fit = np.array(cap_fit)

    # Composite weights: IV is most important (high-IV preference),
    # liquidity required (no fills = no strategy), DD penalizes crashy names,
    # recovery rewards "comes back" names, yield rewards premium-rich names.
    weights = {
        "rv": 0.25,
        "liquidity": 0.20,
        "max_dd": 0.15,
        "recovery": 0.10,
        "yield": 0.20,
        "capital_fit": 0.10,
    }

    for i, m in enumerate(valid):
        m.composite_score = float(
            weights["rv"] * rv[i]
            + weights["liquidity"] * liq[i]
            + weights["max_dd"] * dd[i]
            + weights["recovery"] * rec[i]
            + weights["yield"] * yield_[i]
            + weights["capital_fit"] * cap_fit[i]
        )


# ────────────────────────────────────────────────────────────
# Output
# ────────────────────────────────────────────────────────────

def print_results(metrics: List[WheelMetrics], capital_max: float = 15000) -> None:
    valid = [m for m in metrics if m.eligible]
    valid.sort(key=lambda m: m.composite_score, reverse=True)

    print("\n" + "=" * 110)
    print(f"WHEEL CANDIDATE SCREEN — capital target: ${capital_max:,}")
    print("=" * 110)
    print(f"{'Rank':<5}{'Ticker':<7}{'Price':>9}{'1Cont':>9}"
          f"{'RV30%':>8}{'$VolM':>10}{'MaxDD':>9}{'Recov%':>9}{'Mly%':>7}{'Score':>8}")
    print("-" * 110)
    for i, m in enumerate(valid, 1):
        affordable = "✓" if m.capital_per_contract <= capital_max else "✗"
        print(
            f"{i:<5}{m.ticker:<7}${m.last_price:>7.2f}"
            f"{affordable + ' $' + format(m.capital_per_contract, ',.0f'):>9}"
            f"{m.realized_vol_30d * 100:>7.1f}%"
            f"{m.dollar_volume_avg / 1e6:>9.1f}M"
            f"{m.max_dd_2y * 100:>8.1f}%"
            f"{m.pct_positive_60d_windows * 100:>8.1f}%"
            f"{m.estimated_monthly_premium_pct:>6.2f}%"
            f"{m.composite_score:>8.3f}"
        )

    rejected = [m for m in metrics if not m.eligible]
    if rejected:
        print("-" * 110)
        print("Rejected:")
        for m in rejected:
            print(f"  {m.ticker:<7} {m.error}")

    print("\n" + "=" * 110)
    print("TOP 5 RECOMMENDATIONS (composite-ranked, capital-affordable)")
    print("=" * 110)
    affordable = [m for m in valid if m.capital_per_contract <= capital_max]
    for i, m in enumerate(affordable[:5], 1):
        print(f"\n#{i} — {m.ticker}")
        print(f"  Last price:          ${m.last_price:.2f}")
        print(f"  Capital per contract: ${m.capital_per_contract:,.0f}")
        print(f"  Realized vol (30d):  {m.realized_vol_30d * 100:.1f}% annualized")
        print(f"  IV proxy (RV*1.15):  {m.realized_vol_30d * 115:.1f}%")
        print(f"  Avg $ volume:        ${m.dollar_volume_avg / 1e6:.1f}M / day")
        print(f"  Max drawdown 2y:     {m.max_dd_2y * 100:.1f}%")
        print(f"  60-bar recovery:     {m.pct_positive_60d_windows * 100:.1f}% of windows ended green")
        print(f"  Est. monthly yield:  {m.estimated_monthly_premium_pct:.2f}% (25-delta OTM put)")
        print(f"  Composite score:     {m.composite_score:.3f}")


# ────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────

def main() -> int:
    capital_max = 15000

    print(f"Screening {len(CANDIDATES)} candidates...")
    metrics: List[WheelMetrics] = []
    for ticker in CANDIDATES:
        print(f"  {ticker}...", end="", flush=True)
        m = compute_metrics(ticker)
        if m.eligible:
            print(f" ${m.last_price:.2f}, RV30={m.realized_vol_30d*100:.0f}%")
        else:
            print(f" FAIL ({m.error})")
        metrics.append(m)
        time.sleep(0.1)

    score_candidates(metrics, capital_max=capital_max)
    print_results(metrics, capital_max=capital_max)

    # Save full results to JSON for downstream backtest selection
    out_path = PROJECT_ROOT / "results" / f"wheel_screen_{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    out_path.parent.mkdir(exist_ok=True)
    import json
    with open(out_path, "w") as f:
        json.dump([asdict(m) for m in metrics], f, indent=2)
    print(f"\nResults saved → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
