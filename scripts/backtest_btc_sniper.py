#!/usr/bin/env python3
"""BTC Momentum Sniper v5 — 固定风险仓位管理 + 趋势跟踪.

核心改进:
  1. Fixed-Risk Position Sizing: 每笔交易固定风险X%权益
     qty = risk_amount / stop_distance → 止损宽度和风险脱钩
  2. 宽止损(3-4 ATR) + 小仓位 → 不被噪音打掉
  3. 让赢的跑: 无固定止盈, 只有trailing stop
  4. 趋势方向过滤: EMA(55)方向+价格位置, 只做顺势
  5. 双入场: Donchian突破 + EMA回调
  6. 强平保护: margin上限检查

Usage:
    python scripts/backtest_btc_sniper.py --sweep
"""

from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from typing import Any, Dict, List
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ════════════════════════════════════════════════════════════════
#  SIGNALS
# ════════════════════════════════════════════════════════════════

def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    for span in [8, 21, 55]:
        df[f"ema{span}"] = c.ewm(span=span, adjust=False).mean()
    df["ema55_slope"] = (df["ema55"] - df["ema55"].shift(6)) / df["ema55"].shift(6) * 100

    delta = c.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
    df["rsi"] = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).fillna(50)

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["macd_hist"] = (ema12 - ema26) - (ema12 - ema26).ewm(span=9, adjust=False).mean()

    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=14, adjust=False).mean()

    # ADX
    plus_dm = h.diff()
    minus_dm = -l.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    atr_s = tr.ewm(span=14, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(span=14, adjust=False).mean() / atr_s.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(span=14, adjust=False).mean() / atr_s.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.ewm(span=14, adjust=False).mean()
    df["di_spread"] = plus_di - minus_di

    for p in [20, 10]:
        df[f"dc_high_{p}"] = h.rolling(p).max()
        df[f"dc_low_{p}"] = l.rolling(p).min()
    df["dc_high_prev20"] = df["dc_high_20"].shift(1)
    df["dc_low_prev20"] = df["dc_low_20"].shift(1)
    df["dc_high_prev10"] = df["dc_high_10"].shift(1)
    df["dc_low_prev10"] = df["dc_low_10"].shift(1)

    df["vol_sma"] = v.rolling(20).mean()
    df["vol_ratio"] = v / df["vol_sma"].replace(0, np.nan)

    # Pullback detection
    df["prev_close"] = c.shift(1)
    df["near_ema21_up"] = (l <= df["ema21"] * 1.01) & (c > df["ema21"]) & (c > c.shift(1))
    df["near_ema21_dn"] = (h >= df["ema21"] * 0.99) & (c < df["ema21"]) & (c < c.shift(1))

    return df


def get_trend(row: dict) -> int:
    """Simple trend: +1 bull, -1 bear, 0 neutral.
    Uses 2-of-3 voting: ema55_slope, price vs ema55, di_spread.
    """
    close = row.get("close", 0)
    ema55 = row.get("ema55", 0)
    slope = row.get("ema55_slope", 0)
    di = row.get("di_spread", 0)
    adx = row.get("adx", 0)

    for v in [ema55, slope, di, adx]:
        if isinstance(v, float) and np.isnan(v):
            return 0

    bull = bear = 0
    if slope > 0.05: bull += 1
    elif slope < -0.05: bear += 1
    if close > ema55 * 1.003: bull += 1
    elif close < ema55 * 0.997: bear += 1
    if adx > 18:
        if di > 2: bull += 1
        elif di < -2: bear += 1

    if bull >= 2: return 1
    if bear >= 2: return -1
    return 0


# ════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE v5
# ════════════════════════════════════════════════════════════════

class BTCSniperBacktest:
    def __init__(
        self,
        initial_capital: float = 10_000,
        leverage: float = 20.0,
        # ── Fixed-Risk Sizing ──
        risk_per_trade: float = 0.05,       # Risk 5% of equity per trade
        max_margin_pct: float = 0.30,       # Max 30% of equity as margin
        # ── Entry ──
        min_score: int = 2,
        vol_min: float = 1.2,
        use_donchian10: bool = True,        # Use DC10 (faster) instead of DC20
        # ── Stops (ATR multiples) ──
        atr_stop: float = 3.5,             # Initial stop = 3.5 ATR (wide!)
        atr_trail: float = 2.5,            # Trailing = 2.5 ATR from peak
        atr_trail_activate: float = 2.0,   # Activate after 2 ATR profit
        # ── Time ──
        cooldown: int = 3,
        max_hold: int = 120,               # 20 days
        # ── Fees ──
        fee_rate: float = 0.0005,
        # ── Loss management ──
        pause_after_losses: int = 5,
        pause_bars: int = 18,
    ):
        self.initial_capital = initial_capital
        self.leverage = leverage
        self.risk_per_trade = risk_per_trade
        self.max_margin_pct = max_margin_pct
        self.min_score = min_score
        self.vol_min = vol_min
        self.use_donchian10 = use_donchian10
        self.atr_stop = atr_stop
        self.atr_trail = atr_trail
        self.atr_trail_activate = atr_trail_activate
        self.cooldown = cooldown
        self.max_hold = max_hold
        self.fee_rate = fee_rate
        self.pause_after_losses = pause_after_losses
        self.pause_bars = pause_bars
        self._reset()

    def _reset(self):
        self._cash = self.initial_capital
        self._pos = 0.0
        self._entry = 0.0
        self._margin = 0.0
        self._entry_atr = 0.0
        self._stop_dist = 0.0  # actual stop distance in price
        self._best = 0.0
        self._worst = 0.0
        self._bars_held = 0
        self._bars_since = 999
        self._consec_loss = 0
        self._pause_until = 0

        self._equity_curve: List[Dict] = []
        self._trades: List[Dict] = []

    def _equity(self, price: float) -> float:
        if abs(self._pos) < 1e-12:
            return self._cash
        upnl = (price - self._entry) * self._pos
        return self._cash + self._margin + upnl

    def _open(self, price, direction, atr, timestamp, info):
        equity = self._equity(price)
        risk_amount = equity * self.risk_per_trade

        # Stop distance
        stop_dist = atr * self.atr_stop
        if stop_dist < price * 0.005:  # Min 0.5% stop
            stop_dist = price * 0.005

        # Position size from risk: qty = risk / stop_dist
        qty_abs = risk_amount / stop_dist
        notional = qty_abs * price
        margin_needed = notional / self.leverage

        # Cap margin at max_margin_pct of equity
        if margin_needed > equity * self.max_margin_pct:
            margin_needed = equity * self.max_margin_pct
            notional = margin_needed * self.leverage
            qty_abs = notional / price

        qty = qty_abs * direction
        fee = abs(notional) * self.fee_rate

        self._pos = qty
        self._entry = price
        self._margin = margin_needed
        self._entry_atr = atr
        self._stop_dist = stop_dist
        self._cash -= (margin_needed + fee)
        self._best = price
        self._worst = price
        self._bars_held = 0
        self._bars_since = 0

        actual_risk_pct = (stop_dist * abs(qty)) / equity * 100 if equity > 0 else 0

        self._trades.append({
            "ts": timestamp,
            "action": "LONG" if direction > 0 else "SHORT",
            "price": round(price, 1),
            "qty": round(abs(qty), 6),
            "notional": round(abs(notional), 0),
            "margin": round(margin_needed, 2),
            "margin_pct": round(margin_needed / equity * 100, 1),
            "leverage": round(abs(notional) / max(margin_needed, 1), 1),
            "risk_pct": round(actual_risk_pct, 1),
            "stop_dist_pct": round(stop_dist / price * 100, 2),
            "fee": round(fee, 2),
            **info,
        })

    def _close(self, price, reason, timestamp):
        if abs(self._pos) < 1e-12:
            return
        pnl = (price - self._entry) * self._pos
        fee = abs(self._pos * price) * self.fee_rate
        net_pnl = pnl - fee

        # Cap loss at margin (liquidation)
        if net_pnl < -self._margin * 0.95:
            net_pnl = -self._margin * 0.95
            reason = "liquidation"

        pnl_pct = net_pnl / max(self._margin, 1) * 100
        eq_before = self._cash + self._margin + (self._entry - self._entry) * self._pos  # equity at entry
        equity = self._equity(self._entry)  # approximate equity at entry
        pnl_eq_pct = net_pnl / max(equity, 1) * 100

        self._cash += self._margin + net_pnl

        if net_pnl < 0:
            self._consec_loss += 1
        else:
            self._consec_loss = 0

        self._trades.append({
            "ts": timestamp,
            "action": f"CLOSE_{'LONG' if self._pos > 0 else 'SHORT'}",
            "price": round(price, 1),
            "pnl": round(net_pnl, 2),
            "pnl_margin_pct": round(pnl_pct, 1),
            "pnl_equity_pct": round(pnl_eq_pct, 1),
            "fee": round(fee, 2),
            "reason": reason,
            "entry": round(self._entry, 1),
            "bars": self._bars_held,
        })

        self._pos = 0.0
        self._entry = 0.0
        self._margin = 0.0
        self._entry_atr = 0.0
        self._stop_dist = 0.0
        self._bars_held = 0
        self._bars_since = 0

    def _check_entry(self, row: dict, trend: int) -> tuple:
        """Check for entry. Returns (direction, info) or (0, {})."""
        c = row["close"]
        atr = row.get("atr", 0)
        if np.isnan(atr) or atr < 1e-12:
            return 0, {}

        dc_key = "dc_high_prev10" if self.use_donchian10 else "dc_high_prev20"
        dc_low_key = "dc_low_prev10" if self.use_donchian10 else "dc_low_prev20"
        dc_high = row.get(dc_key, np.nan)
        dc_low = row.get(dc_low_key, np.nan)

        direction = 0
        entry_type = ""

        # ── Breakout entry ──
        if not np.isnan(dc_high) and c > dc_high and trend >= 0:
            direction = 1
            entry_type = "breakout"
        elif not np.isnan(dc_low) and c < dc_low and trend <= 0:
            direction = -1
            entry_type = "breakout"

        # ── Pullback entry (only in confirmed trend) ──
        if direction == 0 and trend != 0:
            if trend == 1 and row.get("near_ema21_up", False):
                ema55 = row.get("ema55", 0)
                if not np.isnan(ema55) and c > ema55:
                    direction = 1
                    entry_type = "pullback"
            elif trend == -1 and row.get("near_ema21_dn", False):
                ema55 = row.get("ema55", 0)
                if not np.isnan(ema55) and c < ema55:
                    direction = -1
                    entry_type = "pullback"

        if direction == 0:
            return 0, {}

        # Must match trend if trend is set
        if trend != 0 and direction != trend:
            return 0, {}

        # ── Score ──
        score = 0
        vol = row.get("vol_ratio", 0)
        if not np.isnan(vol) and vol >= self.vol_min: score += 1
        if not np.isnan(vol) and vol >= 2.0: score += 1

        adx = row.get("adx", 0)
        if not np.isnan(adx) and adx >= 20: score += 1
        if not np.isnan(adx) and adx >= 30: score += 1

        rsi = row.get("rsi", 50)
        if direction > 0 and 45 <= rsi <= 80: score += 1
        elif direction < 0 and 20 <= rsi <= 55: score += 1

        macd = row.get("macd_hist", 0)
        if (direction > 0 and macd > 0) or (direction < 0 and macd < 0): score += 1

        ema8, ema21 = row.get("ema8", 0), row.get("ema21", 0)
        if direction > 0 and ema8 > ema21: score += 1
        elif direction < 0 and ema8 < ema21: score += 1

        if score < self.min_score:
            return 0, {}

        return direction, {
            "type": entry_type, "score": score,
            "vol": round(vol, 1) if not np.isnan(vol) else 0,
            "adx": round(adx, 1) if not np.isnan(adx) else 0,
        }

    def run(self, df: pd.DataFrame) -> Dict[str, Any]:
        self._reset()
        df = compute_signals(df)
        warmup = 60
        ts_arr = df["timestamp"].astype(str).values
        n = len(df)

        entry_types = {"breakout": 0, "pullback": 0}

        for idx in range(n):
            ts = ts_arr[idx]
            row = df.iloc[idx]
            c, h, lo = float(row["close"]), float(row["high"]), float(row["low"])
            atr = float(row["atr"]) if not np.isnan(row["atr"]) else 0

            self._bars_since += 1
            if abs(self._pos) > 1e-12:
                self._bars_held += 1
                if self._pos > 0:
                    self._best = max(self._best, h)
                elif self._pos < 0:
                    self._worst = min(self._worst, lo) if self._worst > 0 else lo

            equity = self._equity(c)

            if idx < warmup:
                self._equity_curve.append({"ts": ts, "equity": equity})
                continue

            # Row dict
            rd = {}
            for k in ["close", "adx", "di_spread", "ema55_slope", "ema55", "ema8", "ema21",
                       "dc_high_prev20", "dc_low_prev20", "dc_high_prev10", "dc_low_prev10",
                       "vol_ratio", "rsi", "macd_hist", "atr",
                       "near_ema21_up", "near_ema21_dn", "prev_close"]:
                val = row.get(k)
                rd[k] = val.item() if isinstance(val, (np.bool_, np.generic)) else val

            trend = get_trend(rd)

            # ════════ EXIT ════════
            if abs(self._pos) > 1e-12 and self._stop_dist > 0:
                reason = None
                dsign = 1 if self._pos > 0 else -1

                stop_price = self._entry - dsign * self._stop_dist

                profit_in_atr = (c - self._entry) * dsign / max(self._entry_atr, 1)
                trail_active = profit_in_atr > self.atr_trail_activate

                trail_dist = self._entry_atr * self.atr_trail
                if self._pos > 0:
                    trail_price = self._best - trail_dist
                    if lo <= stop_price:
                        reason = "stop_loss"
                    elif trail_active and lo <= trail_price:
                        reason = "trailing_stop"
                    elif self._bars_held >= self.max_hold:
                        reason = "time_exit"
                else:
                    trail_price = (self._worst + trail_dist) if self._worst > 0 else (c + trail_dist)
                    if h >= stop_price:
                        reason = "stop_loss"
                    elif trail_active and h >= trail_price:
                        reason = "trailing_stop"
                    elif self._bars_held >= self.max_hold:
                        reason = "time_exit"

                if reason:
                    exit_p = stop_price if reason == "stop_loss" else (
                        trail_price if reason == "trailing_stop" else c)
                    self._close(exit_p, reason, ts)

            # ════════ ENTRY ════════
            if abs(self._pos) < 1e-12:
                equity = self._equity(c)
                paused = idx < self._pause_until
                if not paused and self._consec_loss >= self.pause_after_losses:
                    self._pause_until = idx + self.pause_bars
                    self._consec_loss = 0
                    paused = True

                can = (
                    not paused
                    and self._bars_since >= self.cooldown
                    and equity > self.initial_capital * 0.05
                    and atr > 0
                )
                if can:
                    direction, info = self._check_entry(rd, trend)
                    if direction != 0:
                        entry_types[info.get("type", "?")] = entry_types.get(info.get("type", "?"), 0) + 1
                        self._open(c, direction, atr, ts, info)

            equity = self._equity(c)
            self._equity_curve.append({"ts": ts, "equity": equity})

        if abs(self._pos) > 1e-12:
            self._close(float(df["close"].iloc[-1]), "end", ts_arr[-1])

        r = self._results()
        r["metrics"]["entry_types"] = entry_types
        return r

    def _results(self):
        eq = pd.DataFrame(self._equity_curve)
        if eq.empty:
            return {"error": "no data"}
        equity = eq["equity"]
        rets = equity.pct_change().dropna()
        total_ret = equity.iloc[-1] / equity.iloc[0] - 1
        ppy = 365 * 6
        n_p = len(rets)
        ann_ret = (1 + total_ret) ** (ppy / max(n_p, 1)) - 1
        vol = float(rets.std() * np.sqrt(ppy)) if rets.std() > 0 else 0
        sharpe = float((rets.mean() - 0.05/ppy) / rets.std() * np.sqrt(ppy)) if rets.std() > 0 else 0
        cum = (1 + rets).cumprod()
        max_dd = float(abs(((cum - cum.cummax()) / cum.cummax()).min())) if len(cum) > 0 else 0
        dn = rets[rets < 0]
        dn_vol = float(dn.std() * np.sqrt(ppy)) if len(dn) > 0 and dn.std() > 0 else 0
        sortino = float((ann_ret - 0.05) / dn_vol) if dn_vol > 0 else 0
        calmar = float(ann_ret / max_dd) if max_dd > 0 else 0

        closed = [t for t in self._trades if t["action"].startswith("CLOSE")]
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) <= 0]
        fees = sum(t.get("fee", 0) for t in self._trades)
        wr = len(wins) / max(len(closed), 1)
        aw = float(np.mean([t["pnl"] for t in wins])) if wins else 0
        al = float(np.mean([t["pnl"] for t in losses])) if losses else 0
        pf = abs(sum(t["pnl"] for t in wins)) / max(abs(sum(t["pnl"] for t in losses)), 1)
        ab = float(np.mean([t.get("bars", 0) for t in closed])) if closed else 0

        reasons = {}
        for t in closed:
            reasons[t.get("reason", "?")] = reasons.get(t.get("reason", "?"), 0) + 1

        eq_rets = [t.get("pnl_equity_pct", 0) for t in closed]
        avg_eq_ret = float(np.mean(eq_rets)) if eq_rets else 0

        yearly = {}
        try:
            eq3 = eq.copy()
            eq3["ts"] = pd.to_datetime(eq3["ts"])
            eq3["year"] = eq3["ts"].dt.year
            for yr, grp in eq3.groupby("year"):
                yearly[int(yr)] = round((grp["equity"].iloc[-1] / grp["equity"].iloc[0] - 1) * 100, 2)
        except Exception:
            pass

        try:
            eq2 = eq.copy()
            eq2["ts"] = pd.to_datetime(eq2["ts"])
            eq2.set_index("ts", inplace=True)
            mo = eq2["equity"].resample("ME").last().pct_change().dropna()
            m_hit = float((mo > 0).mean())
            best_m = float(mo.max() * 100)
            worst_m = float(mo.min() * 100)
            n_mo = len(mo)
        except Exception:
            m_hit, best_m, worst_m, n_mo = 0, 0, 0, 0

        return {
            "metrics": {
                "total_return_pct": round(total_ret * 100, 2),
                "annual_return_pct": round(ann_ret * 100, 2),
                "sharpe": round(sharpe, 3),
                "sortino": round(sortino, 3),
                "calmar": round(calmar, 3),
                "max_dd_pct": round(max_dd * 100, 2),
                "volatility": round(vol, 4),
                "win_rate": round(wr, 4),
                "profit_factor": round(pf, 3),
                "avg_win": round(aw, 2),
                "avg_loss": round(al, 2),
                "avg_bars": round(ab, 1),
                "trades": len(closed),
                "fees": round(fees, 2),
                "avg_equity_return_pct": round(avg_eq_ret, 2),
                "monthly_hit": round(m_hit, 3),
                "n_months": n_mo,
                "best_month_pct": round(best_m, 2),
                "worst_month_pct": round(worst_m, 2),
                "yearly": yearly,
                "start_eq": round(float(equity.iloc[0]), 2),
                "end_eq": round(float(equity.iloc[-1]), 2),
                "exits": reasons,
            },
            "equity_curve": self._equity_curve,
            "trades": self._trades,
        }


# ════════════════════════════════════════════════════════════════
#  REPORT & SWEEP
# ════════════════════════════════════════════════════════════════

def report(r, label=""):
    m = r.get("metrics", {})
    trades = r.get("trades", [])
    print(f"\n{'='*68}")
    print(f"  BTC SNIPER v5 — Fixed Risk Sizing {label}")
    print(f"{'='*68}")
    print(f"  Total Return:      {m.get('total_return_pct',0):+.2f}%")
    print(f"  Annual Return:     {m.get('annual_return_pct',0):+.2f}%")
    print(f"  Sharpe:            {m.get('sharpe',0):.3f}")
    print(f"  Sortino:           {m.get('sortino',0):.3f}")
    print(f"  Calmar:            {m.get('calmar',0):.3f}")
    print(f"  Max Drawdown:      {m.get('max_dd_pct',0):.1f}%")
    print(f"  Start -> End:      ${m.get('start_eq',0):,.0f} -> ${m.get('end_eq',0):,.0f}")
    print(f"{'='*68}")
    print(f"  Trades:            {m.get('trades',0)}")
    print(f"  Win Rate:          {m.get('win_rate',0)*100:.1f}%")
    print(f"  Profit Factor:     {m.get('profit_factor',0):.2f}")
    print(f"  Avg Win:           ${m.get('avg_win',0):+.2f}")
    print(f"  Avg Loss:          ${m.get('avg_loss',0):+.2f}")
    print(f"  Avg Bars:          {m.get('avg_bars',0):.1f} ({m.get('avg_bars',0)*4:.0f}h)")
    print(f"  Fees:              ${m.get('fees',0):,.2f}")
    print(f"  Avg Return/Trade:  {m.get('avg_equity_return_pct',0):+.2f}% equity")
    print(f"  Monthly Hit:       {m.get('monthly_hit',0)*100:.0f}% ({m.get('n_months',0)} mo)")
    print(f"  Best/Worst Month:  {m.get('best_month_pct',0):+.1f}% / {m.get('worst_month_pct',0):+.1f}%")
    print(f"{'='*68}")
    yearly = m.get("yearly", {})
    if yearly:
        print(f"  Yearly Returns:")
        for yr in sorted(yearly.keys()):
            v = yearly[yr]
            bar = "+" * min(int(abs(v) / 5), 20) if v > 0 else "-" * min(int(abs(v) / 5), 20)
            print(f"    {yr}: {v:+7.1f}%  {bar}")
        print(f"{'='*68}")
    et = m.get("entry_types", {})
    if et:
        print(f"  Entry Types: {et}")
    exits = m.get("exits", {})
    if exits:
        print(f"  Exit Reasons:")
        for k, v in sorted(exits.items(), key=lambda x: -x[1]):
            print(f"    {k:18s}: {v}")
    print(f"{'='*68}")
    closed = [t for t in trades if t["action"].startswith("CLOSE")]
    if closed:
        print(f"\n  Recent Trades (last 15):")
        for t in closed[-15:]:
            pnl = t.get("pnl", 0)
            b = t.get("bars", 0)
            sym = "+" if pnl > 0 else ""
            eq_r = t.get("pnl_equity_pct", 0)
            print(f"    {t['ts'][:16]}  {t['action']:12s}  "
                  f"entry={t.get('entry',0):>9.0f}  exit={t.get('price',0):>9.0f}  "
                  f"PnL={sym}{pnl:>8.0f} ({sym}{eq_r:.1f}%eq)  "
                  f"{b:3d}bar  {t.get('reason','')}")
    print()


def _get_sweep_cfgs():
    return [
        # ── Risk-based sizing, varying risk% and stops ──
        {"name": "R5 S3.5 T2.5",   "risk_per_trade": 0.05, "atr_stop": 3.5, "atr_trail": 2.5,
         "atr_trail_activate": 2.0, "max_hold": 120},
        {"name": "R3 S3.5 T2.5",   "risk_per_trade": 0.03, "atr_stop": 3.5, "atr_trail": 2.5,
         "atr_trail_activate": 2.0, "max_hold": 120},
        {"name": "R7 S3.5 T2.5",   "risk_per_trade": 0.07, "atr_stop": 3.5, "atr_trail": 2.5,
         "atr_trail_activate": 2.0, "max_hold": 120},
        {"name": "R5 S4.0 T3.0",   "risk_per_trade": 0.05, "atr_stop": 4.0, "atr_trail": 3.0,
         "atr_trail_activate": 2.5, "max_hold": 120},
        {"name": "R5 S2.5 T2.0",   "risk_per_trade": 0.05, "atr_stop": 2.5, "atr_trail": 2.0,
         "atr_trail_activate": 1.5, "max_hold": 120},
        # ── Different leverage ──
        {"name": "L10 R5 S3.5",    "leverage": 10, "risk_per_trade": 0.05, "atr_stop": 3.5,
         "atr_trail": 2.5, "max_hold": 120},
        {"name": "L15 R5 S3.5",    "leverage": 15, "risk_per_trade": 0.05, "atr_stop": 3.5,
         "atr_trail": 2.5, "max_hold": 120},
        {"name": "L30 R5 S3.5",    "leverage": 30, "risk_per_trade": 0.05, "atr_stop": 3.5,
         "atr_trail": 2.5, "max_hold": 120},
        # ── More selective entry ──
        {"name": "R5 S3.5 Score3",  "risk_per_trade": 0.05, "atr_stop": 3.5, "atr_trail": 2.5,
         "min_score": 3, "max_hold": 120},
        {"name": "R5 S3.5 Score4",  "risk_per_trade": 0.05, "atr_stop": 3.5, "atr_trail": 2.5,
         "min_score": 4, "max_hold": 120},
        # ── Long holding period ──
        {"name": "R5 S4 Long",      "risk_per_trade": 0.05, "atr_stop": 4.0, "atr_trail": 3.0,
         "atr_trail_activate": 2.5, "max_hold": 240},
        # ── Higher risk per trade ──
        {"name": "R10 S3.5 T2.5",  "risk_per_trade": 0.10, "atr_stop": 3.5, "atr_trail": 2.5,
         "atr_trail_activate": 2.0, "max_hold": 120},
        # ── DC20 instead of DC10 ──
        {"name": "R5 DC20 S3.5",   "risk_per_trade": 0.05, "atr_stop": 3.5, "atr_trail": 2.5,
         "use_donchian10": False, "max_hold": 120},
        # ── Very aggressive ──
        {"name": "R10 L25 S3 Long", "risk_per_trade": 0.10, "leverage": 25, "atr_stop": 3.0,
         "atr_trail": 2.0, "max_hold": 180},
    ]


def sweep(df, capital):
    cfgs = _get_sweep_cfgs()
    print(f"\n{'='*140}")
    print(f"  BTC SNIPER v5 SWEEP ({len(cfgs)} configs) — Fixed Risk Position Sizing")
    print(f"{'='*140}")
    hdr = (f"  {'Config':<18s} {'Return':>8s} {'AnnRet':>8s} {'Sharpe':>8s} "
           f"{'MaxDD':>8s} {'WinRate':>8s} {'PF':>6s} {'Trades':>7s} "
           f"{'AvgEqR%':>8s} {'MoHit':>7s} {'Fees':>8s} {'EndVal':>12s}")
    print(hdr)
    print(f"  {'-'*134}")

    all_r = []
    for cfg in cfgs:
        name = cfg.pop("name")
        bt = BTCSniperBacktest(initial_capital=capital, **cfg)
        res = bt.run(df)
        m = res.get("metrics", {})
        print(
            f"  {name:<18s} {m.get('total_return_pct',0):>+7.1f}% "
            f"{m.get('annual_return_pct',0):>+7.1f}% "
            f"{m.get('sharpe',0):>8.3f} "
            f"{m.get('max_dd_pct',0):>7.1f}% "
            f"{m.get('win_rate',0)*100:>7.1f}% "
            f"{m.get('profit_factor',0):>5.2f} "
            f"{m.get('trades',0):>7d} "
            f"{m.get('avg_equity_return_pct',0):>+7.2f}% "
            f"{m.get('monthly_hit',0)*100:>6.0f}% "
            f"${m.get('fees',0):>7,.0f} "
            f"${m.get('end_eq',0):>11,.0f}"
        )
        all_r.append({"name": name, **m})

    print(f"  {'-'*134}")
    sorted_r = sorted(all_r, key=lambda x: x.get("total_return_pct", -999), reverse=True)
    print(f"\n  TOP 5:")
    for i, b in enumerate(sorted_r[:5]):
        print(f"    #{i+1}: {b['name']} -> {b.get('total_return_pct',0):+.1f}%, "
              f"Ann={b.get('annual_return_pct',0):+.1f}%, "
              f"WR={b.get('win_rate',0)*100:.0f}%, PF={b.get('profit_factor',0):.2f}, "
              f"Trades={b.get('trades',0)}, DD={b.get('max_dd_pct',0):.0f}%")
    print()
    return all_r


def main():
    parser = argparse.ArgumentParser(description="BTC Momentum Sniper v5")
    parser.add_argument("--data", type=str, default=None)
    parser.add_argument("--symbol", type=str, default="BTC/USDT")
    parser.add_argument("--capital", type=float, default=10000)
    parser.add_argument("--leverage", type=float, default=20)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--save", type=str, default=None)
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--end", type=str, default=None)
    args = parser.parse_args()

    data_file = args.data
    if not data_file:
        safe = args.symbol.replace("/", "_")
        cands = [PROJECT_ROOT / "data" / "sample" / f"{safe}_4h.csv"]
        for cand in cands:
            if cand.exists():
                data_file = str(cand); break

    if not data_file or not Path(data_file).exists():
        print(f"ERROR: No data for {args.symbol}"); sys.exit(1)

    print(f"Loading: {data_file}")
    df = pd.read_csv(data_file)
    df.columns = [c.lower() for c in df.columns]
    for col in ("timestamp", "time", "datetime", "date"):
        if col in df.columns:
            df.rename(columns={col: "timestamp"}, inplace=True); break
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_localize(None)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    if args.start:
        df = df[df["timestamp"] >= pd.Timestamp(args.start)]
    if args.end:
        df = df[df["timestamp"] <= pd.Timestamp(args.end)]
    df.reset_index(drop=True, inplace=True)
    print(f"Shape: {df.shape} | {df.timestamp.iloc[0]} -> {df.timestamp.iloc[-1]}\n")

    if args.sweep:
        all_r = sweep(df, args.capital)
        best = max(all_r, key=lambda x: x.get("total_return_pct", -999))
        print(f"\n{'#'*68}")
        print(f"  DETAILED: {best['name']}")
        print(f"{'#'*68}")
        for cfg in _get_sweep_cfgs():
            if cfg["name"] == best["name"]:
                bt = BTCSniperBacktest(initial_capital=args.capital,
                                       **{k: v for k, v in cfg.items() if k != "name"})
                report(bt.run(df), label=f"[{best['name']}]")
                break
        if args.save:
            with open(args.save, "w") as f:
                json.dump(all_r, f, indent=2, default=str)
    else:
        bt = BTCSniperBacktest(initial_capital=args.capital, leverage=args.leverage)
        res = bt.run(df)
        report(res)
        if args.save:
            with open(args.save, "w") as f:
                json.dump({"metrics": res["metrics"], "trades": res["trades"]},
                          f, indent=2, default=str)


if __name__ == "__main__":
    main()
