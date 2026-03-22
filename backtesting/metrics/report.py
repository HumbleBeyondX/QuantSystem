"""Backtest report generation."""

from __future__ import annotations

from typing import Any, Dict


def generate_text_report(results: Dict[str, Any]) -> str:
    """Generate a text-based backtest report."""
    metrics = results.get("metrics", {})

    lines = [
        "=" * 60,
        f"  Backtest Report: {results.get('strategy_id', 'N/A')}",
        "=" * 60,
        "",
        "--- Performance ---",
        f"  Total Return:      {metrics.get('total_return_pct', 0):>10.2f}%",
        f"  Annual Return:     {metrics.get('annual_return_pct', 0):>10.2f}%",
        f"  Sharpe Ratio:      {metrics.get('sharpe_ratio', 0):>10.2f}",
        f"  Sortino Ratio:     {metrics.get('sortino_ratio', 0):>10.2f}",
        f"  Calmar Ratio:      {metrics.get('calmar_ratio', 0):>10.2f}",
        "",
        "--- Risk ---",
        f"  Max Drawdown:      {metrics.get('max_drawdown_pct', 0):>10.2f}%",
        f"  Annual Volatility: {metrics.get('annual_volatility', 0):>10.4f}",
        "",
        "--- Trading ---",
        f"  Total Trades:      {results.get('n_trades', 0):>10d}",
        f"  Win Rate:          {metrics.get('win_rate', 0):>10.2%}",
        f"  Profit Factor:     {metrics.get('profit_factor', 0):>10.2f}",
        f"  Win/Loss Ratio:    {metrics.get('win_loss_ratio', 0):>10.2f}",
        "",
        "--- Capital ---",
        f"  Initial:           ${results.get('initial_cash', 0):>12,.2f}",
        f"  Final:             ${results.get('final_value', 0):>12,.2f}",
        f"  P&L:               ${results.get('final_value', 0) - results.get('initial_cash', 0):>12,.2f}",
        "",
        "=" * 60,
    ]
    return "\n".join(lines)
