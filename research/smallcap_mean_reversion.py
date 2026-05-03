"""Small-cap mean reversion backtester.

Theory
------
Small caps mean-revert harder than large caps. After a 3-day or 5-day
sharp decline, the rebound probability is well above 50% on a 1-3 day
horizon. This is the classic Lehmann (1990) / Avellaneda-Lee result that
holds particularly well in the small-cap universe due to:

  * less institutional sponsorship (no buy-the-dip from index funds)
  * more retail flow (panic selling, then mean-revert)
  * less analyst coverage (no continuous re-pricing)
  * higher idiosyncratic vol (more "extreme" 3-day moves)

Entry logic
-----------
Buy when: 3-day cumulative return < -threshold (e.g. -3%)
       AND price > 200-day SMA (long-term uptrend filter — don't catch
                                  falling knives in bear markets)
       AND RSI(2) < 15 (very short-term oversold)

Exit logic
----------
Sell when: any of these triggers fire (whichever comes first)
  * 3-day cumulative return turns positive (mean revert)
  * +2% from entry price (profit target)
  * RSI(2) > 70 (overbought after bounce)
  * 5 days held (time stop — avoid carry-cost positions)
  * -3% from entry (stop loss)

Position sizing
---------------
Fixed % of equity per trade (default 95% — full deploy when signal
fires; we're flat most of the time so this is safer than it sounds).

Outputs match other backtesters (ICResult-style WheelResult-style).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class MRTrade:
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    shares: int
    exit_reason: str
    days_held: int
    pnl: float
    pnl_pct: float


@dataclass
class MRResult:
    ticker: str
    initial_capital: float
    trades: List[MRTrade] = field(default_factory=list)
    equity_curve: List[Tuple[pd.Timestamp, float]] = field(default_factory=list)

    def metrics(self) -> Dict[str, Any]:
        if not self.equity_curve:
            return {"ticker": self.ticker, "error": "empty"}

        eq = pd.DataFrame(self.equity_curve, columns=["ts", "value"])
        eq["ret"] = eq["value"].pct_change().fillna(0)

        total_return = eq["value"].iloc[-1] / self.initial_capital - 1.0
        n_days = (eq["ts"].iloc[-1] - eq["ts"].iloc[0]).days
        years = max(n_days / 365.25, 0.01)
        ann_return = (1 + total_return) ** (1 / years) - 1

        ann_vol = eq["ret"].std() * np.sqrt(252)
        sharpe = (ann_return / ann_vol) if ann_vol > 0 else 0.0

        running_max = eq["value"].cummax()
        dd = (eq["value"] / running_max - 1).min()

        n = len(self.trades)
        wins = sum(1 for t in self.trades if t.pnl > 0)
        win_rate = wins / n if n > 0 else 0.0

        gross_win = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

        avg_days = np.mean([t.days_held for t in self.trades]) if self.trades else 0
        time_in_market = (sum(t.days_held for t in self.trades) / max(n_days, 1)) if self.trades else 0

        return {
            "ticker": self.ticker,
            "n_trades": n,
            "win_rate_pct": round(win_rate * 100, 1),
            "profit_factor": round(float(pf), 2) if np.isfinite(pf) else 999.0,
            "avg_days_held": round(float(avg_days), 1),
            "time_in_market_pct": round(float(time_in_market) * 100, 1),
            "initial_capital": self.initial_capital,
            "final_value": round(eq["value"].iloc[-1], 2),
            "total_return_pct": round(total_return * 100, 2),
            "annual_return_pct": round(ann_return * 100, 2),
            "annual_volatility": round(ann_vol, 4),
            "sharpe_ratio": round(float(sharpe), 3),
            "max_drawdown_pct": round(float(abs(dd)) * 100, 2),
            "avg_win_pct": round(np.mean([t.pnl_pct for t in self.trades if t.pnl > 0]) * 100, 2)
                if any(t.pnl > 0 for t in self.trades) else 0,
            "avg_loss_pct": round(np.mean([t.pnl_pct for t in self.trades if t.pnl < 0]) * 100, 2)
                if any(t.pnl < 0 for t in self.trades) else 0,
        }


def _rsi(closes: pd.Series, period: int = 2) -> pd.Series:
    """Wilder's RSI (the smaller the period, the more reactive). RSI(2) is
    Connors's classic mean-reversion gauge."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def run_smallcap_mr_backtest(
    ticker: str,
    df: pd.DataFrame,
    *,
    initial_capital: float = 10_000,
    drop_threshold_pct: float = 3.0,    # 3-day cum return < -3%
    rsi2_oversold: float = 15.0,
    rsi2_overbought: float = 70.0,
    profit_target_pct: float = 2.0,
    stop_loss_pct: float = 3.0,
    max_hold_days: int = 5,
    sma_filter: Optional[int] = 200,    # only trade when close > SMA200; None disables
    deploy_pct: float = 0.95,
    fee_per_share: float = 0.005,       # broker comm proxy
    slippage_bps: float = 5.0,
    vix_series: Optional[pd.Series] = None,   # VIX close indexed by timestamp
    vix_max: float = 30.0,              # skip entries when VIX > this
    zscore_sizing: bool = False,        # scale position by extremeness of oversold
) -> MRResult:
    """Single-ticker daily mean-reversion backtest.

    Optional regime filters:
      * vix_series — when VIX > vix_max on entry day, skip the trade
      * zscore_sizing — scale deploy_pct by how extreme the 3-day drop is
        (deeper drop = larger size, capped at deploy_pct)
    """
    df = df.copy()
    use_sma = sma_filter is not None and sma_filter > 1
    if use_sma:
        df["sma200"] = df["close"].rolling(sma_filter, min_periods=sma_filter // 2).mean()
    else:
        df["sma200"] = -np.inf  # always pass the filter
    df["ret_3d"] = df["close"] / df["close"].shift(3) - 1.0
    df["rsi2"] = _rsi(df["close"], period=2)

    # Attach VIX if provided — align by timestamp date
    if vix_series is not None and len(vix_series) > 0:
        vix_aligned = pd.Series(index=df.index, dtype="float64")
        # Build a lookup: timestamp -> vix close
        vix_lookup = vix_series.copy()
        if hasattr(vix_lookup.index, "tz") and vix_lookup.index.tz is not None:
            vix_lookup.index = vix_lookup.index.tz_localize(None)
        for i, ts in enumerate(df["timestamp"]):
            ts_naive = pd.Timestamp(ts).tz_localize(None) if hasattr(ts, "tz") and ts.tz is not None else pd.Timestamp(ts)
            # Find nearest VIX close (same date)
            if ts_naive in vix_lookup.index:
                vix_aligned.iloc[i] = vix_lookup.loc[ts_naive]
            else:
                # Use the most recent prior VIX value
                prior = vix_lookup.loc[:ts_naive]
                if len(prior) > 0:
                    vix_aligned.iloc[i] = prior.iloc[-1]
        df["vix"] = vix_aligned.ffill().fillna(20.0)
    else:
        df["vix"] = 0.0  # always pass

    result = MRResult(ticker=ticker, initial_capital=initial_capital)

    cash = initial_capital
    shares = 0
    entry_price = 0.0
    entry_ts: Optional[pd.Timestamp] = None
    days_held = 0

    slip_mult = slippage_bps / 10_000.0

    for i, row in df.iterrows():
        ts = row["timestamp"]
        close = float(row["close"])
        sma200 = row["sma200"]
        ret3 = row["ret_3d"]
        rsi2 = row["rsi2"]

        # Mark to market
        equity = cash + shares * close
        result.equity_curve.append((ts, equity))

        if shares > 0:
            # Manage open position
            days_held += 1
            pnl_pct = close / entry_price - 1.0

            exit_reason = None
            if close >= entry_price * (1 + profit_target_pct / 100):
                exit_reason = "profit_target"
            elif close <= entry_price * (1 - stop_loss_pct / 100):
                exit_reason = "stop_loss"
            elif not pd.isna(rsi2) and rsi2 > rsi2_overbought:
                exit_reason = "rsi_overbought"
            elif not pd.isna(ret3) and ret3 > 0:
                exit_reason = "mean_reverted"
            elif days_held >= max_hold_days:
                exit_reason = "time_stop"

            if exit_reason:
                # Close position
                fill_price = close * (1 - slip_mult)  # slippage hurts on sell
                proceeds = shares * fill_price
                fee = shares * fee_per_share
                cash += proceeds - fee
                pnl = (fill_price - entry_price) * shares - fee
                result.trades.append(MRTrade(
                    entry_ts=entry_ts,
                    exit_ts=ts,
                    entry_price=entry_price,
                    exit_price=fill_price,
                    shares=shares,
                    exit_reason=exit_reason,
                    days_held=days_held,
                    pnl=pnl,
                    pnl_pct=fill_price / entry_price - 1.0,
                ))
                shares = 0
                entry_price = 0.0
                entry_ts = None
                days_held = 0
        else:
            # Look for entry
            vix_ok = vix_series is None or row.get("vix", 0) < vix_max
            if (
                not pd.isna(ret3) and ret3 < -drop_threshold_pct / 100
                and not pd.isna(sma200) and close > sma200
                and not pd.isna(rsi2) and rsi2 < rsi2_oversold
                and vix_ok
            ):
                # Z-score sizing: scale deploy by how extreme the drop is.
                # ret3 = -drop_threshold → 1.0× ; ret3 = -2×drop_threshold → up to deploy_pct
                effective_deploy = deploy_pct
                if zscore_sizing:
                    extremeness = min(abs(ret3) / (drop_threshold_pct / 100), 2.0)
                    effective_deploy = deploy_pct * (extremeness / 2.0 + 0.5)
                    effective_deploy = min(effective_deploy, deploy_pct)

                fill_price = close * (1 + slip_mult)
                deploy_cash = cash * effective_deploy
                shares_to_buy = int(deploy_cash // fill_price)
                if shares_to_buy >= 1:
                    fee = shares_to_buy * fee_per_share
                    cash -= shares_to_buy * fill_price + fee
                    shares = shares_to_buy
                    entry_price = fill_price
                    entry_ts = ts
                    days_held = 0

    # Force-close any open position at last close
    if shares > 0:
        last = df.iloc[-1]
        close = float(last["close"])
        proceeds = shares * close
        fee = shares * fee_per_share
        cash += proceeds - fee
        pnl = (close - entry_price) * shares - fee
        result.trades.append(MRTrade(
            entry_ts=entry_ts,
            exit_ts=last["timestamp"],
            entry_price=entry_price,
            exit_price=close,
            shares=shares,
            exit_reason="forced_eod",
            days_held=days_held,
            pnl=pnl,
            pnl_pct=close / entry_price - 1.0,
        ))
        result.equity_curve[-1] = (last["timestamp"], cash)

    return result
