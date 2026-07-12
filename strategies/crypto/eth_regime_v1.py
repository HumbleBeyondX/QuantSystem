"""ETH 1H Regime-Switching Strategy — factor-composition with regime gates.

Design insight from the factor evaluation pipeline on ETH 1H 2024-2026:

  * No single factor has standalone alpha (max Rank IC = 0.049)
  * The 1H ETH return process is dominated by MEAN REVERSION, not trend
  * Cross-asset (BTC-ETH correlation) is the strongest regime signal
  * Different factor subsets work in different regimes — hence the
    regime-switching design.

## Signal composition

Each bar, we:
  1. Compute the market regime via ``RegimeDetector``
  2. Look up ``REGIME_FACTOR_WEIGHTS[regime]`` for the active factor set
  3. For each factor, compute its latest value and z-score it vs the
     trailing 30-day window on that factor
  4. Composite score = Σ weight_f × zscore_f (per regime weights)
  5. Trade signal:
       score >  entry_threshold  → LONG
       score < -entry_threshold  → SHORT
       sign flip while in position → EXIT

## Position sizing

Fixed-risk: ``qty = (equity × risk_per_trade) / stop_distance``.
Stop distance is 2.5 × ATR(24), tighter than the 4H Sniper's 3.5×ATR
because 1H bars are faster.

On OKX the minimum ETH-USDT-SWAP order is 0.1 ETH. We reuse the
``force_min_lot`` logic from the live runner.

## Exits

Two exit conditions:
  1. Signal sign flip — composite score crosses 0
  2. Hard stop — close <= entry - stop_dist (long) or >= entry + stop_dist (short)

No time-based exit at 1H — the 30-day window of the factor z-scoring
already handles stale-regime risk.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional
from uuid import uuid4

import numpy as np
import pandas as pd

from core.enums import SignalDirection
from core.logging import get_logger
from core.models import Bar, Signal, TargetPosition
from strategies.base import StrategyBase, StrategyConfig
from strategies.crypto.regime import (
    MarketRegime,
    REGIME_FACTOR_WEIGHTS,
    RegimeDetector,
    RegimeSnapshot,
)
from strategies.registry import register_strategy
from research.factors import get_all_factors, get_factor

logger = get_logger(__name__)


@register_strategy("eth_regime_v1")
class ETHRegimeStrategy(StrategyBase):
    """ETH 1H long/short using regime-dependent factor weights."""

    # Sliding window of bars per symbol (enough for 30-day z-scoring)
    BAR_WINDOW = 24 * 30  # 30 days of 1H

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # ── Leverage & risk ──
        self.leverage = float(self.params.get("leverage", 20.0))
        self.risk_per_trade = float(self.params.get("risk_per_trade", 0.02))
        self.max_margin_pct = float(self.params.get("max_margin_pct", 0.30))

        # ── Entry / exit ──
        self.entry_threshold = float(self.params.get("entry_threshold", 1.5))
        self.zscore_window = int(self.params.get("zscore_window", 24 * 30))

        # ── Stops ──
        self.atr_period = int(self.params.get("atr_period", 24))
        self.atr_stop = float(self.params.get("atr_stop", 2.5))

        # ── Internal state per symbol ──
        self._bars: Dict[str, Deque[Bar]] = {}
        self._current_time: datetime = datetime.utcnow()

        # Position tracking
        self._entry_price: Dict[str, float] = {}
        self._entry_score: Dict[str, float] = {}   # signal at entry
        self._stop_dist: Dict[str, float] = {}

        # Regime + score history for diagnostics
        self._last_regime: Dict[str, MarketRegime] = {}
        self._last_score: Dict[str, float] = {}

        # External data (BTC close for cross-asset factors + regime corr)
        self._btc_window: Optional[pd.Series] = None

        # Factor instances (all registered), indexed by name
        factors_all = get_all_factors()
        self._factors_by_name = {f.name: f for f in factors_all}

        # Regime detector
        self._regime_detector = RegimeDetector(
            adx_period=int(self.params.get("regime_adx_period", 14)),
            vol_window=int(self.params.get("regime_vol_window", 24)),
            corr_window=int(self.params.get("regime_corr_window", 24)),
            vol_history_window=int(self.params.get("regime_vol_history_window", 720)),
        )

        # Live equity (set by runner in live mode; falls back to param)
        self._live_equity: Optional[float] = None

    # ------------------------------------------------------------------
    # External data injection (called by backtest harness / live runner)
    # ------------------------------------------------------------------
    def set_btc_window(self, btc_close: pd.Series) -> None:
        """Update the BTC reference series used for cross-asset factors."""
        self._btc_window = btc_close

    def set_live_equity(self, equity: float) -> None:
        self._live_equity = equity

    def _get_equity(self) -> float:
        if self._live_equity is not None and self._live_equity > 0:
            return self._live_equity
        return float(self.params.get("initial_capital", 10000))

    # ------------------------------------------------------------------
    # State restoration — called after sync from live exchange position
    # ------------------------------------------------------------------
    def restore_position_state(
        self,
        symbol: str,
        quantity: float,
        avg_entry_price: float,
        current_price: Optional[float] = None,
    ) -> None:
        """Rebuild per-symbol state after a runner restart."""
        self._positions[symbol] = float(quantity)
        self._entry_price[symbol] = float(avg_entry_price)
        # Approximate stop with 2% if no bars yet
        bars = self._bars.get(symbol)
        if bars and len(bars) >= self.atr_period:
            df = self._bars_to_df(symbol)
            atr = self._compute_atr(df, self.atr_period)
            self._stop_dist[symbol] = atr * self.atr_stop
        else:
            self._stop_dist[symbol] = avg_entry_price * 0.02
        self._entry_score[symbol] = 0.0

    # ------------------------------------------------------------------
    # Bar handler
    # ------------------------------------------------------------------
    def on_bar(self, symbol: str, bar: Bar) -> None:
        super().on_bar(symbol, bar)
        self._current_time = bar.timestamp

        if symbol not in self._bars:
            self._bars[symbol] = deque(maxlen=self.BAR_WINDOW)
        self._bars[symbol].append(bar)

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------
    def generate_signals(self) -> List[Signal]:
        signals: List[Signal] = []

        for symbol in self.symbols:
            bars = self._bars.get(symbol)
            if not bars or len(bars) < max(self.zscore_window // 2, 100):
                continue  # need enough history for z-scoring

            df = self._bars_to_df(symbol)

            # Attach BTC column if we have it
            if self._btc_window is not None and len(self._btc_window) > 0:
                df = self._attach_btc(df)

            # 1. Detect regime
            btc_series = self._btc_window if self._btc_window is not None else None
            snapshot = self._regime_detector.detect(df, btc_series)
            self._last_regime[symbol] = snapshot.regime

            # 2. Compute composite score from regime-weighted factors
            score = self._composite_score(df, snapshot.regime)
            self._last_score[symbol] = score

            # 3. Check exit
            if not self.is_flat(symbol):
                exit_sig = self._check_exit(symbol, df, score, snapshot.regime)
                if exit_sig:
                    signals.append(exit_sig)
                    continue

            # 4. Check entry
            if self.is_flat(symbol):
                entry_sig = self._check_entry(symbol, df, score, snapshot.regime)
                if entry_sig:
                    signals.append(entry_sig)

        return signals

    # ------------------------------------------------------------------
    # Composite score
    # ------------------------------------------------------------------
    def _composite_score(self, df: pd.DataFrame, regime: MarketRegime) -> float:
        weights = REGIME_FACTOR_WEIGHTS.get(regime.value, {})
        if not weights:
            return 0.0

        total_weight = 0.0
        total = 0.0
        for factor_name, w in weights.items():
            factor = self._factors_by_name.get(factor_name)
            if factor is None:
                continue
            try:
                series = factor.compute(df)
            except Exception:
                continue

            # Z-score the current factor value vs the trailing window
            vals = series.dropna()
            if len(vals) < 50:
                continue
            mean = vals.mean()
            std = vals.std()
            if std == 0 or np.isnan(std):
                continue
            current = vals.iloc[-1]
            if np.isnan(current):
                continue
            z = (current - mean) / std
            # Clip extreme z-scores to keep one outlier from dominating
            z = float(np.clip(z, -3.0, 3.0))

            total += w * z
            total_weight += abs(w)

        if total_weight == 0:
            return 0.0
        # Normalize so returned score is comparable across regimes
        return total / total_weight

    # ------------------------------------------------------------------
    # Entry / exit decisions
    # ------------------------------------------------------------------
    def _check_entry(
        self,
        symbol: str,
        df: pd.DataFrame,
        score: float,
        regime: MarketRegime,
    ) -> Optional[Signal]:
        if abs(score) < self.entry_threshold:
            return None

        direction = SignalDirection.LONG if score > 0 else SignalDirection.SHORT
        price = float(df["close"].iloc[-1])

        atr = self._compute_atr(df, self.atr_period)
        stop_dist = max(atr * self.atr_stop, price * 0.005)

        self._entry_price[symbol] = price
        self._entry_score[symbol] = score
        self._stop_dist[symbol] = stop_dist

        return Signal(
            signal_id=str(uuid4()),
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=direction,
            strength=float(min(abs(score) / 3.0, 1.0)),
            timestamp=self._current_time,
            metadata={
                "score": round(score, 3),
                "regime": regime.value,
                "atr": round(atr, 2),
                "stop_dist": round(stop_dist, 2),
                "price": round(price, 2),
            },
        )

    def _check_exit(
        self,
        symbol: str,
        df: pd.DataFrame,
        score: float,
        regime: MarketRegime,
    ) -> Optional[Signal]:
        pos = self.get_position(symbol)
        entry_p = self._entry_price.get(symbol, 0)
        stop_dist = self._stop_dist.get(symbol, entry_p * 0.02)
        price = float(df["close"].iloc[-1])

        reason = None
        if pos > 0:
            # LONG exit: score flipped negative OR hard stop hit
            if score <= -0.5:
                reason = "score_flip_short"
            elif price <= entry_p - stop_dist:
                reason = "stop_loss"
        else:
            # SHORT exit: score flipped positive OR hard stop hit
            if score >= 0.5:
                reason = "score_flip_long"
            elif price >= entry_p + stop_dist:
                reason = "stop_loss"

        if reason:
            return Signal(
                signal_id=str(uuid4()),
                strategy_id=self.strategy_id,
                symbol=symbol,
                direction=SignalDirection.FLAT,
                strength=0.0,
                timestamp=self._current_time,
                metadata={
                    "reason": reason,
                    "score": round(score, 3),
                    "regime": regime.value,
                    "price": round(price, 2),
                },
            )
        return None

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------
    def calculate_target_positions(self) -> List[TargetPosition]:
        signals = self.generate_signals()
        self._signals.extend(signals)
        self._metrics["signals_generated"] += len(signals)

        equity = self._get_equity()
        targets: List[TargetPosition] = []
        for sig in signals:
            if sig.direction == SignalDirection.FLAT:
                targets.append(TargetPosition(
                    symbol=sig.symbol,
                    strategy_id=self.strategy_id,
                    target_quantity=0.0,
                    target_weight=0.0,
                    urgency=0.8,
                    timestamp=sig.timestamp,
                ))
                continue

            stop_dist = self._stop_dist.get(sig.symbol, 1.0)
            price = self._entry_price.get(sig.symbol, 1.0)
            risk_amount = equity * self.risk_per_trade
            qty_abs = risk_amount / max(stop_dist, 1e-12)

            # Cap by max margin
            notional = qty_abs * price
            margin = notional / self.leverage
            max_margin = equity * self.max_margin_pct
            if margin > max_margin:
                qty_abs = max_margin * self.leverage / max(price, 1)

            qty = qty_abs if sig.direction == SignalDirection.LONG else -qty_abs

            targets.append(TargetPosition(
                symbol=sig.symbol,
                strategy_id=self.strategy_id,
                target_quantity=qty,
                target_weight=sig.strength,
                urgency=0.6,
                timestamp=sig.timestamp,
            ))
        return targets

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _bars_to_df(self, symbol: str) -> pd.DataFrame:
        bars = list(self._bars[symbol])
        df = pd.DataFrame([{
            "timestamp": b.timestamp,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        } for b in bars])
        # Ensure timestamp is pd.Timestamp (factors use .dt accessor)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.reset_index(drop=True)

    def _attach_btc(self, df: pd.DataFrame) -> pd.DataFrame:
        """Join BTC close by timestamp. Missing timestamps → NaN."""
        if self._btc_window is None:
            return df
        btc = self._btc_window
        # If already a Series indexed by timestamp, just reindex
        if isinstance(btc, pd.Series):
            btc_close = btc.reindex(df["timestamp"].values).values
        else:
            # array-like; assume it aligns positionally on the tail
            n = min(len(btc), len(df))
            btc_close = np.full(len(df), np.nan)
            btc_close[-n:] = np.asarray(btc)[-n:]
        df = df.copy()
        df["btc_close"] = btc_close
        return df

    @staticmethod
    def _compute_atr(df: pd.DataFrame, period: int) -> float:
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / period, adjust=False).mean()
        val = atr.iloc[-1]
        return float(val) if not np.isnan(val) else float(close.iloc[-1] * 0.01)
