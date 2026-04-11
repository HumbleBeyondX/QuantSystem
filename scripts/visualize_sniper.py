#!/usr/bin/env python3
"""Visualize BTC Sniper v5 backtest results.

Generates a comprehensive dashboard with:
  1. Equity curve vs BTC buy-and-hold
  2. Drawdown chart
  3. Monthly returns heatmap
  4. Trade distribution (PnL histogram, win/loss by type)
  5. Yearly performance bars
  6. Entry/exit scatter on price chart

Usage:
    python scripts/visualize_sniper.py
    python scripts/visualize_sniper.py --results results/btc_sniper_v5_aggressive.json
"""

from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import matplotlib
    matplotlib.use("Agg")  # Non-interactive backend
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import matplotlib.gridspec as gridspec
    from matplotlib.colors import TwoSlopeNorm
except ImportError:
    print("ERROR: matplotlib required. Install with: pip install matplotlib")
    sys.exit(1)


def load_results(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def plot_dashboard(data: dict, save_path: str = "results/sniper_dashboard.png"):
    """Generate full backtest dashboard."""
    metrics = data.get("metrics", {})
    eq_data = data.get("equity_curve", [])
    trades = data.get("trades", [])
    config = data.get("config", {})

    if not eq_data:
        print("No equity curve data.")
        return

    # ── Prepare DataFrames ──
    eq = pd.DataFrame(eq_data)
    eq["ts"] = pd.to_datetime(eq["ts"])
    eq.set_index("ts", inplace=True)

    # BTC price for comparison
    btc_csv = PROJECT_ROOT / "data" / "sample" / "BTC_USDT_4h.csv"
    if btc_csv.exists():
        btc = pd.read_csv(btc_csv)
        btc.columns = [c.lower() for c in btc.columns]
        btc["timestamp"] = pd.to_datetime(btc["timestamp"], utc=True).dt.tz_localize(None)
        btc.set_index("timestamp", inplace=True)
        btc_close = btc["close"]
    else:
        btc_close = None

    closed = [t for t in trades if t.get("action", "").startswith("CLOSE")]
    opens = [t for t in trades if t.get("action", "") in ("LONG", "SHORT")]

    # ── Create figure ──
    fig = plt.figure(figsize=(22, 28), facecolor="white")
    gs = gridspec.GridSpec(6, 2, hspace=0.35, wspace=0.25,
                           height_ratios=[2.5, 1.5, 2, 2, 1.5, 2])

    # ════════ 1. EQUITY CURVE ════════
    ax1 = fig.add_subplot(gs[0, :])
    ax1.plot(eq.index, eq["equity"], color="#2196F3", linewidth=1.5, label="Strategy Equity")
    ax1.fill_between(eq.index, eq["equity"], eq["equity"].iloc[0],
                     alpha=0.1, color="#2196F3")

    if btc_close is not None:
        # Normalize BTC to same starting value
        btc_norm = btc_close / btc_close.iloc[0] * eq["equity"].iloc[0]
        ax1.plot(btc_norm.index, btc_norm.values, color="#FF9800", linewidth=1,
                 alpha=0.7, label="BTC Buy & Hold")

    ax1.set_title(
        f"BTC Sniper v5 — Equity Curve\n"
        f"Total: {metrics.get('total_return_pct', 0):+,.1f}% | "
        f"Annual: {metrics.get('annual_return_pct', 0):+,.1f}% | "
        f"Sharpe: {metrics.get('sharpe', 0):.2f} | "
        f"Max DD: {metrics.get('max_dd_pct', 0):.1f}% | "
        f"Leverage: {config.get('leverage', 'N/A')}x",
        fontsize=14, fontweight="bold", pad=15
    )
    ax1.set_ylabel("Equity ($)", fontsize=11)
    ax1.legend(loc="upper left", fontsize=10)
    ax1.grid(True, alpha=0.3)
    ax1.set_yscale("log")
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=6))

    # ════════ 2. DRAWDOWN ════════
    ax2 = fig.add_subplot(gs[1, :], sharex=ax1)
    cum = eq["equity"] / eq["equity"].cummax()
    dd = (cum - 1) * 100
    ax2.fill_between(eq.index, dd, 0, color="#F44336", alpha=0.4)
    ax2.plot(eq.index, dd, color="#F44336", linewidth=0.8)
    ax2.set_ylabel("Drawdown (%)", fontsize=11)
    ax2.set_title("Drawdown", fontsize=12, fontweight="bold")
    ax2.grid(True, alpha=0.3)
    ax2.axhline(y=-metrics.get("max_dd_pct", 0), color="#B71C1C", linestyle="--",
                alpha=0.5, label=f"Max DD: {metrics.get('max_dd_pct', 0):.1f}%")
    ax2.legend(fontsize=9)

    # ════════ 3. MONTHLY RETURNS HEATMAP ════════
    ax3 = fig.add_subplot(gs[2, :])
    try:
        monthly = eq["equity"].resample("ME").last().pct_change() * 100
        monthly = monthly.dropna()
        m_df = pd.DataFrame({
            "year": monthly.index.year,
            "month": monthly.index.month,
            "ret": monthly.values,
        })
        pivot = m_df.pivot_table(index="year", columns="month", values="ret", aggfunc="first")
        pivot.columns = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][:len(pivot.columns)]

        # Add annual total
        yearly = metrics.get("yearly", {})
        if yearly:
            pivot["Annual"] = [yearly.get(int(yr), 0) for yr in pivot.index]

        vmax = max(abs(pivot.values[~np.isnan(pivot.values)].min()),
                   abs(pivot.values[~np.isnan(pivot.values)].max()))
        norm = TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)

        im = ax3.imshow(pivot.values, cmap="RdYlGn", norm=norm, aspect="auto")
        ax3.set_xticks(range(len(pivot.columns)))
        ax3.set_xticklabels(pivot.columns, fontsize=9)
        ax3.set_yticks(range(len(pivot.index)))
        ax3.set_yticklabels(pivot.index, fontsize=10)
        ax3.set_title("Monthly Returns Heatmap (%)", fontsize=12, fontweight="bold")

        for i in range(len(pivot.index)):
            for j in range(len(pivot.columns)):
                val = pivot.values[i, j]
                if not np.isnan(val):
                    color = "white" if abs(val) > vmax * 0.6 else "black"
                    ax3.text(j, i, f"{val:.1f}", ha="center", va="center",
                             fontsize=8, color=color, fontweight="bold")

        fig.colorbar(im, ax=ax3, shrink=0.6, label="Return %")
    except Exception as e:
        ax3.text(0.5, 0.5, f"Heatmap error: {e}", transform=ax3.transAxes,
                 ha="center", fontsize=10)

    # ════════ 4. TRADE PNL DISTRIBUTION ════════
    ax4 = fig.add_subplot(gs[3, 0])
    if closed:
        pnls = [t.get("pnl", 0) for t in closed]
        colors = ["#4CAF50" if p > 0 else "#F44336" for p in pnls]
        ax4.bar(range(len(pnls)), pnls, color=colors, width=1.0, edgecolor="none")
        ax4.set_title(f"Trade PnL Sequence ({len(closed)} trades)", fontsize=12, fontweight="bold")
        ax4.set_xlabel("Trade #")
        ax4.set_ylabel("PnL ($)")
        ax4.axhline(y=0, color="black", linewidth=0.5)
        ax4.grid(True, alpha=0.3, axis="y")

    # ════════ 5. PNL HISTOGRAM ════════
    ax5 = fig.add_subplot(gs[3, 1])
    if closed:
        eq_rets = [t.get("pnl_equity_pct", t.get("pnl_margin_pct", 0)) for t in closed]
        ax5.hist(eq_rets, bins=40, color="#2196F3", edgecolor="white", alpha=0.8)
        ax5.axvline(x=np.mean(eq_rets), color="#FF5722", linewidth=2,
                    label=f"Mean: {np.mean(eq_rets):+.2f}%")
        ax5.axvline(x=0, color="black", linewidth=0.8)
        ax5.set_title("PnL Distribution (% of equity)", fontsize=12, fontweight="bold")
        ax5.set_xlabel("Return per Trade (%)")
        ax5.set_ylabel("Frequency")
        ax5.legend(fontsize=9)
        ax5.grid(True, alpha=0.3)

    # ════════ 6. YEARLY PERFORMANCE ════════
    ax6 = fig.add_subplot(gs[4, 0])
    yearly = metrics.get("yearly", {})
    if yearly:
        years = sorted(yearly.keys())
        rets = [yearly[y] for y in years]
        colors = ["#4CAF50" if r > 0 else "#F44336" for r in rets]
        bars = ax6.bar([str(y) for y in years], rets, color=colors, edgecolor="white", width=0.7)
        for bar_obj, ret in zip(bars, rets):
            ax6.text(bar_obj.get_x() + bar_obj.get_width() / 2, ret + (2 if ret > 0 else -5),
                     f"{ret:+.0f}%", ha="center", fontsize=9, fontweight="bold")
        ax6.set_title("Yearly Returns (%)", fontsize=12, fontweight="bold")
        ax6.axhline(y=0, color="black", linewidth=0.5)
        ax6.set_ylabel("Return %")
        ax6.grid(True, alpha=0.3, axis="y")

    # ════════ 7. EXIT REASONS PIE ════════
    ax7 = fig.add_subplot(gs[4, 1])
    exits = metrics.get("exits", {})
    if exits:
        labels = list(exits.keys())
        sizes = list(exits.values())
        colors_pie = ["#4CAF50", "#F44336", "#FF9800", "#2196F3", "#9C27B0", "#607D8B"]
        explode = [0.05] * len(labels)
        ax7.pie(sizes, labels=labels, colors=colors_pie[:len(labels)], autopct="%1.0f%%",
                explode=explode[:len(labels)], textprops={"fontsize": 9})
        ax7.set_title("Exit Reasons", fontsize=12, fontweight="bold")

    # ════════ 8. PRICE CHART WITH TRADES ════════
    ax8 = fig.add_subplot(gs[5, :])
    if btc_close is not None:
        ax8.plot(btc_close.index, btc_close.values, color="#607D8B", linewidth=0.6, alpha=0.8)

        # Plot entry/exit points
        for t in opens:
            ts = pd.to_datetime(t["ts"])
            p = t["price"]
            if t["action"] == "LONG":
                ax8.scatter(ts, p, color="#4CAF50", marker="^", s=20, zorder=5, alpha=0.7)
            else:
                ax8.scatter(ts, p, color="#F44336", marker="v", s=20, zorder=5, alpha=0.7)

        for t in closed:
            ts = pd.to_datetime(t["ts"])
            p = t["price"]
            pnl = t.get("pnl", 0)
            color = "#4CAF50" if pnl > 0 else "#F44336"
            ax8.scatter(ts, p, color=color, marker="x", s=15, zorder=5, alpha=0.5)

        ax8.set_title("BTC Price with Trade Entries (triangles) & Exits (x marks)",
                       fontsize=12, fontweight="bold")
        ax8.set_ylabel("BTC Price ($)")
        ax8.set_yscale("log")
        ax8.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
        ax8.grid(True, alpha=0.3)
        ax8.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax8.xaxis.set_major_locator(mdates.MonthLocator(interval=6))

    # ── Stats box ──
    stats_text = (
        f"Strategy: BTC Sniper v5 | Leverage: {config.get('leverage', 'N/A')}x | "
        f"Risk/Trade: {config.get('risk_per_trade', 0)*100:.0f}% | "
        f"Trades: {metrics.get('trades', 0)} | "
        f"Win Rate: {metrics.get('win_rate', 0)*100:.1f}% | "
        f"PF: {metrics.get('profit_factor', 0):.2f} | "
        f"Monthly Hit: {metrics.get('monthly_hit', 0)*100:.0f}%"
    )
    fig.text(0.5, 0.01, stats_text, ha="center", fontsize=10,
             style="italic", color="#555555")

    plt.savefig(save_path, dpi=150, bbox_inches="tight", facecolor="white")
    print(f"\nDashboard saved: {save_path}")
    plt.close()


def main():
    parser = argparse.ArgumentParser(description="Visualize BTC Sniper backtest")
    parser.add_argument("--results", type=str,
                        default="results/btc_sniper_v5_aggressive.json")
    parser.add_argument("--output", type=str,
                        default="results/sniper_dashboard.png")
    args = parser.parse_args()

    results_path = PROJECT_ROOT / args.results
    if not results_path.exists():
        print(f"ERROR: Results file not found: {results_path}")
        print("Run the backtest first: python scripts/backtest_btc_sniper.py --sweep")
        sys.exit(1)

    print(f"Loading: {results_path}")
    data = load_results(str(results_path))
    print(f"Metrics: {json.dumps(data.get('metrics', {}), indent=2, default=str)[:500]}...")

    output_path = str(PROJECT_ROOT / args.output)
    plot_dashboard(data, save_path=output_path)

    # Also generate a simple equity-only chart for quick viewing
    eq = pd.DataFrame(data["equity_curve"])
    eq["ts"] = pd.to_datetime(eq["ts"])

    fig2, ax = plt.subplots(figsize=(16, 6), facecolor="white")
    ax.plot(eq["ts"], eq["equity"], color="#2196F3", linewidth=1.5)
    ax.fill_between(eq["ts"], eq["equity"], eq["equity"].iloc[0], alpha=0.1, color="#2196F3")
    ax.set_yscale("log")
    ax.set_title(
        f"BTC Sniper v5 — ${eq['equity'].iloc[0]:,.0f} → ${eq['equity'].iloc[-1]:,.0f} "
        f"({data['metrics'].get('total_return_pct', 0):+,.1f}%)",
        fontsize=13, fontweight="bold"
    )
    ax.set_ylabel("Equity ($)")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax.grid(True, alpha=0.3)
    simple_path = output_path.replace(".png", "_equity.png")
    plt.savefig(simple_path, dpi=150, bbox_inches="tight", facecolor="white")
    print(f"Equity chart saved: {simple_path}")
    plt.close()


if __name__ == "__main__":
    main()
