---
id: Binance_quantitative_trading-20261020-shadow-compose-bind-mount-masks-image-config
类型: issue
项目: Binance_quantitative_trading
标题: compose 宿主 bind mount 空目录遮蔽镜像内烘焙的影子 profile 配置致 crash-loop
日期: 2026-10-20
标签: [bug, 部署, docker-compose, bind-mount, mtcps-xau-shadow, 影子, 配置遮蔽, crash-loop]
任务标识: xau-mtcps-shadow-pilot_2026-10-19
状态: 待复查
审查: 待审查
---

# compose 宿主 bind mount 空目录遮蔽镜像内烘焙的影子 profile 配置致 crash-loop

## 现象

`trading_system-mtcps-xau-shadow` 容器 crash-loop；`docker logs` 报「profile 配置文件不存在 … `xau_original.yaml` / 无可用 profile 配置」。与「缺 aggr COPY」是**并列的第二处独立根因**：前者为镜像内容缺兄弟包（`ModuleNotFoundError`），本次为镜像内容完整、但被**空目录 bind mount 遮蔽**（配置文件「看起来没了」）。

## 根因

`docker-compose.yml` 的 `mtcps-xau-shadow` 服务原先配置了宿主 bind mount `./strategies/btc_eth/shadow/config:/app/strategies/btc_eth/shadow/config:ro`。服务器上该宿主目录 `/root/trading_system/strategies/btc_eth/shadow/config` **为空**（且 Docker 会为**不存在**的 bind 源**自动创建空目录**），空目录挂载**遮蔽**了镜像经 `strategies/btc_eth/Dockerfile` 的 `COPY strategies/btc_eth/ /app/strategies/btc_eth/` 烘焙进去的 `xau_original.yaml` / `xau_aggressive.yaml`，影子采集器启动时读不到 profile，持续 crash-loop。属「宿主 bind mount 覆盖/遮蔽镜像内烘焙内容」型缺陷——比「文件缺失」更隐蔽，因文件其实存在于镜像内。

## 解决

`docker-compose.yml` 的 `mtcps-xau-shadow` 服务**移除**该宿主 bind mount，改由**镜像烘焙配置作为单一可信源**（配置随 `COPY strategies/btc_eth/` 进入镜像、受 git 跟踪、`git ls-files` 可见；shadow 运行期只读该目录、不写回）。同步改写该服务上方注释块（隔离三条之第 1 条由「独立 bind-mount config」改为「profile 配置随镜像烘焙」）。新增静态守护测试 `tests/test_strategies/test_shadow_compose_config_mount.py`：解析 `docker-compose.yml` 中 `mtcps-xau-shadow` 的 `volumes`，若宿主侧与容器内路径**同时命中** `strategies/btc_eth/shadow`（覆盖父目录 `.../shadow` 与子目录 `.../shadow/config` 两种遮蔽情形）即断言失败。

## 验证

守护测试以变异方式验证（去掉遮蔽挂载 → PASS；恢复遮蔽挂载 → FAIL）。**容器侧重新部署验证（`docker ps` 见 `Up` + `docker logs` 无「profile 配置文件不存在」）以实际 deploy Run 与运维复核为准（文档侧未独立复跑）**。注：同一轮新增的守护测试文件内符号引用一致性（常量名）以工程师/审查复测为准。

## 适用范围与修订

适用于 `docker-compose.yml` 的 `mtcps-xau-shadow` 服务与 `strategies/btc_eth/shadow/config/`。当前适用性：已修复、待重新部署验证。无替代条目。与 [shadow-image-missing-aggr-copy-crashloop](./Binance_quantitative_trading-20261020-shadow-image-missing-aggr-copy-crashloop.md) 为**并列的两处独立根因**（同属「mtcps-xau-shadow 复用 btc-eth 镜像后配置/依赖不自洽」型），彼此不替代、不合并。

## 教训

复用镜像的「影子/旁路入口」不仅要求**镜像内容自洽**（兄弟包齐备），还要求**运行期挂载不得遮蔽镜像内烘焙内容**——宿主 bind 源缺失时 Docker 会静默创建空目录并挂载，产生比「文件缺失」更隐蔽的「配置文件不存在」故障。判据：凡「镜像内已有、运行期只需读」的静态配置，**优先随镜像烘焙并移除宿主 bind mount**，而非依赖宿主持久化文件；并配套**静态守护测试**（解析 compose volumes，拦截遮蔽 `shadow` 目录的挂载）。排查此类 crash-loop 仍先看容器日志（「profile 配置文件不存在」）+ `docker ps`（`Restarting`）。AI 归纳，仅供参考。

## 关联

- 本地补充来源: [handoff](../../handoffs/xau-mtcps-shadow-pilot_2026-10-19.md)（不随 Git 同步）
- 持久证据: `docker-compose.yml`（`mtcps-xau-shadow` 移除 bind mount + 注释改写）、`tests/test_strategies/test_shadow_compose_config_mount.py`、`strategies/btc_eth/shadow/config/xau_original.yaml`（git 跟踪）、`.trae/rules/deployment.md` §2.5「影子 profile 配置来源」
- 必要证据摘要: 宿主目录 `./strategies/btc_eth/shadow/config` 为空（Docker 自动创建空目录）→ 遮蔽镜像内 `COPY strategies/btc_eth/` 烘焙的 `xau_original.yaml`/`xau_aggressive.yaml` → 影子入口「无可用 profile 配置」crash-loop；修复=移除该挂载、改由镜像烘焙配置为单一可信源；**遗留项（知悉，待独立评估）**：守护测试文件内符号引用一致性以工程师/审查复测为准。
- 相关: [Binance_quantitative_trading-20261020-shadow-image-missing-aggr-copy-crashloop](./Binance_quantitative_trading-20261020-shadow-image-missing-aggr-copy-crashloop.md)（并列独立根因：镜像漏 COPY 兄弟包致 `ModuleNotFoundError`；本次为空目录 bind mount 遮蔽镜像内配置）/[Binance_quantitative_trading-20261018-relative-path-wrong-root-silent-load-failure](./Binance_quantitative_trading-20261018-relative-path-wrong-root-silent-load-failure.md)（同属「配置路径错位致静默加载失败」范畴）
