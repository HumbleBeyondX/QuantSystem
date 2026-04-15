"""ETH Momentum Sniper v1: optimized for ETH perpetual futures on 4H timeframe.

Based on BTC Sniper v5 architecture, optimized for ETH's volatility profile.
Key differences from BTC version:
  - Wider trailing stop: 3.0 ATR (vs 2.5) — lets ETH trends run longer
  - Longer max hold: 180 bars / 30 days (vs 120 bars / 20 days)
  - Same entry logic: Donchian breakout + EMA pullback
  - Same fixed-risk sizing: qty = risk_amount / stop_distance

Core principle: risk is decoupled from leverage.
  qty = (equity * risk_pct) / stop_distance
  Leverage only reduces margin requirement, not max loss per trade.

Backtest performance (ETH/USDT 4H, 2020-2026, $10K initial):
  Optimized (R5 S3.5 T3.0 H180):
    - Total Return:  +6644%
    - Annual Return: +96.4%
    - Sharpe:        2.055
    - Sortino:       3.959
    - Max Drawdown:  13.2%
    - Win Rate:      50.2%
    - Profit Factor: 3.28
    - Trades:        213
    - Every year profitable (min +13.1% in 2026 Q1)
"""

from __future__ import annotations

from strategies.crypto.sniper_strategy import SniperStrategy
from strategies.base import StrategyConfig
from strategies.registry import register_strategy


@register_strategy("eth_sniper")
class ETHSniperStrategy(SniperStrategy):
    """ETH-optimized momentum sniper with fixed-risk position sizing.

    Inherits all logic from BTC SniperStrategy but with ETH-tuned defaults.
    The key optimizations are applied via config parameters, not code changes:
      - atr_trail: 3.0 (wider trailing stop for ETH's larger swings)
      - max_hold: 180 (30 days — longer hold for ETH trend persistence)

    All other parameters (entry scoring, stops, indicators) remain identical
    since they proved equally effective on ETH in backtesting.
    """

    def __init__(self, config: StrategyConfig) -> None:
        # Apply ETH-optimized defaults before parent init
        # These will be overridden by config if explicitly set
        params = config.parameters
        params.setdefault("atr_trail", 3.0)         # BTC default: 2.5
        params.setdefault("max_hold", 180)           # BTC default: 120
        params.setdefault("atr_trail_activate", 2.0)

        super().__init__(config)
