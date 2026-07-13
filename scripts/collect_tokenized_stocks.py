#!/usr/bin/env python3
"""OKX tokenized-stock perps — forward data collector.

These instruments listed 2026-06-30; every hour of data is irreplaceable.
Collects for the equity-token universe:
  * 5m + 1h OHLCV (full available history each run — idempotent upsert
    into the catalog, dedup on timestamp)
  * funding rate + best bid/ask + order-book depth snapshot
    -> logs/collectors/tokenized_funding.jsonl (append-only)

Run:  python scripts/collect_tokenized_stocks.py          (hourly task)
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

t0 = time.time()


def say(msg: str) -> None:
    print(f"[{time.time()-t0:5.1f}s] {msg}", flush=True)


UNIVERSE = ["SPCX", "SKHYNIX", "NVDA", "TSLA", "HOOD", "MSTR", "CRCL",
            "WDC", "VRT", "UVXY", "USO", "XLE", "URNM", "USAR", "OKB"]
FUNDING_LOG = ROOT / "logs" / "collectors" / "tokenized_funding.jsonl"


def main() -> None:
    say("import ccxt/pandas...")
    import ccxt
    import pandas as pd
    from data.catalog_api import catalog

    # hard timeout: this box has a known DNS-stall failure mode — better a
    # failed hourly run (watchdog-visible) than a 20-minute silent hang
    ex = ccxt.okx({"options": {"defaultType": "swap"}, "timeout": 20000})
    ex.load_markets()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    FUNDING_LOG.parent.mkdir(parents=True, exist_ok=True)

    for base in UNIVERSE:
        sym = f"{base}/USDT:USDT"
        if sym not in ex.markets:
            say(f"{base}: not listed, skip")
            continue
        try:
            # ── bars: paginate back to listing (2026-06-30), merge into
            # catalog. OKX caps each response at ~300 rows — loop with
            # `since` until we reach existing data or the listing date. ──
            for tf, freq, step_ms in (("5m", "5m", 300 * 5 * 60_000),
                                      ("1h", "1h", 300 * 3_600_000)):
                since = ex.parse8601("2026-06-30T00:00:00Z")
                try:
                    old = catalog.read("tokenized_stock", base, freq)
                    # resume from last stored bar (minus overlap)
                    since = max(since,
                                int(pd.Timestamp(old["timestamp"].max())
                                    .timestamp() * 1000) - step_ms // 10)
                except Exception:
                    old = None
                chunks = []
                for _ in range(40):                 # hard page cap
                    page = ex.fetch_ohlcv(sym, tf, since=since, limit=300)
                    if not page:
                        break
                    chunks.extend(page)
                    if len(page) < 300:
                        break
                    since = page[-1][0] + 1
                    time.sleep(0.15)
                if not chunks:
                    continue
                df = pd.DataFrame(
                    chunks, columns=["timestamp", "open", "high", "low",
                                     "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                if old is not None:
                    df = pd.concat([old, df])
                df = (df.drop_duplicates("timestamp", keep="last")
                        .sort_values("timestamp"))
                catalog.write(df, "tokenized_stock", base, freq,
                              source="okx_collector")
            # ── funding + book snapshot ──
            fr = ex.fetch_funding_rate(sym)
            ob = ex.fetch_order_book(sym, limit=20)
            # OKX book rows are [price, qty, deprecated, numOrders] — index,
            # never unpack
            bid = ob["bids"][0][0] if ob["bids"] else None
            ask = ob["asks"][0][0] if ob["asks"] else None
            depth_usd = (sum(r[0] * r[1] for r in ob["bids"][:10])
                         + sum(r[0] * r[1] for r in ob["asks"][:10]))
            row = {
                "ts": now, "symbol": sym,
                "funding_rate": fr.get("fundingRate"),
                "next_funding_ts": fr.get("fundingTimestamp"),
                "mark": fr.get("markPrice"), "index": fr.get("indexPrice"),
                "bid": bid, "ask": ask,
                "spread_bps": round((ask - bid) / ((ask + bid) / 2) * 1e4, 2)
                if bid and ask else None,
                "depth10_usd": round(depth_usd, 0),
            }
            with open(FUNDING_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            say(f"{base}: bars upserted, funding={row['funding_rate']} "
                f"spread={row['spread_bps']}bps depth10=${row['depth10_usd']:,.0f}")
            time.sleep(0.3)
        except Exception as e:
            say(f"{base}: FAILED {type(e).__name__}: {str(e)[:70]}")
    say("collector run complete")


if __name__ == "__main__":
    main()
