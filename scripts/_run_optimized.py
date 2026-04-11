"""Final CPAM backtest: full 6-year period + 2023-2026 bull period."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from scripts.backtest_perp_alpha import CryptoAlphaBacktest, fetch_macro_data, print_report
import json

df = pd.read_csv("data/sample/BTC_USDT_4h.csv")
df.columns = [c.lower() for c in df.columns]
df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_localize(None)

macro = fetch_macro_data("2019-06-01", "2027-01-01")
print(f"Macro: {len(macro)} days\n")

# ─── Run 1: Full period (2020-2026) ───
print("=" * 80)
print("  RUN 1: FULL PERIOD 2020-01 → 2026-03")
print("=" * 80)
bt1 = CryptoAlphaBacktest(
    initial_capital=10_000, leverage=10.0,
    entry_threshold=0.25, atr_stop_mult=2.5, atr_tp_mult=5.0,
    trailing_stop_atr=3.0, max_drawdown_pct=0.30,
)
r1 = bt1.run(df, macro)
print_report(r1)

# ─── Run 2: Bull market (2023-2026) ───
print("=" * 80)
print("  RUN 2: BULL PERIOD 2023-01 → 2026-03")
print("=" * 80)
df2 = df[df["timestamp"] >= "2023-01-01"].reset_index(drop=True)
bt2 = CryptoAlphaBacktest(
    initial_capital=10_000, leverage=10.0,
    entry_threshold=0.25, atr_stop_mult=2.5, atr_tp_mult=5.0,
    trailing_stop_atr=3.0, max_drawdown_pct=0.30,
)
r2 = bt2.run(df2, macro)
print_report(r2)

# ─── Run 3: TightEntry on full period (best from previous sweep) ───
print("=" * 80)
print("  RUN 3: TIGHT ENTRY (threshold=0.35) FULL PERIOD")
print("=" * 80)
bt3 = CryptoAlphaBacktest(
    initial_capital=10_000, leverage=10.0,
    entry_threshold=0.35, strong_entry=0.55,
    atr_stop_mult=2.5, atr_tp_mult=5.0,
    trailing_stop_atr=3.0, max_drawdown_pct=0.30,
)
r3 = bt3.run(df, macro)
print_report(r3)

# Save all
for name, result in [("full", r1), ("bull_2023", r2), ("tight_entry", r3)]:
    s = {
        "strategy": f"cpam_{name}",
        "metrics": result["metrics"],
        "trades": result["trades"],
        "n_equity_points": len(result["equity_curve"]),
    }
    path = f"results/cpam_{name}.json"
    with open(path, "w") as f:
        json.dump(s, f, indent=2, default=str)
    print(f"Saved: {path}")
