# C 段独立代码审查报告：月度资金分配月份口径修复 + 零开仓垫底规则

- 审查任务：capital_allocation_month_fix_zerotrade（R3）
- 审查者实际模型：GLM-5.3（独立新对话；A-review 亦为 GLM-5.3 但独立对话，按工作流 §2.1 允许；未参与 A 段设计与 B 段实现）
- 被审版本：基线 HEAD `b43a8d9`，工作区未提交变更 = §14.5 冻结范围 5 改 1 新（`git status` 亲核）
- 审查依据：需求文档 §14（v1.4，GA 已放行）、工作流 §6-§7/§9.4
- 审查日期：2026-10-09

## 审查结论

**GC 通过。** 无阻塞项（F 级）；3 条非阻塞注记（N 级，均不阻碍发布，含 D 段执行注意事项）。

## 独立复跑证据（不采信 handoff 转述，均在本机真实执行）

| 命令 | 结果 |
|------|------|
| `python3 -m pytest ai_tuner/allocation/tests/ -q` | **95 passed in 4.22s**（macOS / Python 3.9.6 / pytest 8.4.2） |
| 同命令 + `--cov` 三模块 `--cov-branch --cov-report=term-missing` | monthly_job 132 语句/26 分支、allocation_calculator 64/14、pnl_collector 74/18，**Miss=0、BrPart=0，行/分支 100%** |

与 GB 申报完全一致，证据真实。既有 24 用例回归基线保留全绿（test_allocation.py 的 diff 为纯新增 hunk，无删除）。

## 十项审查重点逐条核验

### 1. AC 逐条对照 —— ✅ 全部真实落地

- **AC-1.1**：`run_monthly_allocation` 内 `_resolve_month_inputs` → 幂等查询参数/INSERT month/返回字典均 `2026-10`，PnL 与成交行数窗口参数均为 naive `2026-09-01 ~ 2026-10-01`（`test_ac11` 断言到每个 fetch 参数）。
- **AC-1.2**：`_check_idempotency(effective_month)` 命中即返回 None，无 INSERT、无通知（`test_ac12`）。
- **AC-1.3**：`_next_month(2026,12)→(2027,1)`；`test_ac13` 冻结 12/31 运行断言幂等键/写库 `2027-01`、PnL 窗口 `[12-01, 次年01-01)`。
- **AC-1.4 五处一致**（逐处亲核源码）：① 幂等键 effective_month（monthly_job L357 附近）；② `calculator.calculate(month=effective_month)` → AllocationResult.month；③ 写库 `config_updater` L142 `result.month`；④ tuner L222 与策略配置 L318 `allocation_month = result.month`；⑤ 通知卡片 `result.month`（L446）+ 返回字典 L316。PnL 采集窗口确为 pnl_month（`_calculate_month_range(pnl_month)`）。
- **AC-1.5**：`capital_manager._current_month()`（L438-440 当前月 strftime）、`daily_refresher`（L47/56 `WHERE month=$N AND status='active'`）亲核未改动，契约测试锁定（SQL 文本断言 + 冻结时钟 2026-10-09 返回 `2026-10`）。
- **AC-2.1~2.8**：排序键 `(0 if has_trades else 1, -return_rate, strategy_id)`（calculator L235-237）与 §14.2 冻结设计逐字一致；`has_trades=bool(data.get("has_trades", True))` 缺省兼容；rank 仍按名次取 `rank_ratios[rank-1]`、超档取 0 告警（既有逻辑保留）。全部有正反例断言（含零开仓高于负收益、名义最高收益仍垫底、双零开仓占末两档总额 85%、全零开仓按 strategy_id 序、首月 fallback 不受影响、有开仓零盈亏正常排序）。
- **AC-3.1~3.4 + argparse**：`test_ac31_ac33`（显式 `2026-09/2026-10`、非首月、hrs 零开仓名义最高收益被压至 rank4/0.10）、`test_ac32`（全部写操作为 INSERT，无 UPDATE/DELETE，幂等只查 2026-10）、`test_ac34`（二次执行幂等命中，仅 1 条 INSERT、1 张卡片）、`TestManualTriggerArgparse`（缺省 None/显式解析/未知参数退出）。

### 2. 微决策复核（B 段明示请 C 定夺）—— ✅ 裁定通过

`_resolve_month_inputs` 仅给其一时抛 `ValueError`：

- **方向正确**：半参会导致「盈亏归属月与记录月份错配」（如只给 pnl_month 时自动推导 effective 会把 9 月数据写进错误的生效月），拒绝执行与 R3 资金安全一致，且设计未定义该分支、拒绝是最保守的正确选择。
- **非静默**：ValueError 被 `run_monthly_allocation` 外层 except 捕获后 `logger.error(..., exc_info=True)` 记录完整堆栈并返回 None；`test_partial_month_arguments_rejected` 验证「返回 None + 零写库」。
- **可观测性足够**：该路径仅在手动脚本半参时触发（main.py L557 调度恒为无参，永不触发）；操作员在场可直接看到脚本输出 None 与容器 error 日志。不发飞书通知可接受（见 N3）。

### 3. fail-open 双层路径 —— ✅ 无 fail-closed、无伪装成功

- 仅成交行数查询异常：内层 except → `trade_count=0, has_trades=True`，PnL/capital 照常返回（`test_collect_trade_count_exception_fail_open`）。
- 整策略采集异常：外层 except → `{"pnl":0.0, "capital":0.0, "trade_count":0, "has_trades":True}`（`test_collect_whole_strategy_exception_fail_open`）。
- capital 静默归零的下游影响：`has_trades=True` 保证不垫底；capital=0 → return_rate=0，在「有开仓组」内按 0 收益率排序——上层正确使用，无伪装成功问题。

### 4. SQL 口径防漂移 —— ✅ 逐字一致

`_TRADE_COUNT_QUERY_TEMPLATE` 与 §14.2 冻结 SQL 对照：`strategy = ANY($1::text[])`、`executed_at >= $2 AND executed_at < $3`（左闭右开）、`order_type NOT IN ('PNL_SUMMARY','CONDITIONAL_ORDER')`，**全文无 status 条件**，参数化占位符与模板外注入参数（db_names/naive_start/naive_end）一一对应。排除字面量与 `shared/trade_logger.py` 常量亲核一致（L73 `ORDER_TYPE_CONDITIONAL="CONDITIONAL_ORDER"`、L486 `'PNL_SUMMARY'`）。测试双重锁定：模板文本断言（含 `NotIn("STATUS")`）+ 实际执行查询文本断言（`assertNotIn("status", db.count_query)`）。

### 5. F4 兜底真实性 —— ✅

`_check_idempotency` 异常 → `logger.error` + `return False` 继续执行（monthly_job L383-386）；`config_updater` L134-142 `INSERT INTO public.capital_allocation ... ON CONFLICT (month) DO NOTHING` 真实存在。`test_f4` 注入幂等查询异常，断言继续完成、仅一条 INSERT 且 SQL 含 ON CONFLICT——写库走**真实** `_save_to_db`（非 mock 断言）。

### 6. 消费方零改动 —— ✅ 契约成立

capital_manager / daily_refresher 源码亲核未触碰（变更范围外）；两者按当前月查询，与生效月写入口径对齐。dashboard 按 `ORDER BY month DESC` 取最新记录，2026-10 落库即显示，无其他月份假设。未发现遗漏消费方（allocation_month 写入方 config_updater 为本任务链路，无策略侧按月校验——A 段已核查）。

### 7. 测试质量与盲点 —— ✅ mock 深度合理

- `_FakeDb` 按路由键分发，未命中路由抛 `AssertionError`（防 SQL 漂移被静默吞掉）；写库走真实 `_save_to_db`；`_FrozenDateTime` 继承原生 strptime 行为。
- 边界覆盖核实：非法月份格式（strptime ValueError）、naive/aware 转换（fetch 参数断言 naive）、空参与策略、rank_ratios 溢出取 0、Decimal 余额转 float、同收益率 tie-break、缺 has_trades 键。
- **真实值域 mock 核实**：全部 mock 行 status 仅用 NEW/CREATED；`FILLED` 在测试体中零出现（唯一出现处为禁用说明注释）；SQL 文本断言（§14.4 F1 防回归）真实存在。

### 8. 规范 —— ✅

中文注释；新增函数均 ≤50 行（`collect_all_realized_pnl` 抽出 `_collect_strategy_metrics`）；行长 ≤120；无重复代码；order_type 排除字面量源自口径协议本身（非可调阈值），rank_ratios/fallback/total_capital 全走 config。变更范围纪律：`git status` 亲核，本任务严格 5 改 1 新 + 需求文档（A 段产物），未触碰工作区其他任务未提交变更。

### 9. 静态检查 —— ✅（采信 GB 并抽查核实）

抽查 diff 无 TODO/pass 桩；`AllocationEntry` 未新增字段（`has_trades` 仅存在于排序/日志层，dataclass 构造参数完整匹配——曾疑似不匹配，经读源码澄清为日志 kwargs 误读）。

### 10. D 段就绪性（只审不执行）—— ✅

补生成命令 `--pnl-month 2026-09 --effective-month 2026-10` 在当前实现下的安全性均有测试证据：幂等防重（AC-3.4 双保险）、首月判定不被误触（库中 2 条历史 → 非首月，`test_ac31_ac33` 以 first_month_cnt=2 模拟）、不触碰历史记录（AC-3.2 纯 INSERT）。§14.5 方案（仅重建 ai-tuner、容器内 /tmp 执行、三项只读验证、append-only 回退）可执行。

## 非阻塞发现（N 级，不阻碍 GC）

### N1：`effective_month` 显式透传时无格式校验（供 D 段注意）

- **位置**：`monthly_job._resolve_month_inputs` / `scripts/manual_allocation_trigger.py::_parse_args`。
- **触发情景**：手动传入非法生效月（如 `--effective-month 2026-13` 或格式错误）。pnl_month 会被 `_calculate_month_range` 的 strptime 拦截（ValueError → 拒绝），但 effective_month 仅作字符串用于幂等查询与写库，格式/语义非法（`2026-13`）时会落一条非法月份记录并占用幂等键；dashboard `ORDER BY month DESC` 会把 `2026-13` 排最前。
- **影响评估**：触发条件为 D 段人工敲错参数；命令为已锁定复制粘贴值，概率极低；后果可发现（看板异常月份）、可恢复（append-only 删记录 + 重跑）。
- **建议**：D 段执行时逐字核对两参数；如后续迭代可加 argparse 格式校验或 `_resolve_month_inputs` 对 effective_month 同样 strptime 校验。**不要求本次修改**（修改须退回 B 并重走测试，收益/成本不匹配）。

### N2：`_query_strategy_trade_count` 空名单返回 0 属理论 fail-closed 点（不可达，知悉）

- **位置**：pnl_collector `_query_strategy_trade_count`（`if not db_names: return 0` → has_trades=False → 垫底）。
- **触发情景**：`_resolve_db_names` 返回空列表。实际不可达：空 strategy_id 已在上游循环过滤，`_resolve_db_names` 至少返回 `[strategy_id]`。
- **建议**：知悉即可，无需修改。

### N3：半参 ValueError 拒绝路径不发飞书通知（可接受）

- **位置**：`run_monthly_allocation` 外层 except。
- **触发情景**：手动脚本半参。操作员在场（终端即时输出 + 容器 error 日志含堆栈），可观测性足够；调度路径永不触发。
- **建议**：知悉即可。

## AC → 实现 → 测试对照核实汇总

对照 handoff「AC → 实现路径 → 测试用例对照表」逐行抽验，实现位置与测试用例均真实存在且断言与 AC 语义一致（抽查细项见上文第 1 项）；未发现「测试过但实现偷换语义」。

## GC 放行依据（工作流 §7）

- 无未解决的重大功能、数据一致性、资金/权限或部署阻塞问题。
- N1~N3 均为低概率/不可达/在场可观测场景，处理决定：知悉 + D 段执行注意事项，无需代码变更。
- 独立复跑证据与 GB 申报一致；95 用例、三模块 100% 行/分支覆盖真实。
- 文档一致性：需求文档 §14（v1.4）与实现一致，无需文档修订（本报告即 C 段阶段 7 产出）。

## 后续（按工作流 §2.5/§2.6）

GC 通过 → 交接 **D（Seed-2.1-Pro-0915，独立新对话）**：仅重建 ai-tuner → 容器内补生成 2026-10（`--pnl-month 2026-09 --effective-month 2026-10`，执行时逐字核对参数，见 N1）→ §14.5 三项只读上线验证 → 归档。

---

**审查者**：C 角色（GLM-5.3，独立新对话）
**报告日期**：2026-10-09
**结论**：**GC 通过**
