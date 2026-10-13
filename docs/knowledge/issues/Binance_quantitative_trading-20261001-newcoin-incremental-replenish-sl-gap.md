---
id: Binance_quantitative_trading-20261001-newcoin-incremental-replenish-sl-gap
类型: issue
项目: Binance_quantitative_trading
标题: new_coin 全量重建补挂产生 SL 空窗（改为增量补挂）
日期: 2026-10-01
标签: [bug, new_coin, 资金安全]
任务标识: new_coin_incremental_replenish_2026-10-01.md
状态: 已解决
审查: 已审查
---

# new_coin 全量重建补挂产生 SL 空窗（改为增量补挂）

## 现象
new_coin 持仓保护补挂为「全量重建」语义时，当**仅缺止盈单**「补挂会先撤掉已有止损单」，在撤旧单与重挂新单之间产生 **SL 空窗**（资金安全项）。任务源于用户 2026-10-01 拍板「新开独立任务——消除『仅缺止盈单时补挂会先撤掉已有止损单』造成的 SL 空窗」。

## 根因
`replenish_conditional_orders` 为**全量重建**语义（先撤全部保护单再重挂），默认路径即在重挂前撤销全部保护单。而 `find_missing_protection` 是**类型级**匹配（`tp1`/`tp2` 同记 `TAKE_PROFIT`），所谓「仅缺 TP」实为「无任何 OPEN TP」，无法表达「只缺 TP1」。因此当「仅缺 TP」时，全量路径仍要先撤掉全部（含仍然有效的 SL）再重挂，中间窗口内空头敞口无止损保护 → SL 空窗。

## 解决
将 `strategies/new_coin/` 内补挂由「全量重建」改造为「增量补挂」——核验真实空头敞口缺失的保护单**类型**后只补缺失类型，**绝不撤销/变更任何一条仍有效（OPEN）的保护单**。范围仅 `strategies/new_coin/`，不改 `shared/`。提交 `7ef4b18`（15 文件）：
| 文件 | 改动 |
|---|---|
| `strategies/new_coin/executor.py` | 新增常量 `_MISSING_SL="止损单"`/`_MISSING_TP="止盈单"`；`find_missing_protection(..., strict=False)`；`replenish_conditional_orders(symbol, entry_price, *, missing)` 必需关键字参数；增量编排 `_execute_incremental_plans`（只 `place_sl=_MISSING_SL in missing`、`place_tp=_MISSING_TP in missing`）；删 `_cancel_orders_best_effort`；入口 fail-closed 守卫 `if not missing: return False`（空列表/None 一律零挂零撤，L3394-3397）；新增 `_backfill_algo_ids_from_db(symbol)`（只 `setdefault` 填空缺键、绝不覆盖，INV-4）；抽取共用内核 `_place_protection_orders(symbol, plans, *, place_sl, place_tp)` 与 `_finalize_replenish` |
| `strategies/new_coin/strategy.py` | `_guard_symbol` 用 `strict=True` + `None` 不可判定分支（异常→`None`→零补挂仅告警）、新增 `_record_gap_and_alert` |
| `strategies/new_coin/config.yaml` | 仅更新 `trading.replenish.cancel_after_ready` 注释语义（重定义为「启用增量补挂」，`false` 回退旧全量 `_execute_replenish_plans`） |

决策要点：方案甲（类型级）；取消默认路径全量撤单与 Phase B；`market_close` 只平 TP 零撤 SL；无新增阈值配置（无硬编码）。

## 验证
- `strategies/new_coin/tests/test_p0_replenish_and_registration.py` → **70 passed**。
- `strategies/new_coin/tests tests/test_strategies` → **696 passed / 4 failed / 1 xfailed**（4 failed 全在 `test_stop_loss_mark.py`，MagicMock 未 await 的既有测试桩问题，与 HEAD 基线一致）→ **零新增失败**。
- 加反例测试 `strategies/new_coin/tests/test_p0d_counterexample.py`（39 用例，场景矩阵 S1~S10 全通过，核心点 S2「仅缺 TP → SL 原样保留、零撤单」端到端通过）后全量回归：`python3 -m pytest strategies/new_coin/tests tests/test_strategies -q` → **735 passed / 4 failed / 1 xfailed**，零新增失败；本轮新增/修改代码 100% 行+分支覆盖。
- 部署：Actions **Run #50 attempt 1 全绿**（detect + 10 个 build-all + deploy 全部 success）；五层防幻觉验证全过——L1 增量只 recreate `new_coin`/`hrs`/`dashboard-api`（后两者 Dockerfile 为全项目 `COPY .`）共 17 容器在运行；L2 镜像 `ghcr.io/bearflower/trading-new-coin:latest`；L3 `/app/VERSION` `DEPLOY_ID=00003201`、`GIT_SHA=7ef4b18a6c4421fca16616142895960f603b3fdb`；L4 关键文件 MD5 容器==本地（`executor.py c337edc28f2ca53be0d1f569be7531d7`、`strategy.py 5c6d2719a92da9b33d4cd8d72ea0ed0f`）；L5 日志 `DEPLOY_SUCCESS commit=7ef4b18a… deploy_id=00003201`，new_coin 启动后 10 分钟内 error 计数 0。

## 适用范围与修订
`strategies/new_coin/{executor.py,strategy.py,config.yaml}`；commit `7ef4b18` 已上线（push main）。**已知局限（方案甲，非本轮范围）**：仅 TP2 丢失、TP1 仍在时 `find_missing_protection` 返回 `[]`，**不会补挂 TP2**（已在架构文档与测试显式声明，不影响止损安全）。无替代条目。

## 教训
「补齐缺失」不等于「重建全部」——当保护单存在**档位/类型**语义时，全量重建会先销毁仍有效单，制造安全空窗；正确做法是**差集式增量补挂**（只补缺失类型、绝不撤销仍 OPEN 单），并给入口加 fail-closed 守卫（空/None `missing` 一律零挂零撤）。另一面是类型级匹配（`tp1`/`tp2` 同记 `TAKE_PROFIT`）表达不了档位级缺失，其局限必须在文档与测试中显式声明，不能默认「已覆盖」。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/new_coin_incremental_replenish_2026-10-01.md)（不随 Git 同步）
- 持久证据: 需求/架构 `docs/plans/fix-2026-10-01-incremental-replenish-{requirements,architecture}.md`；commit `7ef4b18`（`strategies/new_coin/executor.py` 的 `replenish_conditional_orders`/`_place_protection_orders`/`_execute_incremental_plans`、`strategies/new_coin/strategy.py` 的 `_guard_symbol`/`_record_gap_and_alert`）
- 必要证据摘要: 增量路径 S2「仅缺 TP → SL 原样保留、零撤单」通过；单测 696 passed（+反例后全量回归 735 passed）/ 4 failed（既有基线）/ 1 xfailed，零新增失败；Run #50 全绿、DEPLOY_ID 00003201 五层验证通过、关键文件 MD5 一致；handoff §5 记 696 passed 与 §6 记 735 passed 为同一批次两个口径，已如实并列
- 相关: [newcoin-zombie-reconcile](./Binance_quantitative_trading-20261001-newcoin-zombie-reconcile.md)（本任务由该条「已移交的独立任务」转入）；[newcoin-target2-never-marked](./Binance_quantitative_trading-20260915-newcoin-target2-never-marked.md)（同涉止盈档位/保护单链路）
