---
id: Binance_quantitative_trading-20260927-newcoin-atr-loss-on-restart
类型: issue
项目: Binance_quantitative_trading
标题: new_coin 容器重启丢失 ATR 致阈值退化为 0 并立即误平真实仓位
日期: 2026-09-27
标签: [bug, ATR, 重启恢复, 资金风险]
任务标识: newcoin_atr_recovery_2026-09-27.md
状态: 已解决
审查: 已审查
---

# new_coin 容器重启丢失 ATR 致阈值退化为 0 并立即误平真实仓位

## 现象
来源终极目标原文：「消除 new_coin 策略『容器重启后 ATR 丢失 → 止盈/止损阈值退化为 0 → 重启后立即误平真实仓位』的资金风险」。即容器重启后，内存中的 ATR 状态丢失，止盈/止损阈值随之退化为 0，重启后首个周期即可能对真实持仓触发错误的止盈/止损平仓。
（事件日期说明：来源 handoff 文件名与内容创建时间均为 2026-09-27，正文按 2026-09-27 记录。）

## 根因
止盈/止损阈值依赖 ATR；ATR 是进程内计算态，重启后未随持仓基线一并重建 / 回填。于是 `_check_trailing_stop` / `_check_dynamic_trailing` 在 `atr<=0` 时仍参与比较，阈值退化为 0 → 立即误平。

## 解决
commit `ba0bdf2`（new_coin 重启恢复回填 ATR，资金风险修复，已 push main）：
| 文件 | 改动 |
|---|---|
| `strategies/new_coin/executor.py` | 新增 `_warn_invalid_atr(symbol, context, atr) -> bool`（约 L2107）；在 `_check_trailing_stop`（约 L2171）与 `_check_dynamic_trailing`（约 L2406）两处 `atr<=0` 时**跳过本轮比较**。守卫位于「更新最低价」早退块**之后**，故 atr 无效期间仍持续维护 `lowest_price` |
| `strategies/new_coin/strategy.py` | `_rebuild_position_baseline` 在同步基线前 `await self._calc_rebuilt_atr_map(...)`（`asyncio.gather` 批量算 ATR）；`_sync_baseline_to_tracking` 增 `atr_map` 入参 + 新增 `@staticmethod _backfill_tracking_atr`（仅「新值>0 且条目 atr<=0」时回填，**绝不覆盖**条件单恢复路径已写入的有效 atr）；仍缺失时汇总一次 WARN |
| `tests/test_strategies/test_new_coin_tracking_entry.py` | 新增 `TestTrailingStopAtrGuard` 3 项 + 3 个既有读侧用例改为显式有效 atr（原断言保留） |
| `tests/test_strategies/test_capital_allocation_enforcement.py` | mock 补绑 `_calculate_atr` |

## 验证
- 测试（实测）：本地 `python3 -m pytest tests/test_strategies -q` → **572 passed / 1 xfailed / 0 failed**。
- 部署：Run #25（head `ef4e2de`）全绿，`run_number=25, attempt=1` → DEPLOY_ID=**00001901**；本次仅构建 `new-coin` / `kline-service` / `kline-monitor`；五层验证**第 3 层已可用**（3 个容器 `/app/VERSION` 的 `DEPLOY_ID=00001901` / `GIT_SHA=ef4e2de...` ✅，MD5 一致，日志 error=0）。
- 运行实测（未误平）：new_coin 重启日志出现「基线重建完成 {position_count: 4}」→「跳过非本策略持仓」→「持仓基线已同步到 position_tracking {symbols: [USDBRLUSDT]}」→「持仓基线重建完成 {position_count: 1}」；**未出现**「重启基线重建：部分币种 ATR 缺失」（该告警触发条件正是回填后 `atr<=0`）与任何「触发移动止盈/动态利润保护止损」→ 证明 ATR 回填成功且未误平。
- MD5（本地=容器内，new-coin）：`executor.py=71ab506293a9d1b2cb2e1ee8ff39386a`、`strategy.py=a6b43acde5e31be6befcdba7f8ee6fc2`（回滚基线：`d04d173311f443565eaa3965e0e0ad11` / `eedd5b02e977e881618a82d2f2446e5d`）。

## 适用范围与修订
`strategies/new_coin/{executor.py,strategy.py}`；commit `ba0bdf2` 已上线（push origin/main）。同批 handoff 另含 VERSION 改由 CI 构建期写入（`ef4e2de`）与 deploy.yml 残留容器清理口径修复（`6d9ae64`）两项，**不属于本问题**，此处不展开。无替代条目。

## 教训
「重启即丢的内存态」必须显式设计恢复 / 回填路径：任何作为阈值的派生量（ATR 等）在重启后若无法即时重建，要 fail-safe（跳过比较、维持必要状态如最低价），而不是让阈值静默退化到 0。回填要「仅在缺失时补、不覆盖已有有效值」，避免污染条件单恢复路径。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/newcoin_atr_recovery_2026-09-27.md)（不随 Git 同步）
- 持久证据: commit ba0bdf2；部署日志 `docs/reports/new_coin/deployment_log.md`（2026-09-27 条目）
- 必要证据摘要: 572 passed / 1 xfailed / 0 failed；Run #25 DEPLOY_ID 00001901 五层验证通过；重启日志无 ATR 缺失告警、无误平
- 相关: [Binance_quantitative_trading-20260915-newcoin-target2-never-marked.md](./Binance_quantitative_trading-20260915-newcoin-target2-never-marked.md)（同属 new_coin 移动止损链路）
