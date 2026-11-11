# PM 单向网格：受保护生产栈定向发布记录

日期：2026-11-11（北京时间）。目标是部署订单网格代码，保持自动执行关闭。账户操作的授权由本次对话另行记录。

## 发布结果

- 受保护定向发布由 [GitHub Actions Run 43000314975](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/43000314975) 完成，主线及生产 pin 为 `4faafd876832e6b87dd7265a37a6fe203eb3d019`，状态 `active`。
- 新镜像为 `ghcr.io/bearflower/trading-grid@sha256:38826ff215f21de2fd8371232aacc432cf71ac74d220c49164123c7bb29c3119`；生产网格信号容器运行且健康。发布前后容器身份核对仅 `grid-strategy` 改变，执行账本 schema 为 5。
- 生产 `.releases/grid-pm-oneway-20261111/<sha>/executor.json` 已保存默认关闭的执行器 profile；执行器容器不存在，自动执行仍为 `off`。
- 子账号已切单向持仓，ETHUSDT 当前 5 倍且无仓位、普通挂单或条件挂单。10 倍请求被交易所 `-4421` 拒绝，用户同意保持 5 倍。现货仍有 300 USDT，PM 权益为 0；API 未获通用划转权限，故未划转或下真实订单。

## 发布前准备（历史快照）

- 网格实现与 2026-11-10 最新 GitHub `main`（`35dee97d`）合并为 `8966ab0f`；合并后的本地回归 `858 passed, 1 skipped`。
- 发布分支：`codex/grid-pm-oneway-release-20261111`。服务器临时源码与 `8966ab0f` 一致。
- 服务器独立构建候选镜像 `grid-stage:8966ab0f`（image ID `sha256:a17e721ab608f5feed64c5dc1c987acfa9197ddb67bafa739e68f006a9fef4a2`）。镜像内 `VERSION` 与五个关键源码校验和均与本地提交一致。
- 候选镜像在 `--network none`、`GRID_EXECUTION_MODE=off` 下启动并正常退出；独立临时目录中的执行账本迁移成功。生产 `./data/grid` 未被迁移或改写。
- 生产 `trading_system-grid` 仍运行原固定镜像 `ghcr.io/bearflower/trading-grid@sha256:148deaca0d6242c9507f3648ebb5ef6ea8411b3482eba2a09b55bf37cf7ab6c5`；`grid-executor` 容器不存在，执行模式为 `off` 或未设置。
- `scripts/preflight_grid_scoped_release.py` 在生产以只读方式通过：以当前 pin、原 Compose 和临时网格覆盖文件渲染有效配置，变化仅为 `grid-strategy` 的镜像与决策交接环境项。该脚本不执行发布。

## 发布前阻断及处理

发布前 `.deploy_tmp/ledger-risk-active.json` 为 `status=active`，SHA `503f7e562c9123eac14b6dfaba9028cd20fef247`。通用发布脚本明确拒绝在此 pin 存在时运行。该 pin 记录了九个服务的镜像、Compose 栈和发布后验收状态；直接 `docker compose up` 会破坏该证据链。本次新增 `grid-pm-oneway` 定向模式后保留了证据链，并更新 pin。

服务器实际 `docker-compose.yml` 与候选版本除了网格配置，还相差独立收入归因服务和看板收入归因环境项。因此本次保留原 Compose 基文件，仅使用发布覆盖文件修改网格镜像和决策交接环境项。

## 本次定向发布的门禁

1. 在最新主线上新增专用网格定向发布模式：固定起始 SHA、变更路径白名单、源码与镜像校验和、不可变镜像 digest、一次性发布标记；构建只限 `trading-grid`。
2. 保留生产 Compose 基文件，以独立覆盖文件仅修改 `grid-strategy` 镜像和决策交接环境项，并加入默认不启动的 `grid-executor` profile。预演比较全部既有服务的有效 Compose 配置；除网格服务外必须逐项相同。
3. 在任何容器重建前，使用新镜像对生产网格数据卷执行幂等执行账本迁移。本次执行账本原先不存在，脚本遇到已存在文件会拒绝发布并要求人工核对；迁移失败则停止发布。不得借此执行全库初始化脚本。
4. 验证现行 pin、全部既有容器身份与健康、交易所只读门禁和网格执行开关。只重建 `grid-strategy`；`grid-executor` 保持未创建。发布前后验证其他服务容器 ID、镜像和状态不变。
5. 以追加式发布状态、前后证据和新的固定覆盖文件更新 pin；失败时保持显式失败状态，不能自动重跑或静默回滚正在运行的交易服务。

实盘自动运行仍需独立验收：PM 钱包有可用权益，并通过有人值守的小额真实写接口验收。单向持仓与独立硬限额已经配置；在验收前，`GRID_EXECUTOR_DEPLOY_ENABLED=false`、`GRID_EXECUTION_MODE=off`。
