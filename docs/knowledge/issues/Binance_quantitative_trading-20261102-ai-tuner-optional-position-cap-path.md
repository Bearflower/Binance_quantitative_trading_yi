---
id: Binance_quantitative_trading-20261102-ai-tuner-optional-position-cap-path
类型: issue
项目: Binance_quantitative_trading
标题: 月度分配对无 position_sizing 的策略写入嵌套路径，记录错误却报告更新成功
日期: 2026-11-02
标签: [bug, ai_tuner, 月度分配, 配置路径, 部分写入]
任务标识: ai_tuner_contract_and_allocation_2026-11-02
状态: 已修复并部署，待资金分配任务实跑验证
审查: 已审查
---

# 月度分配对无 position_sizing 的策略写入嵌套路径，记录错误却报告更新成功

## 现象

2026-10-31 01:30 UTC，`ai-tuner.deploy.config_operator` 记录 `配置路径不存在 key_path=position_sizing.total.account_ratio_cap missing_key=position_sizing`。月度配置更新同时写 `capital_limits` 和该嵌套路径；`ConfigOperator.apply_changes` 只要至少一个字段成功写入就返回成功，因此可能出现错误日志与“策略配置已更新”并存。

## 根因

`AllocationConfigUpdater._update_strategy_configs` 对所有策略无条件提交 `position_sizing.total.account_ratio_cap`。`grid` 与 `new_coin` 的配置没有 `position_sizing` 根节点；该字段只适用于声明了相应持仓结构的策略。错误发生于 `ConfigOperator.set_nested_value`，而其余 `capital_limits` 字段已写入，导致部分成功被当作整项成功。

## 解决

更新前读取策略配置。所有策略仍更新 `capital_limits`；仅当配置已有 `position_sizing.total` 字典时同步 `account_ratio_cap`。若已有 `position_sizing` 但结构无效，整项标记失败，不发布该策略的成功结果。日志中的 `account_ratio_cap` 仅在实际更新时填写。

## 验证与边界

- 回归测试覆盖 `grid`、`new_coin` 缺少 `position_sizing` 时只写 `capital_limits`，以及 `btc_eth` 已声明结构时两处比例同步；目标测试合计 `105 passed`。
- 与同轮提示词修复一起由 [提交 `cbb45a5`](https://github.com/Bearflower/Binance_quantitative_trading_yi/commit/cbb45a5ca4dbc6345116f5c25f08d30ed0890fc7) 部署；2026-11-02 20:02 北京时间生产记录 `DEPLOY_SUCCESS`，容器版本匹配且健康。
- 部署后资金分配任务尚未再次执行，需在下一次实际分配中核对无该路径错误、各策略 `capital_limits` 与数据库发布结果一致。历史日志不会因部署而消失。

## 教训

跨策略批量配置写入不能假设所有策略共享同一配置树。多字段写入接口若以“至少一项成功”表示成功，调用方必须先筛选适用字段并识别非法结构，避免部分写入被误报为完整成功。

## 关联

- 代码：`ai_tuner/allocation/config_updater.py`、`ai_tuner/allocation/tests/test_optional_position_cap.py`、`ai_tuner/deploy/config_operator.py`
- 同轮独立问题：[旧契约提示词冲突](./Binance_quantitative_trading-20261102-ai-tuner-legacy-prompt-contract-mismatch.md)
- 每日变更：[2026-11-02](../changelog/2026-11-02.md)
