---
id: Binance_quantitative_trading-20261016-ci-concurrency-cancel-false-queue-max
类型: decision
项目: Binance_quantitative_trading
标题: CI 并发组采用 cancel-in-progress:false + queue:max，方案 D（持久化 last-deployed sha）评估为可选
日期: 2026-10-16
标签: [部署, CI, GitHub Actions, 并发]
任务标识: ci_concurrency_queue_max_2026-10-16
状态: 已采纳
审查: 待审查
---

# CI 并发组采用 cancel-in-progress:false + queue:max，方案 D（持久化 last-deployed sha）评估为可选

## 背景

GitHub Actions 部署流程存在三条变更丢失/部署撕裂路径（详见 issue `...-20261016-ci-concurrency-cancelled-tearing-and-pending-drop`）：D1 在跑 Run 被取消致撕裂镜像集、D2 被取消的构建仍被放行部署致混版、D3 pending Run 被顶替致变更静默丢失。需选择并发组配置以闭合这些路径，并裁定是否引入更重的「持久化 last-deployed sha」机制。

## 备选方案

- **方案 A**：仅将 `concurrency.cancel-in-progress` 由 `true` 改为 `false`。闭合 D1（在跑 Run 不被中途取消），但**不闭合 D3**（pending 顶替由默认 `queue: single` 语义决定，与 `cancel-in-progress` 无关）。
- **方案 B（`queue: max`）**：在 `cancel-in-progress: false` 基础上新增 `queue: max`。允许同组最多 100 个 pending Run 排队、FIFO 依次执行且互不取消，使每次推送都获得自己的 Run 及其 `before..HEAD` diff → 闭合 D3。成本仅 1 行。
- **方案 D（持久化 last-deployed sha）**：部署后写回状态，以下次 diff 基线取「服务器实际 SHA」。可根治路径 ③（构建失败的 Run 其变更不被后继 Run diff 覆盖），但需引入状态持久化与基线管理，成本高。

## 决策

采纳 **方案 A + 方案 B 组合**：`concurrency` 采用 `cancel-in-progress: false` + `queue: max`（提交 `0c4d088` + `a17a1c2`）。将**方案 D 评估为「可选而非必须」**，不实施，仅作待复查/知悉项登记。

## 理由

- 官方并发语义核实：`queue` 为合法子键（`single` 默认 / `max`）；`queue: max` 允许最多 100 个 pending Run 排队、FIFO 依次执行且互不取消；`queue: max` 与 `cancel-in-progress: false` 可并用（与 `true` 互斥）。
- 方案 B 以 **1 行成本**精确闭合路径 ①②（D1+D3），性价比最高。
- 方案 D 的必要性经**多智能体评估结为非必须**：推送节奏实测近 6 天仅 1 次「≥3 push 落入构建窗」，且为代码提交，**路径 ③ 从未真实触发**；触发时该 Run 变红**可见**、可重跑恢复，无需引入持久化机制。
- 与「最小改动、先解决可见真实事故」原则一致：先闭合已真实发生的 D1/D2 与结构性 D3，再以观察决定是否值得为路径 ③ 偿付方案 D 的技术债。

## 后果

- 眼下收益：在跑 Run 完整跑完自身构建范围并原子部署（消除撕裂镜像集与混版容器组）；pending Run 不再被顶替致变更静默丢失；部署收敛为「按需构建 + 增量 recreate」，实测 Run #84 仅 `hrs`+`dashboard-api` 重建、其余容器正确保留旧 sha。
- 技术债（残留风险）：**路径 ③ 仍开放**——某服务**构建失败**的 Run，其目录变更不被后继 Run 的 `git diff` 覆盖 → 该服务可能滞留旧镜像。此 Run 变红可见、可重跑恢复；如需**根治**（而非依赖重跑）才需方案 D（持久化 last-deployed sha），属**可选设计项**。

## 适用范围与修订

`.github/workflows/deploy.yml`（`concurrency` 块，commit `0c4d088` + `a17a1c2`）；`.trae/rules/deployment.md` §2.7（v3.5）。本决策（`cancel-in-progress:false` + `queue:max`）至今有效、无替代条目；被评估为可选的方案 D 未实施，保留为待复查设计项。

## 关联与证据
- 本地补充来源: [handoff](../../handoffs/ci_concurrency_queue_max_2026-10-16.md)（不随 Git 同步）
- 持久证据: `.github/workflows/deploy.yml`（commit `0c4d088`/`a17a1c2`）；`.trae/rules/deployment.md` §2.7（v3.5）；官方并发文档 `https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency`
- 必要证据摘要: Run #84 全绿（13 job）、真构建仅 `hrs`+`dashboard-api`、服务器 VERSION 与 `DEPLOY_ID=00005401` 一致、其余容器正确未重建 → 方案 A+B 生效且增量语义成立；方案 D 必要性评估结论「非必须」（推送节奏实测 + 路径 ③ 未触发）
- 相关: [撕裂镜像集与 pending 变更丢失 issue](../issues/Binance_quantitative_trading-20261016-ci-concurrency-cancelled-tearing-and-pending-drop.md)
