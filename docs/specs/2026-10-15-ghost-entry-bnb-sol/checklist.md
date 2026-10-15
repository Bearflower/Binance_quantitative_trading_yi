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
- [x] AC1b：清理后 btc_eth 下一 `:05` 轮次对 BNBUSDT 不再推送「开仓被占用互斥拦截」（北京 16:05 轮次全量日志互斥条数=0；该轮 5 币均在信号阶段被拒、未走到互斥判定，属弱证据）→ 补 SQL 直查 `BNBUSDT` 互斥 owner 命中数=0 作为因果证据
- [x] AC2：`SOLUSDT` 处置与交易所事实一致（4026 为真仓 → KEPT 保留，避免误标）；**更正**：该次拦截并非由 4026 触发（真仓 11:10 才开、**晚于 11:05 告警**），11:05 时点 owner = id=3926 幽灵 → 同属幽灵误拦，属缺陷
- [x] AC2-exec（2026-10-15 17:32，用户手动平仓后）：SOLUSDT 空仓窗口 dry-run `CLOSED 49 / KEPT 0 / LIVE 0 / SKIP 0` → `--execute --symbol SOLUSDT` `CLOSED=49`；复检互斥 SQL 命中数 = 0，49 条逐条 CLOSED；其他币种 owner 概览不变
- [x] AC3：`--execute` 后 KEPT/LIVE 类记录 status 逐条未变（BNB 轮次 SOL 分布与执行前逐项一致；SOL 清理轮次 `--symbol` 限定生效，非目标行未动）

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

- [x] AC6：push main → GitHub Actions 构建部署成功（Run 76；deploy_logs `2026-10-15 15:08:08 DEPLOY_SUCCESS commit=8071f0696f92d89c97b5f25fe00339f6259b795d deploy_id=00004C01`）
- [x] AC6-2：五层验证（容器状态 → 镜像 ID → 本轮构建容器 VERSION DEPLOY_ID → 关键文件 MD5 → 部署日志无错）
      L1 11 容器全部 `ghcr.io/bearflower/trading-*:latest` 且 Up 12 min (healthy)；
      L2 镜像 ID 已记录（如 btc_eth `sha256:b177c6be…`）；
      L3 11 容器 `/app/VERSION` 全为 `GIT_SHA=8071f0696f92d89c97b5f25fe00339f6259b795d` / `DEPLOY_ID=00004C01`，与 deploy_logs 的 `deploy_id` 一致；
      L4 `/app/shared/position_ownership.py` 容器内 MD5 `d842463fe38d792669820b56b1fa4ff4` = 本地 `md5 -q`；
      L5 今日部署日志无 error/failed/冲突关键字，`DEPLOY_SUCCESS` 行存在

## 文档类

- [x] T9：知识条目 / 前轮方案 §10 / handoff 与最终代码一致（方案 §10.1–§10.9；issue/decision/changelog/INDEX 幂等合并；handoff `docs/handoffs/ghost_entry_bnb_sol_2026-10-15.md`；AC1b 已通过；SOL 11:05 拦截更正为幽灵误拦；SOL 存量清理执行 `49→CLOSED` 记录于 spec §10.5）
