---
id: Binance_quantitative_trading-20261028-container-json-logs-lost-on-recreate
类型: issue
项目: Binance_quantitative_trading
标题: 容器重建后旧 json-file 日志无法查询
日期: 2026-10-28
标签: [部署, Docker, 日志, 可观测性]
任务标识: container_log_retention_2026-10-27
状态: 已解决
审查: 待审查
---

# 容器重建后旧 json-file 日志无法查询

## 现象

生产项目原有 15 个容器都使用 Docker `json-file` 日志驱动，单容器 `max-size=10m,max-file=3`。2026-10-27 核查时，容器可见日志只从前一次重建时间开始；部署记录中的更早日志无法通过现有容器的 `docker logs` 查询。宿主机 `/root/trading_system/logs` 为空目录，不能作为补充来源。

## 根因

CI 使用 `docker compose up -d --remove-orphans`。镜像或配置变化引发容器重建时，旧容器及其 `json-file` 日志文件随容器删除；轮转也只保留当前容器有限容量。部署日志只记录部署事件，不保存业务 stdout/stderr。并非每次部署必定重建全部服务，故应按每次实际重建范围判断日志缺口。

## 解决

- 重建前先导出当时仍可读的 15 个容器日志；随后在 CI 的 `docker compose up` 前自动归档仍使用 `json-file` 的项目容器，保存时间范围、字节数和 SHA-256，归档失败即中止部署。
- 设置宿主机 journald 持久化、全机容量上限 3 GB、最少保留 5 GB 磁盘空闲、最长保留 14 天。通过 `docker-compose.logging.yml` 将本项目 15 个服务分批切换为 `journald`，没有修改 Docker 全局默认驱动。
- 交易策略在 2026-10-28 14:30 北京时间维护窗口内逐个重建，PostgreSQL 最后重建。当前容器仍可用 `docker logs`；跨重建按 `journalctl CONTAINER_NAME=<容器名>` 查询。

## 验证

- 无业务测试容器删除后，其 journal 记录仍可查；2026-10-28 11:09 的业务部署实际重建了先行切换的 4 个服务，`dashboard-api`、`data-backend`、`aggtrade-collector`、`kline-monitor` 均可按容器名查到两个容器 ID 的记录。
- 最终生产部署 `fe43e9e`（`DEPLOY_ID=00007701`，2026-10-28 15:02 北京时间）后，15/15 容器健康、15/15 实际驱动为 journald、15/15 当前容器有 journal 记录且 `docker logs` 可用；86 份自动归档文件的字节数和 SHA-256 全部匹配。
- 当时根分区剩余约 25 GB，journal 占用约 637 MB。实际能否保留满 14 天尚需运行观察，3 GB 容量上限可能使留存窗口缩短。

## 适用范围与修订

适用于本项目 2026-10-28 起的 15 个生产 Compose 服务和对应部署流程。既往已删除容器的 `json-file` 日志无法补回。若改用外部日志后端或调整留存策略，应修订本条；目前无替代条目。

## 教训

AI 归纳：排查“部署清日志”时，应先区分容器是否真的被重建、日志驱动是否随容器生命周期删除，以及宿主挂载目录是否实际承载 stdout。切换日志驱动前先导出可见旧日志，并通过一次真实重建验证跨容器 ID 查询。

## 关联

- 持久证据：[日志保留运维手册](../../deployment/log-retention.md)、[日志覆盖配置](../../../docker-compose.logging.yml)、[部署前归档脚本](../../../scripts/archive_container_logs.sh)、[CI 部署流程](../../../.github/workflows/deploy.yml)。
- 关键提交：`b1ead61`（部署前自动归档）、`c5857b0`（首批 journald）、`fe43e9e`（全部服务切换）、`8c77470`（最终运维记录）。
- 相关决策：[本项目采用 journald 保留容器日志](../decisions/Binance_quantitative_trading-20261028-journald-container-log-retention.md)。
