"""Auto-label GMM clusters by their feature centroids.

Cluster IDs from sklearn are arbitrary (cluster_0 in fit-A might be the
"bull" cluster but cluster_2 in fit-B). When we walk-forward refit, we
need a consistent label so the strategy can lookup the regime → factor
weights mapping.

This module assigns one of four named labels:

    BULL    — low VIX, positive SPY trend, tight credit spread
    BEAR    — moderate-to-high VIX, negative trend, widening credit
    CRISIS  — extreme VIX, deeply negative trend, blown-out credit
    NEUTRAL — anything else (mid-vol, flat trend)

Algorithm:
  1. Score each cluster's centroid on these dimensions:
     bull_score   = -vix - vix² + spy_ret_20d
     crisis_score = vix + spy_realized_vol  (high = crisis)
     bear_score   = vix*0.5 - spy_ret_20d   (vol up, return down)

  2. Assign:
     - The cluster with the highest crisis_score AND vix > 75th percentile
       of training data → CRISIS (only if vix is genuinely extreme)
     - Of remaining, the one with highest bear_score AND negative
       spy_ret_20d → BEAR
     - Of remaining, the one with highest bull_score → BULL
     - Last one → NEUTRAL
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List

import numpy as np
import pandas as pd


class RegimeLabel(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    NEUTRAL = "neutral"
    CRISIS = "crisis"


def label_regimes(
    centroids: pd.DataFrame,
    *,
    n_components: int = 4,
) -> Dict[int, RegimeLabel]:
    """Map cluster index → RegimeLabel based on centroid features.

    Args:
        centroids: rows = clusters (cluster_0, cluster_1, ...), columns =
                   features (must include vix, spy_ret_20d,
                   spy_realized_vol; others are tolerated).
        n_components: total number of clusters.

    Returns:
        Dict mapping cluster index (int) → RegimeLabel.
    """
    if "vix" not in centroids.columns or "spy_ret_20d" not in centroids.columns:
        raise ValueError("centroids must include 'vix' and 'spy_ret_20d' columns")

    df = centroids.copy()
    df["__cid"] = range(len(df))

    # Score each cluster on the three dimensions
    df["__crisis_score"] = (
        df["vix"] + df.get("spy_realized_vol", 0.0) * 100.0
        - df["spy_ret_20d"] * 5.0
    )
    df["__bull_score"] = (
        df["spy_ret_20d"] * 5.0 - df["vix"] * 0.5
        - df.get("spy_realized_vol", 0.0) * 50.0
    )
    df["__bear_score"] = (
        df["vix"] * 0.7 - df["spy_ret_20d"] * 3.0
        + df.get("spy_realized_vol", 0.0) * 30.0
    )

    labels: Dict[int, RegimeLabel] = {}
    remaining = set(df["__cid"].tolist())

    # 1. CRISIS — highest crisis score AND vix > median + 1.5×IQR from
    #    centroids themselves (i.e., this cluster's vix is the genuinely
    #    extreme one, not just the highest-but-not-extreme)
    sorted_by_crisis = df.sort_values("__crisis_score", ascending=False)
    crisis_candidate = sorted_by_crisis.iloc[0]
    median_vix = df["vix"].median()
    if crisis_candidate["vix"] > median_vix * 1.4 and len(df) >= 4:
        cid = int(crisis_candidate["__cid"])
        labels[cid] = RegimeLabel.CRISIS
        remaining.discard(cid)

    # 2. BEAR — highest bear score among remaining, with negative trend
    rem_df = df[df["__cid"].isin(remaining)]
    if not rem_df.empty:
        bear_sorted = rem_df.sort_values("__bear_score", ascending=False)
        bear_candidate = bear_sorted.iloc[0]
        if bear_candidate["spy_ret_20d"] < 0.005:  # negative or flat trend
            cid = int(bear_candidate["__cid"])
            labels[cid] = RegimeLabel.BEAR
            remaining.discard(cid)

    # 3. BULL — highest bull score among remaining
    rem_df = df[df["__cid"].isin(remaining)]
    if not rem_df.empty:
        bull_sorted = rem_df.sort_values("__bull_score", ascending=False)
        cid = int(bull_sorted.iloc[0]["__cid"])
        labels[cid] = RegimeLabel.BULL
        remaining.discard(cid)

    # 4. NEUTRAL — everything else
    for cid in remaining:
        labels[int(cid)] = RegimeLabel.NEUTRAL

    return labels


def labels_summary(label_map: Dict[int, RegimeLabel]) -> str:
    """Human-readable summary for logging."""
    return ", ".join(f"cluster_{cid}={lbl.value}" for cid, lbl in sorted(label_map.items()))
