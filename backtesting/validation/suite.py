"""Standard robustness validation suite — institutional gate machinery.

Born 2026-07-12 after the sniper loss-cap incident: a single headline
Sharpe is never again sufficient. Every candidate record should attach a
validation report from this suite before the registry calls it validated.

Four tools:
  fee_stress(run)       — friction sensitivity: rerun at 0.5/1/2/3x costs,
                          find the break-even multiplier. Needs a rerun
                          callable: run(cost_mult) -> daily equity Series.
  walk_forward(equity)  — K contiguous folds of the realized curve; fold
                          Sharpe consistency (fixed params — this detects
                          regime concentration, not refit quality).
  leave_one_out(equity) — drop each calendar year, recompute Sharpe;
                          flags single-year dependence (the PLTR trap).
  factor IC             — for factor research use
                          research.factor_mining.evaluator.evaluate_factor
                          (per-date IC / ICIR machinery already exists).

validation_report() bundles them with pass/fail flags. Pure computation,
no I/O.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

import numpy as np
import pandas as pd


def _sharpe(r: pd.Series, ann: int = 365) -> Optional[float]:
    r = r.dropna()
    if len(r) < 30 or r.std() == 0:
        return None
    return round(float(r.mean() / r.std() * np.sqrt(ann)), 2)


def _daily_returns(equity: pd.Series) -> pd.Series:
    eq = equity.dropna()
    idx = pd.to_datetime(eq.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    eq.index = idx.normalize()
    eq = eq.groupby(eq.index).last().sort_index()
    return eq.pct_change().dropna()


def fee_stress(run: Callable[[float], pd.Series],
               multipliers: Sequence[float] = (0.5, 1.0, 2.0, 3.0),
               ann: int = 365) -> Dict[str, Any]:
    """Sharpe vs cost multiplier + break-even estimate.

    run(cost_mult) must rerun the SAME strategy with all frictions
    (fees/spread/slippage) scaled by cost_mult and return an equity Series.
    """
    points = []
    for m in multipliers:
        try:
            sh = _sharpe(_daily_returns(run(float(m))), ann)
        except Exception as e:
            sh = None
            points.append({"cost_mult": m, "sharpe": None,
                           "error": str(e)[:80]})
            continue
        points.append({"cost_mult": m, "sharpe": sh})
    valid = [(p["cost_mult"], p["sharpe"]) for p in points
             if p["sharpe"] is not None]
    breakeven = None
    for (m0, s0), (m1, s1) in zip(valid, valid[1:]):
        if s0 is not None and s1 is not None and s0 > 0 >= s1:
            breakeven = round(m0 + (m1 - m0) * s0 / (s0 - s1), 2)
            break
    if breakeven is None and valid and valid[-1][1] is not None and valid[-1][1] > 0:
        breakeven = f"> {valid[-1][0]}"
    return {"points": points, "breakeven_mult": breakeven,
            "passed": (isinstance(breakeven, str)
                       or (breakeven is not None and breakeven >= 2.0))}


def walk_forward(equity: pd.Series, n_folds: int = 5,
                 ann: int = 365) -> Dict[str, Any]:
    """Fold-wise Sharpe of the realized curve (fixed parameters)."""
    r = _daily_returns(equity)
    if len(r) < n_folds * 60:
        return {"error": f"need >= {n_folds * 60} daily returns, have {len(r)}",
                "passed": None}
    folds = np.array_split(np.arange(len(r)), n_folds)
    rows = []
    for i, ix in enumerate(folds):
        rr = r.iloc[ix]
        rows.append({"fold": i + 1,
                     "start": str(rr.index[0].date()),
                     "end": str(rr.index[-1].date()),
                     "sharpe": _sharpe(rr, ann)})
    sharpes = [x["sharpe"] for x in rows if x["sharpe"] is not None]
    pct_pos = round(sum(s > 0 for s in sharpes) / len(sharpes), 2) if sharpes else None
    return {"folds": rows,
            "median_sharpe": round(float(np.median(sharpes)), 2) if sharpes else None,
            "min_sharpe": min(sharpes) if sharpes else None,
            "pct_positive_folds": pct_pos,
            # regime-robust = most folds positive and no catastrophic fold
            "passed": (pct_pos is not None and pct_pos >= 0.6
                       and min(sharpes) > -1.0)}


def leave_one_out(equity: pd.Series, unit: str = "year",
                  ann: int = 365) -> Dict[str, Any]:
    """Drop each calendar period; how much of the Sharpe was ONE period?"""
    r = _daily_returns(equity)
    base = _sharpe(r, ann)
    if base is None:
        return {"error": "curve too short", "passed": None}
    key = r.index.year if unit == "year" else r.index.to_period("Q")
    rows = []
    for u in sorted(set(key)):
        rr = r[key != u]
        rows.append({"excluded": str(u), "sharpe": _sharpe(rr, ann),
                     "n_days_dropped": int((key == u).sum())})
    valid = [x["sharpe"] for x in rows if x["sharpe"] is not None]
    worst = min(valid) if valid else None
    return {"base_sharpe": base, "excluding": rows, "worst_case": worst,
            # dependence flag: removing any single period must not erase
            # more than half the edge (nor flip its sign)
            "passed": (worst is not None and base is not None
                       and worst > 0 and worst >= 0.5 * base)}


def validation_report(name: str,
                      equity: pd.Series,
                      run: Optional[Callable[[float], pd.Series]] = None,
                      ann: int = 365,
                      n_folds: int = 5) -> Dict[str, Any]:
    """Bundle the suite. `run` optional — fee stress skipped without it."""
    rep: Dict[str, Any] = {
        "name": name,
        "base_sharpe": _sharpe(_daily_returns(equity), ann),
        "walk_forward": walk_forward(equity, n_folds, ann),
        "leave_one_out": leave_one_out(equity, "year", ann),
    }
    if run is not None:
        rep["fee_stress"] = fee_stress(run, ann=ann)
    checks = [rep["walk_forward"].get("passed"),
              rep["leave_one_out"].get("passed")]
    if run is not None:
        checks.append(rep["fee_stress"].get("passed"))
    rep["all_passed"] = all(c is True for c in checks)
    rep["checks"] = {"walk_forward": rep["walk_forward"].get("passed"),
                     "leave_one_out": rep["leave_one_out"].get("passed"),
                     "fee_stress": (rep.get("fee_stress", {}).get("passed")
                                    if run is not None else "skipped")}
    return rep
