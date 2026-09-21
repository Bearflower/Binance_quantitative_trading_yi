# 数据后台独立容器（调度宿主解耦）

- 时间: 2026-09-21 11:45
- 涉及容器: 新建 data-backend、重建 dashboard-api

## 动机
4 个数据维护定时任务（净资产快照 / 指标预计算 / 持仓对账 / 佣金回填）的 APScheduler 全挂在
`dashboard/backend/main_docker.py`，dashboard 一旦被撤改则数据连续性受损。

## 实现
- 新建 `shared/scheduler/__init__.py`：`build_scheduler` / `add_interval_job`（env 驱动间隔）/ `add_cron_job`（env 驱动时分）/ `schedule_startup_run`（启动预热）/ `shutdown_scheduler`（幂等）
- 新建 `services/data_backend/`：Dockerfile + src/main.py（单 DataService + 单 AsyncIOScheduler + 4 job + 预热 + SIGTERM 优雅关闭）+ .deploy_config + deploy.sh
- `dashboard/backend/main_docker.py`：删除 4 个调度器注册/预热/shutdown（-132 行），退化为纯 API 展示
- 单测 `tests/test_shared/test_scheduler.py` 9 passed

## 部署顺序（关键）
先 `services/data_backend/deploy.sh`，再 `dashboard/deploy.sh`（后者会停旧容器，关闭双写窗口）。

## 五层验证结果
- `data-backend` 11:22:40 Up (healthy)，DEPLOY_ID=6081C8BC，L4 5/5 MD5 匹配
- `dashboard-api` 11:37:33 Up (healthy)，镜像 sha256:6f31fc84，L4 4/4 MD5 匹配
- 解耦证据：dashboard 近 5 分钟任务日志数 = 0；data-backend 每分钟 03:3x:16「指标预计算完成」，与 DB `metric_snapshot.updated_at` 完全对齐
- 限频：两容器近 20 分钟 429 计数均为 0
- `equity_snapshot` 今日行 = 0（23:30 CST 定时任务未到点，属预期）

## 已知观察项
1. 首轮任务双跑（interval start_date 与启动预热重叠），UPSERT 幂等无害
2. `services/data_backend/deploy.sh` L212 本地 MD5 标签错误、容器 MD5 未自动比对
3. `dashboard/deploy.sh` 不重新生成 VERSION，镜像内沿用仓库根 VERSION