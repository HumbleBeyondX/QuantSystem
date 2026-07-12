"""Iron-Condor backtester — 4-leg short-vol with capped risk.

Strategy mechanics
------------------
At each entry, simultaneously open 4 legs (all same expiration):
   1. SHORT put  at strike Kp_short (delta target ~0.16, ~1σ OTM)
   2. LONG  put  at strike Kp_long  = Kp_short - wing_width
   3. SHORT call at strike Kc_short (delta target ~0.16)
   4. LONG  call at strike Kc_long  = Kc_short + wing_width

Net credit received (paid up-front). Max loss = wing_width × 100 − credit.

Profit drivers
--------------
* Theta decay (every day no big move → premium decays in our favor)
* IV crush (especially after VIX spikes, IV drops fast)
* Underlying staying between short strikes (the "tent")

Exit rules
----------
* Close at 50% of max profit (industry standard, captures most theta
  while avoiding gamma risk in last 2 weeks)
* Stop-loss at -200% of credit (i.e. unrealized loss = 2× credit)
* Hold to expiry if neither hits

Costs
-----
* $0.65 commission × 4 legs × n contracts
* $0.01 spread cost × 4 legs × n contracts
* Position sizing: ensure max loss × n_contracts ≤ capital × max_risk_pct

Capital model
-------------
Defining-risk margin = (wing_width × 100 - credit) per contract.
Capital allocated = max_risk_pct × initial_capital (default 50%).
n_contracts = floor(capital_alloc / max_loss_per_contract).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Reuse the BS pricers from wheel_backtest (no duplication).
from research.wheel_backtest import (
    bs_call,
    bs_put,
    bs_call_delta,
    bs_put_delta,
    find_strike_for_delta,
)


@dataclass
class ICTrade:
    entry_ts: pd.Timestamp
    expiry_ts: pd.Timestamp
    spot_at_entry: float
    iv_at_entry: float
    short_put_strike: float
    long_put_strike: float
    short_call_strike: float
    long_call_strike: float
    wing_width: float
    contracts: int
    credit_per_contract: float       # net premium received per IC
    total_credit: float
    max_loss_per_contract: float     # wing_width × 100 − credit
    commission: float
    exit_ts: pd.Timestamp = None
    exit_reason: str = ""             # "profit_target" | "stop_loss" | "expired" | "forced_eod"
    exit_value: float = 0.0           # cost to buy back at exit (positive = pay to close)
    spot_at_exit: float = 0.0
    pnl: float = 0.0


@dataclass
class ICResult:
    ticker: str
    initial_capital: float
    trades: List[ICTrade] = field(default_factory=list)
    equity_curve: List[Tuple[pd.Timestamp, float]] = field(default_factory=list)
    profit_target_hits: int = 0
    stop_loss_hits: int = 0
    expirations: int = 0
    losing_legs: int = 0  # how many trades broke a wing
    total_credit: float = 0.0
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

        n = len(self.trades)
        wins = sum(1 for t in self.trades if t.pnl > 0)
        win_rate = wins / n if n > 0 else 0.0

        gross_win = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

        return {
            "ticker": self.ticker,
            "n_trades": n,
            "win_rate_pct": round(win_rate * 100, 1),
            "profit_target_hits": self.profit_target_hits,
            "stop_loss_hits": self.stop_loss_hits,
            "expirations": self.expirations,
            "wing_breaks": self.losing_legs,
            "total_credit": round(self.total_credit, 2),
            "total_commissions": round(self.total_commissions, 2),
            "initial_capital": self.initial_capital,
            "final_value": round(eq["value"].iloc[-1], 2),
            "total_return_pct": round(total_return * 100, 2),
            "annual_return_pct": round(ann_return * 100, 2),
            "annual_volatility": round(ann_vol, 4),
            "sharpe_ratio": round(float(sharpe), 3),
            "max_drawdown_pct": round(float(abs(dd)) * 100, 2),
            "profit_factor": round(float(pf), 2) if np.isfinite(pf) else 999.0,
            "avg_pnl_per_trade": round(sum(t.pnl for t in self.trades) / max(n, 1), 2),
        }


def _ic_value_now(
    spot: float, T: float, sigma: float, r: float,
    Kp_s: float, Kp_l: float, Kc_s: float, Kc_l: float,
) -> float:
    """Cost to BUY BACK the iron condor today (per contract, in dollars).

    Sign convention: this is what we'd PAY to close. Negative profit = positive cost.
      cost = (short_put_now - long_put_now) + (short_call_now - long_call_now)

    Multiply by 100 for per-contract dollar value.
    """
    sp = bs_put(spot, Kp_s, T, r, sigma)
    lp = bs_put(spot, Kp_l, T, r, sigma)
    sc = bs_call(spot, Kc_s, T, r, sigma)
    lc = bs_call(spot, Kc_l, T, r, sigma)
    # We're SHORT put_s + call_s, LONG put_l + call_l
    return (sp - lp + sc - lc) * 100


def run_iron_condor_backtest(
    ticker: str,
    df: pd.DataFrame,
    *,
    initial_capital: float = 10_000,
    short_delta: float = 0.16,
    wing_width_atr_mult: float = 1.0,   # wing distance = N × ATR (≈ 1σ at 30D)
    target_dte: int = 30,
    risk_free_rate: float = 0.04,
    vrp_multiplier: float = 1.15,
    rv_window: int = 30,
    profit_target_pct: float = 0.50,    # close when 50% of max profit captured
    stop_loss_mult: float = 2.0,        # close when loss = 2× credit
    max_risk_pct: float = 0.50,         # max % of capital risked at any time
    commission_per_leg: float = 0.65,
    spread_cost_per_leg: float = 1.00,
) -> ICResult:
    """Single-ticker iron condor backtest.

    Parameters mirror common retail conventions:
      * 16-delta short strikes (~1σ OTM, ~70% prob expire OTM)
      * Wings at ~1σ further out (so wing_width ≈ ATR ≈ short_distance_from_atm)
      * 30 DTE entry, 50% PT exit, 200% SL exit
      * Max risk 50% of capital per IC at a time

    df columns: timestamp, open, high, low, close, volume
    """
    result = ICResult(ticker=ticker, initial_capital=initial_capital)

    log_ret = np.log(df["close"] / df["close"].shift(1))
    rv = log_ret.rolling(rv_window, min_periods=rv_window // 2).std() * np.sqrt(252)
    iv_series = (rv * vrp_multiplier).ffill().fillna(0.30).clip(lower=0.10, upper=2.5)

    cash = initial_capital
    open_position: Optional[Dict[str, Any]] = None

    for i, row in df.iterrows():
        ts = row["timestamp"]
        spot = float(row["close"])
        sigma = float(iv_series.iloc[i]) if i < len(iv_series) else 0.30

        # ── Mark-to-market ──
        mtm = cash
        if open_position is not None:
            T = max((open_position["expiry_ts"] - ts).days / 365, 1e-6)
            cur_buyback = _ic_value_now(
                spot, T, sigma, risk_free_rate,
                open_position["short_put_strike"],
                open_position["long_put_strike"],
                open_position["short_call_strike"],
                open_position["long_call_strike"],
            ) * open_position["contracts"]
            # Unrealized P&L = credit - cost_to_buy_back
            unrealized = open_position["total_credit"] - cur_buyback
            mtm = cash + unrealized

        result.equity_curve.append((ts, mtm))

        # ── Manage open IC ──
        if open_position is not None:
            T = max((open_position["expiry_ts"] - ts).days / 365, 1e-6)
            cur_buyback = _ic_value_now(
                spot, T, sigma, risk_free_rate,
                open_position["short_put_strike"],
                open_position["long_put_strike"],
                open_position["short_call_strike"],
                open_position["long_call_strike"],
            ) * open_position["contracts"]
            unrealized = open_position["total_credit"] - cur_buyback

            credit = open_position["total_credit"]
            target_profit = credit * profit_target_pct
            stop_loss = -credit * stop_loss_mult

            exit_reason = None
            if ts >= open_position["expiry_ts"]:
                exit_reason = "expired"
            elif unrealized >= target_profit:
                exit_reason = "profit_target"
            elif unrealized <= stop_loss:
                exit_reason = "stop_loss"

            if exit_reason:
                # Close: pay buyback cost (and pay commissions for closing legs)
                close_commission = (commission_per_leg + spread_cost_per_leg) * 4 * open_position["contracts"]
                cash -= cur_buyback + close_commission
                result.total_commissions += close_commission

                # Was the underlying outside short strikes? (informational)
                broke_wing = (
                    spot < open_position["short_put_strike"]
                    or spot > open_position["short_call_strike"]
                )

                pnl = unrealized - close_commission
                trade = ICTrade(
                    entry_ts=open_position["entry_ts"],
                    expiry_ts=open_position["expiry_ts"],
                    spot_at_entry=open_position["spot_at_entry"],
                    iv_at_entry=open_position["iv_at_entry"],
                    short_put_strike=open_position["short_put_strike"],
                    long_put_strike=open_position["long_put_strike"],
                    short_call_strike=open_position["short_call_strike"],
                    long_call_strike=open_position["long_call_strike"],
                    wing_width=open_position["wing_width"],
                    contracts=open_position["contracts"],
                    credit_per_contract=credit / open_position["contracts"] / 100,
                    total_credit=credit,
                    max_loss_per_contract=open_position["max_loss_per_contract"],
                    commission=open_position["commission"] + close_commission,
                    exit_ts=ts,
                    exit_reason=exit_reason,
                    exit_value=cur_buyback,
                    spot_at_exit=spot,
                    pnl=pnl,
                )
                result.trades.append(trade)

                if exit_reason == "profit_target":
                    result.profit_target_hits += 1
                elif exit_reason == "stop_loss":
                    result.stop_loss_hits += 1
                else:
                    result.expirations += 1
                if broke_wing:
                    result.losing_legs += 1

                open_position = None

        # ── Open new IC if no position ──
        if open_position is None:
            T = target_dte / 365
            expiry_ts = ts + timedelta(days=target_dte)

            # Find delta-targeted short strikes
            Kp_short = find_strike_for_delta(spot, T, risk_free_rate, sigma,
                                             short_delta, is_put=True)
            Kc_short = find_strike_for_delta(spot, T, risk_free_rate, sigma,
                                             short_delta, is_put=False)

            # Wing width = ~1 ATR (proxied by 1σ move at 30 DTE)
            sigma_30d = sigma * np.sqrt(T)
            wing_width = max(spot * sigma_30d * wing_width_atr_mult, 0.50)
            # Round to nearest $0.50
            wing_width = max(round(wing_width * 2) / 2, 0.50)

            Kp_long = max(Kp_short - wing_width, 0.01)
            Kc_long = Kc_short + wing_width

            # Round all strikes to $0.50
            Kp_short = round(Kp_short * 2) / 2
            Kp_long = round(Kp_long * 2) / 2
            Kc_short = round(Kc_short * 2) / 2
            Kc_long = round(Kc_long * 2) / 2

            # Net credit per IC, per share
            credit_per_ic = (
                bs_put(spot, Kp_short, T, risk_free_rate, sigma)
                - bs_put(spot, Kp_long, T, risk_free_rate, sigma)
                + bs_call(spot, Kc_short, T, risk_free_rate, sigma)
                - bs_call(spot, Kc_long, T, risk_free_rate, sigma)
            )
            credit_per_ic *= 0.99  # spread on entry

            if credit_per_ic <= 0.05:
                # Not worth it — IV too low or strikes too tight
                continue

            credit_per_contract = credit_per_ic * 100
            max_loss_per_contract = wing_width * 100 - credit_per_contract
            if max_loss_per_contract <= 0:
                continue

            # Size by max_risk_pct of capital
            risk_budget = cash * max_risk_pct
            n_contracts = max(int(risk_budget // max_loss_per_contract), 1)

            # Affordability check (margin = max_loss × n_contracts)
            margin = max_loss_per_contract * n_contracts
            while margin > cash * 0.95 and n_contracts > 0:
                n_contracts -= 1
                margin = max_loss_per_contract * n_contracts
            if n_contracts == 0:
                continue

            total_credit = credit_per_contract * n_contracts
            commission = (commission_per_leg + spread_cost_per_leg) * 4 * n_contracts

            cash += total_credit - commission
            result.total_credit += total_credit
            result.total_commissions += commission

            open_position = {
                "entry_ts": ts,
                "expiry_ts": expiry_ts,
                "spot_at_entry": spot,
                "iv_at_entry": sigma,
                "short_put_strike": Kp_short,
                "long_put_strike": Kp_long,
                "short_call_strike": Kc_short,
                "long_call_strike": Kc_long,
                "wing_width": wing_width,
                "contracts": n_contracts,
                "total_credit": total_credit,
                "max_loss_per_contract": max_loss_per_contract,
                "commission": commission,
            }

    # Force-close final position
    if open_position is not None:
        last = df.iloc[-1]
        spot = float(last["close"])
        T = max((open_position["expiry_ts"] - last["timestamp"]).days / 365, 1e-6)
        sigma = float(iv_series.iloc[-1])
        cur_buyback = _ic_value_now(
            spot, T, sigma, 0.04,
            open_position["short_put_strike"],
            open_position["long_put_strike"],
            open_position["short_call_strike"],
            open_position["long_call_strike"],
        ) * open_position["contracts"]
        close_commission = (0.65 + 1.00) * 4 * open_position["contracts"]
        cash -= cur_buyback + close_commission
        unrealized = open_position["total_credit"] - cur_buyback
        result.expirations += 1  # treat as forced expiry
        result.trades.append(ICTrade(
            entry_ts=open_position["entry_ts"],
            expiry_ts=open_position["expiry_ts"],
            spot_at_entry=open_position["spot_at_entry"],
            iv_at_entry=open_position["iv_at_entry"],
            short_put_strike=open_position["short_put_strike"],
            long_put_strike=open_position["long_put_strike"],
            short_call_strike=open_position["short_call_strike"],
            long_call_strike=open_position["long_call_strike"],
            wing_width=open_position["wing_width"],
            contracts=open_position["contracts"],
            credit_per_contract=open_position["total_credit"] / open_position["contracts"] / 100,
            total_credit=open_position["total_credit"],
            max_loss_per_contract=open_position["max_loss_per_contract"],
            commission=open_position["commission"] + close_commission,
            exit_ts=last["timestamp"],
            exit_reason="forced_eod",
            exit_value=cur_buyback,
            spot_at_exit=spot,
            pnl=unrealized - close_commission,
        ))
        result.equity_curve[-1] = (last["timestamp"], cash)

    return result
