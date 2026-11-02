---
id: Binance_quantitative_trading-20261028-journald-container-log-retention
类型: decision
项目: Binance_quantitative_trading
标题: 本项目容器 stdout 使用宿主机 journald 跨重建保留
日期: 2026-10-28
标签: [部署, Docker, journald, 日志保留]
任务标识: container_log_retention_2026-10-27
状态: 已采纳
审查: 待审查
---

# 本项目容器 stdout 使用宿主机 journald 跨重建保留

## 背景

原 `json-file` 日志随容器重建消失，当前空的 `/root/trading_system/logs` 目录无法供历史排障。用户要求先连接生产核实，再给出具体方案，并批准按维护窗口实施。

## 备选方案

- 保持 `json-file`，仅延长轮转或挂载目录：容器删除后仍无法按原方式查询旧 stdout，需另建收集或归档机制。
- 本项目单独改为宿主机 journald：沿用已有持久 journal，支持按容器名跨重建检索，改动范围局限于本项目 Compose；容量和磁盘状态需监测。
- 增加 Loki 等外部日志后端：支持跨主机与长期留存，但需额外服务、运维和容量管理；当前单机排障需求暂不需要。

## 决策

采用项目级 `docker-compose.logging.yml` 覆盖配置，将全部 15 个服务切换到 `journald`；Docker 全局默认日志驱动保持不变。宿主机设置 `Storage=persistent`、`SystemMaxUse=3G`、`SystemKeepFree=5G`、`MaxRetentionSec=14day`。保留部署前旧 `json-file` 归档和 CI 自动归档作为过渡。每日 09:00 自动只读巡检，正常时保持安静；至少观察到 2026-11-11，再按实际留存情况评估降低巡检频率。

## 理由

生产已有 `/var/log/journal`，无业务容器试验确认 `docker logs` 与 `journalctl` 均可用，删除测试容器后 journal 仍有记录。先行切换的 4 个真实服务在 2026-10-28 11:09 重建后仍可查到新旧两个容器 ID，验证了最关键的跨重建需求。分批切换使交易服务和数据库的短暂中断可在批准的维护窗口内逐项验收。

## 后果

跨重建查询改用 `journalctl CONTAINER_NAME=<容器名>`；`docker logs` 仅查看当前容器。journal 的 3 GB 上限是全机共享且优先于 14 天目标，实际留存时间可能不足 14 天；每日检查需观察容量与最早可查询时间。此方案不覆盖服务器磁盘损坏或整机丢失，未来若需跨机检索或长期备份，再评估外部日志后端。

## 适用范围与修订

适用于 2026-10-28 起本项目 15 个生产 Compose 服务。当前配置见 `docker-compose.logging.yml` 和生产机 `/etc/systemd/journald.conf.d/30-trading-log-retention.conf`。若容量、留存需求或部署架构变化，先验证磁盘余量和跨重建查询，再修订决策；暂无替代记录。

2026-10-29 修订：新加入的 `mtcps-xau-calibrated-shadow` 沿用同一项目级日志策略，旧日志先归档，再通过 `74916f3` 单独重建为 journald。现覆盖 16 个服务；该服务的实际 14 天留存从 10 月 29 日起验证，其他决策口径不变。

## 关联与证据

- [问题记录](../issues/Binance_quantitative_trading-20261028-container-json-logs-lost-on-recreate.md)、[运维手册](../../deployment/log-retention.md)、[日志覆盖配置](../../../docker-compose.logging.yml)、[归档脚本](../../../scripts/archive_container_logs.sh)。
- 关键部署：`c5857b0` 首批、`e4aff94` 第二批、`fe43e9e` 全量完成（`DEPLOY_ID=00007701`）；最终生产复核 15/15 健康、15/15 journald、86 份归档校验通过、根分区余量约 25 GB。
- 14 天实际留存窗口属于待观察结果，不以配置项本身作为达标证据。
