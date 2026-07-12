# $2M 实盘规模化规划 — 数据与服务器

*2026-07-12 · 原则:架构 host 无关,规模化 = 搬进程不重写。先搭骨架跑通交易,按阶段付费升级。*

## 数据存储(三温分层)

| 层 | 内容 | 现在($0) | $2M 阶段(~$30-60/月) |
|---|---|---|---|
| 热 | 策略日常读写:catalog、live_state、jsonl 事件流 | 本地盘(data/catalog 统一脊柱,manifest 哈希) | 同结构上 NVMe;执行节点各持所需子集 |
| 温 | 历史行情、回测输入、EDGAR/新闻/altdata 归档 | 本地 + git LFS 不用 | **Cloudflare R2 版本化桶**(无出口费,1TB≈$15/月);本地为缓存、云为真相;每晚 rclone 同步 + manifest 校验 |
| 审计 | promotions/corrections/trades 事件流 | jsonl 追加(已有) | 同步入 R2 不可变前缀;月度打包 |

数据库策略:SQLite/jsonl 够用到多写入者出现;那时上托管 Postgres(~$20/月)。
**tick 级数据进来之前不建 ClickHouse** — 不为想象的规模付复杂度。

## 服务器(三阶段)

**Phase 0 — 现在(≤$25/月)**
- 东京 Lightsail 1GB → **4GB**(现有内存告警 + 未来 Nautilus 需要)
- Windows 桌面 = 纯研究机;唯一的立即行动项

**Phase 1 — 多策略 paper(~$100/月,触发条件:≥2 个策略候选 live)**
- 一台 8GB VPS(Tokyo)承载全部 runner + backend:systemd、healthchecks.io 心跳
- **研究与执行物理分离**:桌面关机不影响任何交易/paper
- Windows 计划任务迁入 VPS cron(gamma/wheel/delta 三个每日任务)

**Phase 2 — $2M live(~$300-500/月,触发条件:首个策略过晋升闸门进真金)**
- 执行节点 ×2:Tokyo(crypto/OKX/Deribit)+ US-East(equities/Alpaca),贴近交易所
- 异区监控节点:互为 watchdog + **dead-man switch**(监控节点失联≥N 分钟 → 执行节点自动转 REDUCE_ONLY)
- 托管 Postgres;secrets 进 vault(AWS SM 或 SOPS)
- 每日**对账作业**:平台事件流 vs 交易所结单,差异即告警(上真金前必须存在)

## 安全(现在就做,不花钱)

1. 交易所 API key 按策略隔离,**一律禁提币** + IP 白名单
2. 交易所账户硬件 2FA
3. .env 永不入库(已遵守);Phase 2 前迁 vault
4. Deribit live 先 testnet 全周期(已内建于 wheel live 模式)

## 触发器摘要

| 事件 | 动作 |
|---|---|
| 现在 | Lightsail 升 4GB;R2 桶建立 + 夜间同步脚本 |
| 第二个策略进 paper 稳定 | Phase 1 VPS,任务迁移 |
| 首个策略过全部晋升闸门 | Phase 2 双执行节点 + 对账作业 + vault |
| tick 数据需求真实出现 | 再评估列式库 |
