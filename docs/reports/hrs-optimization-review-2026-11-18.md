# HRS 最终 OCR delegate 审查覆盖（2026-11-18）

审查范围：`954ee5f26b05e7ffffe83bf411389087f64cff1b` → `1944bb4bf43695ffb69ad1a1b2c805adf30f5dbf`。使用 `ocr delegate preview --format json` 和 `ocr delegate rule --format json` 解析文件与规则，宿主及已有领域代理执行实际审查，未调用外部 LLM 端点。

- total_files：90；OCR reviewable_files：50；reviewed_files：50；skipped_files：0；coverage_rate：100%。
- OCR 自动排除 40 项，主要为测试、Markdown、Dockerfile；原始排除理由单独列于下方。这不是整仓每一行均经独立双人审查的声明。
- 回放/导出/实验 9 文件逐项复查；风险服务/evidence/reconciliation、门禁提交/取消/崩溃路径、市值身份与范围、发布诊断及最小权限修复有独立复审。验收报告由宿主逐段复查，其最后后台事件处理另经独立复审。
- 关键测试在真实 PostgreSQL 中执行，包括非管理员运行角色、收入/分配审计写入、迁移重跑、重启、原子迁移回滚、候选配置身份及独立留证权限。云端最终候选验证结果另见发布记录，未以定向通过代替实际发布成功。

## 最终复审确认并处理的缺陷

| 问题 | 修复 | 验证 |
| --- | --- | --- |
| 实际运行角色 SELECT FOR UPDATE 无权限，管理员测试未覆盖 | `2a978586` 仅列级 UPDATE 锁权限 + 不可变触发器 | 原版真实角色 2 红 → 修复 2 绿；相关 102 项通过；独立 3 项通过 |
| 主交易角色继承了原始证据表的修改权限 | `2eb9ff78` 撤销所有非 SELECT 能力 | 真实默认 ACL 复现、三角色追加/只读/不可修改验证；27 项通过 |
| 回滚 Compose 空/旧 SHA 覆盖候选镜像版本，导致重试预检阻断 | `1944bb4b` overlay 显式绑定 release_sha，其他 drift 检查不变 | None/旧 SHA 红绿回归，发布集 24 项通过，宿主独立复核 |
| 发布失败丢失初始化原因，回滚后看不到候选状态 | `8061f173` 回滚前白名单状态与固定错误码持久化 | 22 项通过，含真实 PG 原子迁移 |
| 小额只减仓退出被离线模型误报最小名义额违规 | `2193272a` HRS reduce-only close 豁免该项 | 回放/实验/导出等 374 项通过 |
| 后台权益/候选刷新形成假扫描，导致技术验收误失败 | `8dd76700` / `56111762` 区分已知后台组，保留完整性否决 | 实际报告集 49 项通过；独立篡改和非因果时间验证仍拒绝 |
| 候选更新后空池委派 LV，outer 标准扫描缺终止证据 | `970e2e36` 记录明确空池委派终止原因 | 先红后绿；采集+报告联合 77 项；独立实际分支 3 项通过 |

目前审查范围无已确认且未解决的代码缺陷。历史跨月绩效、SL/TP 候选胜出、24 小时和五笔自然成交属于尚未获得合格事实的验收条件，保持未通过。

## 逐文件覆盖

| 路径 | OCR 状态 | 审查状态 |
| --- | --- | --- |
| `.github/workflows/deploy.yml` | modified | reviewed |
| `ai_tuner/hrs_evidence_export.py` | added | reviewed |
| `ai_tuner/hrs_exit_experiment.py` | added | reviewed |
| `ai_tuner/replay/adapters/hrs.py` | modified | reviewed |
| `ai_tuner/replay/broker.py` | modified | reviewed |
| `ai_tuner/replay/clock.py` | modified | reviewed |
| `ai_tuner/replay/data_portal.py` | modified | reviewed |
| `ai_tuner/replay/engine.py` | modified | reviewed |
| `ai_tuner/replay/funding_history.py` | modified | reviewed |
| `ai_tuner/replay/models.py` | modified | reviewed |
| `database/postgres/migrations/2026-11-17-hrs-allocation-event-audit.sql` | added | reviewed |
| `database/postgres/migrations/2026-11-17-hrs-condition-account-scope.sql` | added | reviewed |
| `database/postgres/migrations/2026-11-17-hrs-decision-evidence.sql` | added | reviewed |
| `database/postgres/migrations/2026-11-17-hrs-equity-reconciliation.sql` | added | reviewed |
| `database/postgres/migrations/2026-11-17-hrs-equity-service.sql` | added | reviewed |
| `database/postgres/migrations/2026-11-17-hrs-frozen-exit-plan.sql` | added | reviewed |
| `database/postgres/migrations/2026-11-17-hrs-funding-publication.sql` | added | reviewed |
| `database/postgres/migrations/2026-11-17-hrs-risk-ledger-loss-gates.sql` | added | reviewed |
| `docs/reports/hrs-aug-sep-final-input-audit-2026-11-18.json` | added | reviewed |
| `docs/reports/hrs-exit-experiment-2026-11-17.json` | added | reviewed |
| `docs/reports/hrs-exit-experiment-registration-2026-11-17.json` | added | reviewed |
| `docs/reports/hrs-market-universe-audit-2026-11-17.json` | added | reviewed |
| `docs/reports/hrs-optimization-frozen-baseline-2026-11-17.json` | added | reviewed |
| `docs/reports/hrs-production-source-baseline-2026-11-17.json` | added | reviewed |
| `release/hrs-execution/deploy_optimization.py` | added | reviewed |
| `services/market_cap/assets.json` | modified | reviewed |
| `shared/binance_api.py` | modified | reviewed |
| `shared/entry_risk_policy.py` | modified | reviewed |
| `shared/equity_entry_gate.py` | added | reviewed |
| `shared/order_submission_authorization.py` | added | reviewed |
| `strategies/hrs/acceptance_report.py` | added | reviewed |
| `strategies/hrs/asset_universe.py` | added | reviewed |
| `strategies/hrs/core_decision.py` | added | reviewed |
| `strategies/hrs/decision_capture.py` | added | reviewed |
| `strategies/hrs/decision_context.py` | added | reviewed |
| `strategies/hrs/equity_evidence.py` | added | reviewed |
| `strategies/hrs/equity_reconciliation.py` | added | reviewed |
| `strategies/hrs/equity_service.py` | added | reviewed |
| `strategies/hrs/executor.py` | modified | reviewed |
| `strategies/hrs/exit_geometry.py` | added | reviewed |
| `strategies/hrs/loss_gate.py` | added | reviewed |
| `strategies/hrs/loss_gate_repository.py` | added | reviewed |
| `strategies/hrs/main.py` | modified | reviewed |
| `strategies/hrs/market_data.py` | modified | reviewed |
| `strategies/hrs/position_exit_plan.py` | added | reviewed |
| `strategies/hrs/position_manager.py` | modified | reviewed |
| `strategies/hrs/risk_budget.py` | modified | reviewed |
| `strategies/hrs/risk_ledger.py` | added | reviewed |
| `strategies/hrs/scoring_engine.py` | modified | reviewed |
| `strategies/hrs/strategy.py` | modified | reviewed |

## OCR 排除项

- `docs/hrs-evidence-export.md`：OCR policy exclusion
- `docs/reports/hrs-aug-sep-final-input-audit-2026-11-18.md`：OCR policy exclusion
- `docs/reports/hrs-exit-experiment-2026-11-17.md`：OCR policy exclusion
- `docs/reports/hrs-funding-source-contract-2026-11-18.md`：OCR policy exclusion
- `docs/reports/hrs-market-universe-audit-2026-11-17.md`：OCR policy exclusion
- `docs/reports/hrs-optimization-rollout-2026-11-18.md`：OCR policy exclusion
- `docs/superpowers/plans/2026-11-17-hrs-complete-optimization.md`：OCR policy exclusion
- `docs/superpowers/specs/2026-11-17-hrs-remaining-optimization-design.md`：OCR policy exclusion
- `findings.md`：OCR policy exclusion
- `progress.md`：OCR policy exclusion
- `release/hrs-execution/Optimization.Dockerfile`：OCR policy exclusion
- `release/hrs-execution/Optimization.Dockerfile.dockerignore`：OCR policy exclusion
- `strategies/hrs/tests/test_asset_universe.py`：OCR policy exclusion
- `strategies/hrs/tests/test_emm_input_contract.py`：OCR policy exclusion
- `task_plan.md`：OCR policy exclusion
- `tests/test_ai_tuner_mvp/replay/test_adapters_hrs.py`：OCR policy exclusion
- `tests/test_ai_tuner_mvp/replay/test_clock_engine_determinism.py`：OCR policy exclusion
- `tests/test_ai_tuner_mvp/replay/test_hrs_replay_review.py`：OCR policy exclusion
- `tests/test_ai_tuner_mvp/replay/test_hrs_wiring.py`：OCR policy exclusion
- `tests/test_ai_tuner_mvp/test_hrs_evidence_export.py`：OCR policy exclusion
- `tests/test_ai_tuner_mvp/test_hrs_exit_experiment.py`：OCR policy exclusion
- `tests/test_release/test_hrs_optimization_deploy.py`：OCR policy exclusion
- `tests/test_risk/test_entry_policy.py`：OCR policy exclusion
- `tests/test_shared/test_equity_entry_gate.py`：OCR policy exclusion
- `tests/test_shared/test_hrs_final_submit_guard.py`：OCR policy exclusion
- `tests/test_shared/test_hrs_projection_submission.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_acceptance_report.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_decision_capture.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_decision_context.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_equity_evidence.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_equity_reconciliation.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_equity_runtime_permissions.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_equity_service.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_frozen_exit_plan.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_loss_gate.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_loss_gate_repository.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_main_equity_lifecycle.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_replay_core.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_risk_ledger.py`：OCR policy exclusion
- `tests/test_strategies/test_hrs_safety_review_regressions.py`：OCR policy exclusion
