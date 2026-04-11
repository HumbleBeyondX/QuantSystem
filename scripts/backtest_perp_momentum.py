#!/usr/bin/env python3
"""Standalone backtest for the Crypto Perpetual Adaptive Momentum (PAM) strategy.

Dual-regime strategy for BTC/ETH perpetual futures (10x leverage, 4H timeframe):

  TRENDING regime (ADX > 25):
    - EMA crossover (8/21) as entry trigger
    - Price above/below EMA55 for direction filter
    - MACD histogram momentum confirmation
    - Strength scaled by ADX intensity

  RANGING regime (ADX < 20):
    - Bollinger Band touch + RSI extreme for mean-reversion
    - Stochastic RSI for fine timing
    - Tighter stops, target BB midline

  TRANSITION zone (20-25):
    - Donchian breakout (20-period) as trigger
    - Reduced position sizing

Usage:
    python scripts/backtest_perp_momentum.py
    python scripts/backtest_perp_momentum.py --capital 10000 --leverage 10
    python scripts/backtest_perp_momentum.py --sweep
    python scripts/backtest_perp_momentum.py --start 2023-01-01 --end 2026-01-01
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ────────────────────────────────────────────────────────────
#  Vectorized indicator computation
# ────────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all technical indicators vectorized (runs once upfront)."""
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    # ── EMAs ──
    df["ema8"] = c.ewm(span=8, adjust=False).mean()
    df["ema21"] = c.ewm(span=21, adjust=False).mean()
    df["ema55"] = c.ewm(span=55, adjust=False).mean()
    df["ema200"] = c.ewm(span=200, adjust=False).mean()

    # EMA crossover signals (current vs previous bar)
    df["ema8_prev"] = df["ema8"].shift(1)
    df["ema21_prev"] = df["ema21"].shift(1)
    df["ema_cross_up"] = (df["ema8"] > df["ema21"]) & (df["ema8_prev"] <= df["ema21_prev"])
    df["ema_cross_down"] = (df["ema8"] < df["ema21"]) & (df["ema8_prev"] >= df["ema21_prev"])
    # Also track if already aligned (not just crossover)
    df["ema_bull_aligned"] = (df["ema8"] > df["ema21"]) & (c > df["ema21"])
    df["ema_bear_aligned"] = (df["ema8"] < df["ema21"]) & (c < df["ema21"])

    # ── RSI(14) ──
    delta = c.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)

    # ── MACD (12, 26, 9) ──
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    df["macd_hist_prev"] = df["macd_hist"].shift(1)
    # MACD histogram cross zero
    df["macd_cross_up"] = (df["macd_hist"] > 0) & (df["macd_hist_prev"] <= 0)
    df["macd_cross_down"] = (df["macd_hist"] < 0) & (df["macd_hist_prev"] >= 0)

    # ── ADX(14) with +DI/-DI ──
    plus_dm = h.diff().clip(lower=0)
    minus_dm = (-l.diff()).clip(lower=0)
    plus_dm_clean = plus_dm.where(plus_dm > minus_dm, 0)
    minus_dm_clean = minus_dm.where(minus_dm > plus_dm, 0)
    prev_close = c.shift(1)
    tr = pd.concat([h - l, (h - prev_close).abs(), (l - prev_close).abs()], axis=1).max(axis=1)
    atr14_raw = tr.ewm(span=14, adjust=False).mean()
    plus_di = 100 * plus_dm_clean.ewm(span=14, adjust=False).mean() / atr14_raw.replace(0, np.nan)
    minus_di = 100 * minus_dm_clean.ewm(span=14, adjust=False).mean() / atr14_raw.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.ewm(span=14, adjust=False).mean()
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di

    # ── ATR(14) ──
    df["atr"] = atr14_raw

    # ── Bollinger Bands (20, 2.0) ──
    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    df["bb_upper"] = sma20 + 2.0 * std20
    df["bb_middle"] = sma20
    df["bb_lower"] = sma20 - 2.0 * std20
    df["bb_pct"] = (c - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)

    # ── Volume ──
    df["vol_sma20"] = v.rolling(20).mean()
    df["vol_ratio"] = v / df["vol_sma20"].replace(0, np.nan)

    # ── Donchian Channel (20 period) ──
    df["dc_high"] = h.rolling(20).max()
    df["dc_low"] = l.rolling(20).min()
    df["dc_high_prev"] = df["dc_high"].shift(1)
    df["dc_low_prev"] = df["dc_low"].shift(1)

    # ── Stochastic RSI ──
    rsi_min = df["rsi"].rolling(14).min()
    rsi_max = df["rsi"].rolling(14).max()
    df["stoch_rsi"] = (df["rsi"] - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
    df["stoch_rsi"] = df["stoch_rsi"].fillna(0.5)

    # ── Rate of Change ──
    df["roc10"] = c.pct_change(10) * 100
    df["roc20"] = c.pct_change(20) * 100

    return df


# ────────────────────────────────────────────────────────────
#  Backtest Engine
# ────────────────────────────────────────────────────────────

class PerpMomentumBacktest:
    """Dual-regime perpetual futures backtest.

    Regime detection via ADX:
      - Trending (ADX > adx_trend):  EMA crossover + MACD momentum
      - Ranging  (ADX < adx_range):  BB touch + RSI extremes
      - Transition (between):        Donchian breakout, reduced size
    """

    def __init__(
        self,
        initial_capital: float = 10_000,
        leverage: float = 10.0,
        # ── Regime ──
        adx_trend_thresh: float = 25.0,
        adx_range_thresh: float = 20.0,
        # ── Trend entry ──
        trend_rsi_min: float = 40.0,       # Don't long when RSI < 40
        trend_rsi_max: float = 80.0,       # Don't long when overbought
        trend_short_rsi_min: float = 20.0,
        trend_short_rsi_max: float = 60.0,
        require_ema55_filter: bool = True,  # Price must be on right side of EMA55
        # ── Mean reversion entry ──
        mr_rsi_oversold: float = 32.0,
        mr_rsi_overbought: float = 68.0,
        mr_bb_touch: bool = True,  # Require BB touch
        # ── Position sizing ──
        base_position_pct: float = 0.10,
        max_position_pct: float = 0.20,
        transition_size_mult: float = 0.6,
        mr_size_mult: float = 0.7,  # Mean reversion gets smaller size
        # ── Risk (ATR multiples) ──
        atr_stop_mult: float = 2.0,
        atr_tp_mult: float = 4.0,
        trailing_stop_atr: float = 2.5,
        trailing_activate_atr: float = 1.5,
        mr_atr_stop_mult: float = 1.5,  # Tighter stops for MR
        mr_atr_tp_mult: float = 2.5,    # Smaller TP for MR
        max_holding_bars: int = 36,      # 6 days on 4H
        # ── Circuit breaker & limits ──
        max_drawdown_pct: float = 0.25,   # 25% DD tolerance (high risk)
        cooldown_bars: int = 2,
        max_trades_per_day: int = 3,
        cb_cooldown_bars: int = 36,
        max_consecutive_losses: int = 6,
        consec_loss_cooldown: int = 12,  # Bars to wait after consecutive losses
        # ── Fees ──
        fee_rate: float = 0.0005,  # 5 bps taker
    ):
        # Store all params
        self.initial_capital = initial_capital
        self.leverage = leverage
        self.adx_trend_thresh = adx_trend_thresh
        self.adx_range_thresh = adx_range_thresh
        self.trend_rsi_min = trend_rsi_min
        self.trend_rsi_max = trend_rsi_max
        self.trend_short_rsi_min = trend_short_rsi_min
        self.trend_short_rsi_max = trend_short_rsi_max
        self.require_ema55_filter = require_ema55_filter
        self.mr_rsi_oversold = mr_rsi_oversold
        self.mr_rsi_overbought = mr_rsi_overbought
        self.mr_bb_touch = mr_bb_touch
        self.base_position_pct = base_position_pct
        self.max_position_pct = max_position_pct
        self.transition_size_mult = transition_size_mult
        self.mr_size_mult = mr_size_mult
        self.atr_stop_mult = atr_stop_mult
        self.atr_tp_mult = atr_tp_mult
        self.trailing_stop_atr = trailing_stop_atr
        self.trailing_activate_atr = trailing_activate_atr
        self.mr_atr_stop_mult = mr_atr_stop_mult
        self.mr_atr_tp_mult = mr_atr_tp_mult
        self.max_holding_bars = max_holding_bars
        self.max_drawdown_pct = max_drawdown_pct
        self.cooldown_bars = cooldown_bars
        self.max_trades_per_day = max_trades_per_day
        self.cb_cooldown_bars = cb_cooldown_bars
        self.max_consecutive_losses = max_consecutive_losses
        self.consec_loss_cooldown = consec_loss_cooldown
        self.fee_rate = fee_rate

        self._reset_state()

    def _reset_state(self):
        self._cash = self.initial_capital
        self._position = 0.0
        self._entry_price = 0.0
        self._entry_atr = 0.0
        self._entry_regime = ""
        self._highest_since_entry = 0.0
        self._lowest_since_entry = 0.0
        self._peak_equity = self.initial_capital
        self._bars_since_trade = self.cooldown_bars + 1
        self._bars_in_position = 0
        self._daily_trade_count = 0
        self._current_day = ""
        self._circuit_breaker = False
        self._cb_bars = 0
        self._consecutive_losses = 0
        self._consec_loss_cooldown_remaining = 0

        self._equity_curve: List[Dict[str, Any]] = []
        self._trades: List[Dict[str, Any]] = []

    # ── Futures P&L model ──

    def _unrealized_pnl(self, price: float) -> float:
        if abs(self._position) < 1e-12:
            return 0.0
        return (price - self._entry_price) * self._position

    def _equity(self, price: float) -> float:
        return self._cash + self._unrealized_pnl(price)

    # ── Position management ──

    def _open_position(self, price: float, direction: int, strength: float,
                       atr: float, regime: str, timestamp: str, row: dict):
        equity = self._equity(price)
        capped_equity = min(equity, self.initial_capital * 5)
        pos_pct = min(self.base_position_pct * strength, self.max_position_pct)

        if regime == "transition":
            pos_pct *= self.transition_size_mult
        elif regime == "ranging":
            pos_pct *= self.mr_size_mult

        notional = capped_equity * pos_pct * self.leverage
        qty = (notional / price) * direction
        cost = abs(qty * price * self.fee_rate)

        self._position = qty
        self._entry_price = price
        self._entry_atr = atr
        self._entry_regime = regime
        self._cash -= cost
        self._bars_in_position = 0
        self._highest_since_entry = price
        self._lowest_since_entry = price
        self._bars_since_trade = 0
        self._daily_trade_count += 1

        self._trades.append({
            "timestamp": timestamp,
            "action": "LONG" if direction > 0 else "SHORT",
            "price": price,
            "quantity": abs(qty),
            "notional": abs(qty * price),
            "leverage_used": abs(qty * price) / max(equity, 1),
            "fee": cost,
            "regime": regime,
            "strength": round(strength, 3),
            "atr": round(atr, 2),
            "stop_loss": round(price - direction * self._get_stop_mult(regime) * atr, 2),
            "take_profit": round(price + direction * self._get_tp_mult(regime) * atr, 2),
            "adx": round(row.get("adx", 0), 2),
            "rsi": round(row.get("rsi", 0), 2),
        })

    def _get_stop_mult(self, regime: str) -> float:
        return self.mr_atr_stop_mult if regime == "ranging" else self.atr_stop_mult

    def _get_tp_mult(self, regime: str) -> float:
        return self.mr_atr_tp_mult if regime == "ranging" else self.atr_tp_mult

    def _close_position(self, price: float, reason: str, timestamp: str):
        if abs(self._position) < 1e-12:
            return

        pnl = (price - self._entry_price) * self._position
        cost = abs(self._position * price * self.fee_rate)
        self._cash += pnl - cost

        direction = "LONG" if self._position > 0 else "SHORT"
        notional = abs(self._position * self._entry_price)
        pnl_pct = pnl / max(notional, 1) * 100

        if pnl - cost < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.max_consecutive_losses:
                self._consec_loss_cooldown_remaining = self.consec_loss_cooldown
        else:
            self._consecutive_losses = 0

        self._trades.append({
            "timestamp": timestamp,
            "action": f"CLOSE_{direction}",
            "price": round(price, 2),
            "quantity": abs(self._position),
            "pnl": round(pnl - cost, 2),
            "pnl_pct": round(pnl_pct, 2),
            "fee": round(cost, 2),
            "reason": reason,
            "entry_price": round(self._entry_price, 2),
            "bars_held": self._bars_in_position,
            "regime": self._entry_regime,
        })

        self._position = 0.0
        self._entry_price = 0.0
        self._entry_atr = 0.0
        self._entry_regime = ""
        self._bars_in_position = 0
        self._bars_since_trade = 0

    # ── Signal generation ──

    def _detect_regime(self, adx: float) -> str:
        if adx >= self.adx_trend_thresh:
            return "trending"
        elif adx <= self.adx_range_thresh:
            return "ranging"
        return "transition"

    def _check_trend_entry(self, row: dict) -> tuple:
        """Trending regime: EMA crossover OR strong alignment + MACD."""
        rsi = row["rsi"]
        c = row["close"]
        ema55 = row["ema55"]
        macd_hist = row["macd_hist"]
        adx = row["adx"]
        plus_di = row["plus_di"]
        minus_di = row["minus_di"]

        # ── LONG ──
        ema55_ok = (not self.require_ema55_filter) or (c > ema55)
        if ema55_ok and self.trend_rsi_min < rsi < self.trend_rsi_max:
            # Trigger 1: EMA crossover (strongest)
            if row["ema_cross_up"] and macd_hist > 0:
                strength = min(0.7 + 0.3 * min((adx - 25) / 25, 1.0), 1.0)
                return 1, strength
            # Trigger 2: Already aligned + MACD just crossed up
            if row["ema_bull_aligned"] and row["macd_cross_up"]:
                strength = min(0.5 + 0.3 * min((adx - 25) / 25, 1.0), 0.9)
                return 1, strength
            # Trigger 3: Strong trend pull-back entry (RSI dip + recovery in uptrend)
            if (row["ema_bull_aligned"] and macd_hist > 0
                    and plus_di > minus_di
                    and 45 < rsi < 60  # Momentum recovering
                    and row["roc10"] > 0):  # Short-term positive momentum
                strength = 0.5
                return 1, strength

        # ── SHORT ──
        ema55_ok = (not self.require_ema55_filter) or (c < ema55)
        if ema55_ok and self.trend_short_rsi_min < rsi < self.trend_short_rsi_max:
            if row["ema_cross_down"] and macd_hist < 0:
                strength = min(0.7 + 0.3 * min((adx - 25) / 25, 1.0), 1.0)
                return -1, strength
            if row["ema_bear_aligned"] and row["macd_cross_down"]:
                strength = min(0.5 + 0.3 * min((adx - 25) / 25, 1.0), 0.9)
                return -1, strength
            if (row["ema_bear_aligned"] and macd_hist < 0
                    and minus_di > plus_di
                    and 40 < rsi < 55
                    and row["roc10"] < 0):
                strength = 0.5
                return -1, strength

        return 0, 0.0

    def _check_range_entry(self, row: dict) -> tuple:
        """Ranging regime: BB touch + RSI extreme mean-reversion."""
        rsi = row["rsi"]
        c = row["close"]
        bb_lower = row["bb_lower"]
        bb_upper = row["bb_upper"]
        bb_pct = row["bb_pct"]
        stoch_rsi = row["stoch_rsi"]

        # ── LONG: Oversold bounce ──
        bb_touch = bb_pct < 0.05 if self.mr_bb_touch else bb_pct < 0.15
        if bb_touch and rsi < self.mr_rsi_oversold and stoch_rsi < 0.2:
            rsi_depth = (self.mr_rsi_oversold - rsi) / self.mr_rsi_oversold
            strength = min(0.5 + 0.5 * rsi_depth, 1.0)
            return 1, strength

        # ── SHORT: Overbought rejection ──
        bb_touch = bb_pct > 0.95 if self.mr_bb_touch else bb_pct > 0.85
        if bb_touch and rsi > self.mr_rsi_overbought and stoch_rsi > 0.8:
            rsi_depth = (rsi - self.mr_rsi_overbought) / (100 - self.mr_rsi_overbought)
            strength = min(0.5 + 0.5 * rsi_depth, 1.0)
            return -1, strength

        return 0, 0.0

    def _check_transition_entry(self, row: dict) -> tuple:
        """Transition zone: Donchian breakout only."""
        c = row["close"]
        dc_high_prev = row["dc_high_prev"]
        dc_low_prev = row["dc_low_prev"]
        macd_hist = row["macd_hist"]
        rsi = row["rsi"]

        if np.isnan(dc_high_prev) or np.isnan(dc_low_prev):
            return 0, 0.0

        # Breakout above 20-bar high
        if c > dc_high_prev and macd_hist > 0 and 45 < rsi < 80:
            return 1, 0.6

        # Breakdown below 20-bar low
        if c < dc_low_prev and macd_hist < 0 and 20 < rsi < 55:
            return -1, 0.6

        return 0, 0.0

    # ── Main backtest loop ──

    def run(self, df: pd.DataFrame) -> Dict[str, Any]:
        self._reset_state()

        print(f"\n{'=' * 60}")
        print(f"  PERPETUAL ADAPTIVE MOMENTUM (PAM) — BACKTEST")
        print(f"  Capital: ${self.initial_capital:,.0f} | Leverage: {self.leverage}x")
        print(f"  Bars: {len(df):,} | Timeframe: 4H")
        print(f"  ADX Trend > {self.adx_trend_thresh} | Range < {self.adx_range_thresh}")
        print(f"  Trend Stop: {self.atr_stop_mult}x ATR | TP: {self.atr_tp_mult}x ATR")
        print(f"  MR Stop: {self.mr_atr_stop_mult}x ATR | TP: {self.mr_atr_tp_mult}x ATR")
        print(f"{'=' * 60}\n")

        df = compute_indicators(df)
        warmup = 200
        timestamps = df["timestamp"].astype(str).values
        n = len(df)
        regime_counts = {"trending": 0, "ranging": 0, "transition": 0}

        for idx in range(n):
            ts = timestamps[idx]
            row = df.iloc[idx]
            c = float(row["close"])
            h = float(row["high"])
            lo = float(row["low"])

            self._bars_since_trade += 1
            if abs(self._position) > 1e-12:
                self._bars_in_position += 1
            if self._consec_loss_cooldown_remaining > 0:
                self._consec_loss_cooldown_remaining -= 1

            # Track trailing high/low
            if self._position > 0:
                self._highest_since_entry = max(self._highest_since_entry, h)
            elif self._position < 0:
                self._lowest_since_entry = min(self._lowest_since_entry, lo)

            # Equity & drawdown
            equity = self._equity(c)
            if equity > self._peak_equity:
                self._peak_equity = equity
            if self._peak_equity > 0:
                dd = (self._peak_equity - equity) / self._peak_equity
                if self._circuit_breaker:
                    self._cb_bars += 1
                    if self._cb_bars >= self.cb_cooldown_bars and abs(self._position) < 1e-12:
                        self._circuit_breaker = False
                        self._peak_equity = equity
                        self._consecutive_losses = 0
                elif dd > self.max_drawdown_pct:
                    self._circuit_breaker = True
                    self._cb_bars = 0

            # Daily trade count
            day = ts[:10]
            if day != self._current_day:
                self._daily_trade_count = 0
                self._current_day = day

            # Skip warmup
            if idx < warmup:
                self._equity_curve.append({
                    "timestamp": ts, "total_value": equity,
                    "cash": self._cash, "n_positions": 0,
                })
                continue

            atr = float(row["atr"])
            adx = float(row["adx"])
            if np.isnan(atr) or atr == 0 or np.isnan(adx):
                self._equity_curve.append({
                    "timestamp": ts, "total_value": equity,
                    "cash": self._cash,
                    "n_positions": 1 if abs(self._position) > 1e-10 else 0,
                })
                continue

            regime = self._detect_regime(adx)
            regime_counts[regime] += 1
            row_dict = row.to_dict()

            # ════════════════ EXIT LOGIC ════════════════
            if abs(self._position) > 1e-12:
                reason = None
                dsign = 1 if self._position > 0 else -1
                stop_mult = self._get_stop_mult(self._entry_regime)
                tp_mult = self._get_tp_mult(self._entry_regime)

                fixed_stop = self._entry_price - dsign * stop_mult * self._entry_atr
                tp = self._entry_price + dsign * tp_mult * self._entry_atr

                profit_dist = (c - self._entry_price) * dsign
                trailing_active = profit_dist > self.trailing_activate_atr * self._entry_atr

                if self._position > 0:
                    trail = self._highest_since_entry - self.trailing_stop_atr * self._entry_atr
                    if lo <= fixed_stop:
                        reason = "stop_loss"
                    elif trailing_active and lo <= trail:
                        reason = "trailing_stop"
                    elif h >= tp:
                        reason = "take_profit"
                    elif self._bars_in_position >= self.max_holding_bars:
                        reason = "max_holding"
                    elif self._circuit_breaker:
                        reason = "circuit_breaker"
                    # Signal reversal: EMAs flip bearish + MACD negative
                    elif (row_dict["ema_cross_down"]
                          and row_dict["macd_hist"] < 0
                          and row_dict["rsi"] < 45):
                        reason = "signal_reversal"
                else:
                    trail = self._lowest_since_entry + self.trailing_stop_atr * self._entry_atr
                    if h >= fixed_stop:
                        reason = "stop_loss"
                    elif trailing_active and h >= trail:
                        reason = "trailing_stop"
                    elif lo <= tp:
                        reason = "take_profit"
                    elif self._bars_in_position >= self.max_holding_bars:
                        reason = "max_holding"
                    elif self._circuit_breaker:
                        reason = "circuit_breaker"
                    elif (row_dict["ema_cross_up"]
                          and row_dict["macd_hist"] > 0
                          and row_dict["rsi"] > 55):
                        reason = "signal_reversal"

                if reason:
                    exit_price = c
                    if reason == "stop_loss":
                        exit_price = fixed_stop
                    elif reason == "take_profit":
                        exit_price = tp
                    elif reason == "trailing_stop":
                        exit_price = trail
                    self._close_position(exit_price, reason, ts)

            # ════════════════ ENTRY LOGIC ════════════════
            if abs(self._position) < 1e-12:
                can_trade = (
                    self._bars_since_trade >= self.cooldown_bars
                    and self._daily_trade_count < self.max_trades_per_day
                    and not self._circuit_breaker
                    and self._consec_loss_cooldown_remaining <= 0
                )

                if can_trade:
                    direction, strength = 0, 0.0

                    if regime == "trending":
                        direction, strength = self._check_trend_entry(row_dict)
                    elif regime == "ranging":
                        direction, strength = self._check_range_entry(row_dict)
                    else:
                        direction, strength = self._check_transition_entry(row_dict)

                    if direction != 0 and strength > 0:
                        self._open_position(
                            c, direction, strength, atr, regime, ts, row_dict
                        )

            equity = self._equity(c)
            self._equity_curve.append({
                "timestamp": ts, "total_value": equity,
                "cash": self._cash,
                "n_positions": 1 if abs(self._position) > 1e-10 else 0,
            })

        # Force close
        if abs(self._position) > 1e-10:
            self._close_position(float(df["close"].iloc[-1]), "backtest_end", timestamps[-1])

        total_bars = sum(regime_counts.values())
        if total_bars > 0:
            print(f"  Regime Distribution:")
            for r, cnt in sorted(regime_counts.items()):
                print(f"    {r:12s}: {cnt:5d} bars ({cnt / total_bars * 100:.1f}%)")
            print()

        return self._compute_results()

    def _compute_results(self) -> Dict[str, Any]:
        eq_df = pd.DataFrame(self._equity_curve)
        if eq_df.empty:
            return {"error": "No equity data"}

        equity = eq_df["total_value"]
        returns = equity.pct_change().dropna()
        total_return = (equity.iloc[-1] / equity.iloc[0]) - 1

        # Frequency detection
        periods_per_year = 365 * 6
        eq_ts = eq_df.get("timestamp")
        if eq_ts is not None and len(eq_ts) > 2:
            try:
                ts_parsed = pd.to_datetime(eq_ts)
                gap_h = ts_parsed.diff().dropna().median().total_seconds() / 3600
                if gap_h > 18:
                    periods_per_year = 365
                elif gap_h > 2:
                    periods_per_year = 365 * 6
                elif gap_h > 0.5:
                    periods_per_year = 365 * 24
                else:
                    periods_per_year = 365 * 24 * 12
            except Exception:
                pass

        n_periods = len(returns)
        annual_return = (1 + total_return) ** (periods_per_year / max(n_periods, 1)) - 1

        daily_vol = returns.std()
        annual_vol = daily_vol * np.sqrt(periods_per_year) if daily_vol > 0 else 0

        daily_rf = 0.05 / periods_per_year
        excess = returns - daily_rf
        sharpe = float(excess.mean() / returns.std() * np.sqrt(periods_per_year)) if returns.std() > 0 else 0

        cumulative = (1 + returns).cumprod()
        peak = cumulative.cummax()
        drawdown = (cumulative - peak) / peak
        max_dd = abs(drawdown.min()) if len(drawdown) > 0 else 0

        downside = returns[returns < 0]
        downside_vol = downside.std() * np.sqrt(periods_per_year) if len(downside) > 0 else 0
        sortino = float((annual_return - 0.05) / downside_vol) if downside_vol > 0 else 0
        calmar = float(annual_return / max_dd) if max_dd > 0 else 0

        closed = [t for t in self._trades if t["action"].startswith("CLOSE")]
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) <= 0]
        total_fees = sum(t.get("fee", 0) for t in self._trades)

        win_rate = len(wins) / max(len(closed), 1)
        avg_win = float(np.mean([t["pnl"] for t in wins])) if wins else 0
        avg_loss = float(np.mean([t["pnl"] for t in losses])) if losses else 0
        profit_factor = abs(sum(t["pnl"] for t in wins)) / max(abs(sum(t["pnl"] for t in losses)), 1)
        avg_bars = float(np.mean([t.get("bars_held", 0) for t in closed])) if closed else 0

        reasons = {}
        for t in closed:
            r = t.get("reason", "unknown")
            reasons[r] = reasons.get(r, 0) + 1

        entries = [t for t in self._trades if not t["action"].startswith("CLOSE")]
        regime_stats = {}
        for t in entries:
            reg = t.get("regime", "unknown")
            if reg not in regime_stats:
                regime_stats[reg] = {"count": 0, "total_pnl": 0}
            regime_stats[reg]["count"] += 1
        # Map PnL to regime
        for t in closed:
            reg = t.get("regime", "unknown")
            if reg in regime_stats:
                regime_stats[reg]["total_pnl"] = round(
                    regime_stats[reg].get("total_pnl", 0) + t.get("pnl", 0), 2)

        # Monthly returns
        try:
            eq_df_ts = eq_df.copy()
            eq_df_ts["timestamp"] = pd.to_datetime(eq_df_ts["timestamp"])
            eq_df_ts.set_index("timestamp", inplace=True)
            monthly = eq_df_ts["total_value"].resample("ME").last()
            monthly_ret = monthly.pct_change().dropna()
            pos_months = (monthly_ret > 0).sum()
            total_months = len(monthly_ret)
            monthly_hit = pos_months / max(total_months, 1)
            best_month = float(monthly_ret.max() * 100) if len(monthly_ret) > 0 else 0
            worst_month = float(monthly_ret.min() * 100) if len(monthly_ret) > 0 else 0
        except Exception:
            monthly_hit, total_months, best_month, worst_month = 0, 0, 0, 0

        metrics = {
            "total_return_pct": round(total_return * 100, 2),
            "annual_return_pct": round(annual_return * 100, 2),
            "annual_volatility": round(annual_vol, 4),
            "sharpe_ratio": round(sharpe, 3),
            "sortino_ratio": round(sortino, 3),
            "calmar_ratio": round(calmar, 3),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 3),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "avg_bars_held": round(avg_bars, 1),
            "total_trades": len(closed),
            "total_fees": round(total_fees, 2),
            "monthly_hit_rate": round(monthly_hit, 4),
            "total_months": total_months,
            "best_month_pct": round(best_month, 2),
            "worst_month_pct": round(worst_month, 2),
            "start_value": round(float(equity.iloc[0]), 2),
            "end_value": round(float(equity.iloc[-1]), 2),
            "exit_reasons": reasons,
            "regime_stats": regime_stats,
        }

        return {"metrics": metrics, "equity_curve": self._equity_curve, "trades": self._trades}


# ────────────────────────────────────────────────────────────
#  Reporting
# ────────────────────────────────────────────────────────────

def print_report(results: Dict[str, Any]):
    m = results.get("metrics", {})
    trades = results.get("trades", [])

    print(f"\n{'=' * 68}")
    print(f"  PERPETUAL ADAPTIVE MOMENTUM (PAM) — BACKTEST REPORT")
    print(f"{'=' * 68}")
    print(f"  Total Return:      {m.get('total_return_pct', 0):+.2f}%")
    print(f"  Annual Return:     {m.get('annual_return_pct', 0):+.2f}%")
    print(f"  Sharpe Ratio:      {m.get('sharpe_ratio', 0):.3f}")
    print(f"  Sortino Ratio:     {m.get('sortino_ratio', 0):.3f}")
    print(f"  Calmar Ratio:      {m.get('calmar_ratio', 0):.3f}")
    print(f"  Max Drawdown:      {m.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Annual Volatility: {m.get('annual_volatility', 0):.4f}")
    print(f"  Start → End:       ${m.get('start_value', 0):,.2f} → ${m.get('end_value', 0):,.2f}")
    print(f"{'=' * 68}")
    print(f"  Total Trades:      {m.get('total_trades', 0)}")
    print(f"  Win Rate:          {m.get('win_rate', 0) * 100:.1f}%")
    print(f"  Profit Factor:     {m.get('profit_factor', 0):.2f}")
    print(f"  Avg Win:           ${m.get('avg_win', 0):+.2f}")
    print(f"  Avg Loss:          ${m.get('avg_loss', 0):+.2f}")
    print(f"  Avg Bars Held:     {m.get('avg_bars_held', 0):.1f} ({m.get('avg_bars_held', 0) * 4:.0f}h)")
    print(f"  Total Fees:        ${m.get('total_fees', 0):,.2f}")
    print(f"{'=' * 68}")
    print(f"  Monthly Hit Rate:  {m.get('monthly_hit_rate', 0) * 100:.1f}% ({m.get('total_months', 0)} months)")
    print(f"  Best Month:        {m.get('best_month_pct', 0):+.2f}%")
    print(f"  Worst Month:       {m.get('worst_month_pct', 0):+.2f}%")
    print(f"{'=' * 68}")

    reasons = m.get("exit_reasons", {})
    if reasons:
        print(f"  Exit Reasons:")
        for r, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {r:20s}: {cnt}")

    reg = m.get("regime_stats", {})
    if reg:
        print(f"  Regime Performance:")
        for regime, s in sorted(reg.items()):
            print(f"    {regime:12s}: {s['count']:3d} trades  PnL=${s.get('total_pnl', 0):+,.2f}")
    print(f"{'=' * 68}")

    closed = [t for t in trades if t.get("action", "").startswith("CLOSE")]
    if closed:
        print(f"\n  Last 20 Trades:")
        for t in closed[-20:]:
            pnl = t.get("pnl", 0)
            pnl_pct = t.get("pnl_pct", 0)
            bars = t.get("bars_held", 0)
            print(f"    {t['timestamp'][:16]}  {t['action']:12s}  "
                  f"entry={t.get('entry_price', 0):>10.1f}  "
                  f"exit={t.get('price', 0):>10.1f}  "
                  f"PnL={pnl:+10.2f} ({pnl_pct:+5.1f}%)  "
                  f"{bars:2d}bars  {t.get('reason', 'n/a'):16s}  [{t.get('regime', '')}]")
    print()


def run_parameter_sweep(df: pd.DataFrame, capital: float = 10_000):
    configs = [
        {"name": "Baseline",
         "leverage": 10.0, "atr_stop_mult": 2.0, "atr_tp_mult": 4.0,
         "trailing_stop_atr": 2.5},
        {"name": "Tight Stops",
         "leverage": 10.0, "atr_stop_mult": 1.5, "atr_tp_mult": 3.0,
         "trailing_stop_atr": 2.0},
        {"name": "Wide Stops",
         "leverage": 10.0, "atr_stop_mult": 2.5, "atr_tp_mult": 5.0,
         "trailing_stop_atr": 3.0},
        {"name": "High R:R",
         "leverage": 10.0, "atr_stop_mult": 2.0, "atr_tp_mult": 6.0,
         "trailing_stop_atr": 3.0, "trailing_activate_atr": 2.0},
        {"name": "Conservative",
         "leverage": 7.0, "atr_stop_mult": 2.5, "atr_tp_mult": 4.5,
         "trailing_stop_atr": 3.0, "base_position_pct": 0.08},
        {"name": "Aggressive",
         "leverage": 12.0, "atr_stop_mult": 1.8, "atr_tp_mult": 3.5,
         "trailing_stop_atr": 2.0, "base_position_pct": 0.12},
        {"name": "TrendOnly",
         "leverage": 10.0, "adx_trend_thresh": 20.0, "adx_range_thresh": 10.0},
        {"name": "More MR",
         "leverage": 10.0, "adx_trend_thresh": 30.0, "adx_range_thresh": 25.0,
         "mr_rsi_oversold": 35.0, "mr_rsi_overbought": 65.0},
        {"name": "NoEMA55",
         "leverage": 10.0, "require_ema55_filter": False},
        {"name": "FastTrade",
         "leverage": 10.0, "atr_stop_mult": 1.5, "atr_tp_mult": 3.0,
         "max_holding_bars": 18, "cooldown_bars": 1},
    ]

    print(f"\n{'=' * 110}")
    print(f"  PAM STRATEGY — PARAMETER SWEEP ({len(configs)} configs)")
    print(f"{'=' * 110}")
    hdr = (f"  {'Config':<14s} {'Return':>8s} {'AnnRet':>8s} {'Sharpe':>8s} "
           f"{'MaxDD':>8s} {'WinRate':>8s} {'PF':>6s} {'Trades':>7s} "
           f"{'AvgBars':>8s} {'MoHit':>7s} {'EndVal':>10s}")
    print(hdr)
    print(f"  {'-' * 104}")

    results_all = []
    for cfg in configs:
        name = cfg.pop("name")
        bt = PerpMomentumBacktest(initial_capital=capital, **cfg)
        result = bt.run(df)
        m = result.get("metrics", {})

        print(f"  {name:<14s} {m.get('total_return_pct', 0):>+7.1f}% "
              f"{m.get('annual_return_pct', 0):>+7.1f}% "
              f"{m.get('sharpe_ratio', 0):>8.3f} "
              f"{m.get('max_drawdown_pct', 0):>7.1f}% "
              f"{m.get('win_rate', 0) * 100:>7.1f}% "
              f"{m.get('profit_factor', 0):>5.2f} "
              f"{m.get('total_trades', 0):>7d} "
              f"{m.get('avg_bars_held', 0):>7.1f} "
              f"{m.get('monthly_hit_rate', 0) * 100:>6.1f}% "
              f"${m.get('end_value', 0):>9,.0f}")

        results_all.append({"name": name, **m})

    print(f"  {'-' * 104}")

    valid = [r for r in results_all if r.get("total_return_pct", 0) > 0]
    if valid:
        best_sharpe = max(valid, key=lambda x: x.get("sharpe_ratio", -999))
        best_return = max(valid, key=lambda x: x.get("total_return_pct", -999))
        print(f"\n  Best Sharpe:  {best_sharpe['name']} "
              f"(Sharpe={best_sharpe.get('sharpe_ratio', 0):.3f}, "
              f"Return={best_sharpe.get('total_return_pct', 0):+.1f}%)")
        print(f"  Best Return:  {best_return['name']} "
              f"(Return={best_return.get('total_return_pct', 0):+.1f}%, "
              f"Sharpe={best_return.get('sharpe_ratio', 0):.3f})")
    else:
        best = max(results_all, key=lambda x: x.get("total_return_pct", -999))
        print(f"\n  Best: {best['name']} (Return={best.get('total_return_pct', 0):+.1f}%)")
    print()

    return results_all


def main():
    parser = argparse.ArgumentParser(description="PAM Strategy Backtest")
    parser.add_argument("--data", type=str, default=None)
    parser.add_argument("--symbol", type=str, default="BTC/USDT")
    parser.add_argument("--capital", type=float, default=10000)
    parser.add_argument("--leverage", type=float, default=10.0)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--save", type=str, default=None)
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--end", type=str, default=None)
    args = parser.parse_args()

    data_file = args.data
    if not data_file:
        safe = args.symbol.replace("/", "_")
        for c in [PROJECT_ROOT / "data" / "sample" / f"{safe}_4h.csv",
                   PROJECT_ROOT / "data" / "crypto" / f"{safe}_4h.csv"]:
            if c.exists():
                data_file = str(c)
                break

    if not data_file or not Path(data_file).exists():
        print(f"ERROR: No data for {args.symbol}. Run: python scripts/fetch_crypto_4h_data.py")
        sys.exit(1)

    print(f"Loading: {data_file}")
    df = pd.read_csv(data_file)
    df.columns = [c.lower() for c in df.columns]
    for col in ("timestamp", "time", "datetime", "date"):
        if col in df.columns:
            df.rename(columns={col: "timestamp"}, inplace=True)
            break

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    if args.start:
        df = df[df["timestamp"] >= pd.Timestamp(args.start)]
    if args.end:
        df = df[df["timestamp"] <= pd.Timestamp(args.end)]
    df.reset_index(drop=True, inplace=True)

    print(f"Shape: {df.shape} | {df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}")

    if args.sweep:
        results_all = run_parameter_sweep(df, capital=args.capital)
        if args.save:
            p = Path(args.save)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w") as f:
                json.dump(results_all, f, indent=2, default=str)
            print(f"Saved: {p}")
    else:
        bt = PerpMomentumBacktest(initial_capital=args.capital, leverage=args.leverage)
        results = bt.run(df)
        print_report(results)

        if args.save:
            p = Path(args.save)
            p.parent.mkdir(parents=True, exist_ok=True)
            save_data = {
                "strategy": "perp_adaptive_momentum",
                "symbol": args.symbol,
                "params": {"capital": args.capital, "leverage": args.leverage},
                "metrics": results["metrics"],
                "trades": results["trades"],
                "n_equity_points": len(results["equity_curve"]),
            }
            with open(p, "w") as f:
                json.dump(save_data, f, indent=2, default=str)
            print(f"Saved: {p}")


if __name__ == "__main__":
    main()
