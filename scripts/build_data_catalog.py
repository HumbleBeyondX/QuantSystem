#!/usr/bin/env python3
"""One-shot ingestion: data/sample/* -> the unified data catalog.

Rules:
  *.csv OHLCV bars   -> crypto/ or equity/ by symbol (._USDT => crypto);
                        freq parsed from the filename (_4h, _1h, _1d/_daily,
                        _1m, _trades). Venue suffix (okx/bybit) => source.
  *.parquet panels   -> asset_class 'panel', symbol = filename prefix,
                        freq '1d' (long format stored as-is + normalized ts).

When several files map to the same (asset_class, symbol, freq) key, the one
with the most rows wins (tie: source 'okx' preferred, then shortest name);
the losers are reported, never silently swallowed.

Run:  python scripts/build_data_catalog.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.catalog_api import catalog  # noqa: E402

SAMPLE = ROOT / "data" / "sample"

_FREQ_RE = re.compile(r"_(1m|5m|15m|30m|1h|2h|4h|1d|daily|trades)(?=_|\.|$)",
                      re.IGNORECASE)
_VENUE_RE = re.compile(r"_(okx|bybit|binance|deribit)(?=\.|_|$)",
                       re.IGNORECASE)
_FREQ_MAP = {"daily": "1d"}


def parse_csv_name(path: Path):
    """BTC_USDT_1h_2024-01-01_to_2026-04-16_okx.csv ->
    (symbol='BTC_USDT', freq='1h', source='okx')."""
    stem = path.stem
    m = _FREQ_RE.search(stem)
    if not m:
        return None
    freq = _FREQ_MAP.get(m.group(1).lower(), m.group(1).lower())
    symbol = stem[:m.start()]
    v = _VENUE_RE.search(stem)
    source = v.group(1).lower() if v else "sample_csv"
    return symbol, freq, source


def asset_class_for(symbol: str) -> str:
    return "crypto" if re.search(r"_(USDT|USD|USDC|BTC)$",
                                 symbol, re.IGNORECASE) else "equity"


def main() -> int:
    candidates = {}   # key -> list of (path, symbol, freq, source, rows)
    skipped = []

    for p in sorted(SAMPLE.glob("*.csv")):
        parsed = parse_csv_name(p)
        if parsed is None:
            skipped.append((p.name, "no bar-freq token in filename"))
            continue
        symbol, freq, source = parsed
        ac = asset_class_for(symbol)
        rows = sum(1 for _ in open(p, "rb")) - 1
        candidates.setdefault((ac, symbol, freq), []).append(
            (p, symbol, freq, source, rows))

    for p in sorted(SAMPLE.glob("*.parquet")):
        # sp500_2015-01-01_2026-05-04.parquet -> symbol 'sp500'
        symbol = re.split(r"_\d{4}-\d{2}-\d{2}", p.stem)[0]
        df = pd.read_parquet(p)
        candidates.setdefault(("panel", symbol, "1d"), []).append(
            (p, symbol, "1d", "sample_parquet", len(df)))

    entries = []
    for key, cands in sorted(candidates.items()):
        # most rows wins; tie -> okx preferred, then shortest filename
        cands.sort(key=lambda c: (-c[4], 0 if c[3] == "okx" else 1,
                                  len(c[0].name)))
        winner = cands[0]
        for loser in cands[1:]:
            skipped.append((loser[0].name,
                            f"same key {'/'.join(key)} — kept {winner[0].name}"))
        path, symbol, freq, source, _ = winner
        ac = key[0]
        df = (pd.read_parquet(path) if path.suffix == ".parquet"
              else pd.read_csv(path))
        entry = catalog.write(df, ac, symbol, freq, source=source)
        entries.append(entry)
        print(f"  ingested {path.name:55s} -> {ac}/{symbol}/{freq} "
              f"({entry['rows']} rows)")

    print()
    if skipped:
        print("skipped:")
        for name, why in skipped:
            print(f"  {name:55s} {why}")
        print()

    # final inventory (from the catalog itself, not local state)
    inv = catalog.list()
    print(f"catalog inventory — {len(inv)} entries "
          f"(root: {catalog.root})")
    hdr = f"{'asset_class':12s} {'symbol':18s} {'freq':6s} {'rows':>8s}  span"
    print(hdr)
    print("-" * len(hdr) + "-" * 30)
    for e in inv:
        print(f"{e['asset_class']:12s} {e['symbol']:18s} {e['freq']:6s} "
              f"{e['rows']:>8d}  {e['start'][:10]} -> {e['end'][:10]}"
              f"  [{e['source']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
