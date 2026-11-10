# HRS 与激进版小仓位发布验收（2026-11-10）

## ONUSDT 06:35 推送

用户提供的 2026-11-09 22:35:41 UTC 信号对应 `hrs.hrs_signals.id=5682`，做空评分 7.23，`entry_mode=semi_emm`、`signal_origin=shadow_scoring`。这是影子模式信号，设计上不提交订单。生产旧代码又在影子归类前先执行 HRS `monitor_only` 门禁，导致该行保留 `triggered`、`resolution=NULL`；周期推送只显示“做空信号: 1”，未说明未开仓。该时间窗 `trading.trade_records` 的 ONUSDT 记录为 0。按精确 id、方向、模式、来源、时间和原状态条件，已将这一行更正为 `resolved/shadow`（`UPDATE 1`）。其他 202 条同类未结案历史影子行未批量改写，待逐条来源与交易事实核对。

新代码先归类影子信号，再对真实入场执行政策门禁；周期推送分别显示评分信号、实盘开仓、影子不下单和其他未开仓。`semi_emm.execution_mode` 仍为 `shadow`，本次没有把影子策略转为实盘。

## 代码与数据库发布

- 本地隔离 PostgreSQL：风险预留和迁移测试 74 passed；包含 HRS ONUSDT 整笔 TP1 出场几何、revision 绑定、重复意图拒绝与 JSONB 迁移幂等性。
- 发布门槛定向套件：301 passed。全量旧 HRS 套件仍有既存的市场数据/形态夹具与本机 Python 3.9 事件循环问题，详见 `small-position-executability-2026-11-09.md`。
- 提交 `0c62740bb8c82c7003fbf71d688c8222951b1a0d` 经 CLCI [运行 42724545653](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/42724545653) 成功发布。生产 pin `active`、state `complete`；仅重建激进版和 HRS 容器，二者均 `healthy`。镜像 digest 分别为 `c7f1e3e2e83cf9c18100136f29cc9cdf35193ce7ba3b64b6222cf3735225cfa1` 和 `dab45f8446839e04cba6dd2e859c65365d3de5e34494fb384c4d19db45a5e0fd`。源码证明文件齐全。
- 发布脚本先备份数据库，备份 `database-before.dump` 非空（32,969,571 字节），再事务迁移 HRS `exit_ratios` 列；生产读回为 `jsonb`。
- 发布前后交易所只读门禁均 `ok=true`、空仓、普通/条件挂单为空，资金分配哈希均为 `d7398bee4c57fc6c55e75023b244bc394c0c5de9deefd9eb0a2eb62bc33ea057`。

## HRS 缩量政策

代码发布后单独执行经本地独立数据库预演和 SHA-256 核对的原子政策事务，SQL 哈希 `f6422576a3ef5c74b8f658b23821cac262a1d4174257a20fc0369e24afc92f87`。先核生产空仓零挂单、月度 HRS 分配 52.36 USDT、无 HRS 未解决预留；事务中按账户锁验证原政策 revision 2 `monitor_only`，随后 `INSERT 1`、活动指针 `UPDATE 1`、审计 `INSERT 1` 并提交。重复执行在本地预演中被 CAS 拒绝。

生产读回 HRS revision 3：`reduced`、乘数 0.5、比例上限 0.15、绝对保证金上限 26.18 USDT、禁加仓；到期时间沿用 revision 2 的 **2026-11-12 02:54:46 UTC（北京时间 10:54:46）**。其他三策略政策仍为 revision 2，未改资金分配。切换后交易所只读门禁再次 `ok=true`，空仓、零挂单、分配哈希不变，容器均健康。

## 待验收

- 等待真实的非影子 HRS 和激进版合格信号，核对“合格信号 → 预算/最小单检查 → 预留 → 入场成交 → 全量止损/TP1 → 重启恢复/平仓”的逐笔证据。当前没有新成交，不能宣称策略收益改善。
- 影子 `semi_emm`、EMM、LV-RM 仍按既有配置只记录不下单；本次未改变其执行模式。
- 202 条其他历史 HRS 影子信号的空 `resolution` 仍待来源与订单证据逐条核对；不属于 127 笔未归属收入事件。
- HRS revision 3 在上述时间自动到期；后续续期须重新核对实盘风险和结果，不自动延长。
