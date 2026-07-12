# 统一策略 / OMS / 风控 — 重构蓝图

*2026-07-12 · status: DESIGN(未动任何运行中代码)· 原则:绞杀者模式,旧脚本在对账通过前始终是权威*

## 0. 为什么这是隐患(现状诊断)

三条执行链,三套互不相认的一切:

| | ETH Sniper (live) | Gamma Scalp (paper) | BTC Wheel (paper+live-ready) |
|---|---|---|---|
| 策略逻辑 | strategies/crypto/eth_sniper_strategy.py | 1300 行独立脚本 | 独立脚本 |
| 下单 | ccxt_engine + OMS | 影子成交(脚本内) | 脚本内自带 DeribitLive |
| 风控 | runner 内熔断 | 脚本内熔断 | 脚本内 SLA gate + 熔断 |
| 状态 | runner 内存+VPS | 自管 pkl | 自管 json |
| 调度 | systemd | .bat 计划任务 | .bat 计划任务 |

具体隐患:
1. **风控没有组合视角** — gamma 和 wheel 同是卖 vol,尾部风险叠加,但没有任何组件看得到两者之和(vega/尾部预算无人拥有)。
2. **每上一个新策略 = 再抄一遍全套管线**(下单/重试/状态/熔断/日志),bug 各自修三遍(wheel 的 .bat 假成功、gamma 的 tz 例外都是这么来的)。
3. **执行逻辑不可测试** — 独立脚本的信号和执行揉在一起,无法对执行层单独做回归。
4. **框架本身有洞** — 现有 Strategy 基类是线性资产 bar 驱动的:不支持期权多腿、到期/行权生命周期、杠杆保证金(sniper 回测被迫独立脚本就是这个原因)。

## 1. 目标架构(五层)

```
┌─ Registry + Promotion(已有)──────────────────────────────┐
│  stage=paper/live 决定 Runner 加载谁;晋升闸门决定换档       │
└─────────────────────────┬──────────────────────────────────┘
┌─ Runner(一个调度进程)──▼──────────────────────────────────┐
│  按各策略 cadence 驱动(日频 bar / 4h bar / 期权链快照)      │
│  取代 N 个 .bat;心跳、watchdog 对接                          │
└─────────────────────────┬──────────────────────────────────┘
┌─ Strategy 契约 ──────────▼──────────────────────────────────┐
│  on_bar / on_chain / on_expiry / on_fill                     │
│  产出 OrderIntent(声明式),永不直接碰交易所                  │
└─────────────────────────┬──────────────────────────────────┘
┌─ Risk Pipeline(唯一入口)▼──────────────────────────────────┐
│  每个 intent 依次过:SLA preflight → 策略限额 → 组合限额       │
│  (delta/vega/尾部预算)→ 熔断器。risk-reducing 永不拦截       │
│  ——这条教义在这里写一次,不再散落各脚本                        │
└─────────────────────────┬──────────────────────────────────┘
┌─ OMS + Venue Adapters ───▼──────────────────────────────────┐
│  order_manager(幂等生命周期)+ fill_tracker + 事件日志        │
│  adapters: OKX(现有 ccxt_engine)/ Deribit(从 wheel 脚本     │
│  提升)/ AlpacaOptions(从 gamma 脚本提升)/ Paper(影子成交)  │
└──────────────────────────────────────────────────────────────┘
```

关键设计决定:
- **Instrument 模型统一**:spot/perp/option 一个类型体系(option 带 strike/expiry/multiplier/right),Position 支持多腿。这是框架现在最缺的地基。
- **策略输出 OrderIntent 而非订单**:意图("卖 1 张 BTC-25JUL26-58000-P @ bid")进 Risk Pipeline,过了才变成订单。回测和实盘喂同一个 Strategy,只换 OMS 后端 —— parity 由架构保证而非纪律保证。
- **状态 = 事件日志(append-only)+ 派生快照**:orders/fills/marks 全部落一条 jsonl 事件流(与修正账本同哲学),各策略状态从事件重放派生。pkl/json 各自为政的历史结束。
- **Paper 引擎是一等 venue adapter**:paper 和 live 唯一区别是路由到 PaperAdapter 还是 DeribitAdapter —— 这正是晋升系统需要的形态(promote = 换 adapter + 节流)。

## 2. 迁移路线(绞杀者,六阶段)

**每阶段结束旧链依然完整运行;切换前提永远是对账证据。**

- **P0 冻结与固定(0.5 天)**:三条链标记 legacy;为 gamma/wheel 脚本写"金样本"回归测试(给定昨日状态+行情 → 断言今日动作/日志行完全一致),作为后续对账基准。
- **P1 提升 venue adapters(2-3 天,纯新增)**:
  - `execution/engine/deribit_engine.py` ← 从 wheel 脚本的 DeribitLive 提炼(脚本保留自己的副本,不改)
  - `execution/engine/alpaca_options_engine.py` ← 从 gamma 脚本的 OPRA 链/报价代码提炼
  - 各配 testnet/离线单测
- **P2 Risk Pipeline(2-3 天,纯新增)**:`core/risk/pipeline.py` 把已有件组装成一条链(SLA preflight、熔断、限额),新增**组合层 greeks/尾部预算**(数据源:各策略持仓事件)。先只服务新路径;Portfolio Risk 页接它,立刻获得跨策略视角——这一步不用等任何迁移就有独立价值。
- **P3 首个策略移植 = BTC Wheel(1 周,多为影子等待)**:
  - 新写 `strategies/options/btc_wheel_strategy.py`(逻辑照抄脚本,参数 0.92/1.08 原样)走新五层
  - **影子并跑 ≥2 周**:新旧同时运行,逐日断言二者决策/标记完全一致(工具复用 P0 金样本)
  - 时机纪律:**8 周 paper 窗口内不切换**(会污染晋升证据)。影子期正好落在窗口内,~9 月初结算后的平仓点切换
- **P4 Gamma Scalp 移植(1-2 周)**:同法。体量最大(对冲逻辑、OPRA 链、DQC),拆成 on_chain 信号 + 对冲 intent 两块
- **P5 ETH Sniper 对齐(2-3 天 + VPS 窗口)**:它已在框架上,只需把下单路径改走 Risk Pipeline + intent。选平仓窗口升级,留回滚
- **P6 收尾**:.bat 们退役 → 单一 Runner 服务;`strategies/` 里十几个脚手架实验移入 `strategies/experiments/` 隔离(不删,但明确非生产)

## 3. 与 NautilusTrader 的关系(买 vs 建)

这套蓝图 = 自建一个 mini-Nautilus。刻意把 Strategy 契约(事件钩子 + intent)和 adapter 层设计成**与 Nautilus 同构**:若日后试点证明 Nautilus 回测可信,P3-P5 的移植目标可以直接换成 Nautilus Strategy,自建层只剩 Risk Pipeline 和治理(那两块本来就该自有)。搁置的试点不影响本蓝图先行。

## 4. 风险与红线

- 红线①:8 周 wheel paper 窗口内,旧脚本是唯一权威,新实现只影子
- 红线②:任何切换点必须是 flat(结算后/无持仓),且留一键回退(.bat 重新启用即回滚)
- 红线③:每次切换前,影子对账报告(逐日决策 diff = 0)入 promotions.jsonl 同级审计
- 最大风险:P4 gamma 的对冲逻辑重写引入行为漂移 → 靠 P0 金样本 + 长影子期压制
