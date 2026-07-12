# Nautilus 交叉验证通用 Harness — 使用手册

*晋升新规(2026-07-12):候选 → validated 必须附 ①验证套件报告 ②本 harness 的
交叉确认(或记录在案的豁免,如期权无历史链数据)。*

## 为什么

同引擎的 IS/OOS 纪律抓不住会计缺陷(sniper 损失截断事故)。独立引擎用不同的
撮合/保证金/费用实现跑同一信号,数字对不上就是有问题。

## 复用步骤(以 eth_sniper 移植为模板)

1. **环境**:`.venv-nautilus`(钉 1.230.0)。跑任何脚本用
   `E:\Duke\QuantTrade\QuantSystem\.venv-nautilus\Scripts\python.exe`。
2. **数据**:从统一 catalog 导出 bars csv
   (`data.catalog_api.export_bars_csv`),或直接用 `load_data.py` 的
   `BarDataWrangler` 模式。BarType 字符串形如
   `ETHUSDT-PERP.SIM-4-HOUR-LAST-EXTERNAL`。
3. **策略移植**(拷贝 `eth_sniper_nautilus.py` 改六处):
   - 指标:逐 bar 增量实现,和 pandas `ewm(adjust=False)` 逐位核对
     (用 `check_indicators.py` 的 parity 模式,目标 ~1e-12)
   - 入场评分 / 出场规则 / 仓位公式照抄参考引擎
   - 止损:resting STOP_MARKET 逐 bar modify(Nautilus L1 以触发价成交,
     与参考引擎"止损价出场"语义一致 — 已在 backtest/engine.pyx 验证)
4. **引擎**(拷贝 `run_backtest.py`):SIM venue,`account_type=MARGIN`、
   `default_leverage`、taker fee。**一个进程只能建一个 BacktestEngine**
   (Rust 日志层限制)— 多配置对比用 subprocess。
5. **对账**:先信号 parity(指标逐 bar),再统计对照(Sharpe/CAGR/DD/
   笔数/胜率,日频重采样 √365)。差异要能归因:成交语义(次bar开
   vs 收盘)、费用入账时点、数量取整。**不能归因的差异 = 缺陷线索,
   这正是 harness 的目的。**
6. **产出**:`{strategy}_cross_check.json` 写入 results/,并在研究记录
   json 加字段:`"cross_engine": {"engine": "nautilus-1.230.0",
   "confirmed": true, "report": "results/..."}`
   — 晋升检查读的就是这个字段。期权类无法交叉时,在记录的 `caveats`
   写明 "cross-check infeasible"+原因(真实链 paper 为仲裁),
   晋升检查按豁免放行。

## 已知坑(1.230.0 / Windows)

- 单进程单引擎;Cython 签名看 docstring 不看 IDE;pandas 3.x 弃用告警无害;
- Windows wheel 只有 64 位精度(对 crypto/股票价格域无实际影响)。
