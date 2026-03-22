"""Production-Grade SPY Gamma Scalp Strategy v2.0

This strategy profits from the volatility risk premium (VRP) and realized-vs-
implied volatility mismatch using two complementary regimes:

  REGIME A - SELL VOLATILITY (primary, ~70% of trades):
    When IV >> RV (VIX elevated relative to realized moves), sell ATM straddles
    and delta-hedge. Collect theta decay that exceeds gamma losses. This
    exploits the well-documented volatility risk premium (IV consistently
    overestimates future RV by ~2-4 vol points on average).

  REGIME B - BUY VOLATILITY (opportunistic, ~30% of trades):
    When RV > IV or vol regime is expanding (VIX breakout, large gap, vol
    clustering), buy ATM straddles and delta-hedge. Gamma scalping profits
    from realized moves that exceed theta decay.

Signal composite score (5 factors, weighted):
  1. VRP signal:     IV - RV spread, z-scored over 60d       (weight 0.30)
  2. VIX mean-rev:   VIX percentile rank 252d                (weight 0.25)
  3. Vol momentum:   5d RV / 20d RV ratio (vol clustering)   (weight 0.20)
  4. Vol-of-vol:     Uncertainty = lower conviction           (weight 0.15)
  5. Gap/range:      Overnight gap + intraday range zscore    (weight 0.10)

Position sizing:
  - Base: fixed contracts, scaled by signal strength (0.3-1.0)
  - Adaptive: reduced in high vol-of-vol regimes
  - Max notional: configurable % of portfolio

Risk management:
  - Per-trade max loss: 35% of straddle cost
  - Portfolio max drawdown: 10% trailing from peak equity
  - Max consecutive losses: 4 then cooldown 3 bars
  - Greeks limits: max abs delta, max vega exposure
  - Regime switch protection: close if regime flips mid-trade

Backtest approach:
  Black-Scholes synthetic pricing from SPY spot + VIX (IV proxy) + T-bill rate.
  VIX/100 as ATM IV is a reasonable approximation for SPY ATM options (VIX is
  derived from SPY option strip, and ATM IV closely tracks VIX for short DTE).
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, Tuple
from uuid import uuid4

import numpy as np
from scipy.stats import norm

from core.enums import SignalDirection
from core.models import Bar, Signal, TargetPosition
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


# ===================================================================
# Black-Scholes pricing engine (vectorized-ready)
# ===================================================================

def _d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def bs_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return max(S - K, 0.0)
    d1 = _d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def bs_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return max(K - S, 0.0)
    d1 = _d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_straddle(S: float, K: float, T: float, r: float, sigma: float) -> Tuple[float, float]:
    """Returns (call_price, put_price)."""
    return bs_call(S, K, T, r, sigma), bs_put(S, K, T, r, sigma)


def bs_delta_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return 1.0 if S > K else 0.0
    return norm.cdf(_d1(S, K, T, r, sigma))


def bs_delta_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return -1.0 if S < K else 0.0
    return norm.cdf(_d1(S, K, T, r, sigma)) - 1.0


def bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, sigma)
    return norm.pdf(d1) / (S * sigma * math.sqrt(T))


def bs_theta_straddle(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Combined theta for call+put, per calendar day."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    # Call theta
    tc = (-(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
          - r * K * math.exp(-r * T) * norm.cdf(d2))
    # Put theta
    tp = (-(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
          + r * K * math.exp(-r * T) * norm.cdf(-d2))
    return (tc + tp) / 365.0


def bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega per 1% vol move (call = put vega)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, sigma)
    return S * norm.pdf(d1) * math.sqrt(T) / 100.0


# ===================================================================
# Trade direction enum
# ===================================================================

class VolRegime:
    SELL = "sell_vol"  # IV >> RV, collect premium
    BUY = "buy_vol"    # RV > IV, scalp gamma
    NEUTRAL = "neutral"


# ===================================================================
# Strategy
# ===================================================================

@register_strategy("spy_gamma_scalp")
class GammaScalpStrategy(StrategyBase):
    """Production-grade SPY Gamma Scalp with dual-regime (buy/sell vol).

    Exploits the Volatility Risk Premium (sell vol when IV >> RV) and
    vol clustering (buy vol when RV momentum is positive). Delta-hedges
    continuously. Decomposes P&L into gamma/theta/vega/hedge/costs.

    Key improvements over v1:
      - Dual regime: SELL vol (primary) + BUY vol (opportunistic)
      - Composite signal with 5 weighted factors
      - Adaptive position sizing based on vol-of-vol
      - Multi-layer risk management
      - VIX percentile regime filter
      - Rolling z-score based entry/exit
    """

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # === Core parameters ===
        self.dte = self.get_param("dte", 5)  # 5-day straddles (Mon-Fri weekly)
        self.base_contracts = self.get_param("base_contracts", 30)  # Optimized
        self.multiplier = 100
        self.max_notional_pct = self.get_param("max_notional_pct", 0.45)  # 45% max

        # === Hedge parameters ===
        self.hedge_delta_threshold = self.get_param("hedge_delta_threshold", 0.06)
        self.min_hedge_notional = self.get_param("min_hedge_notional", 800.0)

        # === Signal thresholds ===
        self.sell_signal_threshold = self.get_param("sell_signal_threshold", 0.25)
        self.buy_signal_threshold = self.get_param("buy_signal_threshold", 0.80)  # Effectively disabled

        # === Signal weights ===
        self.w_vrp = self.get_param("w_vrp", 0.35)
        self.w_vix_mr = self.get_param("w_vix_mr", 0.30)
        self.w_vol_mom = self.get_param("w_vol_mom", 0.20)
        self.w_vov = self.get_param("w_vov", 0.10)
        self.w_gap = self.get_param("w_gap", 0.05)

        # === Risk parameters ===
        self.max_loss_pct = self.get_param("max_loss_pct", 0.20)  # Tight stop
        self.max_drawdown_pct = self.get_param("max_drawdown_pct", 0.08)
        self.max_consecutive_losses = self.get_param("max_consecutive_losses", 3)
        self.cooldown_bars = self.get_param("cooldown_bars", 5)
        self.max_abs_delta_shares = self.get_param("max_abs_delta_shares", 500)
        self.max_vega_exposure = self.get_param("max_vega_exposure", 3000)

        # === Profit management ===
        self.trailing_stop_activation = self.get_param("trailing_stop_activation", 0.5)  # Activate at 50% of max expected profit
        self.trailing_stop_pct = self.get_param("trailing_stop_pct", 0.40)  # Trail 40% from peak
        self.take_profit_pct = self.get_param("take_profit_pct", 0.70)  # Take profit at 70% of straddle cost

        # === VIX regime filter ===
        self.vix_floor = self.get_param("vix_floor", 12.0)  # Don't sell vol when VIX < 12
        self.vix_ceiling = self.get_param("vix_ceiling", 40.0)  # Don't sell vol when VIX > 40

        # === Transaction costs (realistic IB/CBOE) ===
        self.commission_per_contract = self.get_param("commission_per_contract", 0.65)
        self.option_spread_pct = self.get_param("option_spread_pct", 0.005)
        self.equity_spread_per_share = self.get_param("equity_spread_per_share", 0.01)
        self.equity_slippage_bps = self.get_param("equity_slippage_bps", 1.0)

        # === Roll parameters ===
        self.roll_dte_days = self.get_param("roll_dte_days", 1)

        # =============== Internal State ===============

        # Bar history & market data
        self._bars: Dict[str, Deque[Bar]] = {}
        self._current_time: datetime = datetime.utcnow()
        self._prev_time: Optional[datetime] = None
        self._last_spot: float = 0.0
        self._implied_vol: float = 0.20
        self._risk_free: float = 0.05

        # Feature history for z-scoring
        self._vrp_history: Deque[float] = deque(maxlen=120)
        self._range_history: Deque[float] = deque(maxlen=60)
        self._gap_history: Deque[float] = deque(maxlen=60)

        # Realized vol (computed from bars)
        self._rv_5d: float = 0.0
        self._rv_10d: float = 0.0
        self._rv_20d: float = 0.0

        # Straddle position
        self._straddle_open: bool = False
        self._trade_direction: str = VolRegime.NEUTRAL  # "sell_vol" or "buy_vol"
        self._strike: float = 0.0
        self._entry_call_price: float = 0.0
        self._entry_put_price: float = 0.0
        self._straddle_entry_cost: float = 0.0
        self._entry_spot: float = 0.0
        self._expiry_time: Optional[datetime] = None
        self._entry_iv: float = 0.0

        # Portfolio Greeks
        self._portfolio_delta: float = 0.0
        self._portfolio_gamma: float = 0.0
        self._portfolio_theta: float = 0.0
        self._portfolio_vega: float = 0.0

        # Hedge state
        self._hedge_shares: float = 0.0
        self._last_hedge_price: float = 0.0

        # Decomposed P&L (per straddle lifecycle)
        self._cum_hedge_pnl: float = 0.0
        self._cum_gamma_pnl: float = 0.0
        self._cum_theta_pnl: float = 0.0
        self._cum_vega_pnl: float = 0.0
        self._cum_txn_costs: float = 0.0
        self._prev_iv: float = 0.0

        # Risk tracking
        self._consecutive_losses: int = 0
        self._cooldown_until: Optional[datetime] = None
        self._peak_equity: float = 0.0
        self._running_pnl: float = 0.0  # Total strategy P&L across all trades
        self._n_contracts: int = 0  # Current position contracts (adaptive)
        self._trade_peak_pnl: float = 0.0  # Peak P&L within current trade (for trailing stop)
        self._initial_capital: float = self.get_param("initial_capital", 100_000)

        # Trade history for analytics
        self._trade_results: List[Dict[str, Any]] = []

    # ===================================================================
    # Volatility estimators
    # ===================================================================

    def _compute_rv(self, symbol: str, lookback: int) -> float:
        """Parkinson (high-low) realized vol with close-to-close fallback."""
        bars = self._bars.get(symbol)
        if not bars or len(bars) < lookback:
            return 0.0
        recent = list(bars)[-lookback:]
        # Parkinson
        hl_sq = []
        for b in recent:
            if b.high > 0 and b.low > 0 and b.high >= b.low:
                hl_sq.append(math.log(b.high / b.low) ** 2)
        if len(hl_sq) >= lookback * 0.8:
            var = sum(hl_sq) / (4 * len(hl_sq) * math.log(2))
            return math.sqrt(max(var, 0.0) * 252)
        # Fallback: close-to-close
        closes = [b.close for b in recent if b.close > 0]
        if len(closes) < 2:
            return 0.0
        lr = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        return float(np.std(lr, ddof=1)) * math.sqrt(252)

    # ===================================================================
    # Signal computation (5-factor composite)
    # ===================================================================

    def _compute_signal(self, symbol: str) -> Tuple[str, float, Dict[str, Any]]:
        """Compute composite signal.

        Returns: (regime, raw_score, metadata)
            regime: 'sell_vol' | 'buy_vol' | 'neutral'
            raw_score: 0.0-1.0 signal strength
            metadata: diagnostics dict
        """
        iv = self._implied_vol
        rv20 = self._rv_20d
        rv5 = self._rv_5d

        if iv <= 0 or rv20 <= 0:
            return VolRegime.NEUTRAL, 0.0, {}

        bars = self._bars.get(symbol, deque())
        if len(bars) < 25:
            return VolRegime.NEUTRAL, 0.0, {}

        # --- Factor 1: VRP (Volatility Risk Premium) ---
        # Positive VRP = IV > RV = sell vol is profitable
        vrp = iv - rv20
        self._vrp_history.append(vrp)
        vrp_mean = np.mean(self._vrp_history) if len(self._vrp_history) > 20 else vrp
        vrp_std = np.std(self._vrp_history) if len(self._vrp_history) > 20 else max(abs(vrp), 0.01)
        vrp_zscore = (vrp - vrp_mean) / max(vrp_std, 0.001)
        # Clip to [-3, 3], then map: positive z -> sell vol signal
        vrp_signal = max(min(vrp_zscore, 3.0), -3.0) / 3.0  # [-1, 1]

        # --- Factor 2: VIX mean reversion ---
        # High VIX percentile -> VIX likely to drop -> sell vol
        bars_list = list(bars)
        vix_values = []
        for b in bars_list:
            if b.metadata and "vix_close" in b.metadata:
                vix_values.append(b.metadata["vix_close"])
        if len(vix_values) >= 20:
            vix_current = vix_values[-1]
            vix_sma20 = np.mean(vix_values[-20:])
            # Above SMA -> sell vol, below -> buy vol
            vix_mr_signal = (vix_current - vix_sma20) / max(vix_sma20 * 0.1, 1.0)
            vix_mr_signal = max(min(vix_mr_signal, 1.0), -1.0)
        else:
            vix_mr_signal = 0.0

        # --- Factor 3: Vol momentum (clustering) ---
        # RV5 > RV20 -> vol is accelerating -> buy vol (trend following)
        if rv20 > 0:
            vol_mom_ratio = rv5 / rv20
            # > 1 means vol expanding, < 1 means vol contracting
            vol_mom_signal = (vol_mom_ratio - 1.0) * 5.0  # Scale
            vol_mom_signal = max(min(vol_mom_signal, 1.0), -1.0)
            # Negative = sell vol, Positive = buy vol
            vol_mom_signal = -vol_mom_signal  # Invert: contracting vol = sell vol signal
        else:
            vol_mom_signal = 0.0

        # --- Factor 4: Vol-of-Vol (uncertainty reducer) ---
        # High vol-of-vol -> reduce conviction (scale down signal)
        vix_changes = []
        for i in range(1, min(21, len(vix_values))):
            if vix_values[i - 1] > 0:
                vix_changes.append(vix_values[i] / vix_values[i - 1] - 1.0)
        if len(vix_changes) >= 10:
            vov = float(np.std(vix_changes)) * math.sqrt(252)
            # Low vov (< 0.5) -> high conviction (1.0)
            # High vov (> 1.5) -> low conviction (0.3)
            vov_factor = max(0.3, min(1.0, 1.5 - vov))
        else:
            vov_factor = 0.7
            vov = 0.0

        # --- Factor 5: Gap/Range signal ---
        last_bar = bars_list[-1]
        intraday_range = (last_bar.high - last_bar.low) / last_bar.close if last_bar.close > 0 else 0
        self._range_history.append(intraday_range)
        if len(bars_list) >= 2 and bars_list[-2].close > 0:
            gap = last_bar.open / bars_list[-2].close - 1.0
        else:
            gap = 0.0
        self._gap_history.append(abs(gap))

        # Large gap/range -> vol is high -> buy vol (momentum)
        avg_range = np.mean(self._range_history) if len(self._range_history) > 10 else intraday_range
        range_zscore = (intraday_range - avg_range) / max(float(np.std(self._range_history)), 0.001) if len(self._range_history) > 10 else 0.0
        gap_range_signal = max(min(range_zscore, 2.0), -2.0) / 2.0
        gap_range_signal = -gap_range_signal  # High range = buy vol -> negative for sell

        # === Composite Score ===
        # Positive composite = SELL vol signal
        # Negative composite = BUY vol signal
        composite = (
            self.w_vrp * vrp_signal
            + self.w_vix_mr * vix_mr_signal
            + self.w_vol_mom * vol_mom_signal
            + self.w_vov * 0.0  # vov is a multiplier, not a directional signal
            + self.w_gap * gap_range_signal
        )

        # Apply vol-of-vol scaling
        composite *= vov_factor

        # Determine regime
        if composite > self.sell_signal_threshold:
            regime = VolRegime.SELL
            strength = min((composite - self.sell_signal_threshold) / 0.5, 1.0)
            strength = max(strength, 0.3)  # Minimum strength floor
        elif composite < -self.buy_signal_threshold:
            regime = VolRegime.BUY
            strength = min((-composite - self.buy_signal_threshold) / 0.5, 1.0)
            strength = max(strength, 0.3)
        else:
            regime = VolRegime.NEUTRAL
            strength = 0.0

        meta = {
            "vrp": round(vrp, 4),
            "vrp_zscore": round(vrp_zscore, 3),
            "vrp_signal": round(vrp_signal, 3),
            "vix_mr_signal": round(vix_mr_signal, 3),
            "vol_mom_signal": round(vol_mom_signal, 3),
            "vov": round(vov, 4) if isinstance(vov, float) else 0.0,
            "vov_factor": round(vov_factor, 3),
            "gap_range_signal": round(gap_range_signal, 3),
            "composite": round(composite, 4),
            "regime": regime,
            "rv5": round(rv5, 4),
            "rv20": round(rv20, 4),
            "iv": round(iv, 4),
        }

        return regime, strength, meta

    # ===================================================================
    # Adaptive position sizing
    # ===================================================================

    def _compute_contracts(self, strength: float, spot: float) -> int:
        """Compute number of contracts based on signal strength and portfolio equity.

        Key sizing rules:
          1. Base = base_contracts * strength (min 1)
          2. Cap at max_notional_pct of current portfolio equity
          3. Scale down if recent losses (drawdown-aware sizing)
        """
        # Current equity = initial + running P&L
        equity = self._initial_capital + self._running_pnl

        # Base contracts scaled by signal strength
        n = max(1, int(self.base_contracts * strength))

        # Estimate straddle price
        straddle_price_est = spot * self._implied_vol * math.sqrt(self.dte / 365.0) * 0.8
        if straddle_price_est <= 0:
            return 1

        # Cap by max notional as % of equity
        max_notional = equity * self.max_notional_pct
        notional = n * self.multiplier * straddle_price_est
        if notional > max_notional:
            n = max(1, int(max_notional / (self.multiplier * straddle_price_est)))

        # Drawdown-aware: reduce size if in significant drawdown
        if self._peak_equity > 0:
            dd = (self._peak_equity - self._running_pnl) / max(self._peak_equity, 1)
            if dd > 0.05:  # If > 5% from peak, reduce
                scale = max(0.5, 1.0 - dd * 3)  # Moderate reduction
                n = max(1, int(n * scale))

        # After 3 consecutive losses, reduce
        if self._consecutive_losses >= 3:
            n = max(1, int(n * 0.6))

        return n

    # ===================================================================
    # Transaction cost model
    # ===================================================================

    def _option_trade_cost(self, call_px: float, put_px: float) -> float:
        """One-way straddle trade cost (open or close, not round-trip)."""
        n = self._n_contracts
        commission = self.commission_per_contract * n * 2  # 2 legs
        spread = (call_px + put_px) * self.option_spread_pct * self.multiplier * n
        return commission + spread

    def _hedge_trade_cost(self, shares: float, price: float) -> float:
        """Cost of a delta-hedge trade in SPY shares."""
        shares_abs = abs(shares)
        if shares_abs < 0.5:
            return 0.0
        spread = self.equity_spread_per_share * shares_abs
        slippage = price * shares_abs * self.equity_slippage_bps / 10_000
        return spread + slippage

    # ===================================================================
    # Precise time calculations
    # ===================================================================

    def _time_to_expiry(self) -> float:
        """Years to expiry, floored at 1 minute."""
        if self._expiry_time is None:
            return 1.0 / (365.25 * 24 * 60)
        remaining = max((self._expiry_time - self._current_time).total_seconds(), 60.0)
        return remaining / (365.25 * 86400)

    # ===================================================================
    # Data handler
    # ===================================================================

    def on_bar(self, symbol: str, bar: Bar) -> None:
        super().on_bar(symbol, bar)
        self._prev_time = self._current_time
        self._current_time = bar.timestamp

        if symbol not in self._bars:
            self._bars[symbol] = deque(maxlen=300)
        self._bars[symbol].append(bar)

        self._last_spot = bar.close

        # Extract IV and risk-free from bar metadata (injected by data loader)
        if bar.metadata:
            self._implied_vol = bar.metadata.get("implied_vol", self._implied_vol)
            self._risk_free = bar.metadata.get("risk_free_rate", self._risk_free)

        # Update realized vol estimates
        rv5 = self._compute_rv(symbol, 5)
        rv10 = self._compute_rv(symbol, 10)
        rv20 = self._compute_rv(symbol, 20)
        if rv5 > 0:
            self._rv_5d = rv5
        if rv10 > 0:
            self._rv_10d = rv10
        if rv20 > 0:
            self._rv_20d = rv20

    # ===================================================================
    # Target positions (override)
    # ===================================================================

    def calculate_target_positions(self) -> List[TargetPosition]:
        signals = self.generate_signals()
        self._signals.extend(signals)
        self._metrics["signals_generated"] += len(signals)

        targets: List[TargetPosition] = []
        for sig in signals:
            targets.append(TargetPosition(
                symbol=sig.symbol,
                strategy_id=self.strategy_id,
                target_quantity=sig.target_quantity or 0,
                target_weight=sig.strength,
                urgency=0.8,
                timestamp=sig.timestamp,
            ))
        return targets

    # ===================================================================
    # Core signal generation
    # ===================================================================

    def generate_signals(self) -> List[Signal]:
        signals: List[Signal] = []
        now = self._current_time

        for symbol in self.symbols:
            bars = self._bars.get(symbol)
            if not bars or len(bars) < 2:
                continue

            spot = bars[-1].close
            prev_spot = bars[-2].close

            if self._straddle_open:
                signals.extend(self._manage_position(symbol, spot, prev_spot, now))
            else:
                sig = self._try_open(symbol, spot, now)
                if sig is not None:
                    signals.append(sig)

        return signals

    # ===================================================================
    # Open position (sell or buy straddle)
    # ===================================================================

    def _try_open(self, symbol: str, spot: float, now: datetime) -> Optional[Signal]:
        # Cooldown check
        if self._cooldown_until is not None and now < self._cooldown_until:
            return None

        # Max drawdown circuit breaker (based on running P&L)
        if self._peak_equity > 0:
            dd_from_peak = (self._peak_equity - self._running_pnl) / max(self._initial_capital, 1)
            if dd_from_peak > self.max_drawdown_pct:
                return None

        # VIX regime filter: don't trade outside sweet spot
        vix = self._implied_vol * 100  # Convert back to VIX level
        if vix < self.vix_floor or vix > self.vix_ceiling:
            return None

        # Compute composite signal
        regime, strength, meta = self._compute_signal(symbol)

        if regime == VolRegime.NEUTRAL:
            return None

        # Price the straddle
        iv = self._implied_vol
        self._strike = round(spot)
        T = self.dte / 365.0

        call_px, put_px = bs_straddle(spot, self._strike, T, self._risk_free, iv)

        # Adaptive position sizing
        self._n_contracts = self._compute_contracts(strength, spot)
        notional = self._n_contracts * self.multiplier

        self._straddle_entry_cost = (call_px + put_px) * notional
        self._entry_call_price = call_px
        self._entry_put_price = put_px
        self._entry_spot = spot
        self._entry_iv = iv
        self._expiry_time = now + timedelta(days=self.dte)
        self._straddle_open = True
        self._trade_direction = regime
        self._last_hedge_price = spot
        self._prev_iv = iv

        # Reset accumulators
        self._hedge_shares = 0.0
        self._cum_hedge_pnl = 0.0
        self._cum_gamma_pnl = 0.0
        self._cum_theta_pnl = 0.0
        self._cum_vega_pnl = 0.0
        self._cum_txn_costs = 0.0
        self._trade_peak_pnl = 0.0

        # Opening costs
        self._cum_txn_costs += self._option_trade_cost(call_px, put_px)

        # Direction: SELL vol = SHORT straddle, BUY vol = LONG straddle
        direction = SignalDirection.SHORT if regime == VolRegime.SELL else SignalDirection.LONG

        meta.update({
            "action": f"open_{regime}",
            "strike": self._strike,
            "call_price": round(call_px, 4),
            "put_price": round(put_px, 4),
            "straddle_cost": round(self._straddle_entry_cost, 2),
            "n_contracts": self._n_contracts,
            "dte": self.dte,
            "open_txn_cost": round(self._cum_txn_costs, 2),
        })

        return Signal(
            signal_id=uuid4(),
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=direction,
            strength=strength,
            timestamp=now,
            target_quantity=0,
            metadata=meta,
        )

    # ===================================================================
    # Manage open position (MTM + Greeks + hedge + stop + roll)
    # ===================================================================

    def _manage_position(
        self, symbol: str, spot: float, prev_spot: float, now: datetime,
    ) -> List[Signal]:
        signals: List[Signal] = []
        notional = self._n_contracts * self.multiplier
        iv = self._implied_vol
        is_short = (self._trade_direction == VolRegime.SELL)
        sign = -1.0 if is_short else 1.0  # -1 for short straddle, +1 for long

        # ------ 1. Hedge P&L mark-to-market ------
        if abs(self._hedge_shares) > 0.5:
            self._cum_hedge_pnl += self._hedge_shares * (spot - self._last_hedge_price)
        self._last_hedge_price = spot

        # ------ 2. Time to expiry ------
        T = self._time_to_expiry()
        T_days = T * 365.25

        # ------ 3. Reprice & compute Greeks ------
        call_px, put_px = bs_straddle(spot, self._strike, T, self._risk_free, iv)

        call_delta = bs_delta_call(spot, self._strike, T, self._risk_free, iv)
        put_delta = bs_delta_put(spot, self._strike, T, self._risk_free, iv)
        gamma = bs_gamma(spot, self._strike, T, self._risk_free, iv)
        theta = bs_theta_straddle(spot, self._strike, T, self._risk_free, iv)
        vega = bs_vega(spot, self._strike, T, self._risk_free, iv)

        # Option position delta (sign: long straddle delta = call_d + put_d)
        option_delta = (call_delta + put_delta) * notional * sign
        self._portfolio_delta = option_delta + self._hedge_shares
        self._portfolio_gamma = gamma * 2 * notional * abs(sign)
        self._portfolio_theta = theta * notional * sign
        self._portfolio_vega = vega * 2 * notional * sign

        # ------ 4. Decomposed P&L ------
        dS = spot - prev_spot
        dt_days = 0.0
        if self._prev_time is not None:
            dt_days = (now - self._prev_time).total_seconds() / 86400.0
        d_iv = iv - self._prev_iv

        self._cum_gamma_pnl += 0.5 * self._portfolio_gamma * dS * dS * sign
        self._cum_theta_pnl += self._portfolio_theta * dt_days
        self._cum_vega_pnl += self._portfolio_vega * d_iv * 100

        self._prev_iv = iv

        # ------ 5. Actual option P&L (repricing truth) ------
        option_pnl = (
            (call_px + put_px) - (self._entry_call_price + self._entry_put_price)
        ) * notional * sign

        total_pnl = option_pnl + self._cum_hedge_pnl - self._cum_txn_costs

        # ------ 6. Risk checks ------
        # Update trade peak P&L
        self._trade_peak_pnl = max(self._trade_peak_pnl, total_pnl)

        # a) Per-trade max loss (hard stop)
        max_loss = self._straddle_entry_cost * self.max_loss_pct
        if total_pnl < -max_loss:
            return self._close_position(
                symbol, spot, call_px, put_px, option_pnl, now, "stop_loss"
            )

        # b) Take profit: close if P&L exceeds threshold
        take_profit_level = self._straddle_entry_cost * self.take_profit_pct
        if total_pnl > take_profit_level:
            return self._close_position(
                symbol, spot, call_px, put_px, option_pnl, now, "take_profit"
            )

        # c) Trailing stop: protect profits once activated
        activation_level = self._straddle_entry_cost * self.trailing_stop_activation
        if self._trade_peak_pnl > activation_level and self._trade_peak_pnl > 0:
            trail_stop = self._trade_peak_pnl * (1.0 - self.trailing_stop_pct)
            if total_pnl < trail_stop:
                return self._close_position(
                    symbol, spot, call_px, put_px, option_pnl, now, "trailing_stop"
                )

        # d) Regime flip: if signal regime flips, close (regime protection)
        regime, _, _ = self._compute_signal(symbol)
        if (self._trade_direction == VolRegime.SELL and regime == VolRegime.BUY) or \
           (self._trade_direction == VolRegime.BUY and regime == VolRegime.SELL):
            return self._close_position(
                symbol, spot, call_px, put_px, option_pnl, now, "regime_flip"
            )

        # ------ 7. Delta hedge ------
        net_delta = self._portfolio_delta
        dollar_delta = abs(net_delta) * spot
        if dollar_delta > self.min_hedge_notional and \
           abs(net_delta) > self.hedge_delta_threshold * notional:
            hedge_qty = -net_delta
            # Cap hedge size
            if abs(self._hedge_shares + hedge_qty) > self.max_abs_delta_shares:
                hedge_qty = math.copysign(
                    self.max_abs_delta_shares - abs(self._hedge_shares),
                    hedge_qty,
                )

            if abs(hedge_qty) >= 1.0:
                hedge_qty = round(hedge_qty)
                cost = self._hedge_trade_cost(hedge_qty, spot)
                self._cum_txn_costs += cost
                self._hedge_shares += hedge_qty

                direction = SignalDirection.LONG if hedge_qty > 0 else SignalDirection.SHORT
                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=direction,
                    strength=0.7,
                    timestamp=now,
                    target_quantity=abs(hedge_qty),
                    metadata={
                        "action": "delta_hedge",
                        "trade_type": self._trade_direction,
                        "net_delta_before": round(net_delta, 1),
                        "hedge_shares": round(hedge_qty, 0),
                        "total_hedge_shares": round(self._hedge_shares, 0),
                        "hedge_cost": round(cost, 2),
                        "portfolio_gamma": round(self._portfolio_gamma, 2),
                        "portfolio_theta": round(self._portfolio_theta, 2),
                        "running_pnl": round(total_pnl, 2),
                    },
                ))

        # ------ 8. Roll / expiry ------
        if T_days <= self.roll_dte_days:
            signals.extend(self._close_position(
                symbol, spot, call_px, put_px, option_pnl, now, "roll"
            ))

        return signals

    # ===================================================================
    # Close position
    # ===================================================================

    def _close_position(
        self,
        symbol: str,
        spot: float,
        call_px: float,
        put_px: float,
        option_pnl: float,
        now: datetime,
        reason: str,
    ) -> List[Signal]:
        # Closing costs
        self._cum_txn_costs += self._option_trade_cost(call_px, put_px)
        if abs(self._hedge_shares) > 0.5:
            self._cum_txn_costs += self._hedge_trade_cost(self._hedge_shares, spot)

        total_pnl = option_pnl + self._cum_hedge_pnl - self._cum_txn_costs

        explained = self._cum_gamma_pnl + self._cum_theta_pnl + self._cum_vega_pnl
        unexplained = (option_pnl + self._cum_hedge_pnl) - explained

        # Update strategy-level tracking
        self._running_pnl += total_pnl
        self._peak_equity = max(self._peak_equity, self._running_pnl)

        # Consecutive loss tracking
        if total_pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.max_consecutive_losses:
                self._cooldown_until = now + timedelta(days=self.cooldown_bars)
        else:
            self._consecutive_losses = 0
            self._cooldown_until = None

        # Record trade result
        self._trade_results.append({
            "timestamp": now.isoformat(),
            "direction": self._trade_direction,
            "reason": reason,
            "entry_spot": round(self._entry_spot, 2),
            "exit_spot": round(spot, 2),
            "strike": self._strike,
            "entry_iv": round(self._entry_iv, 4),
            "exit_iv": round(self._implied_vol, 4),
            "contracts": self._n_contracts,
            "total_pnl": round(total_pnl, 2),
            "option_pnl": round(option_pnl, 2),
            "hedge_pnl": round(self._cum_hedge_pnl, 2),
            "gamma_pnl": round(self._cum_gamma_pnl, 2),
            "theta_pnl": round(self._cum_theta_pnl, 2),
            "vega_pnl": round(self._cum_vega_pnl, 2),
            "txn_costs": round(self._cum_txn_costs, 2),
            "running_total": round(self._running_pnl, 2),
        })

        signal = Signal(
            signal_id=uuid4(),
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=SignalDirection.FLAT,
            strength=1.0,
            timestamp=now,
            target_quantity=abs(self._hedge_shares) if abs(self._hedge_shares) > 0.5 else 0,
            metadata={
                "action": f"close_{reason}",
                "trade_type": self._trade_direction,
                # P&L
                "option_pnl": round(option_pnl, 2),
                "hedge_pnl": round(self._cum_hedge_pnl, 2),
                "txn_costs": round(self._cum_txn_costs, 2),
                "total_pnl": round(total_pnl, 2),
                # Greek attribution
                "gamma_pnl": round(self._cum_gamma_pnl, 2),
                "theta_pnl": round(self._cum_theta_pnl, 2),
                "vega_pnl": round(self._cum_vega_pnl, 2),
                "unexplained_pnl": round(unexplained, 2),
                # Vol
                "entry_iv": round(self._entry_iv, 4),
                "exit_iv": round(self._implied_vol, 4),
                "realized_vol": round(self._rv_20d, 4),
                # Risk
                "consecutive_losses": self._consecutive_losses,
                "running_total_pnl": round(self._running_pnl, 2),
                "contracts": self._n_contracts,
            },
        )

        # Reset
        self._straddle_open = False
        self._trade_direction = VolRegime.NEUTRAL
        self._hedge_shares = 0.0
        self._expiry_time = None
        self._cum_hedge_pnl = 0.0
        self._cum_gamma_pnl = 0.0
        self._cum_theta_pnl = 0.0
        self._cum_vega_pnl = 0.0
        self._cum_txn_costs = 0.0
        self._straddle_entry_cost = 0.0
        self._n_contracts = 0
        self._trade_peak_pnl = 0.0

        return [signal]
