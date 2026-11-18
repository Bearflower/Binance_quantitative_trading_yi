# HRS 最终 OCR delegate 审查覆盖（2026-11-18）

审查范围：`954ee5f26b05e7ffffe83bf411389087f64cff1b` → `bfcef846088d2efc4fc2ec79d38c172a2873c555`。使用 `ocr delegate preview --format json` 与 `ocr delegate rule --format json` 解析文件/规则，宿主及领域代理执行实际审查，未调用外部 LLM 端点。

- total_files：109；reviewable_files：59；reviewed_files：59；skipped_files：0；coverage_rate：100%。
- OCR自动排除50项，理由逐项列于下方；相关测试另有实际执行，不能把此覆盖率称为整仓每行独立双审。
- 旧风险/锁/提交/采集/回放/市值/发布代码的审查记录保留。新增归档、资金费引用与离线来源闭包逐文件复审；最终独立293项、实现域459项通过，额外真实PG两种字段删除探针与5000refs单次source遍历验证通过。修复已确认的完整性和复杂度缺陷后再发布。
- 合成大体积和真实隔离PG用例属于工程证据，不能替代生产收益。云端候选与实盘核验以发布记录为准。

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

| 原始 claim 时间导致真实扫描失败 | e2b6cdc1/132321d4 保留数据库原始表示，启动核验真实上下文 | 3436656d生产热修、1157云端测试及实际自然扫描通过 |
| 月度证据超旧单体容量 | 17c3411a/fff8f040 完整CAS分片、独立COUNT/cursor与lazy转换 | 416项领域测试、119项独立复审、>50k/>64MiB真实CLI链路 |
| 九项自然扫描身份缺失 | 0431f49c核对唯一资产ID，保留分母，CMC未校验者禁用 | 34项映射/服务测试，备用候选1167项通过；实际发布状态见rollout |
| forecast累计全历史复制，存储二次增长 | ebe17ee7新parent常数引用，旧payload/hash保留 | 实际生产460条原始source只读校验通过、ref628bytes；原始receipt闭包保留 |
| 每parent重复全prefix校验，月窗读取二次增长 | acd97fbe共享一次增量校验，历史parent cursor并只保留必要view | 独立5000refs/5000source仅一遍读取；完整rawparent仍先hash |
| 删除历史capture/ledger_version可绕过raw hash校验 | f1fb1ef4在任何shape筛选前先校验完整原parent | 两种真实PG独立红绿探针：source查询0、ledger仍2、无修订receipt |

当前审查范围无已确认且未修复的代码缺陷。8–9月反事实、SL/TP参数胜出及未来24小时/五个自然闭合仓位仍未通过，不能以工程测试代替绩效。

封存后的新增最终发现JSON已逐字段核对：真实28个runtime source身份AVAILABLE、BLOCKED_DATA、0 engine runs、无选择、未接受绩效。候选workflow仅改文件名、原deploy恢复分支基线；YAML和原始字节核对通过，441559ae已测试运行源码未改。

## 逐文件覆盖

| 路径 | OCR状态 | 审查状态 |
| --- | --- | --- |
| `.github/workflows/hrs-optimization-candidate.yml` | added | reviewed |
| `ai_tuner/hrs_evidence_export.py` | added | reviewed |
| `ai_tuner/hrs_exit_experiment.py` | added | reviewed |
| `ai_tuner/hrs_forecast_sources.py` | added | reviewed |
| `ai_tuner/replay/adapters/hrs.py` | modified | reviewed |
| `ai_tuner/replay/broker.py` | modified | reviewed |
| `ai_tuner/replay/clock.py` | modified | reviewed |
| `ai_tuner/replay/data_portal.py` | modified | reviewed |
| `ai_tuner/replay/engine.py` | modified | reviewed |
| `ai_tuner/replay/funding_history.py` | modified | reviewed |
| `ai_tuner/replay/hrs_evidence_archive.py` | added | reviewed |
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
| `docs/reports/hrs-exit-experiment-discovery-2026-11-18-final.json` | added | reviewed |
| `docs/reports/hrs-exit-experiment-discovery-2026-11-18-v2.json` | added | reviewed |
| `docs/reports/hrs-exit-experiment-discovery-2026-11-18-v3.json` | added | reviewed |
| `docs/reports/hrs-exit-experiment-registration-2026-11-17.json` | added | reviewed |
| `docs/reports/hrs-exit-experiment-registration-2026-11-18-v2.json` | added | reviewed |
| `docs/reports/hrs-exit-experiment-registration-2026-11-18-v3.json` | added | reviewed |
| `docs/reports/hrs-market-universe-audit-2026-11-17.json` | added | reviewed |
| `docs/reports/hrs-optimization-frozen-baseline-2026-11-17.json` | added | reviewed |
| `docs/reports/hrs-production-optimization-status-2026-11-18.json` | added | reviewed |
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
| `strategies/hrs/funding_reference.py` | added | reviewed |
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

## OCR排除项

- `docs/hrs-evidence-export.md`：unsupported_ext
- `docs/reports/hrs-aug-sep-final-input-audit-2026-11-18.md`：unsupported_ext
- `docs/reports/hrs-exit-experiment-2026-11-17.md`：unsupported_ext
- `docs/reports/hrs-exit-experiment-revision-2026-11-18-v2.md`：unsupported_ext
- `docs/reports/hrs-funding-source-contract-2026-11-18.md`：unsupported_ext
- `docs/reports/hrs-market-cap-nine-identities-2026-11-18.md`：unsupported_ext
- `docs/reports/hrs-market-universe-audit-2026-11-17.md`：unsupported_ext
- `docs/reports/hrs-monthly-evidence-archive-2026-11-18.md`：unsupported_ext
- `docs/reports/hrs-optimization-review-2026-11-18.md`：unsupported_ext
- `docs/reports/hrs-optimization-rollout-2026-11-18.md`：unsupported_ext
- `docs/superpowers/plans/2026-11-17-hrs-complete-optimization.md`：unsupported_ext
- `docs/superpowers/plans/2026-11-18-hrs-monthly-evidence-archive.md`：unsupported_ext
- `docs/superpowers/specs/2026-11-17-hrs-remaining-optimization-design.md`：unsupported_ext
- `docs/superpowers/specs/2026-11-18-hrs-funding-forecast-reference.md`：unsupported_ext
- `findings.md`：unsupported_ext
- `progress.md`：unsupported_ext
- `release/hrs-execution/Optimization.Dockerfile`：unsupported_ext
- `release/hrs-execution/Optimization.Dockerfile.dockerignore`：unsupported_ext
- `strategies/hrs/tests/test_asset_universe.py`：default_path
- `strategies/hrs/tests/test_emm_input_contract.py`：default_path
- `task_plan.md`：unsupported_ext
- `tests/test_ai_tuner_mvp/replay/test_adapters_hrs.py`：default_path
- `tests/test_ai_tuner_mvp/replay/test_clock_engine_determinism.py`：default_path
- `tests/test_ai_tuner_mvp/replay/test_funding_history.py`：default_path
- `tests/test_ai_tuner_mvp/replay/test_hrs_replay_review.py`：default_path
- `tests/test_ai_tuner_mvp/replay/test_hrs_wiring.py`：default_path
- `tests/test_ai_tuner_mvp/test_hrs_evidence_archive.py`：default_path
- `tests/test_ai_tuner_mvp/test_hrs_evidence_export.py`：default_path
- `tests/test_ai_tuner_mvp/test_hrs_exit_experiment.py`：default_path
- `tests/test_ai_tuner_mvp/test_hrs_forecast_closure.py`：default_path
- `tests/test_release/test_hrs_optimization_deploy.py`：default_path
- `tests/test_risk/test_entry_policy.py`：default_path
- `tests/test_shared/test_equity_entry_gate.py`：default_path
- `tests/test_shared/test_hrs_final_submit_guard.py`：default_path
- `tests/test_shared/test_hrs_projection_submission.py`：default_path
- `tests/test_strategies/test_hrs_acceptance_report.py`：default_path
- `tests/test_strategies/test_hrs_decision_capture.py`：default_path
- `tests/test_strategies/test_hrs_decision_context.py`：default_path
- `tests/test_strategies/test_hrs_equity_evidence.py`：default_path
- `tests/test_strategies/test_hrs_equity_reconciliation.py`：default_path
- `tests/test_strategies/test_hrs_equity_runtime_permissions.py`：default_path
- `tests/test_strategies/test_hrs_equity_service.py`：default_path
- `tests/test_strategies/test_hrs_frozen_exit_plan.py`：default_path
- `tests/test_strategies/test_hrs_funding_reference.py`：default_path
- `tests/test_strategies/test_hrs_loss_gate.py`：default_path
- `tests/test_strategies/test_hrs_loss_gate_repository.py`：default_path
- `tests/test_strategies/test_hrs_main_equity_lifecycle.py`：default_path
- `tests/test_strategies/test_hrs_replay_core.py`：default_path
- `tests/test_strategies/test_hrs_risk_ledger.py`：default_path
- `tests/test_strategies/test_hrs_safety_review_regressions.py`：default_path
