#!/usr/bin/env python3
"""教学 Step 1 — 认识你的数据脊柱。

跑法:  python scripts/tutorial/step1_explore_catalog.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from data.catalog_api import catalog  # noqa: E402

print("=" * 70)
print("你的数据资产清单(和 Data Layer 页 Catalog tab 是同一份数据):")
print("=" * 70)
for e in catalog.list():
    print(f"  {e['asset_class']:8s} {e['symbol']:18s} {e['freq']:6s} "
          f"{e['rows']:>8,} rows   {str(e['start'])[:10]} -> {str(e['end'])[:10]}")

print()
print("=" * 70)
print("本次 ML 之旅的原料:SP500 长格式面板")
print("=" * 70)
df = catalog.read("panel", "sp500", "1d")
print(f"shape: {df.shape}")
print(f"columns: {df.columns.tolist()}")
print(f"tickers: {df['ticker'].nunique()}")
print()
print(df.head(3).to_string())
