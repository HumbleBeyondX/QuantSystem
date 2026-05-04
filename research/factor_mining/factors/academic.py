"""Academic / well-known anomalies.

References:
  * Frazzini & Pedersen (2014) — "Betting Against Beta"
  * Asness, Frazzini, Pedersen (2014) — "Quality Minus Junk"
  * Bali, Cakici, Whitelaw (2011) — Lottery / MAX anomaly
  * Black (1972) — beta squared (failed CAPM term)
  * D'Avolio (2002) — convexity / call-option-like profile
  * Daniel & Moskowitz (2016) — Momentum crashes
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_mining.base import CrossSectionalFactor, register_factor
from research.factor_mining.data_panel import DataPanel
from research.factor_mining.operators import (
    beta as op_beta,
    realized_vol,
    ts_mean,
    ts_std,
)


# ────────────────────────────────────────────────────────────
@register_factor
class BAB_proxy(CrossSectionalFactor):
    """Frazzini-Pedersen "Betting Against Beta" proxy.

    Long low-beta, short high-beta. Returns (1 − beta) so low-beta names
    score positively.
    """
    name = "bab_proxy"
    category = "academic"
    universe_type = "multi"
    lookback_days = 252
    description = "1 − beta_252 (low beta = high score per Frazzini-Pedersen)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        market = ret.mean(axis=1)
        betas = op_beta(ret, market, n=252)
        return 1.0 - betas


# ────────────────────────────────────────────────────────────
@register_factor
class QualityMinusJunk_proxy(CrossSectionalFactor):
    """Asness-Frazzini-Pedersen "QMJ" composite (proxy version).

    Without fundamentals, we composite the four QMJ pillars from price
    series:
      profit  → 252d Sharpe (return per unit risk)
      growth  → 12-month momentum (price growth)
      safety  → −252d realized vol (stability)
      payout  → not available without dividend data; omitted
    Each pillar cross-sectionally z-scored, then equal-weight averaged.
    """
    name = "qmj_proxy"
    category = "academic"
    universe_type = "multi"
    lookback_days = 252
    description = "QMJ proxy: equal-weight composite of profit + growth + safety."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        c = panel.closes_wide

        sharpe = ts_mean(ret, 252) / ts_std(ret, 252).replace(0, np.nan) * np.sqrt(252)
        mom = np.log(c.shift(21) / c.shift(252))
        safety = -realized_vol(ret, 252)

        def _zscore(x: pd.DataFrame) -> pd.DataFrame:
            mean = x.mean(axis=1)
            std = x.std(axis=1).replace(0, np.nan)
            return x.sub(mean, axis=0).div(std, axis=0)

        return (_zscore(sharpe) + _zscore(mom) + _zscore(safety)) / 3.0


# ────────────────────────────────────────────────────────────
@register_factor
class Lottery_MAX5(CrossSectionalFactor):
    """Bali-Cakici-Whitelaw lottery anomaly: avg of 5 highest daily
    returns over last 21d. High-MAX names underperform → sign-flip."""
    name = "lottery_max5_21"
    category = "academic"
    universe_type = "multi"
    lookback_days = 21
    description = "Negative of avg top-5 daily returns over 21d (Bali MAX)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.returns_wide
        # Approximate via mean + 1.5×std (top tail proxy without expensive sort)
        # Reuses realized vol & mean to vectorize
        mean = ts_mean(ret, 21)
        sigma = ts_std(ret, 21)
        # Top-5 of 21 ≈ ~95th percentile, conservative proxy
        return -(mean + 1.65 * sigma)


# ────────────────────────────────────────────────────────────
@register_factor
class BetaSquared(CrossSectionalFactor):
    """Black (1972) — second-moment beta term. Negative score:
    high-β-squared (extreme either way) underperforms after CAPM."""
    name = "beta_squared_252"
    category = "academic"
    universe_type = "multi"
    lookback_days = 252
    description = "Negative of beta² over 252d (penalize extremes)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        market = ret.mean(axis=1)
        betas = op_beta(ret, market, n=252)
        return -(betas ** 2)


# ────────────────────────────────────────────────────────────
@register_factor
class Convexity_252(CrossSectionalFactor):
    """D'Avolio convexity: how much does a name's return resemble a long
    call option (positive convex)?

    Rough proxy: Up-day capture / Down-day capture ratio over 252 days.
    Up-capture > 1, down-capture < 1 = positive convexity.
    """
    name = "convexity_252"
    category = "academic"
    universe_type = "multi"
    lookback_days = 252
    description = "Up-capture / Down-capture ratio vs market (252d)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        market = ret.mean(axis=1)
        up = market > 0
        down = market < 0
        # Per-ticker mean return on up-days vs down-days
        out = pd.DataFrame(index=ret.index, columns=ret.columns, dtype="float64")
        n = 252
        min_p = 126

        for col in ret.columns:
            r = ret[col]
            up_ret = r.where(up).rolling(n, min_periods=min_p).mean()
            down_ret = r.where(down).rolling(n, min_periods=min_p).mean()
            mkt_up = market.where(up).rolling(n, min_periods=min_p).mean()
            mkt_down = market.where(down).rolling(n, min_periods=min_p).mean()

            up_cap = up_ret / mkt_up.replace(0, np.nan)
            down_cap = down_ret / mkt_down.replace(0, np.nan)
            out[col] = up_cap - down_cap
        return out


# ────────────────────────────────────────────────────────────
@register_factor
class MomentumCrash_proxy(CrossSectionalFactor):
    """Daniel-Moskowitz crash proxy: in bear-market regimes, momentum
    inverts. Multiplies 12-1 momentum by −sign(market 12m return).

    When market is up: factor = +mom (normal momentum exposure)
    When market is down: factor = −mom (anti-momentum / contrarian)
    """
    name = "momentum_crash_aware"
    category = "academic"
    universe_type = "multi"
    lookback_days = 252
    description = "Mom_12_1 × sign(market 12-month return)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        mom = np.log(c.shift(21) / c.shift(252))
        # Market 12m return = mean across tickers
        market_close = c.mean(axis=1)
        market_mom = np.log(market_close.shift(21) / market_close.shift(252))
        # Broadcast sign across tickers
        regime = np.sign(market_mom).fillna(0)
        return mom.multiply(regime, axis=0)
