---
id: Binance_quantitative_trading-20261015-newcoin-legacy-test-debt
类型: issue
项目: Binance_quantitative_trading
标题: new_coin 既有测试 25 项长期失败（测试债）：MagicMock fixture 被 await + 3 处陈旧断言
日期: 2026-10-15
标签: [测试债, new_coin, 测试, AsyncMock, 断言脱节]
任务标识: newcoin_legacy_test_debt_2026-10-15
状态: 待复查
审查: 待审查
---

# new_coin 既有测试 25 项长期失败（测试债）：MagicMock fixture 被 await + 3 处陈旧断言

## 现象
`strategies/new_coin/` 测试套件存在 **25 个长期失败的既有用例**，与任何单次修复无关，属存量「测试债」。

- HEAD 干净副本（`fce42bf`）跑 `pytest strategies/new_coin/ -q` → `25 failed, 163 passed`
- 当前工作区（含在途改动）→ `25 failed, 176 passed`（其中 +13 通过用例为另一任务新增）
- 两份失败清单逐字节一致（`cmp` 结果 IDENTICAL），即**在途改动未改变失败集合**，失败确为存量。

失败用例清单（25 项，按文件）：
- `strategies/new_coin/test_realized_pnl_fix.py`（12）
  - `TestNewCoinSQLFix::test_close_order_has_no_order_id`
  - `TestNewCoinSQLFix::test_close_order_multiple_records_limit_1`
  - `TestNewCoinSQLFix::test_close_order_no_record_found`
  - `TestNewCoinSQLFix::test_close_order_sql_has_between_and_not_null`
  - `TestNewCoinSQLFix::test_close_order_sql_has_limit_1`
  - `TestNewCoinSQLFix::test_close_order_sql_has_order_id_not_null`
  - `TestRealizedPnlFix::test_post_close_log_when_entry_time_missing`
  - `TestRealizedPnlFix::test_post_close_skip_when_pnl_none`
  - `TestRealizedPnlFix::test_post_close_skip_when_trade_logger_none`
  - `TestRealizedPnlFix::test_post_close_update_realized_pnl`
  - `TestRealizedPnlFix::test_update_realized_pnl_success`
  - `TestUpdateRealizedPnlEnhanced::test_fallback_sql_correctness`
- `strategies/new_coin/test_drawdown_fix.py::TestDrawdownFix`（8）
  - `test_post_close_consecutive_losses_increment_when_pnl_negative`
  - `test_post_close_consecutive_losses_reset_when_pnl_positive`
  - `test_post_close_skip_when_pnl_none`
  - `test_restore_state_drawdown_pause_loaded`
  - `test_restore_state_new_version_loads_peak`
  - `test_restore_state_new_version_none_peak`
  - `test_restore_state_old_field_name_compatible`
  - `test_restore_state_old_version_resets_peak`
- `strategies/new_coin/tests/test_stop_loss_mark.py`（4）
  - `test_止损打标_亏损平仓触发`
  - `test_止损打标_平仓后清理持仓`
  - `test_止损打标_pnl为None但平仓单为亏损_补正打标`
  - `test_止损打标_pnl为None且无法判定_不打标仅清理`
- `strategies/new_coin/test_delisted_symbols.py`（1）
  - `test_delisted_symbol_first_call_caches_and_logs`

> 与任务简报的差异：简报推测失败分布含 `tests/test_p0_replenish_and_registration.py`、`tests/test_cycle_open_positions_report.py`，**实测清单中并无这两个文件的失败**；实际分布以上表（依据 `/tmp/cur_fail.txt`）为准。

## 根因
两类，均已用基线副本逐用例复现回溯（见「验证」）：

### 一、MagicMock fixture 被 `await`（22/25）
生产代码演化后新增了对执行器方法的 `await`，而多个既有测试的 fixture 用 `s.trading_executor = MagicMock()`（其方法为 `MagicMock`，不可 `await`），触发 `TypeError: object MagicMock can't be used in 'await' expression`。

- 触发点 A：`NewCoinStrategy._monitor_positions` 的「持仓已平仓」分支 `await self.trading_executor._update_short_position_closed(symbol=symbol)`（工作区 `strategies/new_coin/strategy.py` 约 1418 行；方法定义于 `strategies/new_coin/executor.py` 约 1459 行，另有 executor.py 2828/2854 的同类调用）。
- 触发点 B：`NewCoinStrategy._restore_state` 末尾 `await self._rebuild_position_baseline()`（`fce42bf` 副本约 1964 行），内部 `await executor.get_contract_size_map()`（`fce42bf` 副本 `strategy.py` 约 1983 行）——即回溯中 `TypeError` 的落点。
- fixture 特征：仅把 `check_position_management` / `cancel_all_algo_orders` 换成了 `AsyncMock`，**未**把 `_update_short_position_closed` / `get_contract_size_map` 换成 `AsyncMock`。涉及 fixture：[test_realized_pnl_fix.py](../../../strategies/new_coin/test_realized_pnl_fix.py) 约 89/760/1104 行、[test_drawdown_fix.py](../../../strategies/new_coin/test_drawdown_fix.py) 约 67 行、[tests/test_stop_loss_mark.py](../../../strategies/new_coin/tests/test_stop_loss_mark.py) 约 28 行。
- 失败表现分两态（同一根因）：
  1. `_monitor_positions` 的逐币 `try/except Exception`（工作区 `strategy.py` 约 1591 行）把 `TypeError` **吞掉并记 logger.error**，故测试只见下游断言失败：持仓未清理（`assert 'BTCUSDT' not in positions`）、`consecutive_losses` 未更新（`assert 1 == 2` / `assert 3 == 0`）、`update_realized_pnl` 未被调用（`Expected ... to have been called once. Called 0 times`）、`db.fetch_one.call_args` 为 `None` 导致 `AttributeError: 'NoneType' object has no attribute 'args'`。
  2. `_restore_state` 被测试直接调用时 `TypeError` 向上抛出（5 个 `test_restore_state_*`）。

### 二、陈旧断言与生产脱节（3/25）
1. `test_realized_pnl_fix.py` 约 131 行（`TestRealizedPnlFix::test_update_realized_pnl_success`）：断言 `call_args.args[1] == "100.50"`（字符串），实测 `Decimal('100.50') == '100.50'` 为 False。生产 [shared/trade_logger.py](../../../shared/trade_logger.py) 的 `update_realized_pnl` 现已直接把 `Decimal`（类型标注 `realized_pnl: Decimal`）传给 `db.execute`，不再 `str()`。
2. `test_realized_pnl_fix.py` 约 512/516 行（`TestUpdateRealizedPnlEnhanced::test_fallback_sql_correctness`）：断言降级匹配 SQL 含 `SELECT id FROM trading.trade_records`；生产已改为带别名 `SELECT tr.id FROM trading.trade_records tr`（[shared/trade_logger.py](../../../shared/trade_logger.py) 降级匹配分支）。
3. `test_delisted_symbols.py` 约 53 行（`test_delisted_symbol_first_call_caches_and_logs`）：断言 info 日志含 `币种已下架`；生产 [ListingDetector._fetch_oi_for_symbol](../../../strategies/new_coin/detector.py) 现输出 `币种无效（无法获取合约数据），加入跳过列表: {symbol}`。全库 grep 确认 `币种已下架` 仅存于该测试文件，生产已无此文案。（本条为本次核对**新增发现**，任务简报只列了前 2 条陈旧断言。）

## 解决
**未解决（本条目为「另行登记」，登记时不修改任何生产/测试代码）**。修复处置见下：

- 作为**独立任务**择机修复，不与任何单次修复（如在途的 new_coin 时区修复）混做。
- 优先统一：在 fixture 中把会被 `await` 的执行器方法（`_update_short_position_closed`、`get_contract_size_map` 等）替换为 `AsyncMock`；再逐个复核陈旧断言是否仍代表期望的生产行为（若生产行为正确则更新断言，若生产行为错误则另立修复）。
- **禁止**以删除断言、放宽门槛或 `skip` 的方式「消除」失败。

## 验证
基线只读复现（不使用任何 git 写操作）：
```bash
rm -rf /tmp/nc_baseline && mkdir -p /tmp/nc_baseline
git archive HEAD | tar -x -C /tmp/nc_baseline     # 取 HEAD(fce42bf) 干净副本
cd /tmp/nc_baseline && python3 -m pytest strategies/new_coin/ -q
```
- 基线（`fce42bf`）：`25 failed, 163 passed`
- 工作区：`25 failed, 176 passed`
- `cmp /tmp/base_fail.txt /tmp/cur_fail.txt` → IDENTICAL（失败集合不受在途改动影响，证实为存量测试债）
- 逐文件复跑基线确认根因：`test_drawdown_fix.py + test_delisted_symbols.py + tests/test_stop_loss_mark.py` → `13 failed, 25 passed`；`test_realized_pnl_fix.py` → `12 failed, 27 passed`。回溯显示：`strategy.py:1983 TypeError`（5×，restore 分支）、`strategy.py` 逐币 `try/except` 吞掉的 TypeError（下游断言失败）、`assert Decimal('100.50') == '100.50'`（陈旧断言）、`assert 'SELECT id FROM trading.trade_records' in "…SELECT tr.id … tr…"`（陈旧断言）、`assert '币种已下架' in '币种无效（无法获取合约数据），加入跳过列表: BTCU'`（陈旧断言）。
- 待验证项：修复任务完成后须重跑 `strategies/new_coin/` 全量，确认 25 个失败全部转绿且不引入新增失败；需逐项确认「更新后的断言」代表期望的生产行为（而非仅迁就现状）。

## 适用范围与修订
- 仅适用于 `strategies/new_coin/` 的测试套件，基线为 `fce42bf`（2026-10-15 复核）。
- 与在途的 new_coin 时区修复（新增 `_entry_time_to_naive_beijing`）**无关**：该改动只增加了 13 个通过用例，失败集合不变（`cmp` 无差异），故不得把本测试债并入该修复的结论。
- 增量补充（2026-10-15，R2 时区修复发布后）：该修复已提交 `94ca3ed` 并按「新增/受影响测试通过 + 基线失败集合不扩大（逐条同名）」的 AC5 判定通过——基线 `25 failed, 163 passed` → 工作区 `25 failed, 176 passed`，失败集合逐条同名，**本测试债不阻塞 R2 发布**；本条目状态维持「待复查」，仍待独立任务修复至 25→0。
- `_update_short_position_closed`（B1 修复）与 `_rebuild_position_baseline`（R4 基线重建）为后续演化新增的 `await` 调用点，是本组失败的引入源；行号以对应提交/工作区读数为准，函数名定位优先。
- 无替代条目。

## 教训
1. **fixture 用 `MagicMock` 是 await 定时炸弹**：一旦生产给某个执行器方法加了 `await`，所有未同步换 `AsyncMock` 的既有测试会静默失效；且若该调用处于 `try/except Exception` 内，失败会伪装成下游「断言不符」，浪费大量排查时间。改造生产调用链新增 `await` 时，必须同批核对单测 fixture。
2. **坚持「绿色基线」关卡**：接手任务前先在干净副本（只读方式）跑一遍目标模块测试，记录失败集合与数量，才能把「存量测试债」与「本次引入的失败」分开；否则测红时无法判断是否回归。
3. **断言应表达期望而非复述实现**：本组 3 处陈旧断言（参数类型、SQL 别名、日志文案）都是「照抄当时实现细节」，生产一改就失效；涉及文案/别名/类型时应断言语义不变式，而非字符串/类型字面量。
（AI 归纳，仅供参考）

## 关联
- 本地补充来源: 无 handoff（本次为轻量登记，未落交接文件）
- 持久证据: 失败清单 `/tmp/base_fail.txt`（25 行）、`/tmp/cur_fail.txt`（25 行，`cmp` IDENTICAL，随机器重启失效，清单已完整转录于本条目「现象」）；基线只读复现命令见「验证」；`fce42bf` 各文件函数：`NewCoinStrategy._monitor_positions` / `_restore_state` / `_rebuild_position_baseline`（`strategies/new_coin/strategy.py`）、`ListingDetector._fetch_oi_for_symbol`（`strategies/new_coin/detector.py`）、`update_realized_pnl`（`shared/trade_logger.py`）
- 必要证据摘要: 22/25 为 MagicMock fixture 未同步 `AsyncMock` 致 `TypeError: object MagicMock can't be used in 'await' expression`（触发点 `_update_short_position_closed` 与 `get_contract_size_map`）；3/25 为陈旧断言（参数 `Decimal` vs 字符串、SQL 别名 `tr`、日志文案）。基线 `25 failed, 163 passed`，工作区 `25 failed, 176 passed`，失败集合一致。待验证项：修复后 25→0 且无新增失败。
- 相关: [Binance_quantitative_trading-20260922-shared-account-position-ownership](../issues/Binance_quantitative_trading-20260922-shared-account-position-ownership.md)（R4 持仓基线重建 `_rebuild_position_baseline` 与 PM 账户归属过滤同源，其失败触发点亦来自该改造）
