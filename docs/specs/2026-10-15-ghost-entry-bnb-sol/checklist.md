---
id: checklist-2026-10-15-ghost-entry-bnb-sol
类型: checklist
项目: Binance_quantitative_trading
对应 spec: spec-2026-10-15-ghost-entry-bnb-sol
日期: 2026-10-15
状态: 执行中
---

# 验收检查清单

## 功能类

- [x] AC1：`BNBUSDT` 的 `_open_entry_sql(False)` 命中数 = 0（互斥 owner 为空，8 条幽灵已置 CLOSED）
- [ ] AC1b：清理后 btc_eth 下一 `:05` 轮次对 BNBUSDT 不再推送「开仓被占用互斥拦截」（需线上日志确认）
- [x] AC2：`SOLUSDT` 处置与交易所事实一致（4026 为真仓 → KEPT 保留，互斥继续生效）
- [x] AC3：`--execute` 后 KEPT/LIVE 类记录 status 逐条未变（SOL 分布与执行前逐项一致）

## 代码类

- [x] T2：`--symbol` / `--strategy` 过滤参数化（`build_candidate_query`，无字符串拼接）
- [x] T2b：持仓查询成功但无该币种行 → 归零（不再误判 SKIP，FILLED+已平仓记录可收敛）
- [x] D2：`release_claim` 按 reason 落终态（`position_closed`→CLOSED；`open_failed`/`open_exception`/`micro_zeroed`→CANCELED；未命中不标记）
- [x] D3：终态标记命中 0 行时输出 warning（可观测）
- [x] 无重复代码：终态 SQL 由 `_mark_entry_terminal_sql` 单点构建；终态列表/订单类型引用既有常量
- [x] 无幽灵参数：`_ENTRY_ORDER_TYPES` 已被终态 SQL 构建器使用

## 测试类

- [x] AC4：单测覆盖扩 reason（3 例）、`release_claim_if_no_position` 守卫（2 例）、`position_closed` 不回归
- [x] AC5：`pytest tests/test_shared/test_ghost_entry_cleanup.py` 全绿（70 passed）
- [x] 回归：`tests/test_shared` + `tests/test_position_ownership.py` + `tests/test_mtpcs_fixes/test_r05_r06_mtpcs.py` 全绿

## 发布类

- [ ] AC6：push main → GitHub Actions 构建部署成功
- [ ] AC6-2：五层验证（容器状态 → 镜像 ID → 本轮构建容器 VERSION DEPLOY_ID → 关键文件 MD5 → 部署日志无错）

## 文档类

- [ ] T9：知识条目 / 前轮方案 §10 / handoff 与最终代码一致
