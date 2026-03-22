"""Performance metrics: Sharpe, Sortino, Calmar, MaxDD, win rate, etc."""

from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd


def compute_metrics(
    equity_df: pd.DataFrame,
    risk_free_rate: float = 0.05,
    periods_per_year: int = 252,
) -> Dict[str, Any]:
    """Compute comprehensive backtest performance metrics.

    Args:
        equity_df: DataFrame with 'total_value' column (and optionally 'returns')
        risk_free_rate: Annual risk-free rate
        periods_per_year: Trading periods per year (252 for daily, 365*24 for hourly crypto)
    """
    if equity_df.empty or "total_value" not in equity_df.columns:
        return {}

    equity = equity_df["total_value"]

    # Returns
    if "returns" in equity_df.columns:
        returns = equity_df["returns"]
    else:
        returns = equity.pct_change().dropna()

    if len(returns) < 2:
        return {"total_return": 0, "n_periods": len(equity)}

    # Basic
    total_return = (equity.iloc[-1] / equity.iloc[0]) - 1
    annual_return = (1 + total_return) ** (periods_per_year / len(returns)) - 1

    # Volatility
    daily_vol = returns.std()
    annual_vol = daily_vol * np.sqrt(periods_per_year)

    # Sharpe ratio
    daily_rf = risk_free_rate / periods_per_year
    excess_returns = returns - daily_rf
    sharpe = (excess_returns.mean() / returns.std() * np.sqrt(periods_per_year)) if returns.std() > 0 else 0

    # Sortino ratio
    downside_returns = returns[returns < 0]
    downside_vol = downside_returns.std() * np.sqrt(periods_per_year) if len(downside_returns) > 0 else 0
    sortino = (annual_return - risk_free_rate) / downside_vol if downside_vol > 0 else 0

    # Drawdown
    cumulative = (1 + returns).cumprod()
    peak = cumulative.cummax()
    drawdown = (cumulative - peak) / peak
    max_drawdown = abs(drawdown.min())

    # Calmar ratio
    calmar = annual_return / max_drawdown if max_drawdown > 0 else 0

    # Win rate (if trades available)
    positive_returns = (returns > 0).sum()
    total_periods = len(returns[returns != 0])
    win_rate = positive_returns / total_periods if total_periods > 0 else 0

    # Profit factor
    gross_profit = returns[returns > 0].sum()
    gross_loss = abs(returns[returns < 0].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Average win/loss
    avg_win = returns[returns > 0].mean() if len(returns[returns > 0]) > 0 else 0
    avg_loss = returns[returns < 0].mean() if len(returns[returns < 0]) > 0 else 0
    win_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    return {
        "total_return": float(total_return),
        "total_return_pct": float(total_return * 100),
        "annual_return": float(annual_return),
        "annual_return_pct": float(annual_return * 100),
        "annual_volatility": float(annual_vol),
        "sharpe_ratio": float(sharpe),
        "sortino_ratio": float(sortino),
        "calmar_ratio": float(calmar),
        "max_drawdown": float(max_drawdown),
        "max_drawdown_pct": float(max_drawdown * 100),
        "win_rate": float(win_rate),
        "profit_factor": float(profit_factor),
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "win_loss_ratio": float(win_loss_ratio),
        "n_periods": len(returns),
        "start_value": float(equity.iloc[0]),
        "end_value": float(equity.iloc[-1]),
    }
