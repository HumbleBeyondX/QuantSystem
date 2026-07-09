#!/usr/bin/env python3
"""High-leverage options strategy exploration: trend-gated SHORT put spreads.

Rationale (evidence from this project, 2026-06/07):
  * Selling vol premium is the only options edge that has survived testing
    here (gamma scalp: Sharpe 1.98 full / 2.91 OOS).
  * Buying calls on drift signals failed three times (13F, ATM rotation,
    ITM rotation) — theta+spread eat sub-2% expected moves.
  → So harvest premium WITH the trend instead of buying convexity:
    sell defined-risk put spreads on momentum leaders / indices.

Trade (per underlying, when trend-qualified):
  SELL 30-DTE put @ 0.95×spot / BUY put @ 0.85×spot  (defined risk)
  Exit: +50% of credit (take-profit), −200% of credit (stop), or 10-day rebal.
  Qualify: price > 50d MA > 200d MA and positive 21d momentum.
  Sizing: max-loss risk = RISK_PCT of equity per position.
  Leverage: spread notional runs several × equity — high leverage, capped loss.

Variants: leaders universe vs index-only (SPY/QQQ). Daily MTM. IS/OOS split.
Caveat: BS pricing with IV = HV×1.15. Real put IV skew is HIGHER than this
→ real credits are RICHER than simulated → this sim UNDERSTATES the seller's
edge (conservative in the right direction).
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import yfinance as yf
from strategies.options.gamma_scalp_strategy import bs_put

LEADERS = ["NVDA", "MSFT", "META", "GOOGL", "AMZN", "TSLA", "JPM", "LLY",
           "XOM", "CAT", "COST", "GE"]
INDICES = ["SPY", "QQQ"]
DTE = 30
SHORT_K, LONG_K = 0.95, 0.85
REBAL = 10
TP, SL = 0.50, -2.00          # exit at +50% credit / -200% credit
RISK_PCT = 0.05               # max-loss risk per position
MAX_POS = 4
SPREAD_COST = 0.02            # 2% of credit each way
R = 0.04
START = "2018-06-01"
IS_END = "2023-12-31"


def fetch(tickers):
    px = {}
    for t in tickers:
        s = yf.Ticker(t).history(start=START, auto_adjust=True)["Close"]
        s.index = s.index.tz_localize(None).normalize()
        if len(s) > 300:
            px[t] = s
    return pd.DataFrame(px).ffill()


def spread_value(s, k_short, k_long, t_yr, iv):
    return bs_put(s, k_short, t_yr, R, iv) - bs_put(s, k_long, t_yr, R, iv)


def run(px: pd.DataFrame, universe: list) -> dict:
    dates = px.index[px.index >= "2019-01-01"]
    equity, curve, open_pos = 100_000.0, {}, []
    n_trades = wins = 0
    notional_peak = 0.0

    rebal_marks = set(dates[::REBAL])
    ma50 = px.rolling(50).mean()
    ma200 = px.rolling(200).mean()

    for d in dates:
        row = px.loc[d]

        # ── daily MTM / exits ──
        still = []
        for p in open_pos:
            s = row.get(p["tk"], np.nan)
            if np.isnan(s):
                still.append(p); continue
            t_left = max(DTE - (d - p["d0"]).days, 0.5) / 365
            v = spread_value(float(s), p["ks"], p["kl"], t_left, p["iv"])
            pnl = (p["credit"] - v * (1 + SPREAD_COST)) * p["mult"]  # short spread
            p["pnl"] = pnl
            frac = pnl / p["credit_total"] if p["credit_total"] else 0
            if frac >= TP or frac <= SL or (d - p["d0"]).days >= DTE - 2:
                equity += pnl
                n_trades += 1; wins += pnl > 0
            else:
                still.append(p)
        open_pos = still

        # ── rebalance / entries ──
        if d in rebal_marks:
            for p in open_pos:      # roll: close remainder
                equity += p.get("pnl", 0.0); n_trades += 1; wins += p.get("pnl", 0) > 0
            open_pos = []
            hist = px.loc[:d]
            if len(hist) < 210:
                curve[d] = equity; continue
            # qualify + rank by momentum
            cands = []
            for tk in universe:
                s = row.get(tk, np.nan)
                if np.isnan(s) or np.isnan(ma200[tk].loc[d]):
                    continue
                if s > ma50[tk].loc[d] > ma200[tk].loc[d]:
                    mom = s / hist[tk].iloc[-21] - 1
                    if mom > 0:
                        cands.append((mom, tk))
            for _, tk in sorted(cands, reverse=True)[:MAX_POS]:
                s = float(row[tk])
                lr = np.log(px[tk].loc[:d]).diff().dropna().tail(63)
                if len(lr) < 40:
                    continue
                iv = max(float(lr.std() * np.sqrt(252)) * 1.15, 0.18)
                ks, kl = s * SHORT_K, s * LONG_K
                credit = spread_value(s, ks, kl, DTE / 365, iv) * (1 - SPREAD_COST)
                if credit <= 0.05:
                    continue
                max_loss_per = (ks - kl) - credit
                mult = (equity * RISK_PCT) / max_loss_per
                open_pos.append({"tk": tk, "ks": ks, "kl": kl, "iv": iv,
                                 "credit": credit, "mult": mult,
                                 "credit_total": credit * mult, "d0": d})
            notional_peak = max(notional_peak,
                                sum(p["ks"] * p["mult"] for p in open_pos) / equity)

        curve[d] = equity + sum(p.get("pnl", 0.0) for p in open_pos)

    eq = pd.Series(curve).sort_index()
    out = {}
    for label, sl_ in [("FULL", eq), ("IS 2019-2023", eq[eq.index <= IS_END]),
                       ("OOS 2024-2026", eq[eq.index > IS_END])]:
        r_d = sl_.pct_change().dropna()
        if len(r_d) < 60:
            continue
        yrs = (sl_.index[-1] - sl_.index[0]).days / 365.25
        out[label] = {
            "cagr%": round(((sl_.iloc[-1] / sl_.iloc[0]) ** (1 / yrs) - 1) * 100, 1),
            "sharpe": round(r_d.mean() / r_d.std() * np.sqrt(252), 2) if r_d.std() > 0 else 0,
            "maxDD%": round(abs((sl_ / sl_.cummax() - 1).min()) * 100, 1),
        }
    yrs_all = (eq.index[-1] - eq.index[0]).days / 365.25
    out.update(n_trades=n_trades, per_year=round(n_trades / yrs_all),
               win=round(wins / max(n_trades, 1) * 100),
               peak_notional_x=round(notional_peak, 1), _curve=eq)
    return out


def main():
    px = fetch(sorted(set(LEADERS + INDICES)))
    print(f"data: {px.shape[0]} days → {px.index[-1].date()}\n")
    for label, uni in [("LEADERS put-spreads", LEADERS),
                       ("INDEX-ONLY put-spreads", INDICES)]:
        res = run(px, uni)
        print(f"=== {label} ===")
        for k in ["FULL", "IS 2019-2023", "OOS 2024-2026"]:
            if k in res:
                m = res[k]
                print(f"  {k:14s} CAGR {m['cagr%']:+6.1f}%  Sharpe {m['sharpe']:5.2f}  MaxDD {m['maxDD%']:5.1f}%")
        print(f"  trades {res['n_trades']} (~{res['per_year']}/yr)  win {res['win']}%  "
              f"peak notional {res['peak_notional_x']}x equity\n")
        slug = label.split()[0].lower()
        res["_curve"].to_csv(ROOT / "results" / f"putspread_{slug}_curve.csv")
    print("CAVEAT: IV=HV×1.15 has no put skew → real credits richer → sim "
          "understates seller edge (conservative).")


if __name__ == "__main__":
    main()
