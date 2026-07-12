"""Mean-reversion factors.

The classic Jegadeesh (1990) finding: 1-month "loser" stocks
outperform 1-month "winner" stocks. Sign convention: factor returns
the NEGATIVE of recent return (so big drop → high score).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_mining.base import CrossSectionalFactor, register_factor
from research.factor_mining.data_panel import DataPanel
from research.factor_mining.operators import ts_max


# ────────────────────────────────────────────────────────────
@register_factor
class STR_1m(CrossSectionalFactor):
    """Short-term reversal: −(1-month return).
    Loser stocks bounce in next month."""
    name = "str_1m"
    category = "reversal"
    universe_type = "multi"
    lookback_days = 21
    description = "Negative 1-month return (Jegadeesh short-term reversal)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        return -np.log(c / c.shift(21))


# ────────────────────────────────────────────────────────────
@register_factor
class STR_5d(CrossSectionalFactor):
    """5-day reversal — useful for high-frequency rebalance."""
    name = "str_5d"
    category = "reversal"
    universe_type = "multi"
    lookback_days = 5
    description = "Negative 5-day return."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        return -np.log(c / c.shift(5))


# ────────────────────────────────────────────────────────────
@register_factor
class STR_1d(CrossSectionalFactor):
    """1-day reversal — overnight + intraday combined.
    Lehmann (1990) showed strong 1-day reversal in equities."""
    name = "str_1d"
    category = "reversal"
    universe_type = "multi"
    lookback_days = 1
    description = "Negative 1-day return."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        return -np.log(c / c.shift(1))


# ────────────────────────────────────────────────────────────
@register_factor
class LongTermReversal_36m(CrossSectionalFactor):
    """De Bondt-Thaler (1985) long-term reversal: 3y losers outperform
    3y winners. Negative of 36-month return."""
    name = "ltr_36m"
    category = "reversal"
    universe_type = "multi"
    lookback_days = 756
    description = "Negative 36-month return (De Bondt-Thaler LTR)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        return -np.log(c.shift(21) / c.shift(756))


# ────────────────────────────────────────────────────────────
@register_factor
class Drawdown_252(CrossSectionalFactor):
    """Current drawdown from 252-day high.
    Deeper drawdown = higher score (mean reversion candidate).
    Returns: (close − 252d_max) / 252d_max  (always ≤ 0).
    Sign-flipped so deeper drop = higher value."""
    name = "drawdown_252"
    category = "reversal"
    universe_type = "multi"
    lookback_days = 252
    description = "Negative drawdown from 252d high (deeper DD = higher score)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        rolling_max = ts_max(c, 252)
        dd = (c - rolling_max) / rolling_max  # ≤ 0
        return -dd  # negate so deeper DD → larger positive value
