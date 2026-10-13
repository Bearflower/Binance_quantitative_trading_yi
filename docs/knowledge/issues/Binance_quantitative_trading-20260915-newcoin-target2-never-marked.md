---
id: Binance_quantitative_trading-20260915-newcoin-target2-never-marked
类型: issue
项目: Binance_quantitative_trading
标题: new_coin target2_reached 从未被标记导致移动止损整条链路永不生效
日期: 2026-09-15
标签: [bug, 移动止损, 止盈检测]
任务标识: new_coin_2026-09-15.md
状态: 已解决
审查: 已审查
---

# new_coin target2_reached 从未被标记导致移动止损整条链路永不生效

## 现象
new_coin 新币做空策略「移动止损从未生效」，TP2 后剩余 30% 仓位无法由移动止损保护，只能依赖纯固定止盈。（来源终极目标原文：「修复 new_coin 新币做空策略『移动止损从未生效』的 bug」及任务源「将 new_coin 固定止盈升级为移动止损」。）

## 根因
代码中已有 MTPCS/HRS 同源的移动止损机制（回撤阶梯动态止损 + 反弹 1.5ATR），其激活条件依赖 `target2_reached` 被标记；但缺少「按持仓数量对比检测止盈成交」的机制，TP 成交后不会标记 `target1_reached` / `target2_reached`，故 TP2 后剩余 30% 仓位无从进入移动止损保护——整条链路永不生效。
（说明：「`target1/target2` 未被标记」为对来源中「移动止损从未生效 / 通过止盈成交检测激活」的机制归纳；原始 handoff **未逐字**写明「`target2_reached` 从未被标记」这一表述。）

## 解决
commit `8f882bf`（fix: 激活新币做空策略移动止损，已 push origin/main）：
| 文件 | 改动 |
|---|---|
| `strategies/new_coin/executor.py` | 新增 `detect_take_profit_fills()`（L2105）+ `_get_exchange_position_qty()`（L2096）+ `clear_position_tracking()`（L2157）：对比交易所 `/papi/v1/um/positionRisk` 实际做空持仓数量与 `_last_tracked_qty` 检测 TP 成交——100%→70% 标记 `target1_reached`；70%→30% 标记 `target2_reached` 激活移动止损；→0 全部平仓 |
| `strategies/new_coin/executor.py` | `check_position_management()`（L1058）每周期先做止盈成交检测（`fill_level==0` 只 return，由 `_monitor_positions` 二次确认 + `cancel_all_algo_orders` 清理，避免孤儿条件单）；紧急/时间/移动止盈 3 处 `del position_tracking[symbol]` 改为 `clear_position_tracking` |
| `strategies/new_coin/config.yaml` | 新增 `trading.position_detection`（enabled / qty_tolerance_ratio=0.01 / qty_tolerance_absolute=0.0001 / zero_qty_threshold=0.0001）；`update_target_status()` 增加 `exchange_qty` 可选参数，`remaining_quantity` 优先用真实数量 |

## 验证
- 测试：`tests/test_strategies/test_take_profit_fill_detect.py` 新增 14 用例（全通过）；全量回归 **272 passed**。
- 部署：按需重建 `trading_system-new_coin` 容器（仅该容器，`--no-cache`，DEPLOY_ID **9D2DB354**），五层验证通过（容器 Up healthy、镜像 fc6ea8 匹配、executor/strategy/config 三文件 MD5 本地=服务器=容器内一致、200 行日志无 error）。
- 待观察（来源已注明卡点）：移动止损虽已上线，但**尚无实际持仓触发 TP2 的真实运行数据验证**（部署时容器无持仓）；需后续观察持仓，TP2 成交后日志应出现「第二目标止盈成交，激活移动止损」。

## 适用范围与修订
`strategies/new_coin/{executor.py,strategy.py,config.yaml}`；commit `8f882bf` 已上线（push origin/main）。移动止损本体在 `shared/dynamic_trailing.py`（MTPCS 同源）。来源另记一处设计取舍（止盈成交检测单靠「持仓数量下降」判定，极端滑价 / 非 TP 减少可能误标 target2；当前系统仅有 TP/止损 reduce 路径、风险低，未叠加「价格触及目标价」多重确认）与一处未提交改动（`strategies/new_coin/scoring_engine.py` 在 git status 仍为 M，非本任务主线）。无替代条目。

## 教训
「代码里有能力 ≠ 链路已激活」：既有实现若依赖某个状态位（如 `target2_reached`），必须确认该状态位**真的有写入路径**，否则整条下游链路静默失效、长期无人察觉。上线后还应补「真实运行触发」的验证，不能只凭单测 + 部署就判定业务链路已贯通。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/new_coin_2026-09-15.md)（不随 Git 同步）
- 持久证据: 需求文档 `docs/requirements/new_coin/新币做空策略 V4.0 完整版.md`（§5.2/§5.3）、`docs/plans/new_coin动态利润保护需求文档.md`；部署日志 `docs/reports/new_coin/deployment_log.md`（2026-09-15 条目）；commit 8f882bf
- 必要证据摘要: 新增 14 用例全通过、全量回归 272 passed；DEPLOY_ID 9D2DB354 五层验证通过；真实 TP2 触发待观察
- 相关: [Binance_quantitative_trading-20260927-newcoin-atr-loss-on-restart.md](./Binance_quantitative_trading-20260927-newcoin-atr-loss-on-restart.md)（同属 new_coin 移动止损链路）
