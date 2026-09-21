# new_coin 持仓基线归属过滤（P0 热修）

- 时间: 2026-09-21 11:45
- 涉及容器: trading_system-new_coin（按需重建）

## 问题
资金限额强制执行上线后，`_rebuild_position_baseline` 重建出 8 个币种、total_margin=348.8515，
但其中仅 APLDUSDT / AMCUSDT / PATHUSDT 属 new_coin。

双重危害：
1. `calc_current_occupied_margin()` 默认取 `position_tracking.keys()` → 占用虚增 348.85 > 限额 156.33 → 永久停止开仓
2. `_sync_baseline_to_tracking` 为外来币种建 tracking 条目 → `check_position_management` 守卫失效 → 越权管理他策略仓位（可能取消他策略条件单）

根因：PM 投资组合保证金账户 `positionRisk` 返回账户内全部空头，无策略隔离能力。

## 修复
- `strategies/new_coin/executor.py`：新增 `get_open_short_symbols()`，以 DB `new_coin.short_positions`（status='open'）为「本策略自有币种」权威来源，异常返回 None
- `strategies/new_coin/strategy.py`：`_rebuild_position_baseline` 中插入 `_filter_own_positions()`，过滤后再合并；DB 不可用降级本地记录，本地亦空则跳过并告警
- 单测：新增 `TestOwnPositionFilter` 4 用例；`tests/test_strategies/` 551 passed / 1 xfailed

## 止血
10:52 CST `docker stop trading_system-new_coin` 阻断 03:00 UTC 周期；
核验旧容器 02:42 启动 → 02:52 停止，日志停在「等待 1071 秒到 03:00」，周期未执行，无越权行为。
PATHUSDT 按指令减仓 21.99 → 7.33 张。

## 部署验证（五层全通过）
- L1 Up (healthy)；L2 container == latest == sha256:ddc5e03d...940fd6
- L3 容器内 DEPLOY_ID=59C00584 == 本地；L4 5/5 MD5 匹配；L5 错误 0
- 生效证据：`position_count=3, total_margin=154.217`，`position_tracking` 仅 3 个自有币种
- 部署确认报告：`/tmp/deploy_report_new_coin_p0_20260921.md`