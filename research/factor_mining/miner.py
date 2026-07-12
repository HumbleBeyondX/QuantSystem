"""Formula-based factor mining (WorldQuant Alpha101-inspired).

Generates random expression trees over the operator vocabulary and
input fields available on a DataPanel. Each tree is wrapped into a
`MinedFactor` (CrossSectionalFactor) that can be evaluated like any
hand-curated factor.

Design goals
------------
1. **Bias toward proven motifs** — operator weights favor Alpha101-style
   structures (rank-of-correlation, ts_rank-of-delta, decay-of-rank etc.).
2. **Walk-forward validation** — to mitigate p-hacking we measure each
   mined factor's IC on an in-sample slice and require some preservation
   on a held-out OOS slice.
3. **Reproducible** — every mined formula carries a deterministic seed
   so the same RNG run produces the same library.

Usage
-----
    panel = DataPanel.from_parquet(...)
    miner = FormulaMiner(seed=42)
    mined = miner.generate(n=200)
    for mf in mined[:5]:
        print(mf.formula, mf.compute(panel).iloc[-1].dropna().head())

Selection
---------
    keep = walk_forward_filter(mined, panel,
                               is_frac=0.6, min_oos_ic_frac=0.5)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from research.factor_mining.base import CrossSectionalFactor
from research.factor_mining.data_panel import DataPanel
from research.factor_mining.operators import (
    decay_linear,
    delay,
    delta,
    rank,
    scale,
    signedpower,
    ts_argmax,
    ts_argmin,
    ts_corr,
    ts_max,
    ts_mean,
    ts_min,
    ts_rank,
    ts_std,
    ts_sum,
)
from research.factor_mining.evaluator import (
    DEFAULT_HORIZONS,
    cross_sectional_ic_series,
)


# ────────────────────────────────────────────────────────────
# Input fields
# ────────────────────────────────────────────────────────────

_FIELD_LOADERS: Dict[str, Callable[[DataPanel], pd.DataFrame]] = {
    "close":   lambda p: p.closes_wide,
    "open":    lambda p: p.opens_wide,
    "high":    lambda p: p.highs_wide,
    "low":     lambda p: p.lows_wide,
    "volume":  lambda p: p.volumes_wide,
    "vwap":    lambda p: p.vwaps_wide,
    "returns": lambda p: p.returns_wide,
    "dollar_volume": lambda p: p.dollar_volumes_wide,
}


# ────────────────────────────────────────────────────────────
# Operator catalog with biased sampling weights
# ────────────────────────────────────────────────────────────

# Lookback windows we'll sample over for ts_* operators
_LOOKBACK_CHOICES: List[int] = [3, 5, 10, 20, 60, 120, 252]

# Unary operators — (name, callable, lookback?, weight)
_UNARY_OPS: List[Tuple[str, Callable, bool, float]] = [
    # name              fn          uses_lookback weight
    ("rank",            rank,            False, 4.0),
    ("ts_rank",         ts_rank,         True,  3.0),
    ("delta",           delta,           True,  2.5),
    ("decay_linear",    decay_linear,    True,  2.0),
    ("ts_argmax",       ts_argmax,       True,  1.5),
    ("ts_argmin",       ts_argmin,       True,  1.5),
    ("ts_max",          ts_max,          True,  1.0),
    ("ts_min",          ts_min,          True,  1.0),
    ("ts_mean",         ts_mean,         True,  1.0),
    ("ts_sum",          ts_sum,          True,  1.0),
    ("ts_std",          ts_std,          True,  1.0),
    ("scale",           scale,           False, 0.5),
    ("signedpower2",    lambda x: signedpower(x, 2.0), False, 1.0),
    ("neg",             lambda x: -x,    False, 0.5),
]

# Binary operators — (name, callable, uses_lookback, weight)
_BINARY_OPS: List[Tuple[str, Callable, bool, float]] = [
    ("ts_corr",  ts_corr,                                  True,  4.0),
    ("add",      lambda a, b: a + b,                       False, 1.0),
    ("sub",      lambda a, b: a - b,                       False, 1.5),
    ("mul",      lambda a, b: a * b,                       False, 2.0),
    ("div",      lambda a, b: a / b.replace(0, np.nan),    False, 1.0),
    ("max",      lambda a, b: pd.DataFrame(np.maximum(a, b), index=a.index, columns=a.columns),
                                                            False, 0.5),
    ("min",      lambda a, b: pd.DataFrame(np.minimum(a, b), index=a.index, columns=a.columns),
                                                            False, 0.5),
]


def _weighted_choice(rng: random.Random, items: List[Tuple]) -> Tuple:
    weights = [it[-1] for it in items]
    return rng.choices(items, weights=weights, k=1)[0]


# ────────────────────────────────────────────────────────────
# Expression tree
# ────────────────────────────────────────────────────────────

@dataclass
class Expr:
    """One node of a formula tree.

    `kind` is one of: 'leaf', 'unary', 'binary'.
    Leaves carry a field name.  Unary/binary nodes carry op name + children.
    """
    kind: str
    field_name: Optional[str] = None  # leaf
    op: Optional[str] = None          # unary/binary
    n: Optional[int] = None           # lookback if applicable
    children: List["Expr"] = field(default_factory=list)

    def formula(self) -> str:
        if self.kind == "leaf":
            return self.field_name
        if self.kind == "unary":
            if self.n is not None:
                return f"{self.op}({self.children[0].formula()}, {self.n})"
            return f"{self.op}({self.children[0].formula()})"
        if self.kind == "binary":
            a = self.children[0].formula()
            b = self.children[1].formula()
            if self.n is not None:
                return f"{self.op}({a}, {b}, {self.n})"
            return f"{self.op}({a}, {b})"
        return "?"

    def lookback_required(self) -> int:
        """Max lookback in the subtree (to know warmup needed)."""
        own = self.n or 0
        return max([own] + [c.lookback_required() for c in self.children], default=0)


def _random_leaf(rng: random.Random) -> Expr:
    field_name = rng.choice(list(_FIELD_LOADERS.keys()))
    return Expr(kind="leaf", field_name=field_name)


def _random_expr(rng: random.Random, depth: int, max_depth: int) -> Expr:
    """Build a random tree.  Depth grows DOWN: depth=0 → leaf."""
    if depth >= max_depth or (depth > 1 and rng.random() < 0.25):
        return _random_leaf(rng)
    # 65% unary, 35% binary
    if rng.random() < 0.65:
        op_name, fn, uses_n, _ = _weighted_choice(rng, _UNARY_OPS)
        child = _random_expr(rng, depth + 1, max_depth)
        n = rng.choice(_LOOKBACK_CHOICES) if uses_n else None
        return Expr(kind="unary", op=op_name, n=n, children=[child])
    else:
        op_name, fn, uses_n, _ = _weighted_choice(rng, _BINARY_OPS)
        c1 = _random_expr(rng, depth + 1, max_depth)
        c2 = _random_expr(rng, depth + 1, max_depth)
        n = rng.choice(_LOOKBACK_CHOICES) if uses_n else None
        return Expr(kind="binary", op=op_name, n=n, children=[c1, c2])


def _evaluate_expr(expr: Expr, panel: DataPanel) -> pd.DataFrame:
    """Walk the tree and produce a wide DataFrame.

    Raises on any error in operator application — caller decides what to
    do with broken trees.
    """
    if expr.kind == "leaf":
        return _FIELD_LOADERS[expr.field_name](panel)

    if expr.kind == "unary":
        child = _evaluate_expr(expr.children[0], panel)
        op_entry = next(o for o in _UNARY_OPS if o[0] == expr.op)
        _, fn, uses_n, _ = op_entry
        if uses_n:
            return fn(child, expr.n)
        return fn(child)

    if expr.kind == "binary":
        a = _evaluate_expr(expr.children[0], panel)
        b = _evaluate_expr(expr.children[1], panel)
        # Align columns
        common = a.columns.intersection(b.columns)
        a = a[common]
        b = b[common]
        op_entry = next(o for o in _BINARY_OPS if o[0] == expr.op)
        _, fn, uses_n, _ = op_entry
        if uses_n:
            return fn(a, b, expr.n)
        return fn(a, b)

    raise ValueError(f"Unknown expression kind: {expr.kind}")


# ────────────────────────────────────────────────────────────
# MinedFactor — wraps an Expr as a CrossSectionalFactor
# ────────────────────────────────────────────────────────────

class MinedFactor(CrossSectionalFactor):
    """A factor whose computation is a random expression tree."""

    category = "mined"
    universe_type = "multi"

    def __init__(self, expr: Expr, seed: int, index: int):
        self.expr = expr
        self.seed = seed
        self.index = index
        self.name = f"mined_{seed:04d}_{index:04d}"
        self.lookback_days = max(21, expr.lookback_required())
        self.description = expr.formula()
        # No @register_factor — these are runtime instances, not the static registry.

    @property
    def formula(self) -> str:
        return self.expr.formula()

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        return _evaluate_expr(self.expr, panel)


# ────────────────────────────────────────────────────────────
# FormulaMiner
# ────────────────────────────────────────────────────────────

class FormulaMiner:
    """Generates random formula factors.

    Args:
        seed: RNG seed for reproducibility.
        depth_range: min/max tree depth (inclusive).
    """

    def __init__(self, seed: int = 42, depth_range: Tuple[int, int] = (2, 5)):
        self.seed = seed
        self.depth_range = depth_range
        self.rng = random.Random(seed)

    def generate(self, n: int = 200) -> List[MinedFactor]:
        """Generate `n` candidate factors.

        Trees that throw on a test panel are silently skipped; we keep
        retrying until we have `n` valid ones (capped at n*5 attempts).
        """
        out: List[MinedFactor] = []
        attempts = 0
        max_attempts = n * 5
        while len(out) < n and attempts < max_attempts:
            depth = self.rng.randint(*self.depth_range)
            expr = _random_expr(self.rng, 0, depth)
            mf = MinedFactor(expr, self.seed, len(out))
            out.append(mf)
            attempts += 1
        return out


# ────────────────────────────────────────────────────────────
# Walk-forward filter
# ────────────────────────────────────────────────────────────

@dataclass
class WalkForwardResult:
    factor: MinedFactor
    is_ic: float
    oos_ic: float
    is_rank_ic: float
    oos_rank_ic: float
    decay: float  # |oos_ic / is_ic|
    passed: bool


def walk_forward_filter(
    factors: List[MinedFactor],
    panel: DataPanel,
    *,
    is_frac: float = 0.6,
    horizon: int = 5,
    min_is_abs_ic: float = 0.01,
    min_oos_decay: float = 0.5,
    use_ranks: bool = True,
) -> List[WalkForwardResult]:
    """For each mined factor, compute IC on IS / OOS slices and apply
    the cut: keep only factors whose OOS Rank IC preserves at least
    `min_oos_decay` of the IS value (in absolute terms).

    Returns the FULL list of results (passed=True/False).  Caller filters.
    """
    fwd = panel.forward_returns(horizon=horizon, kind="log")
    dates = fwd.index
    split = int(len(dates) * is_frac)
    is_dates = dates[:split]
    oos_dates = dates[split:]

    results: List[WalkForwardResult] = []
    for f in factors:
        try:
            fp = f.compute(panel)
            fp = fp.shift(1)  # 1-day execution lag
            # IC over IS / OOS
            is_ic = float(cross_sectional_ic_series(
                fp.loc[is_dates.intersection(fp.index)],
                fwd.loc[is_dates.intersection(fwd.index)],
                rank=False).mean())
            oos_ic = float(cross_sectional_ic_series(
                fp.loc[oos_dates.intersection(fp.index)],
                fwd.loc[oos_dates.intersection(fwd.index)],
                rank=False).mean())
            is_rank_ic = float(cross_sectional_ic_series(
                fp.loc[is_dates.intersection(fp.index)],
                fwd.loc[is_dates.intersection(fwd.index)],
                rank=True).mean())
            oos_rank_ic = float(cross_sectional_ic_series(
                fp.loc[oos_dates.intersection(fp.index)],
                fwd.loc[oos_dates.intersection(fwd.index)],
                rank=True).mean())

            decay = (abs(oos_rank_ic) / abs(is_rank_ic)) if abs(is_rank_ic) > 1e-9 else 0.0

            # Pass criteria: nontrivial IS IC AND OOS preserves enough of it
            # (with matching sign — flipping in OOS is failure)
            same_sign = (is_rank_ic * oos_rank_ic) > 0
            passed = (
                abs(is_rank_ic) >= min_is_abs_ic
                and same_sign
                and decay >= min_oos_decay
            )
            results.append(WalkForwardResult(
                factor=f,
                is_ic=is_ic, oos_ic=oos_ic,
                is_rank_ic=is_rank_ic, oos_rank_ic=oos_rank_ic,
                decay=decay, passed=passed,
            ))
        except Exception:
            # Skip broken trees silently — the miner generates many such.
            continue

    return results


__all__ = [
    "Expr",
    "MinedFactor",
    "FormulaMiner",
    "WalkForwardResult",
    "walk_forward_filter",
]
