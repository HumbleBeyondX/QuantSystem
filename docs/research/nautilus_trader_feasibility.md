# NautilusTrader 接入可行性评估

*2026-07-12 · verdict: **试点单策略(定位:回测引擎替换)** — 不做全迁移*

## 核心事实(带来源,均为 2026-07 实查)

| 维度 | 结论 |
|---|---|
| 版本/活跃度 | v1.230.0 Beta (2026-06-29),~月更,24.6k stars;**98% commits 来自单一维护者**(bus-factor 风险,但有公司背书) |
| 许可 | LGPL-3.0+ — 个人当库用,策略无需开源 |
| Python/Windows | 3.12–3.14 ✓(本机 3.13 OK);Windows 有 wheel 但**只有 64 位精度**(Linux 128 位),CI 非一等公民 |
| 内存 | 实况节点闲置 300-600MB;**live 数据路径有内存泄漏 open issue #3485(~7MB/min)** → **1GB 东京 VPS 跑不动 live node** |
| 架构 | Rust 核心 + Python 策略层;**回测/实盘同一份 Strategy 代码** — 正中我们 backtest-vs-live parity 的痛点 |
| 杠杆回测 | `account_type=MARGIN` + 逐 instrument leverage + fill/latency 模型 — 解决我们事件引擎不支持杠杆的缺口 |
| 数据接入 | DataFrame → BarDataWrangler / ParquetDataCatalog,~50 行 loader 搞定现有 parquet/CSV |
| 期权 | 2026 年已一等公民(CryptoOption、4 腿 spread、venue greeks、BS 计算器);**但 assignment/exercise/到期结算模拟没有文档保证** — 对 wheel 是核心机制 |
| Adapters | OKX 稳定(含期权,limit-only);**Deribit adapter 才 ~5 个月大**(2026-02 落地),按 beta 对待;Bybit/IB 稳定;**Alpaca 无 adapter**(仅 RFC #3374)→ gamma scalp 无法经它实盘 |
| API 稳定性 | 1.x beta 期间**每次发版都可能 breaking** — 必须钉版本 |

## 结论

- **全迁移否决**:Alpaca 缺失挡死 gamma scalp;1GB VPS 挡死 live node;beta API 月月破坏。
- **完全跳过也不对**:它免费解决我们两个真实架构短板 —— 回测里的杠杆保证金模拟、回测/实盘同代码。
- **正确姿势:试点 ETH perp momentum 的回测端**,与现有引擎逐笔对账,用 parity 证据决定它是否成为 canonical 回测引擎;现有 ccxt/urllib live runner、FastAPI dashboard、YAML registry 全部保留 — Nautilus 只是一个 pip 包,天然并行。

## 试点步骤(决定采纳前不动生产)

1. 独立 3.13 venv,`pip install nautilus_trader==1.230.0`(钉死)。
2. ~50 行 loader:`data/sample/*.parquet` → ParquetDataCatalog。
3. 移植 ETH momentum 为 Nautilus Strategy(~150-250 行 + runner),MARGIN 账户 + 50x leverage + taker fee + OneTickSlippage。
4. **对账闸门**:同区间与现有引擎逐笔比 fills/PnL;再上 OKX DEMO 环境跑一周,比 live fills vs 它自己的回测。数据说了算。
5. 缓办:wheel 迁移(先用丢弃式回测验证 assignment/到期行为)、任何 live node(VPS ≥2GB 之前免谈)、gamma scalp(等 Alpaca RFC #3374)。

替代品一句话:vectorbt 只适合做参数扫描前筛(无事件驱动/实盘/期权);backtrader 实质停更;zipline-reloaded 股票日历模型与 crypto/期权八字不合。四者中只有 Nautilus 同时有活跃维护 + 事件驱动 parity + OKX/Deribit crypto 期权 + 杠杆模拟。
