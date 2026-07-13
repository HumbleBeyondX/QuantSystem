#!/usr/bin/env python3
"""OKX tokenized-stock perps probe — SPCX/SKHYNIX class instruments.

Facts before opinions (public data only, no auth needed for market data):
  1. which xStock-style perps exist + max leverage
  2. listing depth (how much history for a backtest)
  3. intraday RV vs the 20x liquidation budget (~4.7%)
  4. funding rates + spreads (is the edge directional or carry?)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

t0 = time.time()


def say(msg: str) -> None:
    print(f"[{time.time()-t0:5.1f}s] {msg}", flush=True)


say("import ccxt...")
import ccxt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ex = ccxt.okx({"options": {"defaultType": "swap"}})
say("load_markets...")
mkts = ex.load_markets()

# find tokenized-equity style swaps (non-crypto underlyings)
CRYPTO_HINTS = ("BTC", "ETH", "SOL", "DOGE", "XRP", "ADA", "LTC", "BNB",
                "AVAX", "LINK", "DOT", "TRX", "TON", "SHIB", "PEPE", "SUI",
                "APT", "ARB", "OP", "FIL", "NEAR", "ATOM", "UNI", "ETC",
                "BCH", "STX", "INJ", "IMX", "WLD", "ORDI", "TIA", "SEI")
cands = []
for sym, m in mkts.items():
    if not m.get("swap") or not m.get("active"):
        continue
    base = m.get("base", "")
    if any(h == base or base.startswith(h) for h in CRYPTO_HINTS):
        continue
    # equity-ish tickers: SPCX, SKHYNIX, NVDA, TSLA, OPENAI...
    if base.isalpha() and len(base) >= 3:
        cands.append((sym, m))
say(f"non-crypto-looking active swaps: {len(cands)}")

rows = []
for sym, m in cands:
    base = m["base"]
    try:
        tick = ex.fetch_ticker(sym)
        lev = (m.get("limits", {}).get("leverage", {}) or {}).get("max")
        bid, ask = tick.get("bid"), tick.get("ask")
        spread_bps = ((ask - bid) / ((ask + bid) / 2) * 1e4
                      if bid and ask else None)
        fr = None
        try:
            fr = ex.fetch_funding_rate(sym).get("fundingRate")
        except Exception:
            pass
        # history depth + realized vol from 1h bars
        ohlcv = ex.fetch_ohlcv(sym, "1h", limit=500)
        n_bars = len(ohlcv)
        rv_1h = daily_range = first_ts = None
        if n_bars > 24:
            df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
            r = pd.Series(df["c"]).pct_change().dropna()
            rv_1h = float(r.std())
            daily_range = float(((df["h"] - df["l"]) / df["c"]).tail(7 * 24)
                                .groupby(np.arange(len(df.tail(7 * 24))) // 24)
                                .sum().mean()) if n_bars >= 48 else None
            first_ts = pd.to_datetime(df["ts"].iloc[0], unit="ms")
        rows.append({
            "symbol": sym, "base": base, "max_lev": lev,
            "last": tick.get("last"),
            "spread_bps": round(spread_bps, 1) if spread_bps else None,
            "funding_8h": round(fr * 1e4, 2) if fr is not None else None,  # bps
            "vol24h_usd": tick.get("quoteVolume"),
            "bars_1h": n_bars,
            "hist_from": str(first_ts)[:16] if first_ts is not None else None,
            "rv_1h_pct": round(rv_1h * 100, 2) if rv_1h else None,
        })
        say(f"  {sym}: lev={lev} spread={rows[-1]['spread_bps']}bps "
            f"funding={rows[-1]['funding_8h']}bps/8h rv1h={rows[-1]['rv_1h_pct']}% "
            f"bars={n_bars}")
        time.sleep(0.25)
    except Exception as e:
        say(f"  {sym}: probe failed ({type(e).__name__}: {str(e)[:60]})")

out = pd.DataFrame(rows)
if not out.empty:
    out["day_vol_pct_est"] = (out["rv_1h_pct"] * np.sqrt(24)).round(1)
    out["liq_budget_20x_pct"] = 4.75
    out["survives_20x"] = out["day_vol_pct_est"] < 4.75
    out = out.sort_values("vol24h_usd", ascending=False, na_position="last")
    out.to_csv(ROOT / "results" / "okx_tokenized_stock_probe.csv", index=False)
    say(f"\nsaved -> results/okx_tokenized_stock_probe.csv ({len(out)} instruments)")
    cols = ["symbol", "max_lev", "spread_bps", "funding_8h", "vol24h_usd",
            "day_vol_pct_est", "survives_20x", "bars_1h", "hist_from"]
    print(out[cols].to_string(index=False))
else:
    say("no tokenized-stock swaps found")
