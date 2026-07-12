"""Market regime detection — features + GMM clustering + label resolution.

Three pieces (each one importable independently):
  * macro_features.py — per-day macro feature DataFrame
  * gmm_detector.py    — GaussianMixture fit/predict with walk-forward
  * regime_labels.py   — auto-label clusters from feature centroids

Used by ``strategies/equity/sector_rotation_strategy.py`` to switch
factor-weight schemes per regime.
"""
from research.regime.gmm_detector import GMMRegimeDetector  # noqa: F401
from research.regime.macro_features import compute_macro_features  # noqa: F401
from research.regime.regime_labels import label_regimes, RegimeLabel  # noqa: F401
