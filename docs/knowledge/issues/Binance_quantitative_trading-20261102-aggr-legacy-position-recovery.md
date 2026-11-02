---
id: Binance_quantitative_trading-20261102-aggr-legacy-position-recovery
类型: issue
项目: Binance_quantitative_trading
标题: 激进策略重启后旧仓未接管且新仓退出元数据只写不读
日期: 2026-11-02
标签: [bug, btc_eth_aggressive, 重启恢复, 资金限额, 持仓归属, 保护单]
任务标识: aggr_legacy_recovery_2026-11-02
状态: 待复查
审查: 已审查
---

# 激进策略重启后旧仓未接管且新仓退出元数据只写不读

## 现象

- 生产交易所中激进策略的 ETH、SOL、XRP 旧仓在重启后未进入 `self.positions`，旧仓未显示在策略持仓看板。额度检查虽已读取交易所仓位，旧仓普通软件止盈和时间退出无法仅凭内存状态执行。
- 新仓等级、ATR 等退出参数写入独立 `position:<symbol>` 状态键，但原恢复路径没有读取并核验该键；启动保护补挂路径还可能用默认 A 级和当前 ATR 构造软件持仓状态。
- 数据库 `condition_orders.status='OPEN'` 有历史残留，不能证明交易所当前在挂保护单。SOL、XRP 各有多组交易所止盈单，单个 TP1/TP2 字段无法表达全部历史订单。

## 根因

启动恢复、保护补挂、看板上报分别依赖内存状态或旧数据库条件单，缺少以交易所当前净仓、完整成交 lot、真实在挂止损及归属为共同依据的核验。元数据持久化与恢复不是闭环；保护补挂也未继承“拒绝猜测接管”的结果。

## 解决

- 新仓独立元数据保存真实首笔成交时间、方向、等级、ATR、数量、退出标志与订单 ID；重启恢复须同时核对归属、成交 lot、方向/数量、入场价和交易所真实在挂保护单，失败时不恢复软件退出。
- 未接管仓位只核查并必要时补足交易所硬止损，不用默认参数重建软件 TP；旧仓保留原交易所 TP/SL，并以已核实首笔成交时间执行保守 120 小时上限。
- 旧仓单独上报 `strategy_states` 与 `strategy_open_positions`；两者使用相同的严格归属集合。旧仓或归属核验失败时只刷新心跳，保留先前快照；“受保护”标记必须核对止损触发价及覆盖数量。
- 额度减仓与到期平仓前重新核对完整成交 lot；拒单时保留交易所止损，避免取消条件单后留下裸仓。

## 验证

- 代码合并主线 `dbfcc652`，文档更新至 `b1eb8ac1`。相关单测 68 通过；资金限额测试 150 通过、1 项预期失败。
- 2026-11-02 16:07 北京时间只读核验：CI 自动部署镜像 `ghcr.io/bearflower/trading-btc-eth-aggr:latest` 与主线四个关键文件 SHA-256 一致，容器健康，首轮策略执行无 error；SOL 多仓 0.47、XRP 多仓 128.2，真实止损数量分别覆盖 0.47、128.2，看板两笔均有记录。ETH 已归零且残余条件单为 0。
- **待复查**：SOL、XRP 的 120 小时到期实盘退出尚未发生；下一笔新仓及一次自然重启后的元数据恢复尚无实盘证据。逐小时静默巡检已启用。

## 适用范围与修订

适用于 `strategies/btc_eth_aggressive/{strategy.py,main.py}`、`shared/{binance_api.py,position_entry_recovery.py,capital_manager.py}`，从 `dbfcc652` 起。旧仓原始等级和 ATR 已丢失，不得声称普通软件 TP 已恢复；交易所原保护单继续管理。无替代条目。

## 教训

AI 归纳，仅供参考：共享净额账户的持仓恢复需要“当前交易所仓位 + 原始成交 lot + 真实保护单 + 归属”四项共同证明。数据库历史 OPEN 标志和内存快照都不能单独证明仓位身份或保护完整性。恢复路径与后续补挂路径必须执行同一拒绝接管边界。

## 关联

- 持久证据：[部署记录](../../reports/aggr-legacy-recovery-deploy-2026-11-02.md)、`dbfcc652`、`tests/test_strategies/test_aggr_metadata_restore.py`、`tests/test_strategies/test_aggr_safety_regressions.py`。
- 必要证据摘要：交易所 live SOL/XRP 0.47/128.2 且止损全量覆盖；看板同步；68 项相关测试通过；到期和新仓自然重启仍待观察。
- 相关：[共享账户持仓归属](./Binance_quantitative_trading-20260922-shared-account-position-ownership.md)、[安全退出决策](../decisions/Binance_quantitative_trading-20261102-untracked-position-safe-exit.md)。
