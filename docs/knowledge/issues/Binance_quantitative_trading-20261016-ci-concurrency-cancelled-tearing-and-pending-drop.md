---
id: Binance_quantitative_trading-20261016-ci-concurrency-cancelled-tearing-and-pending-drop
类型: issue
项目: Binance_quantitative_trading
标题: deploy 并发策略致撕裂镜像集与 pending Run 变更静默丢失
日期: 2026-10-16
标签: [配置, 部署, CI, GitHub Actions, 并发]
任务标识: ci_concurrency_queue_max_2026-10-16
状态: 已解决
审查: 待审查
---

# deploy 并发策略致撕裂镜像集与 pending Run 变更静默丢失

## 现象

CI 部署（GitHub Actions → GHCR → SSH 服务器）出现两处异常：
1. **混版容器组**（真实事故，2026-10-15 run 78）：一次部署后 GHCR 中出现「部分服务镜像已重建、部分未重建」的**撕裂镜像集**，并被部署落地到服务器，致 `data-backend` 已更新、`ai-tuner` 未更新，同一批容器处于不同代码版本。
2. **变更静默丢失**：某次 push 的目录变更在后续 Run 中不再出现于构建范围，该变更**静默丢失**（此前 CS3 差点永不上线）。

## 根因

三条复合缺陷（`D1`/`D2`/`D3`）：

- **D1（在跑 Run 被中途取消 → 撕裂镜像集）**：`.github/workflows/deploy.yml` 的 `concurrency.cancel-in-progress: true` 会在新 push 到来时**取消正在进行的旧 Run**。若取消发生在 `build-all` 矩阵构建中途，则 GHCR 出现「部分服务已重建、部分未重建」的**非原子镜像集**。
- **D2（撕裂集被落地 → 混版容器组）**：deploy job 的 `if` 显式允许 `needs.build-all.result == 'cancelled'` 仍继续执行（`deploy.yml:271`）→ 被取消 Run 的撕裂镜像集被部署落地，形成混版容器组（2026-10-15 run 78：`data-backend` 已更新、`ai-tuner` 未更新）。
- **D3（pending Run 被顶替 → 变更静默丢失）**：每次 Run 的 diff 基固定为 `github.event.before`（仅覆盖本次推送范围 `before..HEAD`）。默认 `concurrency.queue: single` 语义下，新 push 到来时**排队中的 pending Run 被顶替（cancel）**，其覆盖的推送范围不再被**后继 Run** 的 `git diff before..HEAD` 覆盖 → 该次推送的目录变更**静默丢失**。

**关键归因更正**：D3 的「pending Run 被顶替」是 `concurrency` 默认 `queue: single` 语义所致，**与 `cancel-in-progress` 取值无关**；`cancel-in-progress: false` 只保证「**正在跑**的 Run 不被取消」，并不能保护 pending Run 不被顶替。故仅把 `cancel-in-progress` 改为 `false`（方案 A）**不能**闭合 D3。

## 解决

`.github/workflows/deploy.yml` `concurrency` 块（当前态，L27-L39）：

```yaml
concurrency:
  group: deploy-${{ github.ref }}
  cancel-in-progress: false
  queue: max
```

- 提交 `0c4d088`：`cancel-in-progress: true → false`，闭合 **D1**（在跑 Run 先跑完自身完整构建范围并原子部署，不被中途取消）。
- 提交 `a17a1c2`：新增 `queue: max`，闭合 **D3**（同一并发组最多 100 个 pending Run 排队、FIFO 依次执行且互不取消，于是每次推送都获得自己的 Run 及其 `before..HEAD` diff，被顶替推送的目录变更不再静默丢失）。同时更正 §2.7 归因、升 `.trae/rules/deployment.md` 至 v3.5。
- `queue: max` 与 `cancel-in-progress: false` 可并用（与 `true` 互斥，否则校验报错）。官方语义文档：`https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency`。

## 验证

- **Run #83（commit `0c4d088`）**：Actions **success**；真构建仅 `hrs` + `dashboard-api`（其余 9 服务 skip）；服务器 `hrs` VERSION `GIT_SHA=0c4d088…` / `DEPLOY_ID=00005301`；部署日志 `[2026-10-16 14:50:44] DEPLOY_SUCCESS commit=0c4d088… deploy_id=00005301`。
- **Run #84（commit `a17a1c2`，最终态）**：13 个 job **全绿**；真构建仅 `hrs` + `dashboard-api`（按「🏷️ 生成 VERSION」「🐳 Build + Push」步骤是否 skipped 判定，其余 9 个 skip，与预测 blast radius 完全吻合）；服务器 VERSION 核对：
  - `trading_system-hrs` = `a17a1c2` / `00005401`（已 recreate）；`dashboard-api` = `a17a1c2` / `00005401`（已 recreate）；
  - `ai-tuner` 保持 `e0f582c` / `00005101`、`data-backend` 保持 `fd03712` / `00004E01`（**正确未重建**，印证「只 recreate 镜像变了的容器」增量语义）；
  - 部署日志 `[2026-10-16 15:18:14] DEPLOY_SUCCESS commit=a17a1c2… deploy_id=00005401`，`00005401` = `(84<<8)|1` 与 `DEPLOY_ID=printf '%08X' ((run_number<<8)|run_attempt)` 公式精确一致。
- **防幻觉第三层（VERSION DEPLOY_ID）与第四层（关键文件 MD5）一致，无混版容器组**。

## 适用范围与修订

`.github/workflows/deploy.yml`（`concurrency` 块，commit `0c4d088` + `a17a1c2`）；`.trae/rules/deployment.md` §2.7（v3.4 → v3.5）。**路径 ①② 已闭合**；**路径 ③（构建失败的 Run 其变更不被后继 Run diff 覆盖）仍开放**，仅方案 D（持久化 last-deployed sha 作 diff 基）可根治，经评估为**可选设计项**（该 Run 变红可见、可重跑恢复），见相关 decision 条目。无替代条目。

## 教训

三条独立缺陷可同时伪装成「部署偶发异常」：①**非原子构建**（中途取消 → 撕裂镜像集）与 ②**放行被取消的构建**（deploy `if` 允许 `cancelled`）叠加才成事故，二者需同时治理；③**diff 基绑定单次推送范围**是结构性隐患——任何「Run 未跑完/被顶替」都会使其覆盖的变更脱离后续构建视野。判断要点：排查 CI「部署了但代码没变」时，先分清是「镜像没构建」（撕裂/取消）还是「变更没进构建范围」（diff 基问题），二者根因不同、修复不同；仅改 `cancel-in-progress` 不会闭合 pending 顶替路径，须核对 `queue` 语义。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/ci_concurrency_queue_max_2026-10-16.md)（不随 Git 同步）
- 持久证据: `.github/workflows/deploy.yml`（commit `0c4d088` / `a17a1c2`，`concurrency` 块 L27-L39、deploy `if` L271）；`.trae/rules/deployment.md` §2.7（v3.5）；commit `0c4d088`、`a17a1c2`（`git show --stat`）
- 必要证据摘要: Run #83/#84 全绿；#84 真构建仅 `hrs`+`dashboard-api`；服务器 VERSION `hrs`/`dashboard-api`=`a17a1c2`/`00005401`（已 recreate），`ai-tuner`/`data-backend` 保持旧 sha（正确未重建）；部署日志 `DEPLOY_SUCCESS deploy_id=00005401`（=`(84<<8)|1`，公式一致）；防幻觉第三/四层一致无混版
- 相关: [并发组决策条目](../decisions/Binance_quantitative_trading-20261016-ci-concurrency-cancel-false-queue-max.md)；[deploy.yml 未同步 init-scripts 致新表迁移失效](./Binance_quantitative_trading-20261001-deploy-yml-init-scripts-unsynced.md)（同属 deploy.yml 部署流程可靠性问题）
