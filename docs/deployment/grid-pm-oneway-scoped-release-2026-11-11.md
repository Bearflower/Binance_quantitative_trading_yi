# PM 单向网格：受保护生产栈定向发布记录

日期：2026-11-11（北京时间）。目标是部署订单网格代码，保持自动执行关闭；此记录不授权账户划转、持仓模式切换或真实下单。

## 已完成的发布准备

- 网格实现与 2026-11-10 最新 GitHub `main`（`35dee97d`）合并为 `8966ab0f`；合并后的本地回归 `858 passed, 1 skipped`。
- 发布分支：`codex/grid-pm-oneway-release-20261111`。服务器临时源码与 `8966ab0f` 一致。
- 服务器独立构建候选镜像 `grid-stage:8966ab0f`（image ID `sha256:a17e721ab608f5feed64c5dc1c987acfa9197ddb67bafa739e68f006a9fef4a2`）。镜像内 `VERSION` 与五个关键源码校验和均与本地提交一致。
- 候选镜像在 `--network none`、`GRID_EXECUTION_MODE=off` 下启动并正常退出；独立临时目录中的执行账本迁移成功。生产 `./data/grid` 未被迁移或改写。
- 生产 `trading_system-grid` 仍运行原固定镜像 `ghcr.io/bearflower/trading-grid@sha256:148deaca0d6242c9507f3648ebb5ef6ea8411b3482eba2a09b55bf37cf7ab6c5`；`grid-executor` 容器不存在，执行模式为 `off` 或未设置。

## 当前不能直接激活候选镜像的原因

生产 `.deploy_tmp/ledger-risk-active.json` 为 `status=active`，SHA `503f7e562c9123eac14b6dfaba9028cd20fef247`。通用发布脚本明确拒绝在此 pin 存在时运行。现行 pin 记录了九个服务的镜像、Compose 栈和发布后验收状态；直接 `docker compose up` 会破坏该证据链。

服务器实际 `docker-compose.yml` 与候选版本除了网格配置，还相差独立收入归因服务和看板收入归因环境项。因此不能将候选 Compose 文件整份覆盖到服务器。现有定向发布脚本针对其他策略和旧 pin 编写，不能借用其模式发布网格。

## 下一次定向发布的必要门禁

1. 在最新主线上新增专用网格定向发布模式：固定起始 SHA、变更路径白名单、源码与镜像校验和、不可变镜像 digest、一次性发布标记；构建只限 `trading-grid`。
2. 保留生产 Compose 基文件，以独立覆盖文件仅修改 `grid-strategy` 镜像和决策交接环境项，并加入默认不启动的 `grid-executor` profile。预演比较全部既有服务的有效 Compose 配置；除网格服务外必须逐项相同。
3. 在任何容器重建前，使用新镜像对生产网格数据卷执行幂等执行账本迁移。先备份网格 SQLite 文件并核对版本；迁移失败则停止发布。不得借此执行全库初始化脚本。
4. 验证现行 pin、全部既有容器身份与健康、交易所只读门禁和网格执行开关。只重建 `grid-strategy`；`grid-executor` 保持未创建。发布前后验证其他服务容器 ID、镜像和状态不变。
5. 以追加式发布状态、前后证据和新的固定覆盖文件更新 pin；失败时保持显式失败状态，不能自动重跑或静默回滚正在运行的交易服务。

实盘自动运行仍需独立验收：专用 PM 子账号切至单向持仓、有 PM 可用权益、设置独立的保证金/名义本金/日亏损/回撤/挂单数硬限额，并通过有人值守的小额真实写接口验收。在此之前，`GRID_EXECUTOR_DEPLOY_ENABLED=false`、`GRID_EXECUTION_MODE=off`。
