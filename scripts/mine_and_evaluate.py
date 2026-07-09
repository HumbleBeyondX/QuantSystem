#!/usr/bin/env python3
"""Phase C deliverable: mine formula factors and evaluate them together
with the hand-curated library on the SP500 panel.

Pipeline:
  1. Load SP500 panel (153 tickers, ~2015-2026).
  2. Evaluate all hand-curated + alpha101 factors with the industry
     evaluator.
  3. Generate N random formula factors via FormulaMiner.
  4. Apply walk-forward filter: keep mined factors with OOS rank IC
     preserving ≥50% of IS.
  5. Run full FactorMetrics on the survivors.
  6. Combine, rank, write catalog Markdown + JSON.

Usage:
    python scripts/mine_and_evaluate.py
    python scripts/mine_and_evaluate.py --n-mined 200 --is-frac 0.6 \
        --start 2018-01-01 --panel data/sample/sp500_2015-01-01_2026-05-04.parquet
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Dict, List

warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

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
from research.factor_mining.miner import (  # noqa: E402
    FormulaMiner,
    walk_forward_filter,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--panel",
                   default="data/sample/sp500_2015-01-01_2026-05-04.parquet")
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--n-mined", type=int, default=150,
                   help="Number of random formula factors to generate")
    p.add_argument("--is-frac", type=float, default=0.6,
                   help="In-sample fraction for walk-forward filter")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--universe-tag", default="sp500_153")
    p.add_argument("--max-tickers", type=int, default=None,
                   help="Restrict universe to first N tickers (debug)")
    p.add_argument("--skip-hand", action="store_true",
                   help="Skip hand-curated factors (only run miner)")
    p.add_argument("--out-dir", default="results")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    panel_path = Path(args.panel)
    if not panel_path.exists():
        print(f"ERROR: panel parquet not found: {panel_path}")
        return 1

    print(f"Loading panel: {panel_path.name}")
    panel = DataPanel.from_parquet(panel_path)
    universe = panel.universe
    if args.max_tickers:
        universe = universe[: args.max_tickers]
    panel = panel.restrict(universe=universe, start=args.start, end=args.end)
    print(f"  {panel}")
    print(f"  Universe size: {len(panel.universe)} tickers\n")

    results: List[FactorMetrics] = []
    factor_panels: Dict[str, pd.DataFrame] = {}

    # ── A. Hand-curated + alpha101 ──
    if not args.skip_hand:
        hand = get_all_factors()
        print(f"Evaluating {len(hand)} hand-curated + alpha101 factors...")
        for f in hand:
            t0 = time.time()
            try:
                fp = f.compute(panel)
                m = evaluate_factor(fp, panel,
                                    factor_name=f.name, universe=args.universe_tag)
                elapsed = time.time() - t0
                print(f"  {f.name:24s} {f.category:10s} "
                      f"RankIC IR={m.rank_ic_ir:+6.3f}  "
                      f"L-S Shp={m.ls_sharpe:+5.2f}  ({elapsed:4.1f}s)")
                results.append(m)
                factor_panels[f.name] = fp
            except Exception as e:
                print(f"  {f.name:24s} FAILED: {type(e).__name__}: {e}")

    # ── B. Formula mining ──
    print(f"\nGenerating {args.n_mined} mined formulas (seed={args.seed})...")
    miner = FormulaMiner(seed=args.seed)
    mined = miner.generate(n=args.n_mined)

    print(f"Walk-forward filter: IS={args.is_frac*100:.0f}%, "
          f"horizon=5d, decay≥0.5...")
    t0 = time.time()
    wf = walk_forward_filter(mined, panel, is_frac=args.is_frac,
                              horizon=5, min_is_abs_ic=0.005,
                              min_oos_decay=0.5)
    survivors = [r for r in wf if r.passed]
    print(f"  → {len(wf)} computable / {len(survivors)} passed "
          f"(time={time.time()-t0:.1f}s)")

    # ── C. Full evaluation on survivors ──
    print(f"\nEvaluating {len(survivors)} mined survivors with full metrics...")
    for r in survivors:
        f = r.factor
        try:
            fp = f.compute(panel)
            m = evaluate_factor(fp, panel,
                                factor_name=f.name, universe=args.universe_tag)
            print(f"  {f.name:24s} mined      "
                  f"RankIC IR={m.rank_ic_ir:+6.3f}  "
                  f"L-S Shp={m.ls_sharpe:+5.2f}  "
                  f"decay={r.decay:.2f}  "
                  f"formula={f.formula[:60]}")
            results.append(m)
            factor_panels[f.name] = fp
        except Exception as e:
            print(f"  {f.name:24s} FAILED: {type(e).__name__}: {e}")

    if not results:
        print("\nNo factors evaluated.")
        return 1

    # ── D. Ranked table ──
    df = pd.DataFrame([m.to_dict() for m in results])
    df = df.sort_values("rank_ic_ir", key=lambda s: s.abs(), ascending=False)

    print("\n" + "=" * 130)
    print("FACTOR LIBRARY — Ranked by |Rank IC IR|")
    print("=" * 130)
    head = df.head(25)
    for i, (_, r) in enumerate(head.iterrows(), 1):
        cat = r.get("universe", "")[:10]
        print(f"{i:3d}. {r['factor_name']:28s} {cat:11s} "
              f"RankIC IR={r['rank_ic_ir']:+6.3f}  "
              f"t={r['rank_ic_t_stat']:+5.2f}  "
              f"L-S Shp={r['ls_sharpe']:+5.2f}  "
              f"DD={r['ls_max_dd']:5.1%}  "
              f"mono={r['monotonicity']:+4.2f}")
    print("=" * 130)

    # Summary stats
    strong = df[df["rank_ic_ir"].abs() > 0.20]
    tradable = df[(df["ls_sharpe"] > 1.0) & (df["ls_max_dd"] < 0.30)]
    hi_sharpe = df[df["ls_sharpe"] > 1.5]
    print(f"\nStrong (|RankIC IR| > 0.20):     {len(strong)}/{len(df)}")
    print(f"Tradable (Shp > 1.0, DD < 30%):  {len(tradable)}/{len(df)}")
    print(f"Above user gate (Shp > 1.5):      {len(hi_sharpe)}/{len(df)}")

    # ── E. Decorrelation ──
    if len(factor_panels) >= 3:
        print(f"\nCorrelation + clustering on {len(factor_panels)} factors...")
        corr = factor_correlation_matrix(factor_panels, use_ranks=True)
        clusters = cluster_factors(corr, threshold=0.7)
        metrics_by_name = {m.factor_name: m for m in results}
        decorrelated = select_top_per_cluster(metrics_by_name, clusters, "rank_ic_ir")
        print(f"  → {len(clusters)} clusters, "
              f"{len(decorrelated)} factors in decorrelated set")
        # Decorrelated tradable set
        decorr_tradable = [
            n for n in decorrelated
            if metrics_by_name[n].ls_sharpe > 1.0
               and metrics_by_name[n].ls_max_dd < 0.30
        ]
        print(f"  → {len(decorr_tradable)} decorrelated AND tradable factors")
        for n in decorr_tradable[:15]:
            m = metrics_by_name[n]
            print(f"     • {n:30s} RankIC IR {m.rank_ic_ir:+.3f}  "
                  f"L-S Sharpe {m.ls_sharpe:+.2f}  DD {m.ls_max_dd:.1%}")

    # ── F. Persist ──
    out_dir = PROJECT_ROOT / args.out_dir
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"mined_catalog_{ts}.json"
    md_path = out_dir / f"mined_catalog_{ts}.md"
    with open(json_path, "w") as f:
        json.dump([m.to_dict() for m in results], f, indent=2, default=str)
    print(f"\nJSON catalog → {json_path}")

    # Markdown report
    lines = [
        f"# Mined Factor Catalog — generated {datetime.now().isoformat(timespec='seconds')}",
        f"",
        f"Universe: **{args.universe_tag}** · Panel: `{panel_path.name}` · "
        f"Window: {args.start} → {args.end or 'latest'}",
        f"",
        f"Total factors: **{len(df)}** "
        f"(hand+alpha101: {len(df) - len(survivors)}, "
        f"mined survivors: {len(survivors)})",
        f"",
        f"## Acceptance",
        f"- Strong (|RankIC IR| > 0.20):      **{len(strong)}/{len(df)}**",
        f"- Tradable (Shp>1.0, DD<30%):       **{len(tradable)}/{len(df)}**",
        f"- Above user gate (L-S Shp > 1.5):  **{len(hi_sharpe)}/{len(df)}**",
        f"",
        f"## Top 25 by |Rank IC IR|",
        f"",
        f"| # | Factor | Cat | RankIC IR | t-stat | L-S Shp | L-S DD | "
        f"Spread bps | Mono | Turn |",
        f"|---|--------|-----|-----------|--------|---------|--------|"
        f"------------|------|------|",
    ]
    for i, (_, r) in enumerate(head.iterrows(), 1):
        lines.append(
            f"| {i} | `{r['factor_name']}` | {r['universe']} | "
            f"{r['rank_ic_ir']:+.3f} | {r['rank_ic_t_stat']:+.2f} | "
            f"{r['ls_sharpe']:+.2f} | {r['ls_max_dd']:.1%} | "
            f"{r['quintile_spread_bps']:+.0f} | "
            f"{r['monotonicity']:+.2f} | {r['ls_turnover_ann']:.1f} |"
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Markdown report → {md_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
