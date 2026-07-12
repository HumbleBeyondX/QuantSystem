"""Cross-sectional equity / sector-ETF factor library for the
sector-rotation strategy.

Unlike ``research/factors/`` (which has TimeSeriesFactor for single-asset
crypto), these factors are CROSS-SECTIONAL: at each date, they produce a
score per ticker, then are z-scored across tickers. Higher score = "more
attractive long".

Sign conventions (so positive = bullish for that ticker):
  * Momentum factors:  positive return → positive score
  * Reversal factors:  negative recent return → positive score
                       (already sign-flipped inside the factor)
  * Trend factor:      price > SMA → positive score
  * Risk factor:       LOWER vol → HIGHER score (sign-flipped)
  * Beta factor:       LOWER beta → HIGHER score (defensive bias)
  * Carry factor:      higher dividend yield → positive score
"""
from research.factors.equity.scorer import (  # noqa: F401
    compute_factor_panel,
    cross_sectional_zscore,
    composite_score_per_regime,
)
