#!/usr/bin/env python3
"""Daily Gamma Squeeze candidate scanner — CLI wrapper.

Examples:
    python scripts/scan_gamma_squeeze.py
    python scripts/scan_gamma_squeeze.py --top 20 --max-dte 45
    python scripts/scan_gamma_squeeze.py --tickers GME AMC PLTR  # ad-hoc subset
    python scripts/scan_gamma_squeeze.py --no-report             # console only
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.gamma_squeeze import GammaSqueezeScanner, load_universe  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Daily gamma-squeeze candidate scanner")
    p.add_argument("--universe", default=None, help="Path to universe.yaml (default: config/gamma_squeeze/universe.yaml)")
    p.add_argument("--tickers", nargs="*", default=None, help="Ad-hoc subset (overrides universe)")
    p.add_argument("--top", type=int, default=15, help="How many top candidates to display")
    p.add_argument("--max-dte", type=int, default=60, help="Max days-to-expiry for option chains")
    p.add_argument("--no-report", action="store_true", help="Skip markdown report")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def _print_table(df: pd.DataFrame, top: int) -> None:
    cols = [
        "ticker", "category", "composite_score",
        "spot", "max_oi_strike", "spot_to_max_oi_pct",
        "cp_oi_ratio", "otm_call_concentration",
        "volume_surge", "short_interest_pct", "iv_rank", "gex_proxy",
    ]
    head = df.head(top)
    print("\n" + "=" * 145)
    print(f"{'#':<3}{'Tkr':<7}{'Cat':<18}{'Score':>7}"
          f"{'Spot':>8}{'MaxOI K':>9}{'%toK':>6}"
          f"{'C/P':>5}{'OTM%':>6}{'VolSrg':>7}"
          f"{'SI%':>6}{'IVrnk':>7}{'GEX($M)':>9}")
    print("-" * 145)
    for i, row in enumerate(head.itertuples(index=False), 1):
        if pd.isna(row.composite_score):
            continue
        print(
            f"{i:<3}{row.ticker:<7}{str(row.category)[:17]:<18}"
            f"{row.composite_score:>+7.2f}"
            f"{row.spot:>8.2f}"
            f"{row.max_oi_strike:>9.2f}"
            f"{row.spot_to_max_oi_pct*100:>5.0f}%"
            f"{row.cp_oi_ratio:>5.1f}"
            f"{row.otm_call_concentration*100:>5.0f}%"
            f"{row.volume_surge:>7.2f}"
            f"{row.short_interest_pct:>5.1f}%"
            f"{row.iv_rank*100:>6.0f}%"
            f"{row.gex_proxy:>+9.2f}"
        )
    print("=" * 145)


def _write_markdown_report(df: pd.DataFrame, out_path: Path, top: int) -> Path:
    """Write a daily report under results/."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ok = df[df["error"].isnull() | (df["error"] == "")]
    bad = df[~(df["error"].isnull() | (df["error"] == ""))]

    lines = []
    lines.append(f"# Gamma Squeeze Daily Scan — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append(f"Scanned **{len(df)}** candidates · "
                 f"OK: **{len(ok)}** · skipped: **{len(bad)}** "
                 f"(liquidity / no chain / no history)")
    lines.append("")
    lines.append("## Top setups")
    lines.append("")
    lines.append("| # | Tkr | Cat | Score | Spot | MaxOI K | %→K | C/P | OTM% | VolSurg | SI% | IVrk | GEX($M) | NextExp |")
    lines.append("|---|-----|-----|-------|------|---------|-----|-----|------|---------|-----|------|---------|---------|")
    head = ok.head(top)
    for i, row in enumerate(head.itertuples(index=False), 1):
        if pd.isna(row.composite_score):
            continue
        lines.append(
            f"| {i} | **{row.ticker}** | {row.category} | "
            f"{row.composite_score:+.2f} | "
            f"${row.spot:.2f} | "
            f"${row.max_oi_strike:.2f} | "
            f"{row.spot_to_max_oi_pct*100:.0f}% | "
            f"{row.cp_oi_ratio:.1f} | "
            f"{row.otm_call_concentration*100:.0f}% | "
            f"{row.volume_surge:.2f}x | "
            f"{row.short_interest_pct:.1f}% | "
            f"{row.iv_rank*100:.0f}% | "
            f"{row.gex_proxy:+.2f} | "
            f"{row.nearest_expiry} |"
        )

    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append("- **Score > +1.5** = strong setup; multiple signals aligned")
    lines.append("- **C/P > 2.0** = retail call-buying skew")
    lines.append("- **OTM%** = share of call OI parked in 5-15% OTM strikes (gamma fuel)")
    lines.append("- **VolSurg** = today's volume vs 30-day average (attention spike)")
    lines.append("- **SI%** = short interest % of float (squeeze accelerant)")
    lines.append("- **IVrk** = inverted IV rank — higher score = more room to expand")
    lines.append("- **GEX** = call-OI dollar gamma minus put-OI dollar gamma "
                 "(positive = dealers short more calls than puts ≈ squeeze setup)")
    lines.append("")
    lines.append("## Trade framework (research stage — paper trade first)")
    lines.append("")
    lines.append("- **Entry**: score > +1.5 **AND** spot breaks 5-day high **AND** IVrk < 50%")
    lines.append("- **Size**: 1-2% account risk per trade (small float = wide spreads = real slippage)")
    lines.append("- **Exit**: +5% profit target OR IV crush > 30% OR 5-day time stop OR -3% stop")
    lines.append("")

    if not bad.empty:
        lines.append("## Skipped (data issues)")
        lines.append("")
        for _, row in bad.iterrows():
            lines.append(f"- `{row['ticker']}` ({row['category']}): {row.get('error', 'unknown')}")
        lines.append("")

    lines.append("## Limitations (don't ignore)")
    lines.append("")
    lines.append("- yfinance returns **current snapshot only** — no historical option chain. "
                 "**No proper backtest is possible** without a paid feed (Polygon, Tradier).")
    lines.append("- Short interest is **stale by up to 2 weeks** (FINRA bi-monthly).")
    lines.append("- IV rank uses realized vol percentile as a proxy (no historical IV term structure).")
    lines.append("- GEX proxy is **indicative** — proper dealer positioning requires CBOE LiveVol or similar.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Generated by `scripts/scan_gamma_squeeze.py`")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def main() -> int:
    args = parse_args()

    universe_path = Path(args.universe) if args.universe else None
    cfg = load_universe(universe_path) if universe_path else load_universe()

    if args.tickers:
        # Override universe with ad-hoc subset
        cfg = {
            "categories": {"adhoc": {"tickers": args.tickers}},
            "scoring": cfg.get("scoring", {}),
        }

    scanner = GammaSqueezeScanner(max_dte=args.max_dte)
    t0 = time.time()
    df = scanner.scan_universe(cfg, verbose=args.verbose)
    elapsed = time.time() - t0
    print(f"Scanned {len(df)} tickers in {elapsed:.1f}s")

    _print_table(df, args.top)

    if not args.no_report:
        out = PROJECT_ROOT / "results" / f"gamma_squeeze_{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        report = _write_markdown_report(df, out, top=args.top)
        print(f"\nReport → {report}")

        # Also dump JSON for downstream
        json_out = out.with_suffix(".json")
        df.to_json(json_out, orient="records", indent=2)
        print(f"JSON   → {json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
