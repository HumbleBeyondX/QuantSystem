#!/usr/bin/env python3
"""Build / refresh the factor catalog on a chosen panel.

Session 1 deliverable: load sector ETF panel, evaluate all registered
hand-curated factors with the full FactorMetrics suite, print a ranked
table and save the catalog to JSON.

Usage:
    python scripts/build_factor_library.py
    python scripts/build_factor_library.py --panel data/sample/sector_rotation_2010-01-01_2026-05-03.parquet
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.factor_mining import (  # noqa: E402
    DataPanel,
    FactorMetrics,
    evaluate_factor,
    get_all_factors,
)


DEFAULT_PANEL = "data/sample/sector_rotation_2010-01-01_2026-05-03.parquet"

# Restrict to sector + factor ETFs (skip macro tickers like ^VIX, ^TNX, ^IRX)
SECTOR_AND_FACTOR_ETFS = [
    "XLK", "XLF", "XLV", "XLY", "XLP",
    "XLE", "XLI", "XLU", "XLB", "XLRE", "XLC",
    "MTUM", "QUAL", "USMV", "VLUE",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--panel", default=DEFAULT_PANEL)
    p.add_argument("--start", default="2012-01-01",
                   help="Effective start (after factor warmup)")
    p.add_argument("--end", default=None)
    p.add_argument("--category", default=None,
                   help="Filter to one category (momentum/volatility/reversal/etc)")
    p.add_argument("--out", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    panel_path = Path(args.panel)
    if not panel_path.exists():
        print(f"ERROR: panel parquet not found: {panel_path}")
        return 1

    print(f"Loading panel: {panel_path.name}")
    panel = DataPanel.from_parquet(panel_path)
    panel = panel.restrict(universe=SECTOR_AND_FACTOR_ETFS,
                           start=args.start, end=args.end)
    print(f"  Panel: {panel}")
    print(f"  Universe: {panel.universe}")

    factors = get_all_factors(category=args.category)
    print(f"\nEvaluating {len(factors)} factors...\n")

    results: List[FactorMetrics] = []
    for f in factors:
        t0 = time.time()
        try:
            factor_panel = f.compute(panel)
            metrics = evaluate_factor(
                factor_panel, panel,
                factor_name=f.name,
                universe="sector_etfs_15",
            )
            elapsed = time.time() - t0
            print(f"  {f.name:24s} {f.category:12s} "
                  f"IC IR={metrics.ic_ir:+6.3f}  RankIC IR={metrics.rank_ic_ir:+6.3f}  "
                  f"L-S Sharpe={metrics.ls_sharpe:+5.2f}  "
                  f"cov={metrics.coverage_pct:5.1f}%  ({elapsed:4.1f}s)")
            results.append(metrics)
        except Exception as e:
            print(f"  {f.name:24s} FAILED: {e}")
            import traceback
            traceback.print_exc()

    if not results:
        print("\nNo factors evaluated.")
        return 1

    # ── Ranked table ──
    df = pd.DataFrame([m.to_dict() for m in results])
    df = df.sort_values("rank_ic_ir", key=lambda s: s.abs(), ascending=False)

    print("\n" + "=" * 130)
    print("FACTOR LIBRARY — Ranked by |Rank IC IR|")
    print("=" * 130)
    print(f"{'#':<3}{'name':<26}{'cat':<12}{'IC':>7}{'RankIC':>9}"
          f"{'RankIC_IR':>11}{'tstat':>8}{'L-S Shp':>9}{'L-S MaxDD':>11}"
          f"{'spread bps':>13}{'monot':>8}{'turnover':>10}{'cov':>7}")
    print("-" * 130)
    for i, (_, r) in enumerate(df.iterrows(), 1):
        print(
            f"{i:<3}{r['factor_name']:<26}{r['universe'][:11]:<12}"
            f"{r['ic_mean']:>+7.4f}{r['rank_ic_mean']:>+9.4f}"
            f"{r['rank_ic_ir']:>+11.3f}{r['rank_ic_t_stat']:>+8.2f}"
            f"{r['ls_sharpe']:>+9.2f}{r['ls_max_dd']:>10.1%}"
            f"{r['quintile_spread_bps']:>+13.0f}"
            f"{r['monotonicity']:>+8.2f}"
            f"{r['ls_turnover_ann']:>10.1f}"
            f"{r['coverage_pct']:>6.0f}%"
        )
    print("=" * 130)

    # Summary
    strong = df[df["rank_ic_ir"].abs() > 0.20]
    profitable = df[df["ls_sharpe"] > 1.0]
    print(f"\nStrong (|RankIC IR| > 0.20): {len(strong)}/{len(df)}")
    print(f"Profitable (L-S Sharpe > 1.0): {len(profitable)}/{len(df)}")

    # Save
    out = args.out
    if out is None:
        out = PROJECT_ROOT / "results" / f"factor_catalog_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    Path(out).parent.mkdir(exist_ok=True)
    with open(out, "w") as f:
        json.dump([m.to_dict() for m in results], f, indent=2, default=str)
    print(f"\n→ {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
