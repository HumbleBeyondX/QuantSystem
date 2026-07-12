#!/usr/bin/env python3
"""ETH Momentum Sniper ported to NautilusTrader 1.230.0.

Faithful port of scripts/backtest_btc_sniper.py (class BTCSniperBacktest.run):
  - Indicators replicated incrementally to match pandas ewm(adjust=False) /
    rolling exactly: EMA(8/21/55), RSI(14, ewm alpha=1/14), MACD hist,
    ATR(14, ewm span), ADX/DI (ewm span=14 variant), Donchian(10/20) prev,
    vol ratio vs SMA(20), EMA21 pullback flags.
  - Entry: Donchian-prev breakout or EMA21 pullback, 2-of-3 trend vote,
    score >= min_score (volume/ADX/RSI/MACD/EMA-alignment components).
  - Exits: initial stop 3.5*ATR (min 0.5% of price), trailing 3.0*ATR from
    peak activated after 2.0*ATR profit (re-evaluated every bar, can
    deactivate), max_hold time exit, all managed via a resting STOP_MARKET
    order (Nautilus L1 bar execution fills stops at trigger price, matching
    the reference's exit-at-stop-price semantics) modified bar-by-bar.
  - Sizing: fixed risk 5% equity / stop distance, margin capped at 30% equity
    at the configured leverage.
  - Cooldown 3 bars, pause 18 bars after 5 consecutive losses, 60-bar warmup.

Notes on known semantic gaps vs the reference engine are in README.md.
"""
from __future__ import annotations

import math
from collections import deque

from nautilus_trader.core.message import Event
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import (
    OrderCancelRejected,
    OrderModifyRejected,
    OrderRejected,
    PositionClosed,
    PositionOpened,
)
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.config import StrategyConfig

NAN = float("nan")


def _isnan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


class _Ewm:
    """pandas ewm(adjust=False).mean() replica, seeded at first valid input."""

    __slots__ = ("alpha", "value")

    def __init__(self, span: float | None = None, alpha: float | None = None):
        self.alpha = alpha if alpha is not None else 2.0 / (span + 1.0)
        self.value: float | None = None

    def update(self, x: float) -> float:
        if self.value is None:
            self.value = x
        else:
            self.value += self.alpha * (x - self.value)
        return self.value


class SniperSignals:
    """Incremental replication of compute_signals() from backtest_btc_sniper.py."""

    def __init__(self) -> None:
        self.ema8 = _Ewm(span=8)
        self.ema21 = _Ewm(span=21)
        self.ema55 = _Ewm(span=55)
        self._ema55_hist: deque = deque(maxlen=7)  # need value 6 bars back
        self._gain = _Ewm(alpha=1.0 / 14.0)
        self._loss = _Ewm(alpha=1.0 / 14.0)
        self._ema12 = _Ewm(span=12)
        self._ema26 = _Ewm(span=26)
        self._macd_sig = _Ewm(span=9)
        self.atr = _Ewm(span=14)
        self._pdm = _Ewm(span=14)
        self._mdm = _Ewm(span=14)
        self._adx = _Ewm(span=14)
        self._highs10: deque = deque(maxlen=10)
        self._lows10: deque = deque(maxlen=10)
        self._highs20: deque = deque(maxlen=20)
        self._lows20: deque = deque(maxlen=20)
        self._vols: deque = deque(maxlen=20)
        self._prev_close: float | None = None
        self._prev_high: float | None = None
        self._prev_low: float | None = None

    def update(self, h: float, l: float, c: float, v: float) -> dict:
        pc = self._prev_close

        ema8 = self.ema8.update(c)
        ema21 = self.ema21.update(c)
        ema55 = self.ema55.update(c)

        # ema55 slope over 6 bars (pct); deque holds [t-6 .. t] after append
        self._ema55_hist.append(ema55)
        if len(self._ema55_hist) == 7:
            base = self._ema55_hist[0]
            slope = (ema55 - base) / base * 100.0
        else:
            slope = NAN

        # RSI (pandas: bar-0 NaN delta becomes 0.0 via .where(..., 0.0), so the
        # gain/loss EWMs seed at 0.0 on bar 0; loss==0 -> NaN -> fillna(50))
        if pc is not None:
            delta = c - pc
            g = self._gain.update(delta if delta > 0 else 0.0)
            lo_ = self._loss.update(-delta if delta < 0 else 0.0)
        else:
            g = self._gain.update(0.0)
            lo_ = self._loss.update(0.0)
        rsi = 50.0 if lo_ == 0 else 100.0 - 100.0 / (1.0 + g / lo_)

        # MACD histogram
        e12 = self._ema12.update(c)
        e26 = self._ema26.update(c)
        macd = e12 - e26
        macd_hist = macd - self._macd_sig.update(macd)

        # ATR (true range; bar 0 uses h-l)
        if pc is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - pc), abs(l - pc))
        atr = self.atr.update(tr)

        # ADX / DI (reference uses ewm span=14 on DM and TR, then ewm of DX)
        if self._prev_high is None:
            pdm_raw, mdm_raw = 0.0, 0.0  # pandas: NaN comparisons -> where -> 0.0
        else:
            up = h - self._prev_high
            dn = self._prev_low - l
            pdm_raw = up if (up > dn and up > 0) else 0.0
            mdm_raw = dn if (dn > up and dn > 0) else 0.0
        pdm = self._pdm.update(pdm_raw)
        mdm = self._mdm.update(mdm_raw)
        if atr and atr > 0:
            pdi = 100.0 * pdm / atr
            mdi = 100.0 * mdm / atr
            di_spread = pdi - mdi
            dx = 100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) != 0 else NAN
        else:
            di_spread, dx = NAN, NAN
        adx = self._adx.update(dx) if not _isnan(dx) else (
            self._adx.value if self._adx.value is not None else NAN
        )

        # Donchian prev (rolling(N).max().shift(1)): stats BEFORE adding this bar
        dc_high_prev10 = max(self._highs10) if len(self._highs10) == 10 else NAN
        dc_low_prev10 = min(self._lows10) if len(self._lows10) == 10 else NAN
        dc_high_prev20 = max(self._highs20) if len(self._highs20) == 20 else NAN
        dc_low_prev20 = min(self._lows20) if len(self._lows20) == 20 else NAN
        self._highs10.append(h)
        self._lows10.append(l)
        self._highs20.append(h)
        self._lows20.append(l)

        # Volume ratio vs SMA(20), current bar inclusive
        self._vols.append(v)
        if len(self._vols) == 20:
            vol_sma = sum(self._vols) / 20.0
            vol_ratio = v / vol_sma if vol_sma != 0 else NAN
        else:
            vol_ratio = NAN

        # Pullback flags (use current-bar ema21 and prev close)
        near_up = (l <= ema21 * 1.01) and (c > ema21) and (pc is not None and c > pc)
        near_dn = (h >= ema21 * 0.99) and (c < ema21) and (pc is not None and c < pc)

        self._prev_close = c
        self._prev_high = h
        self._prev_low = l

        return {
            "close": c,
            "ema8": ema8,
            "ema21": ema21,
            "ema55": ema55,
            "ema55_slope": slope,
            "rsi": rsi,
            "macd_hist": macd_hist,
            "atr": atr,
            "adx": adx,
            "di_spread": di_spread,
            "dc_high_prev10": dc_high_prev10,
            "dc_low_prev10": dc_low_prev10,
            "dc_high_prev20": dc_high_prev20,
            "dc_low_prev20": dc_low_prev20,
            "vol_ratio": vol_ratio,
            "near_ema21_up": near_up,
            "near_ema21_dn": near_dn,
        }


def get_trend(sig: dict) -> int:
    """2-of-3 vote: ema55 slope, price vs ema55, DI spread (ADX-gated)."""
    close = sig["close"]
    ema55 = sig["ema55"]
    slope = sig["ema55_slope"]
    di = sig["di_spread"]
    adx = sig["adx"]
    for x in (ema55, slope, di, adx):
        if _isnan(x):
            return 0
    bull = bear = 0
    if slope > 0.05:
        bull += 1
    elif slope < -0.05:
        bear += 1
    if close > ema55 * 1.003:
        bull += 1
    elif close < ema55 * 0.997:
        bear += 1
    if adx > 18:
        if di > 2:
            bull += 1
        elif di < -2:
            bear += 1
    if bull >= 2:
        return 1
    if bear >= 2:
        return -1
    return 0


class EthSniperConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    initial_capital: float = 100_000.0
    leverage: float = 50.0
    risk_per_trade: float = 0.05
    max_margin_pct: float = 0.30
    min_score: int = 2
    vol_min: float = 1.2
    use_donchian10: bool = True
    atr_stop: float = 3.5
    atr_trail: float = 3.0
    atr_trail_activate: float = 2.0
    cooldown: int = 3
    max_hold: int = 180
    pause_after_losses: int = 5
    pause_bars: int = 18
    warmup: int = 60
    long_only: bool = False
    # Emulate the reference engine's "liquidation" loss cap (95% of margin) by
    # tightening the initial stop to the price where the loss equals that cap.
    liq_cap: bool = False


class EthSniperStrategy(Strategy):
    def __init__(self, config: EthSniperConfig) -> None:
        super().__init__(config)
        self.instrument = None
        self.signals = SniperSignals()

        # Bar bookkeeping (mirrors reference run-loop state)
        self.bar_idx = 0
        self.bars_since = 999
        self.consec_loss = 0
        self.pause_until = 0
        self.just_closed = False   # set when a resting stop closed us intrabar

        # Position state
        self.pos_dir = 0
        self.entry_px = 0.0
        self.entry_atr = 0.0
        self.stop_dist = 0.0
        self.best = 0.0
        self.worst = 0.0
        self.bars_held = 0
        self.pending_entry: dict | None = None
        self.closing = False
        self.close_reason: str | None = None
        self.stop_order = None
        self.stop_kind = "stop_loss"  # what the resting trigger currently represents
        self.position_id = None
        self._qty = 0.0
        self._liq_capped = False

        # Records
        self.equity_curve: list[tuple[int, float]] = []
        self.closed_trades: list[dict] = []
        self.entry_types: dict[str, int] = {}
        self.exit_reasons: dict[str, int] = {}

    # ──────────────────────────────────────────────────────────────
    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Instrument {self.config.instrument_id} not found")
            self.stop()
            return
        self.subscribe_bars(self.config.bar_type)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.config.instrument_id)
        self.close_all_positions(self.config.instrument_id)

    # ──────────────────────────────────────────────────────────────
    def _equity(self) -> float:
        account = self.portfolio.account(self.config.instrument_id.venue)
        bal = account.balance_total(USDT).as_double()
        upnl = self.portfolio.unrealized_pnl(self.config.instrument_id)
        return bal + (upnl.as_double() if upnl is not None else 0.0)

    # ──────────────────────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> None:
        h = bar.high.as_double()
        l = bar.low.as_double()
        c = bar.close.as_double()
        v = bar.volume.as_double()
        sig = self.signals.update(h, l, c, v)
        atr = sig["atr"] if not _isnan(sig["atr"]) else 0.0
        idx = self.bar_idx
        self.bar_idx += 1

        # Top-of-loop bookkeeping (reference: bars_since += 1; exit resets to 0)
        self.bars_since += 1
        if self.just_closed:
            # A resting stop closed the position during THIS bar's ticks —
            # the reference would have closed it this bar and set bars_since=0.
            self.bars_since = 0
            self.just_closed = False

        in_pos = self.pos_dir != 0
        if in_pos:
            self.bars_held += 1
            if self.pos_dir > 0:
                self.best = max(self.best, h)
            else:
                self.worst = min(self.worst, l) if self.worst > 0 else l

        if idx < self.config.warmup:
            self.equity_curve.append((bar.ts_event, self._equity()))
            return

        trend = get_trend(sig)

        # ════════ EXIT MANAGEMENT ════════
        if self.pos_dir != 0 and not self.closing:
            dsign = self.pos_dir
            stop_price = self.entry_px - dsign * self.stop_dist
            profit_in_atr = (c - self.entry_px) * dsign / max(self.entry_atr, 1.0)
            trail_active = profit_in_atr > self.config.atr_trail_activate
            trail_dist = self.entry_atr * self.config.atr_trail

            if dsign > 0:
                trail_price = self.best - trail_dist
                desired = max(stop_price, trail_price) if trail_active else stop_price
                kind = "trailing_stop" if (trail_active and trail_price > stop_price) else self._init_stop_kind()
                through = desired >= c
            else:
                trail_price = (self.worst + trail_dist) if self.worst > 0 else (c + trail_dist)
                desired = min(stop_price, trail_price) if trail_active else stop_price
                kind = "trailing_stop" if (trail_active and trail_price < stop_price) else self._init_stop_kind()
                through = desired <= c

            if self.bars_held >= self.config.max_hold:
                self._market_close("time_exit")
            elif through:
                # Market already through the desired trigger (reference would
                # exit this bar); a stop modify would be rejected — close now.
                self._market_close(kind)
            else:
                self._ensure_stop(desired, kind)

        # ════════ ENTRY ════════
        if self.pos_dir == 0 and self.pending_entry is None and not self.closing:
            paused = idx < self.pause_until
            if not paused and self.consec_loss >= self.config.pause_after_losses:
                self.pause_until = idx + self.config.pause_bars
                self.consec_loss = 0
                paused = True
            equity = self._equity()
            can = (
                not paused
                and self.bars_since >= self.config.cooldown
                and equity > self.config.initial_capital * 0.05
                and atr > 0
            )
            if can:
                direction, info = self._check_entry(sig, trend)
                if self.config.long_only and direction < 0:
                    direction = 0
                if direction != 0:
                    et = info.get("type", "?")
                    self.entry_types[et] = self.entry_types.get(et, 0) + 1
                    self._enter(direction, c, atr, equity)

        self.equity_curve.append((bar.ts_event, self._equity()))

    def _init_stop_kind(self) -> str:
        return "liquidation" if (self.config.liq_cap and self._liq_capped) else "stop_loss"

    # ──────────────────────────────────────────────────────────────
    def _check_entry(self, sig: dict, trend: int) -> tuple[int, dict]:
        """Verbatim port of BTCSniperBacktest._check_entry."""
        c = sig["close"]
        atr = sig["atr"]
        if _isnan(atr) or atr < 1e-12:
            return 0, {}

        dc_high = sig["dc_high_prev10"] if self.config.use_donchian10 else sig["dc_high_prev20"]
        dc_low = sig["dc_low_prev10"] if self.config.use_donchian10 else sig["dc_low_prev20"]

        direction = 0
        entry_type = ""
        if not _isnan(dc_high) and c > dc_high and trend >= 0:
            direction = 1
            entry_type = "breakout"
        elif not _isnan(dc_low) and c < dc_low and trend <= 0:
            direction = -1
            entry_type = "breakout"

        if direction == 0 and trend != 0:
            if trend == 1 and sig["near_ema21_up"]:
                if not _isnan(sig["ema55"]) and c > sig["ema55"]:
                    direction = 1
                    entry_type = "pullback"
            elif trend == -1 and sig["near_ema21_dn"]:
                if not _isnan(sig["ema55"]) and c < sig["ema55"]:
                    direction = -1
                    entry_type = "pullback"

        if direction == 0:
            return 0, {}
        if trend != 0 and direction != trend:
            return 0, {}

        score = 0
        vol = sig["vol_ratio"]
        if not _isnan(vol) and vol >= self.config.vol_min:
            score += 1
        if not _isnan(vol) and vol >= 2.0:
            score += 1
        adx = sig["adx"]
        if not _isnan(adx) and adx >= 20:
            score += 1
        if not _isnan(adx) and adx >= 30:
            score += 1
        rsi = sig["rsi"]
        if direction > 0 and 45 <= rsi <= 80:
            score += 1
        elif direction < 0 and 20 <= rsi <= 55:
            score += 1
        macd = sig["macd_hist"]
        if (direction > 0 and macd > 0) or (direction < 0 and macd < 0):
            score += 1
        if direction > 0 and sig["ema8"] > sig["ema21"]:
            score += 1
        elif direction < 0 and sig["ema8"] < sig["ema21"]:
            score += 1

        if score < self.config.min_score:
            return 0, {}
        return direction, {"type": entry_type, "score": score}

    # ──────────────────────────────────────────────────────────────
    def _enter(self, direction: int, price: float, atr: float, equity: float) -> None:
        cfg = self.config
        risk_amount = equity * cfg.risk_per_trade
        stop_dist = atr * cfg.atr_stop
        if stop_dist < price * 0.005:
            stop_dist = price * 0.005

        qty_abs = risk_amount / stop_dist
        notional = qty_abs * price
        margin_needed = notional / cfg.leverage
        if margin_needed > equity * cfg.max_margin_pct:
            margin_needed = equity * cfg.max_margin_pct
            notional = margin_needed * cfg.leverage
            qty_abs = notional / price

        self._liq_capped = False
        if cfg.liq_cap:
            # Reference caps any loss at 95% of margin ("liquidation"); emulate
            # by tightening the stop to the equivalent-loss price.
            liq_dist = 0.95 * margin_needed / qty_abs
            if liq_dist < stop_dist:
                stop_dist = liq_dist
                self._liq_capped = True

        qty = self.instrument.make_qty(round(qty_abs, self.instrument.size_precision))
        if qty.as_double() <= 0:
            return

        order = self.order_factory.market(
            instrument_id=cfg.instrument_id,
            order_side=OrderSide.BUY if direction > 0 else OrderSide.SELL,
            quantity=qty,
            time_in_force=TimeInForce.GTC,
        )
        self.pending_entry = {
            "dir": direction,
            "atr": atr,
            "stop_dist": stop_dist,
            "signal_px": price,
        }
        self.bars_since = 0  # reference sets this at open
        self.submit_order(order)

    def _ensure_stop(self, trigger: float, kind: str) -> None:
        self.stop_kind = kind
        trig_price = self.instrument.make_price(trigger)
        if self.stop_order is not None and self.stop_order.is_open:
            if self.stop_order.trigger_price != trig_price:
                self.modify_order(self.stop_order, trigger_price=trig_price)
            return
        if self.stop_order is not None and not self.stop_order.is_closed:
            return  # in flight
        order = self.order_factory.stop_market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.SELL if self.pos_dir > 0 else OrderSide.BUY,
            quantity=self.instrument.make_qty(abs(self._qty)),
            trigger_price=trig_price,
            reduce_only=True,
        )
        self.stop_order = order
        self.submit_order(order, position_id=self.position_id)

    def _market_close(self, reason: str) -> None:
        self.closing = True
        self.close_reason = reason
        self.bars_since = 0  # reference _close resets it on the exit bar
        if self.stop_order is not None and self.stop_order.is_open:
            self.cancel_order(self.stop_order)
        position = self.cache.position(self.position_id)
        if position is not None and not position.is_closed:
            self.close_position(position)

    # ──────────────────────────────────────────────────────────────
    def on_event(self, event: Event) -> None:
        if isinstance(event, PositionOpened):
            self._handle_opened(event)
        elif isinstance(event, PositionClosed):
            self._handle_closed(event)
        elif isinstance(event, OrderRejected):
            self.log.error(f"Order rejected: {event}")
            if self.pending_entry is not None:
                self.pending_entry = None
        elif isinstance(event, OrderModifyRejected):
            self.log.warning(f"Modify rejected: {event}")
        elif isinstance(event, OrderCancelRejected):
            self.log.warning(f"Cancel rejected: {event}")

    def _handle_opened(self, event: PositionOpened) -> None:
        if self.pending_entry is None:
            return
        position = self.cache.position(event.position_id)
        pe = self.pending_entry
        self.pending_entry = None
        self.position_id = event.position_id
        self.pos_dir = pe["dir"]
        self.entry_px = float(position.avg_px_open)
        self.entry_atr = pe["atr"]
        self.stop_dist = pe["stop_dist"]
        self.best = self.entry_px
        self.worst = self.entry_px
        self.bars_held = 0
        self._qty = position.quantity.as_double()
        # Place the initial protective stop
        trigger = self.entry_px - self.pos_dir * self.stop_dist
        self._ensure_stop(trigger, self._init_stop_kind())

    def _handle_closed(self, event: PositionClosed) -> None:
        position = self.cache.position(event.position_id)
        pnl = position.realized_pnl.as_double() if position is not None else 0.0

        if self.closing:
            reason = self.close_reason or "market_close"
        else:
            reason = self.stop_kind  # resting stop filled intrabar
            self.just_closed = True
        self.exit_reasons[reason] = self.exit_reasons.get(reason, 0) + 1

        self.closed_trades.append(
            {
                "ts": event.ts_event,
                "dir": self.pos_dir,
                "entry": self.entry_px,
                "exit": float(position.avg_px_close) if position is not None else NAN,
                "pnl": pnl,
                "bars": self.bars_held,
                "reason": reason,
            }
        )

        if pnl < 0:
            self.consec_loss += 1
        else:
            self.consec_loss = 0

        # Cancel any leftover protective stop
        if self.stop_order is not None and self.stop_order.is_open:
            self.cancel_order(self.stop_order)

        self.pos_dir = 0
        self.entry_px = 0.0
        self.entry_atr = 0.0
        self.stop_dist = 0.0
        self.best = 0.0
        self.worst = 0.0
        self.bars_held = 0
        self.closing = False
        self.close_reason = None
        self.stop_order = None
        self.position_id = None
