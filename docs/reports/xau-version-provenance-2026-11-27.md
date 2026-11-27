# XAU 版本追踪修复与复查（2026-11-27）

状态：实现、本地测试与独立上下文复审完成，可按审定方案受限发布；尚未部署。风险 R2：只改影子事件版本身份和 run 分段，不改信号、参数或交易行为。沿用用户已授权的连续修复、测试后部署范围。

## 生产事实与根因

北京时间 21:36 只读检查：三个 profile 自 11/26 02:00 UTC 起各有 36 条事件，全部 no_signal/no_candidate，质量正常。资金费 11/26 01:45 UTC 至 11/27 13:30 UTC 共 430 个应到槽位无缺失、无失败；最新 13:35 UTC 槽成功。四张 production 表中本实例/profile 仍为零。

后续全量发布的 `c4a0bea5b9304779e4d64a526865541afce764a6` 镜像 VERSION 只有 GIT_SHA，没有 GIT_COMMIT。11/27 03:00 UTC 起每个 profile 的 11 条事件（共 33 条）git_commit 与 producer_git_commit 为空。active run 仍标记 `acee4c82dfefda216ee35b365533284d8ada44ee`，与现役镜像不符。

真实代码路径 `_read_git_info → run_forever → _prepare_shadow_run / ShadowCollector` 中，读取器没有 SHA 回退，run 复用只比较 commit 与配置；两个空 commit 导致版本变化未分段。归因状态本身持续落在 `research.shadow_decision_events.feature_snapshot`，并未丢失。

## 修复与验收契约

- 只有 GIT_SHA 时用其补齐 git_commit；保留原有显式 commit 和环境优先语义。
- 只有已知版本、commit、SHA、配置哈希都匹配，才复用 active run；缺版本或版本变化关闭旧 run、建立新 run。历史事件不更新、不删除，也不推断缺失版本。
- 现役底图固定为 `ghcr.io/bearflower/trading-btc-eth@sha256:80a5d2fc78f768f659f9ba5855013704ea3628f691d2afe9c48ed6550488962c`；仅叠加 collector.py、main.py、VERSION。只更新两个影子容器，资金费和其他容器纳入排除保护。
- 后续全量发布已去掉 calibrated 的 main.py 绑定，但 Compose 标签仍指向旧快照。仅清除“已不在实际容器上、来源精确匹配 603a2a4e 发布目录、只读”的陈旧声明；未知/仍在使用的绑定拒绝发布，其他运行配置保留。
- 权限清单、审计工具、规则、说明从已提交生产源 c4a0bea5 原样移入当前 main 基线（本地原工作区未提交文件不变）。CI 随本次 SHA 打包并记录两个审计文件哈希；激活前验证并执行包内 audit，不执行 apply、不覆盖服务器根目录契约。此轮无 DDL/GRANT。

## 验证证据

- 新增版本用例先在旧实现得到 5 failed / 4 passed，修复后全绿；最终 13 项覆盖 SHA-only env/file、SHA 独立变化、未知身份、配置独立变化，以及三个 profile 的真实入口→collector→普通/候选事件两条路径。
- 发布范围与旧挂载守护先失败再修复；审计包校验另有失败复现及篡改拒绝用例。
- 完整相关组合：544 passed、1 skipped；其中真实 Unix socket PostgreSQL 权限契约测试 18 项通过。跳过的资金费 PostgreSQL 测试与 SHA-only 归因持久化测试另行运行：2 passed。归因测试写入 15 条三 profile 候选/无信号事件，写入进程退出后独立读库并导出，事件、producer 与 run 的 SHA 一致。
- 初次移入来源测试文件包含 5 项依赖尚未并入 main 的 classic 账户业务集成，因缺对应迁移/业务版本失败。本次仅移入该文件中 18 项权限工具/契约测试；不修改或降低 classic 业务测试，来源分支仍保留。没有把这 5 项称为通过。
- 生产候选契约只读预审：1,355 对象、0 issues。两目标现役镜像、Env、挂载与资源约束预检通过。语法编译和 git diff --check 通过。
- 独立上下文审查已关闭核心逻辑与测试盲点；发布审查提出“审计资源须绑定本次发布”的问题已修复并复审放行，复核者独立运行版本/发布/真实PG权限 49 项通过。当前平台不具备项目流程指定的第三方模型，不宣称跨模型独立复核。

## 上线验收与不变边界

发布后须确认实际 digest / VERSION / 文件哈希、运行 healthy、新 active run 的 commit/SHA、自然 cron 新事件的 producer_git_commit，并核对无关容器、冻结配置、生产表零写入和小时缺口。没有真实候选则只能证明无信号状态归因正常，不能宣称已有新开仓归因样本。

原版/激进版历史缺口 0、重复小时各 12；校准版历史缺口 6、重复小时 6，不回填也不重置观察期。注册覆盖至 2026-12-30 16:00 UTC。窗口仍为 12/15 与 12/23；试跑准入仍按既定门槛，历史缺口仍影响充分性判断。

知识复用：采用 `container-json-logs-lost-on-recreate`（持久证据不能仅靠日志）、`shadow-compose-bind-mount-masks-image-config`（核对实际挂载）、`xau-replay-report-caliber-position-aggregation`（不改变历史评估口径）。本次只做局部元数据修复，不需要重做策略设计或服务器回测。
