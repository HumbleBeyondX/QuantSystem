"""Implied volatility surface construction and analysis.

Builds a 2D IV surface (strike x expiry) for options strategies.
Detects IV skew, term structure anomalies, and mean-reversion opportunities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from core.logging import get_logger
from core.models import OptionContract
from strategies.options.greeks_calculator import compute_implied_vol

logger = get_logger(__name__)


@dataclass
class VolSurfacePoint:
    """Single point on the vol surface."""
    strike: float
    dte: float  # Days to expiry
    iv: float
    moneyness: float  # strike / spot


@dataclass
class VolSurface:
    """Implied volatility surface for an underlying."""
    underlying: str
    spot: float
    points: List[VolSurfacePoint]
    timestamp: str = ""

    @property
    def atm_vol(self) -> float:
        """ATM implied volatility (moneyness closest to 1.0)."""
        if not self.points:
            return 0.0
        closest = min(self.points, key=lambda p: abs(p.moneyness - 1.0))
        return closest.iv

    @property
    def skew_25d(self) -> float:
        """25-delta skew: IV(25d put) - IV(25d call)."""
        # Approximate using moneyness
        puts_otm = [p for p in self.points if p.moneyness < 0.95]
        calls_otm = [p for p in self.points if p.moneyness > 1.05]
        if not puts_otm or not calls_otm:
            return 0.0
        put_iv = np.mean([p.iv for p in puts_otm[:3]])
        call_iv = np.mean([p.iv for p in calls_otm[:3]])
        return put_iv - call_iv

    def get_iv(self, strike: float, dte: float) -> Optional[float]:
        """Interpolate IV for a given strike and DTE."""
        # Simple nearest-neighbor for now; bilinear interpolation in production
        if not self.points:
            return None
        closest = min(
            self.points,
            key=lambda p: (abs(p.strike - strike) / strike + abs(p.dte - dte) / max(dte, 1)),
        )
        return closest.iv


def build_vol_surface(
    underlying: str,
    spot: float,
    options: List[OptionContract],
    rate: float = 0.05,
) -> VolSurface:
    """Build implied volatility surface from a list of option contracts."""
    points: List[VolSurfacePoint] = []

    for opt in options:
        if opt.mid_price <= 0 or opt.days_to_expiry <= 0:
            continue

        is_call = opt.option_type.upper() == "CALL"
        tte = opt.days_to_expiry / 365.0

        iv = opt.implied_vol
        if iv <= 0:
            iv = compute_implied_vol(opt.mid_price, spot, opt.strike, tte, rate, is_call)

        if iv > 0:
            points.append(VolSurfacePoint(
                strike=opt.strike,
                dte=opt.days_to_expiry,
                iv=iv,
                moneyness=opt.strike / spot,
            ))

    return VolSurface(underlying=underlying, spot=spot, points=points)


def detect_vol_anomalies(surface: VolSurface) -> List[Dict]:
    """Detect anomalies in the vol surface that may signal trading opportunities."""
    anomalies: List[Dict] = []

    if not surface.points:
        return anomalies

    # Group by DTE bucket
    dte_buckets: Dict[int, List[VolSurfacePoint]] = {}
    for p in surface.points:
        bucket = int(p.dte / 7) * 7  # Weekly buckets
        dte_buckets.setdefault(bucket, []).append(p)

    # Check for butterfly arbitrage (non-convex smile)
    for dte, pts in dte_buckets.items():
        sorted_pts = sorted(pts, key=lambda p: p.strike)
        for i in range(1, len(sorted_pts) - 1):
            # Butterfly condition: IV should be convex in strike
            iv_left = sorted_pts[i - 1].iv
            iv_mid = sorted_pts[i].iv
            iv_right = sorted_pts[i + 1].iv
            if iv_mid > (iv_left + iv_right) / 2 * 1.1:
                anomalies.append({
                    "type": "non_convex_smile",
                    "dte": dte,
                    "strike": sorted_pts[i].strike,
                    "iv": iv_mid,
                })

    # Check term structure inversion
    if len(dte_buckets) >= 2:
        sorted_dtes = sorted(dte_buckets.keys())
        for i in range(len(sorted_dtes) - 1):
            near_iv = np.mean([p.iv for p in dte_buckets[sorted_dtes[i]]])
            far_iv = np.mean([p.iv for p in dte_buckets[sorted_dtes[i + 1]]])
            if near_iv > far_iv * 1.15:
                anomalies.append({
                    "type": "term_structure_inversion",
                    "near_dte": sorted_dtes[i],
                    "far_dte": sorted_dtes[i + 1],
                    "near_iv": near_iv,
                    "far_iv": far_iv,
                })

    return anomalies
