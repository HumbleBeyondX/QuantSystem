"""Persistence layer for the factor catalog.

Writes FactorMetrics rows to the existing FactorEvaluationRecord
SQLAlchemy model and Parquet caches for the factor values themselves.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from core.database.engine import get_session, init_db
from core.database.repository import FactorEvaluationRepo
from research.factor_mining.evaluator import FactorMetrics


# ────────────────────────────────────────────────────────────
# Local Parquet cache for raw factor values
# ────────────────────────────────────────────────────────────

PARQUET_ROOT = Path("data/factors")


def cache_factor_values(
    factor_name: str,
    universe: str,
    factor_panel: pd.DataFrame,
    *,
    root: Path = PARQUET_ROOT,
) -> Path:
    """Save factor values (date × ticker) to Parquet for re-use."""
    out_dir = root / universe
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{factor_name}.parquet"
    # Reset index so timestamp becomes a column for round-tripping
    df = factor_panel.copy()
    df.index.name = "timestamp"
    df.reset_index().to_parquet(out_path, index=False)
    return out_path


def load_factor_values(
    factor_name: str,
    universe: str,
    *,
    root: Path = PARQUET_ROOT,
) -> Optional[pd.DataFrame]:
    """Load cached factor values."""
    p = root / universe / f"{factor_name}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    return df.set_index("timestamp")


# ────────────────────────────────────────────────────────────
# DB persistence
# ────────────────────────────────────────────────────────────

async def save_metrics_batch(
    metrics_list: List[FactorMetrics],
    timeframe: str = "1d",
) -> int:
    """Insert FactorEvaluationRecord rows for a batch of metrics.

    Returns the number of rows inserted.
    """
    await init_db(create_tables=True)

    inserted = 0
    async with get_session() as session:
        for m in metrics_list:
            try:
                # Map FactorMetrics → FactorEvaluationRecord fields
                # decay dict → JSON
                decay_json = json.dumps(m.decay) if m.decay else None
                # Use best horizon as the record's horizon
                eval_config_json = json.dumps({
                    "rank_ic_mean": m.rank_ic_mean,
                    "rank_ic_ir": m.rank_ic_ir,
                    "ls_sharpe": m.ls_sharpe,
                    "ls_max_dd": m.ls_max_dd,
                    "ls_turnover_ann": m.ls_turnover_ann,
                    "monotonicity": m.monotonicity,
                    "autocorr_1d": m.autocorr_1d,
                    "coverage_pct": m.coverage_pct,
                    "q1_ret_ann": m.q1_ret_ann,
                    "q5_ret_ann": m.q5_ret_ann,
                    "quintile_spread_bps": m.quintile_spread_bps,
                })
                await FactorEvaluationRepo.insert(
                    session,
                    factor_name=m.factor_name,
                    category="",  # we no longer pass category here; could lookup
                    symbol=m.universe or "panel",
                    timeframe=timeframe,
                    horizon_bars=m.best_horizon,
                    ic_mean=m.ic_mean,
                    ic_std=m.ic_std,
                    ir=m.ic_ir,
                    rank_ic=m.rank_ic_mean,
                    quintile_spread_bps=m.quintile_spread_bps,
                    t_stat=m.ic_t_stat,
                    n_obs=m.n_obs,
                    decay_json=decay_json,
                    eval_config_json=eval_config_json,
                    start_date=m.period_start,
                    end_date=m.period_end,
                )
                inserted += 1
            except Exception as e:
                print(f"  WARN: failed to save {m.factor_name}: {e}")
        await session.commit()
    return inserted


# ────────────────────────────────────────────────────────────
# Catalog summary
# ────────────────────────────────────────────────────────────

def metrics_to_summary_df(metrics_list: List[FactorMetrics]) -> pd.DataFrame:
    """Flatten metrics list to a single DataFrame for sorting/display."""
    rows = []
    for m in metrics_list:
        rows.append({
            "factor_name": m.factor_name,
            "universe": m.universe,
            "ic_mean": m.ic_mean,
            "ic_ir": m.ic_ir,
            "rank_ic_mean": m.rank_ic_mean,
            "rank_ic_ir": m.rank_ic_ir,
            "ic_t_stat": m.ic_t_stat,
            "rank_ic_t_stat": m.rank_ic_t_stat,
            "best_horizon": m.best_horizon,
            "decay_1d": m.decay.get(1, 0.0),
            "decay_5d": m.decay.get(5, 0.0),
            "decay_21d": m.decay.get(21, 0.0),
            "decay_63d": m.decay.get(63, 0.0),
            "ls_sharpe": m.ls_sharpe,
            "ls_max_dd": m.ls_max_dd,
            "ls_turnover_ann": m.ls_turnover_ann,
            "monotonicity": m.monotonicity,
            "autocorr_1d": m.autocorr_1d,
            "quintile_spread_bps": m.quintile_spread_bps,
            "q1_ret_ann": m.q1_ret_ann,
            "q5_ret_ann": m.q5_ret_ann,
            "coverage_pct": m.coverage_pct,
            "n_obs": m.n_obs,
            "period_start": m.period_start,
            "period_end": m.period_end,
        })
    return pd.DataFrame(rows)


def write_catalog_markdown(
    summary_df: pd.DataFrame,
    out_path: Path,
    *,
    top_n: int = 50,
) -> Path:
    """Write a human-readable Markdown catalog sorted by |Rank IC IR|."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    df = summary_df.copy()
    df = df.sort_values("rank_ic_ir", key=lambda s: s.abs(), ascending=False)
    df_top = df.head(top_n)

    lines = []
    lines.append(f"# Factor Catalog — generated {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append(f"Total factors: {len(df)} · top {min(top_n, len(df))} shown below.")
    lines.append("")
    lines.append("| # | Factor | Cat | IC | RankIC | RankIC IR | t-stat | Decay 21d | L-S Shp | L-S DD | Q-spread bps | Mono | Turn | Cov |")
    lines.append("|---|--------|-----|----|----|-----|----|-----|------|------|------|------|------|------|")
    for i, (_, r) in enumerate(df_top.iterrows(), 1):
        lines.append(
            f"| {i} | `{r['factor_name']}` | "
            f"{str(r['universe'])[:8]} | "
            f"{r['ic_mean']:+.4f} | {r['rank_ic_mean']:+.4f} | "
            f"**{r['rank_ic_ir']:+.3f}** | {r['rank_ic_t_stat']:+.2f} | "
            f"{r['decay_21d']:+.4f} | "
            f"{r['ls_sharpe']:+.2f} | {r['ls_max_dd']*100:.0f}% | "
            f"{r['quintile_spread_bps']:+.0f} | "
            f"{r['monotonicity']:+.2f} | "
            f"{r['ls_turnover_ann']:.1f} | "
            f"{r['coverage_pct']:.0f}% |"
        )

    lines.append("")
    lines.append("## Acceptance bar")
    lines.append("")
    lines.append("- **Strong**: `|RankIC IR| > 0.20`")
    lines.append("- **Significant**: `|t-stat| > 2.0` (Rank IC)")
    lines.append("- **Tradable L-S**: `Sharpe > 1.0` AND `MaxDD < 30%`")

    strong = (df["rank_ic_ir"].abs() > 0.20).sum()
    sig = (df["rank_ic_t_stat"].abs() > 2.0).sum()
    tradable = ((df["ls_sharpe"] > 1.0) & (df["ls_max_dd"] < 0.30)).sum()
    lines.append("")
    lines.append(f"- Strong: **{strong} / {len(df)}**")
    lines.append(f"- Significant: **{sig} / {len(df)}**")
    lines.append(f"- Tradable L-S: **{tradable} / {len(df)}**")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def write_correlation_report(
    corr_matrix: pd.DataFrame,
    clusters: Dict[int, List[str]],
    out_path: Path,
) -> Path:
    """Write a markdown report of the factor correlation clusters."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append(f"# Factor Decorrelation — generated {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append(f"## Cluster summary ({len(clusters)} clusters at corr threshold)")
    lines.append("")
    for cid, names in sorted(clusters.items()):
        lines.append(f"### Cluster {cid} ({len(names)} factors)")
        for n in names:
            lines.append(f"- `{n}`")
        lines.append("")

    lines.append("## Pairwise rank-correlation matrix (top entries)")
    lines.append("")
    # Get top 20 pairs by |correlation| (off-diagonal)
    pairs = []
    cols = corr_matrix.columns
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            pairs.append((cols[i], cols[j], corr_matrix.iat[i, j]))
    pairs.sort(key=lambda x: abs(x[2]), reverse=True)
    lines.append("| Rank | Factor A | Factor B | Corr |")
    lines.append("|------|----------|----------|------|")
    for i, (a, b, c) in enumerate(pairs[:30], 1):
        lines.append(f"| {i} | `{a}` | `{b}` | {c:+.3f} |")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
