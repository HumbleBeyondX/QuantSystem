"""Wheel-strategy backtester — simulates the full CSP → assigned → CC cycle.

Scope
-----
Single-ticker, daily-bar, monthly cycle (30-DTE puts/calls). Uses
Black-Scholes for option pricing with a per-stock IV proxy derived
from rolling 30-day realized volatility plus a configurable VRP markup.

State machine
-------------
    CASH ───sell CSP at delta D──> hold to expiry ──┐
      ▲                                              │
      │                                              ▼
      │                                  ┌─ OTM at expiry: keep premium, repeat
      │                                  └─ ITM at expiry: get assigned at strike
      │                                              │
      │                                              ▼
      │                                       SHARES (long 100 × n_contracts)
      │                                              │
      │                                              ▼
      │                                    sell CC at delta D
      │                                              │
      │                                              ▼
      │                                ┌─ OTM at expiry: keep premium + shares, sell CC again
      └────────── ITM at expiry: shares called away at strike ──┘

Pricing model
-------------
* IV per ticker = realized_vol_30d × VRP_multiplier (default 1.15)
* Per-bar IV uses the trailing 30-day realized vol so it actually moves
* Black-Scholes call/put with continuous dividend yield = 0 for simplicity
* Delta-targeted strikes computed from BS via Newton's method

Costs modeled
-------------
* $0.65 per-contract commission (typical retail option cost)
* $0.01 spread cost per option (fill at mid, slip 1¢)
* No share commission (most retail = $0)
* No assignment fee (Tradier/IBKR = $0)
* Tax: NOT modeled

Outputs
-------
WheelResult dataclass with:
* equity curve, trades log, assignments log
* aggregated metrics (total return, Sharpe, max DD, win rate, etc.)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.stats import norm


# ────────────────────────────────────────────────────────────
# Black-Scholes pricer
# ────────────────────────────────────────────────────────────

def bs_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """European call premium (Black-Scholes)."""
    K = max(K, 0.01)
    if T <= 0 or sigma <= 0:
        return max(S - K, 0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return float(S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))


def bs_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """European put premium."""
    K = max(K, 0.01)
    if T <= 0 or sigma <= 0:
        return max(K - S, 0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return float(K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))


def bs_put_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Put delta (negative)."""
    K = max(K, 0.01)
    if T <= 0 or sigma <= 0:
        return -1.0 if S < K else 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    return float(norm.cdf(d1) - 1.0)


def bs_call_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Call delta (positive)."""
    K = max(K, 0.01)
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    return float(norm.cdf(d1))


def find_strike_for_delta(
    S: float, T: float, r: float, sigma: float,
    target_delta: float, is_put: bool,
    max_iter: int = 30,
) -> float:
    """Find strike where option delta ≈ target_delta. Newton's method
    on |delta(K) - target|. target_delta is positive magnitude (e.g. 0.25).
    Returns a strike floored at 5% of spot to avoid degenerate near-0 strikes."""
    floor = max(S * 0.05, 0.01)
    ceil = S * 5.0
    K = S * (0.97 if is_put else 1.03)
    for _ in range(max_iter):
        if is_put:
            d = bs_put_delta(S, K, T, r, sigma)
            error = abs(d) - target_delta
        else:
            d = bs_call_delta(S, K, T, r, sigma)
            error = d - target_delta
        if abs(error) < 0.005:
            break
        K -= error * S * 0.10
        K = max(min(K, ceil), floor)
    return max(K, floor)


# ────────────────────────────────────────────────────────────
# Wheel state machine
# ────────────────────────────────────────────────────────────

@dataclass
class WheelTrade:
    entry_ts: pd.Timestamp
    expiry_ts: pd.Timestamp
    leg: str                # "csp" or "cc"
    side: str               # "sell"
    strike: float
    spot_at_entry: float
    iv: float
    contracts: int
    premium_per_contract: float
    total_premium_received: float
    commission: float
    spot_at_expiry: float = 0.0
    outcome: str = ""        # "expired_otm" | "assigned" | "called_away"
    pnl: float = 0.0


@dataclass
class WheelResult:
    ticker: str
    initial_capital: float
    trades: List[WheelTrade] = field(default_factory=list)
    equity_curve: List[Tuple[pd.Timestamp, float]] = field(default_factory=list)
    assignments: int = 0
    callaways: int = 0
    expired_otm: int = 0
    total_premium: float = 0.0
    total_commissions: float = 0.0

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

        n_trades = len(self.trades)
        wins = sum(1 for t in self.trades if t.pnl > 0)
        win_rate = wins / n_trades if n_trades > 0 else 0.0

        return {
            "ticker": self.ticker,
            "period_days": n_days,
            "n_trades": n_trades,
            "expired_otm": self.expired_otm,
            "assignments": self.assignments,
            "callaways": self.callaways,
            "total_premium": round(self.total_premium, 2),
            "total_commissions": round(self.total_commissions, 2),
            "initial_capital": self.initial_capital,
            "final_value": round(eq["value"].iloc[-1], 2),
            "total_return_pct": round(total_return * 100, 2),
            "annual_return_pct": round(ann_return * 100, 2),
            "annual_volatility": round(ann_vol, 4),
            "sharpe_ratio": round(float(sharpe), 3),
            "max_drawdown_pct": round(float(abs(dd)) * 100, 2),
            "win_rate_pct": round(win_rate * 100, 1),
            "avg_premium_per_trade": round(self.total_premium / max(n_trades, 1), 2),
        }


# ────────────────────────────────────────────────────────────
# Backtest runner
# ────────────────────────────────────────────────────────────

def run_wheel_backtest(
    ticker: str,
    df: pd.DataFrame,           # daily OHLCV with timestamp/close columns
    *,
    initial_capital: float = 10_000,
    target_delta: float = 0.25,
    target_dte: int = 30,
    risk_free_rate: float = 0.04,
    vrp_multiplier: float = 1.15,
    rv_window: int = 30,
    commission_per_contract: float = 0.65,
    spread_cost_per_contract: float = 1.00,  # $0.01/share × 100 shares
) -> WheelResult:
    """Run the wheel backtest on a single ticker.

    df must have columns: timestamp (datetime), close (float), and ideally
    open/high/low. timestamp must be sorted ascending.
    """
    result = WheelResult(ticker=ticker, initial_capital=initial_capital)

    # Per-bar realized vol (annualized) from log returns
    log_ret = np.log(df["close"] / df["close"].shift(1))
    rv = log_ret.rolling(rv_window, min_periods=rv_window // 2).std() * np.sqrt(252)
    iv_series = (rv * vrp_multiplier).ffill().fillna(0.30).clip(lower=0.10, upper=2.5)

    cash = initial_capital
    shares = 0
    contracts_in_play = 1   # always start with 1 — adjust below if cash >> needed

    open_position: Optional[Dict[str, Any]] = None
    # State: "cash" (sell CSP) or "shares" (sell CC)
    state = "cash"

    for i, row in df.iterrows():
        ts = row["timestamp"]
        spot = float(row["close"])
        sigma = float(iv_series.iloc[i]) if i < len(iv_series) else 0.30

        # ── Mark-to-market equity at end of bar ──
        if state == "cash":
            mtm = cash
            if open_position is not None:
                # Held put — its value reduces cash collateral
                T = max((open_position["expiry_ts"] - ts).days / 365, 1e-6)
                cur_put = bs_put(spot, open_position["strike"], T,
                                 risk_free_rate, sigma) * 100 * open_position["contracts"]
                # CSP P&L = premium_received - current_value
                cur_pnl = open_position["premium_collected"] - cur_put
                mtm = cash + cur_pnl
        else:
            # Holding shares + sold call
            mtm = cash + shares * spot
            if open_position is not None:
                T = max((open_position["expiry_ts"] - ts).days / 365, 1e-6)
                cur_call = bs_call(spot, open_position["strike"], T,
                                   risk_free_rate, sigma) * 100 * open_position["contracts"]
                # Short call has negative value to seller
                cur_pnl = open_position["premium_collected"] - cur_call
                mtm = cash + shares * spot + cur_pnl

        result.equity_curve.append((ts, mtm))

        # ── Decision logic ──
        if open_position is None:
            # Open a new position
            T = target_dte / 365
            expiry_ts = ts + timedelta(days=target_dte)

            if state == "cash":
                # Sell cash-secured put
                # Size by available cash: contracts = floor(cash / (strike × 100))
                strike_guess = spot * 0.95   # rough
                max_contracts = max(1, int(cash // (strike_guess * 100)))
                contracts_in_play = max_contracts

                strike = find_strike_for_delta(spot, T, risk_free_rate, sigma,
                                               target_delta, is_put=True)
                # Round to nearest $0.50 for realism
                strike = round(strike * 2) / 2

                # Check we can actually afford the collateral
                collateral = strike * 100 * contracts_in_play
                while collateral > cash and contracts_in_play > 0:
                    contracts_in_play -= 1
                    collateral = strike * 100 * contracts_in_play
                if contracts_in_play == 0:
                    # Can't even afford 1 contract — skip
                    continue

                premium = bs_put(spot, strike, T, risk_free_rate, sigma)
                premium *= 0.99   # spread slippage on the way in (sell at mid - 1 tick)
                premium_received = premium * 100 * contracts_in_play
                commission = (commission_per_contract + spread_cost_per_contract) * contracts_in_play

                cash += premium_received - commission
                result.total_premium += premium_received
                result.total_commissions += commission

                open_position = {
                    "leg": "csp",
                    "strike": strike,
                    "expiry_ts": expiry_ts,
                    "contracts": contracts_in_play,
                    "premium_collected": premium_received,
                    "iv_at_entry": sigma,
                    "spot_at_entry": spot,
                    "entry_ts": ts,
                    "commission": commission,
                }

            else:  # state == "shares"
                # Sell covered call
                contracts_in_play = shares // 100
                if contracts_in_play == 0:
                    continue
                strike = find_strike_for_delta(spot, T, risk_free_rate, sigma,
                                               target_delta, is_put=False)
                strike = round(strike * 2) / 2

                premium = bs_call(spot, strike, T, risk_free_rate, sigma)
                premium *= 0.99
                premium_received = premium * 100 * contracts_in_play
                commission = (commission_per_contract + spread_cost_per_contract) * contracts_in_play

                cash += premium_received - commission
                result.total_premium += premium_received
                result.total_commissions += commission

                open_position = {
                    "leg": "cc",
                    "strike": strike,
                    "expiry_ts": expiry_ts,
                    "contracts": contracts_in_play,
                    "premium_collected": premium_received,
                    "iv_at_entry": sigma,
                    "spot_at_entry": spot,
                    "entry_ts": ts,
                    "commission": commission,
                }

        else:
            # Position open — check expiry
            if ts >= open_position["expiry_ts"]:
                strike = open_position["strike"]
                contracts = open_position["contracts"]
                spot_at_exp = spot
                trade = WheelTrade(
                    entry_ts=open_position["entry_ts"],
                    expiry_ts=open_position["expiry_ts"],
                    leg=open_position["leg"],
                    side="sell",
                    strike=strike,
                    spot_at_entry=open_position["spot_at_entry"],
                    iv=open_position["iv_at_entry"],
                    contracts=contracts,
                    premium_per_contract=open_position["premium_collected"] / (100 * contracts),
                    total_premium_received=open_position["premium_collected"],
                    commission=open_position["commission"],
                    spot_at_expiry=spot_at_exp,
                )

                if open_position["leg"] == "csp":
                    if spot_at_exp <= strike:
                        # Assigned: we buy 100*contracts at strike (cash → shares)
                        cost = strike * 100 * contracts
                        cash -= cost
                        shares += 100 * contracts
                        state = "shares"
                        trade.outcome = "assigned"
                        trade.pnl = (
                            open_position["premium_collected"]
                            - open_position["commission"]
                            + (spot_at_exp - strike) * 100 * contracts  # mark-to-market on shares
                        )
                        result.assignments += 1
                    else:
                        # Expired OTM: keep premium, no shares
                        trade.outcome = "expired_otm"
                        trade.pnl = open_position["premium_collected"] - open_position["commission"]
                        result.expired_otm += 1
                        # Stay in cash state
                else:  # cc
                    if spot_at_exp >= strike:
                        # Called away: shares → cash at strike
                        proceeds = strike * 100 * contracts
                        cash += proceeds
                        shares -= 100 * contracts
                        state = "cash"
                        trade.outcome = "called_away"
                        trade.pnl = (
                            open_position["premium_collected"]
                            - open_position["commission"]
                            + (strike - open_position.get("share_basis", open_position["spot_at_entry"])) * 100 * contracts
                        )
                        result.callaways += 1
                    else:
                        # Expired OTM: keep premium + shares, sell another CC
                        trade.outcome = "expired_otm"
                        trade.pnl = open_position["premium_collected"] - open_position["commission"]
                        result.expired_otm += 1

                result.trades.append(trade)
                open_position = None

    # Force-close any final open position at last close
    if open_position is not None:
        last = df.iloc[-1]
        spot_at_exp = float(last["close"])
        # Treat as expired at current spot for clean accounting
        # (more conservative than letting it roll)
        if open_position["leg"] == "csp":
            if spot_at_exp <= open_position["strike"]:
                cost = open_position["strike"] * 100 * open_position["contracts"]
                cash -= cost
                shares += 100 * open_position["contracts"]
                state = "shares"

    # Final equity = cash + shares × last close
    if state == "shares" and shares > 0:
        last = df.iloc[-1]
        final_value = cash + shares * float(last["close"])
        result.equity_curve[-1] = (last["timestamp"], final_value)

    return result
