# 容器日志跨部署保留

本项目生产容器的标准输出由 Docker 记录。`docker-compose.logging.yml` 为全部 16 个服务启用宿主机持久化 journald。生产部署脚本显式设置 `COMPOSE_FILE=docker-compose.yml:docker-compose.logging.yml`，因此手动执行 Compose 命令时也必须使用同一变量或两个 `-f` 参数。

## 查询

```bash
cd /root/trading_system
export COMPOSE_FILE=docker-compose.yml:docker-compose.logging.yml
docker compose ps
journalctl --since '2026-10-27 14:00:00' --until '2026-10-27 15:00:00' CONTAINER_NAME=dashboard-api
journalctl -f CONTAINER_NAME=dashboard-api
```

`journalctl` 按容器名跨重建查询，`CONTAINER_ID_FULL=<完整 ID>` 可定位某次运行。`docker logs` 只用于当前容器。业务日志可能含敏感信息，勿将原文复制到公开 CI 日志。

## 容量与验证

生产机使用 `/etc/systemd/journald.conf.d/30-trading-log-retention.conf`：持久化、全机 3 GB 上限、根分区保留 5 GB、最长 14 天。容量上限优先，14 天是目标而非保证。检查：

```bash
journalctl --disk-usage
df -h /
docker inspect -f '{{.HostConfig.LogConfig.Type}}' dashboard-api
journalctl -q --no-pager -n 1 CONTAINER_NAME=dashboard-api
```

当前可恢复的旧 `json-file` 日志已导出到服务器 `/root/trading_system/log_archive/pre_journald_20261027_122204/`，目录权限 700，文件权限 600；`manifest.tsv` 含服务、容器 ID、时间范围、字节数和 SHA-256。这个快照只覆盖导出时仍可读取的日志。

从部署 `b1ead61` 起，CI 在每次 `docker compose up` 前调用 `scripts/archive_container_logs.sh`，把仍使用 `json-file` 的本项目容器日志保存到 `log_archive/pre_up_<时间>_<DEPLOY_ID>/`。归档目录只供 root 访问，清单含容器 ID、日志起止时间、字节数和 SHA-256；已切换到 journald 的服务不会重复归档。归档失败会中止部署。

2026-10-28 15:02（北京时间），全部 15 个服务已分批切换并通过健康、驱动、当前 journal 记录和旧日志归档检查；交易策略逐个切换，数据库最后切换。11:09 的一次业务部署已重建先行切换的 4 个服务，`dashboard-api` 等服务的 journal 仍可按容器名查到新旧两个容器 ID，证明跨重建查询有效。最终复核：15/15 健康、15/15 journald、86 份部署前归档文件的字节数和 SHA-256 均匹配；根分区剩余 25 GB，journal 占用约 637 MB。14 天实际留存窗口尚需运行满 14 天后验证，容量上限仍可能使窗口缩短。每日 09:00 的只读自动检查监控日志驱动、记录、journal 容量、磁盘余量和容器健康；正常状态保持安静。

2026-10-29 09:25（北京时间），新加入的 `mtcps-xau-calibrated-shadow` 经提交 `74916f3`（`DEPLOY_ID=00007D01`）单独重建并切换到 journald。重建前手动归档于 `log_archive/pre_journald_calibrated_20261029_092137/`，CI 又将旧容器日志归档于 `log_archive/pre_up_20261029_092426_00007D01/`；后者的旧容器 ID、8,791 字节和 SHA-256 均已核对。新容器有 journal 记录且 `docker logs` 可读；最终复核 16/16 健康、16/16 journald、16/16 当前容器有 journal 记录。根分区剩余约 11 GB，journal 占用约 823 MB；新服务的 14 天实际留存窗口从本次切换起观察。

## 回退

先导出受影响容器的当前日志。将对应服务从 `docker-compose.logging.yml` 移除并按项目部署流程发布；Compose 会重建该服务并恢复基础配置中的 `json-file`。已写入 journal 的日志不会因容器删除而消失。回退前后核对容器健康、驱动、日志和部署记录。
