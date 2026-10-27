# 容器日志跨部署保留

本项目生产容器的标准输出由 Docker 记录。`docker-compose.logging.yml` 是分批切换清单：文件中列出的服务使用宿主机持久化 journald，未列出的服务沿用 `docker-compose.yml` 的 `json-file` 设置。生产部署脚本显式设置 `COMPOSE_FILE=docker-compose.yml:docker-compose.logging.yml`，因此手动执行 Compose 命令时也必须使用同一变量或两个 `-f` 参数。

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

## 回退

先导出受影响容器的当前日志。将对应服务从 `docker-compose.logging.yml` 移除并按项目部署流程发布；Compose 会重建该服务并恢复基础配置中的 `json-file`。已写入 journal 的日志不会因容器删除而消失。回退前后核对容器健康、驱动、日志和部署记录。
