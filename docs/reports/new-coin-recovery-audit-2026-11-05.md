# 新币重启恢复验收（2026-11-05）

分支：`codex/new-coin-recovery-audit`。仅本地修复与测试；未访问生产、发送交易、推送或部署。

## 行为与边界

- 重启不再以当前时间冒充开仓时间。唯一且数值一致的 `new_coin.short_positions` open 行仅提供**候选**开仓时间和初始数量；同币种、同价、同量不能证明当前交易所仓位与该记录连续。
- 所有恢复仓位均保持 `review_required` 或 `unknown`，包括恢复时尚未满 72 小时的仓位；时间止损分支只节流告警，不调用复核或自动平仓。当前实现没有自动解除该状态的路径，须另行核实成交链并审查后续恢复方案。
- PFE 验收使用 DB quantity `3.28`、entry_price `30.41271340`、opened_at `2026-10-30 11:01:44.496097`，交易所同价同量。按既有 naive UTC 写入契约解析时间，恢复仅获得候选时间；即使超过 72 小时且模拟复核返回平仓，仍零时间退出。
- 数量下降仅生成 `recovery_target1_candidate` / `recovery_target2_candidate`，不作为 TP 成交证明，不直接置实际 target 标志。DB 缺失、多条、未来时间、非有限数值、数量/价格矛盾、估算交易所价格或精度证据缺失均保守处理。
- `emergency_entry_time` 是独立的紧急价格管理兼容时钟，**不是开仓证据**，不得用于 72 小时退出。完整持仓管理与保护守卫测试确认紧急价格、动态利润、移动止盈和保护单检查仍可运行；新开仓原有时间规则保持不变。
- 恢复阶段零平仓、零撤单；交易所重建失败也在网络请求前将已有恢复条目的时间退出置为 unknown。数量、保证金及既有保护状态的其他管理路径不加总开关。

## 本地验证

先写失败用例并观察原实现失败，再修复；随后按审查收紧所有恢复仓的时间退出授权，并再次观察新增用例失败后修复。修正两处旧 tracking 测试遗漏的 `await`。

```sh
python3 -m pytest tests/test_strategies/test_new_coin_recovery_evidence.py tests/test_strategies/test_new_coin_tracking_entry.py tests/test_strategies/test_time_stop_review.py tests/test_strategies/test_capital_allocation_enforcement.py -q --tb=short -p no:cacheprovider
```

结果：**209 passed**。`git diff --check` 通过。

两个旧 TP 检测失败均在基线复现：

- `test_take_profit_fill_detect.py::TestDetectTakeProfitFills::test_target1_fill_detected`
- `test_take_profit_fill_detect.py::TestDetectTakeProfitFills::test_target2_fill_detected`

基线复现方式：在独立 Python 测试进程中将 `git show HEAD:strategies/new_coin/{executor,strategy}.py` 原始源码加载回各模块，再运行原测试，未修改工作树源码。这两例的既有期望没有提供当前检测器要求的减仓来源证据，因此返回 None。扩大范围运行 `test_new_coin_reconcile_guard_p0a.py`、`test_take_profit_fill_detect.py`、`test_executor_trailing.py` 时，基线合计为 **71 passed / 8 failed**，包含上述两例以及守卫参数/开仓旧 mock 相关六例；本次未改动这些测试或把全套回归宣称通过。

## 残余风险

生产 DB 的 naive 时间时区与当前仓位连续性尚未由本任务独立验证；候选时间不能用来授权平仓。未知/待复核仓位的自动时间退出会持续暂停，普通价格保护仍可能按既有规则执行交易。未完成生产、真实交易所成交链或真实数据库集成验收；本报告不构成部署或恢复自动时间退出的授权。
