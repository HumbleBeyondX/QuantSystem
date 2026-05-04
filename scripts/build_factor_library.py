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
import asyncio
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
from research.factor_mining.evaluator import (  # noqa: E402
    cluster_factors,
    factor_correlation_matrix,
    select_top_per_cluster,
)
from research.factor_mining.persistence import (  # noqa: E402
    cache_factor_values,
    metrics_to_summary_df,
    save_metrics_batch,
    write_catalog_markdown,
    write_correlation_report,
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
    p.add_argument("--no-cache", action="store_true",
                   help="Skip writing factor-value Parquet cache")
    p.add_argument("--no-db", action="store_true",
                   help="Skip writing FactorEvaluationRecord rows")
    p.add_argument("--cluster-threshold", type=float, default=0.7,
                   help="Cluster factors with |corr| > this together")
    p.add_argument("--universe-tag", default="sector_etfs_15",
                   help="Label written to FactorEvaluationRecord.universe")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    panel_path = Path(args.panel)
    if not panel_path.exists():
        print(f"ERROR: panel parquet not found: {panel_path}")
        return 1

    print(f"Loading panel: {panel_path.name}")
    panel = DataPanel.from_parquet(panel_path)

    # Restrict universe: if SECTOR_AND_FACTOR_ETFS appear in the panel, use them
    # (sector rotation use case); otherwise use everything that's not a macro
    # ticker (^VIX/^TNX/^IRX), so SP500 panels run on the full equity universe.
    available = set(panel.universe)
    macro_tickers = {"^VIX", "^TNX", "^IRX", "HYG", "IEF"}
    if set(SECTOR_AND_FACTOR_ETFS).issubset(available):
        target_universe = SECTOR_AND_FACTOR_ETFS
    else:
        target_universe = sorted(available - macro_tickers)
    panel = panel.restrict(universe=target_universe,
                           start=args.start, end=args.end)
    print(f"  Panel: {panel}")
    print(f"  Universe size: {len(panel.universe)} tickers")

    factors = get_all_factors(category=args.category)
    print(f"\nEvaluating {len(factors)} factors...\n")

    results: List[FactorMetrics] = []
    factor_panels: Dict[str, "pd.DataFrame"] = {}  # name → panel, for decorrelation
    for f in factors:
        t0 = time.time()
        try:
            factor_panel = f.compute(panel)
            metrics = evaluate_factor(
                factor_panel, panel,
                factor_name=f.name,
                universe=args.universe_tag,
            )
            elapsed = time.time() - t0
            print(f"  {f.name:26s} {f.category:14s} "
                  f"IC IR={metrics.ic_ir:+6.3f}  RankIC IR={metrics.rank_ic_ir:+6.3f}  "
                  f"L-S Sharpe={metrics.ls_sharpe:+5.2f}  "
                  f"cov={metrics.coverage_pct:5.1f}%  ({elapsed:4.1f}s)")
            results.append(metrics)
            factor_panels[f.name] = factor_panel

            # Cache factor values to Parquet
            if not args.no_cache:
                cache_factor_values(f.name, args.universe_tag, factor_panel)
        except Exception as e:
            print(f"  {f.name:26s} FAILED: {type(e).__name__}: {e}")

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

    # ── Decorrelation matrix + clustering ──
    if len(factor_panels) >= 2:
        print(f"\nComputing factor correlation matrix ({len(factor_panels)} factors)...")
        corr = factor_correlation_matrix(factor_panels, use_ranks=True)
        clusters = cluster_factors(corr, threshold=args.cluster_threshold)
        print(f"  → {len(clusters)} clusters at corr threshold {args.cluster_threshold}")

        metrics_by_name = {m.factor_name: m for m in results}
        decorrelated = select_top_per_cluster(metrics_by_name, clusters, "rank_ic_ir")
        print(f"  → Decorrelated set: {len(decorrelated)} factors\n")
        for name in decorrelated:
            m = metrics_by_name[name]
            print(f"     • {name:30s} RankIC IR {m.rank_ic_ir:+.3f}  L-S Sharpe {m.ls_sharpe:+.2f}")

        # Write decorrelation report
        decorr_md = PROJECT_ROOT / "results" / f"factor_decorrelation_{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        write_correlation_report(corr, clusters, decorr_md)
        print(f"\nDecorrelation report → {decorr_md}")

    # ── DB persistence ──
    if not args.no_db and results:
        try:
            print("\nSaving to FactorEvaluationRecord...")
            inserted = asyncio.run(save_metrics_batch(results))
            print(f"  Inserted {inserted} rows.")
        except Exception as e:
            print(f"  WARN: DB save failed: {e}")

    # ── Markdown catalog ──
    summary_df = metrics_to_summary_df(results)
    cat_md = PROJECT_ROOT / "results" / f"factor_catalog_{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    write_catalog_markdown(summary_df, cat_md)
    print(f"\nCatalog markdown → {cat_md}")

    # ── JSON catalog (raw metrics) ──
    out = args.out
    if out is None:
        out = PROJECT_ROOT / "results" / f"factor_catalog_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    Path(out).parent.mkdir(exist_ok=True)
    with open(out, "w") as f:
        json.dump([m.to_dict() for m in results], f, indent=2, default=str)
    print(f"JSON dump → {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
