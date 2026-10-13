---
id: Binance_quantitative_trading-20260921-hrs-monthly-return-distortion
类型: issue
项目: Binance_quantitative_trading
标题: HRS 月度收益率 -185.55% 失真（分母取月初 open_margin）
日期: 2026-09-21
标签: [bug, ai_tuner, 收益率口径]
任务标识: monthly_allocation_4strategy_split_2026-09-21.md
状态: 待复查
审查: 已审查
---

# HRS 月度收益率 -185.55% 失真（分母取月初 open_margin）

## 现象
看板【月度资金分配】中 HRS 策略月度收益率显示 **-185.55%**，明显失真。

## 根因
数据与公式本身没错，是收益率**分母口径**问题：旧口径 `return_rate = 当月 realized_pnl / 上月分配额`（`pnl_collector._query_prev_month_capital`）。而 HRS 的**实际占用保证金（79.7）远超其名义分配额（53.84）**，叠加存量持仓与累计亏损远超分配额，使收益率被放大失真。

## 解决
口径模型：`return_rate = 当月 realized_pnl / 策略月初实际占用保证金(open_margin)`。
| 文件 | 改动 |
|---|---|
| `ai_tuner/allocation/pnl_collector.py` | 新增 `_query_month_start_margin`：查 `strategy_position_snapshot_history` 本月初最早一条 `open_margin`，回退上月最近一条，全无返回 0；新增类常量 `_SNAPSHOT_HISTORY_TABLE` / `_MONTH_START_MARGIN_QUERY` / `_PREV_PERIOD_MARGIN_QUERY`；提取公共函数 `_to_naive` 消除 3 处重复 |
| `ai_tuner/allocation/allocation_calculator.py` | 仅更新 docstring |

HRS 限额核验（受约束）：`strategy_name="hrs"` → 命中 DB 分配表 `strategy_id="hrs"` → 生效限额 = 月度分配额 53.84（月度额优先于 config 静态兜底 110）；但约束只拦新开仓、**不强制清存量**（存量持仓 79.7 超限 48%）。历史表写入端：`dashboard/backend/services/data_service_docker.py`（按小时 upsert，随持仓对账任务落库）。

## 验证
- ai_tuner allocation 测试 **37 passed**（含 5 个新用例：正常 / 回退 / 返 0 / 带时区 / 分母 0 收益率记 0）。
- 规范检测：无强制违规（采纳建议提取 `_to_naive`）。
- 部署：按需重建 ai-tuner 容器，**DEPLOY_ID=b4f70588**，五层验证全通过（容器 healthy、镜像 ID e42677fe、容器内 VERSION/MD5 == 本地/服务器；需求文档 §5.2 已更新）。
- **待观察卡点（本条目为「待复查」的依据）**：`strategy_position_snapshot_history` 最早到 **2026-09-10 17:00** 才有数据，缺 9-1~9-9 月初快照 → **本月仍取不到真实月初占用，看板 -185% 仍失真**；用户决策「改好代码 + 确保落库完善，下月起完整生效」。**下次月度（10/31）以新口径运行后应回填此卡点结论。**

## 适用范围与修订
- 模块：`ai_tuner/allocation/pnl_collector.py`；本次改动未随该任务提交（部署直接生效，DEPLOY_ID=b4f70588）；据 git 复核，该改动于 commit `3e17962`（2026-09-24，随「月度资金分配支持每日按净资产刷新金额」等特性合并提交）首次入库。
- **后续修正（2026-10-13，需求文档 §5.2 v1.5）**：分母再次修订为与看板一致的「策略资金基数」**4 步回退链**（优先 `allocated_amount`，退化 `open_margin`），`_query_month_start_margin` 更名为 `_query_base_capital`；原因：纯 `open_margin` 口径在无月初快照时会把分母取 0、收益率被上层除零保护**静默置 0**（2026-10-13 commit 05888cc）。无替代条目。

## 教训
分母（资金基数）口径必须与「资金使用效率」语义一致，且要显式处理「无数据 → 取 0」的静默降级：用名义分配额会低估占用、用实际占用保证金又受快照缺失牵连。任何回退链都要保证能取到有效基数，否则失败会以「收益率 0」的面貌被隐藏，比报错更难发现。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/monthly_allocation_4strategy_split_2026-09-21.md)（不随 Git 同步）
- 持久证据: 需求文档 `docs/requirements/capital_allocation/月度资金分配需求文档.md` §5.2（收益率分母口径）；`ai_tuner/allocation/pnl_collector.py::_query_month_start_margin`（2026-10-13 更名为 `_query_base_capital`）；部署 DEPLOY_ID=b4f70588
- 必要证据摘要: -185.55% 失真 → 分母口径改为月初 open_margin；37 passed（含 5 新用例）；DEPLOY_ID=b4f70588 五层验证通过；快照表 2026-09-10 起才有数据 → 本月仍失真、下月生效（待复查卡点）
- 相关: [Binance_quantitative_trading-20260921-allocation-pnl-collect-always-zero.md](./Binance_quantitative_trading-20260921-allocation-pnl-collect-always-zero.md)（同 `pnl_collector.py` 采集模块）；[Binance_quantitative_trading-20260921-allocation-first-month-fallback-caliber.md](./Binance_quantitative_trading-20260921-allocation-first-month-fallback-caliber.md)（同任务）；[Binance_quantitative_trading-20261009-capital-allocation-month-offset.md](./Binance_quantitative_trading-20261009-capital-allocation-month-offset.md)（同属月度资金分配模块）
