---
id: Binance_quantitative_trading-20261102-ai-tuner-legacy-prompt-contract-mismatch
类型: issue
项目: Binance_quantitative_trading
标题: 旧契约策略收到新信封系统提示词，网格调优响应缺 reasons 和 summary
日期: 2026-11-02
标签: [bug, ai_tuner, grid, LLM, prompt, 契约不一致]
任务标识: ai_tuner_contract_and_allocation_2026-11-02
状态: 已修复并部署，待周度任务实跑验证
审查: 已审查
---

# 旧契约策略收到新信封系统提示词，网格调优响应缺 reasons 和 summary

## 现象

2026-11-01 15:59 UTC，`ai-tuner` 的 `grid` 周度调优先后记录两次 `旧契约缺少必需字段 missing=['reasons', 'summary']`，语义重试后仍解析失败，本轮执行记录落为 `error`。告警汇总为 23 个错误（共 24 条日志），并不代表 23 次独立调优。

## 根因

`WeeklyTuningJob` 按适配器是否声明 `parameter_manifest` 选择解析契约：网格没有 manifest，走 legacy；但 `_build_prompts` 无条件拼入 `ai_tuner/prompts/common_rules.txt`。该通用提示词要求新信封的 `decision`、`objective` 等字段，并限定候选策略为 `btc_eth_aggressive` / `hrs`。网格自己的提示词要求 `reasons`、`summary`，两者冲突，LLM 输出未满足 legacy 解析器的必需字段。

## 解决

`_build_prompts` 采用与解析器相同的 manifest 判定：新契约沿用 `common_rules.txt`；旧契约使用新增的 `legacy_common_rules.txt`，明确 `reasons`、`summary`、`adjustments`、`expected_impact` 等输出字段。旧契约解析器的字段要求未放宽。

## 验证与边界

- 回归测试覆盖网格旧契约提示词和 HRS manifest 新契约提示词；与资金分配修复的目标测试在远端最新 `main` 隔离检出上合计 `105 passed`。
- [提交 `cbb45a5`](https://github.com/Bearflower/Binance_quantitative_trading_yi/commit/cbb45a5ca4dbc6345116f5c25f08d30ed0890fc7) 经自动部署落地；2026-11-02 20:02 北京时间，生产部署日志为 `DEPLOY_SUCCESS`，`ai-tuner` 的 `/app/VERSION` 为该 SHA、`DEPLOY_ID=00009101`，容器 `running/healthy`，启动日志无错误。
- 部署后周度调优尚未再次执行，因此“LLM 实际输出恢复 legacy 字段”仍待下一次 `grid` 调优日志与 `ai_tuner_runs` 结果验证；不为验收主动触发带成本和审批副作用的线上调优。

## 教训

同一策略的提示词格式、重试提示和解析器必须由同一契约选择条件驱动。单独校验解析器无法发现上游系统提示词与策略提示词相互冲突。

## 关联

- 代码：`ai_tuner/scheduler/weekly_job.py`、`ai_tuner/prompts/legacy_common_rules.txt`、`ai_tuner/tests/test_prompt_contract_routing.py`
- 同轮独立问题：[月度分配写入不存在的持仓上限路径](./Binance_quantitative_trading-20261102-ai-tuner-optional-position-cap-path.md)
- 每日变更：[2026-11-02](../changelog/2026-11-02.md)
