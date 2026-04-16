#!/usr/bin/env python3
"""Batch-evaluate all registered factors on ETH 1H data.

Loads ETH + BTC 1H OHLCV, runs ``research.factors.get_all_factors()``,
scores each factor across multiple forward-return horizons, and writes
results to:

  1. Console (ranked table by |IR|)
  2. Local SQLite via FactorEvaluationRepo
  3. JSON sidecar at <user_data>/data/factor_eval/<YYYYMMDD>.json
  4. (Optional) Supabase cloud via CloudSyncScheduler on the next tick

Usage:
    python scripts/evaluate_factors.py
    python scripts/evaluate_factors.py --category microstructure
    python scripts/evaluate_factors.py --eth-csv path/to/ETH_1h.csv
    python scripts/evaluate_factors.py --horizons 1 4 24
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.database.engine import get_session, init_db  # noqa: E402
from core.database.repository import FactorEvaluationRepo  # noqa: E402
from core.logging import get_logger, setup_logging  # noqa: E402
from core.paths import get_data_dir  # noqa: E402
from research.factor_eval import evaluate_factor, format_summary_row  # noqa: E402
from research.factors import get_all_factors  # noqa: E402

logger = get_logger(__name__)


DEFAULT_ETH = "data/sample/ETH_USDT_1h_2024-01-01_to_2026-04-16_okx.csv"
DEFAULT_BTC = "data/sample/BTC_USDT_1h_2024-01-01_to_2026-04-16_okx.csv"


def load_merged_data(
    eth_csv: Path,
    btc_csv: Optional[Path] = None,
) -> pd.DataFrame:
    """Load ETH OHLCV and optionally merge BTC close as 'btc_close'."""
    df = pd.read_csv(eth_csv, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    logger.info("loaded_eth", bars=len(df), first=str(df["timestamp"].iloc[0]),
                last=str(df["timestamp"].iloc[-1]))

    if btc_csv and btc_csv.exists():
        btc = pd.read_csv(btc_csv, parse_dates=["timestamp"])
        btc = btc[["timestamp", "close"]].rename(columns={"close": "btc_close"})
        df = df.merge(btc, on="timestamp", how="left")
        coverage = df["btc_close"].notna().mean() * 100
        logger.info("merged_btc", coverage_pct=round(coverage, 2))
    else:
        logger.info("no_btc_data_cross_asset_factors_will_be_skipped")

    return df


async def persist_to_sqlite(results: List, eval_config: dict) -> int:
    """Insert FactorEvaluationRecord rows for each (factor, horizon) result."""
    from sqlalchemy.ext.asyncio import AsyncSession
    await init_db(create_tables=True)

    inserted = 0
    async with get_session() as session:  # type: AsyncSession
        for r in results:
            for h, hr in r.by_horizon.items():
                await FactorEvaluationRepo.insert(
                    session,
                    factor_name=r.factor_name,
                    category=r.category,
                    symbol=r.symbol,
                    timeframe=r.timeframe,
                    horizon_bars=h,
                    ic_mean=hr.ic_mean,
                    ic_std=hr.ic_std,
                    ir=hr.ir,
                    rank_ic=hr.rank_ic,
                    quintile_spread_bps=hr.quintile_spread_bps,
                    t_stat=hr.t_stat,
                    n_obs=hr.n_obs,
                    ic_series_json=json.dumps(r.ic_series[:500]) if r.ic_series else None,
                    decay_json=json.dumps({str(k): v.ic_mean for k, v in r.by_horizon.items()}),
                    eval_config_json=json.dumps(eval_config),
                    start_date=r.start_date,
                    end_date=r.end_date,
                )
                inserted += 1
        await session.commit()
    return inserted


def print_ranked_table(results: List) -> None:
    rows = [format_summary_row(r) for r in results if r.by_horizon]
    df = pd.DataFrame(rows)
    if df.empty:
        print("No factor evaluations produced results.")
        return
    df = df.sort_values("ir", key=lambda s: s.abs(), ascending=False)

    print("\n" + "=" * 100)
    print(f"{'Factor':<32} {'Category':<22} {'Best H':>6} {'IR':>7} {'RankIC':>8} "
          f"{'tstat':>7} {'QSpread':>9} {'N':>6}")
    print("-" * 100)
    for _, r in df.iterrows():
        print(f"{r['factor']:<32} {r['category']:<22} "
              f"{str(r['best_h']):>6} {r['ir']:>7.3f} {r['rank_ic']:>8.4f} "
              f"{r['t_stat']:>7.2f} {r['qspread_bps']:>9,.0f} {r['n_obs']:>6,}")
    print("=" * 100)

    # Summary stats
    strong = df[df["ir"].abs() > 0.1]
    signif = df[df["t_stat"].abs() > 2.0]
    print(f"\nStrong (|IR| > 0.1): {len(strong)}/{len(df)}")
    print(f"Significant (|t| > 2): {len(signif)}/{len(df)}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Batch-evaluate alpha factors on ETH 1H data")
    p.add_argument("--eth-csv", default=DEFAULT_ETH, help="Path to ETH 1H CSV")
    p.add_argument("--btc-csv", default=DEFAULT_BTC, help="Path to BTC 1H CSV (for cross-asset factors)")
    p.add_argument("--category", default=None, help="Filter to one category")
    p.add_argument("--horizons", nargs="+", type=int, default=[1, 4, 8, 24, 48],
                   help="Forward-return horizons in bars")
    p.add_argument("--save-db", action="store_true", default=True, help="Persist to SQLite (default: on)")
    p.add_argument("--no-save", dest="save_db", action="store_false", help="Disable DB persistence")
    return p.parse_args()


def main() -> int:
    setup_logging(level="INFO", json_format=False)
    args = parse_args()

    eth_path = Path(args.eth_csv)
    btc_path = Path(args.btc_csv) if args.btc_csv else None

    if not eth_path.exists():
        print(f"ERROR: ETH CSV not found: {eth_path}")
        print("Run:  python scripts/fetch_eth_1h_data.py  to download it.")
        return 1

    df = load_merged_data(eth_path, btc_path)

    factors = get_all_factors(category=args.category)
    print(f"\nEvaluating {len(factors)} factors on {len(df):,} bars...")

    start_date = str(df["timestamp"].iloc[0])
    end_date = str(df["timestamp"].iloc[-1])

    results = []
    t0 = time.time()
    for f in factors:
        try:
            factor_vals = f.compute(df)
            if factor_vals.notna().sum() < 100:
                print(f"  SKIP {f.name}: only {factor_vals.notna().sum()} non-null values")
                continue
            res = evaluate_factor(
                factor_name=f.name,
                category=f.category,
                factor_values=factor_vals,
                close=df["close"],
                start_date=start_date,
                end_date=end_date,
                horizons=args.horizons,
            )
            results.append(res)
            print(f"  OK   {f.name:32s} {len(res.by_horizon)} horizons")
        except Exception as e:
            print(f"  FAIL {f.name}: {e}")

    elapsed = time.time() - t0
    print(f"\nCompleted in {elapsed:.1f}s")

    print_ranked_table(results)

    if args.save_db:
        eval_config = {
            "eth_csv": str(eth_path.name),
            "horizons": args.horizons,
            "category_filter": args.category,
        }
        try:
            inserted = asyncio.run(persist_to_sqlite(results, eval_config))
            print(f"\n[DB] Inserted {inserted} factor_evaluation rows")
        except Exception as e:
            print(f"\n[DB] Persistence failed: {e}")

    # Also write a JSON sidecar for easy browsing
    out_dir = get_data_dir() / "factor_eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    with open(out_path, "w") as f:
        json.dump({"results": [r.to_dict() for r in results]}, f, indent=2, default=str)
    print(f"[JSON] {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
