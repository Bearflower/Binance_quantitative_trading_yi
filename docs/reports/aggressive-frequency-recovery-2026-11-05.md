# 激进版频控恢复最小修复验收

范围：`codex/aggressive-cooldown-audit` 隔离分支，仅频控恢复、故障时新入场拒绝和时区比较。未部署、未修改参数、STOP/TP、资金分配或生产政策。线上 revision 1 的风险减半、禁止加仓和冻结额度继续由现有政策控制。

## 已确认根因与修复

- `FrequencyController.load_state` 按无序 SQL 结果覆盖同币最后开仓时间。同一数据正反序可恢复为 0 小时或 72 小时前，导致重启缩短冷却。现按最大绝对时间归并，整份数据校验后才发布快照；重复读取不累加计数。
- 原读取异常只记录警告，空内存仍可交易。现在初始化、缺 DB、查询异常、非列表响应、非法/缺失数值、无时区的持久化时间及写失败均保持 `state_ready=False`；成功查询的空列表表示已知无历史，允许 ready。成功 reload 可以恢复 readiness，已有暂停仍生效。无自动定时解除机制。
- 新入场在 `execute_signal`、直接 `_place_entry_order` 和 `submit_aggressive_entry` 拒绝未就绪状态；最终提交在慢查询及预约锁等待之后再次检查。纯保护、减仓和恢复逻辑不经过新门禁。
- 旧代码剥除数据库 UTC 时区，与本地时钟相减。现统一按绝对时间比较，交易日继续用进程本地日期，持久化时刻保持 aware。
- 旧周快照读取后未参与周损失计算。现使用同周最保守快照，并加上恢复之后的日盈亏变化量，避免重启绕过周暂停；`-70` 恢复后新增 `-10` 必须按 `-80` 判断，不遗漏新损失。

## 数据与语义边界

协调者提供的生产只读统计：`btc_eth.frequency_control_state` 的 `MTPCS激进策略` 有 142 行，日期 2026-09-10 至 2026-11-04；`trade_date/daily_trade_count/daily_pnl/consecutive_losses/weekly_pnl` NULL 数均为 0。时间列是可空 timestamptz，合法 NULL 时间允许；非空 naive 时间拒绝。因此当前已查数据不会因上述数值空值校验普遍禁单。未来异常将明确阻断新风险，不能静默当作零。

这些 legacy 行包含重复的全局日/周快照。本包保留既有当日日盈亏累计方式；周恢复选保守最小值，可能比最新真实值更严格。它不是可用于收益排名的交易账本，不能证明真实策略盈利或可靠平仓归属。

本包**没有实现 STOP 专属退出冷却**。现有退出路径依赖估算 PnL、条件单状态、取消错误码或 symbol±5 分钟 trade_records 匹配；它们不能证明 owned 的实际退出成交。`record_trade(pnl=...)` 还会增加开仓计数，不能直接接到每个部分成交回调。STOP 触发后的限价单也可能仍未成交。

后续退出驱动的验收契约必须有：account/strategy/position-lot 归属、真实 orderId 与 tradeId、reduce-only 和方向、实际累计平仓量/剩余量、成交时间及费用；稳定幂等键、持久化事件与频控更新原子性；重复/乱序/部分成交/重启测试。只有已核实事件可推进退出冷却；未知事件不能被估算收益或“仓位不见了”洗成已核实。当前 readiness 只表示频控快照可用，不代表该退出契约已实现；成功恢复后仍按原开仓时间冷却和次数上限运行，不声称阻止了所有止损后重复信号。

10 月既有订单报告可支持“STOP 相关损失值得调查”，不能推出最优参数或把订单数当独立交易次数。本包不改入场过滤、冷却时长、止盈止损参数。

## 验证

TDD：先运行恢复/异常/时区 7 个失败用例，再实现；随后周损失重启、naive 暂停和恢复后损失增量各自先失败再修复。最终专用测试 31 项。

```sh
RISK_TEST_DSN='postgresql://yl@127.0.0.1:55439/risk_tests' PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_aggressive_frequency_readiness.py tests/test_risk
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_strategies/test_btc_eth_aggressive_reduceonly.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_strategies/test_btc_eth_aggressive_adopt_positions.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_strategies/test_btc_eth_aggressive_core_forwarding.py tests/test_strategies/test_btc_eth_aggressive_core_equivalence.py
```

专用及风险套件 115 passed（含真实 PostgreSQL）；保护回归分别 14、18、13 passed。未设置 RISK_TEST_DSN 时真实 PG 用例明确 skip，不读取生产配置。只使用独立本地 risk_tests。

覆盖命令：

```sh
COVERAGE_FILE=/tmp/aggressive-frequency.coverage PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_aggressive_frequency_readiness.py --cov=strategies.btc_eth_aggressive.strategy --cov=shared.entry_risk_policy --cov-branch --cov-report=json:/tmp/aggressive-frequency-coverage.json
```

31 passed；对 git diff 新增可执行行与 coverage JSON 比对，两个生产文件的新增行 missing=[]、新增行起点的 missing_branches=[]。这是本次改动覆盖，不是整份 7000 行策略的 100% 覆盖。coverage 导入有已有 NumPy reload warning。系统 Python 3.9 将四个旧策略测试文件放同一进程时存在 sync fixture 的 event-loop 生命周期顺序错误；上述分进程执行均通过，未为此修改无关业务。
