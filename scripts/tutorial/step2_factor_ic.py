#!/usr/bin/env python3
"""教学 Step 2 — 亲手给一个因子算 IC。

跑法:  python scripts/tutorial/step2_factor_ic.py

流程:catalog 读 SP500 面板 → DataPanel(长转宽) → 算 20 日动量因子(宽)
     → evaluate_factor(每日截面 Spearman IC + ICIR + 分层收益)

练习:把 LOOKBACK 改成 120 重跑,对比 IC —— 然后想想为什么。
"""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from research.factor_mining.data_panel import DataPanel  # noqa: E402
from research.factor_mining.evaluator import evaluate_factor  # noqa: E402

LOOKBACK = 120        # ← 练习:改成 120 重跑
SKIP_DAYS = 1        # 跳过最近 1 天(避开短期反转污染)

import time as _t
_t0 = _t.time()

def say(msg: str) -> None:
    print(f"[{_t.time()-_t0:5.1f}s] {msg}", flush=True)

# 1. 数据:从统一 catalog 读(和 Data Layer 页 Catalog tab 同一份字节)
say("读取 SP500 面板 (43万行 parquet)...")
panel = DataPanel.from_parquet(
    ROOT / "data/catalog/panel/sp500/1d.parquet", validate=False)
say(f"长转宽 pivot... {panel}")

# 2. 因子(宽格式计算):过去 LOOKBACK 日动量,跳过最近 SKIP_DAYS 日
closes = panel.closes_wide
factor = closes.shift(SKIP_DAYS) / closes.shift(SKIP_DAYS + LOOKBACK) - 1.0
say(f"因子矩阵就绪 {factor.shape}")

# 3. 评估:每日截面 Spearman IC(rebalance_lag=1:t 日信号 t+1 日才可交易)
say("评估中: 3 个期限 × (IC + rankIC) × 2849 截面 + 五分层回测 ...")
m = evaluate_factor(factor, panel,
                    factor_name=f"momentum_{LOOKBACK}d_skip{SKIP_DAYS}",
                    universe="sp500_current_members(有幸存者偏差!)",
                    horizons=[1, 5, 20], rebalance_lag=1)
say("完成")

print("\n" + "=" * 66)
print(f"因子: {m.factor_name}   区间: {m.period_start[:10]} -> {m.period_end[:10]}")
print(f"覆盖率: {m.coverage_pct:.1f}%   有效截面数: {m.n_valid_dates}")
print("=" * 66)
print(f"  Rank IC (mean) = {m.rank_ic_mean:+.4f}   std = {m.rank_ic_std:.4f}")
print(f"  ICIR           = {m.rank_ic_ir:+.3f}    t-stat = {m.rank_ic_t_stat:+.2f}")
print(f"  因子自相关(1d) = {m.autocorr_1d:+.3f}  (越高换手越低)")
print("-" * 66)
print(f"  IC 衰减 (rank): " + "  ".join(
    f"{h}d={v:+.4f}" for h, v in sorted(m.decay_rank.items())))
print("-" * 66)
print(f"  最佳期限 {m.best_horizon}d 的五分层年化收益 (Q1差 -> Q5好):")
print(f"    Q1={m.q1_ret_ann:+.1%}  Q2={m.q2_ret_ann:+.1%}  Q3={m.q3_ret_ann:+.1%}"
      f"  Q4={m.q4_ret_ann:+.1%}  Q5={m.q5_ret_ann:+.1%}")
print(f"  多空组合: 年化 {m.ls_return_ann:+.1%}  Sharpe {m.ls_sharpe:+.2f}"
      f"  MaxDD {m.ls_max_dd:+.1%}  年换手 {m.ls_turnover_ann:.0f}x")
print("=" * 66)
print("判读标尺: |IC|>0.02 有信息, >0.05 强; ICIR>0.3 可用, >0.5 优秀")
print("注意: 分层收益未扣费, 且此宇宙有幸存者偏差 —— 数字方向偏乐观")
