---
id: Binance_quantitative_trading-20261020-shadow-image-missing-aggr-copy-crashloop
类型: issue
项目: Binance_quantitative_trading
标题: 复用 btc-eth 镜像漏 COPY 兄弟包目录致影子容器 ModuleNotFoundError crash-loop
日期: 2026-10-20
标签: [bug, 部署, 镜像, mtcps-xau-shadow, 影子, 依赖缺失, crash-loop]
任务标识: xau-mtcps-shadow-pilot_2026-10-19
状态: 待复查
审查: 待审查
---

# 复用 btc-eth 镜像漏 COPY 兄弟包目录致影子容器 ModuleNotFoundError crash-loop

## 现象

2026-10-19 完成 XAU 影子策略交付并部署（Actions Run 全绿）后，新增容器 `mtcps-xau-shadow` 反复重启（crash-loop）。部署后五层验证的**第一层（容器状态）即暴露**：`docker ps` 显示该容器 `STATUS` 为 `Restarting`（非 `Up`）；`docker logs mtcps-xau-shadow` 报 `ModuleNotFoundError: No module named 'strategies.btc_eth_aggressive'`。CI Run 全绿与容器活体失败并存——Run 只证明「镜像构建/推送/部署脚本执行成功」，不证明容器能跑起来。

## 根因

`mtcps-xau-shadow` 复用 `ghcr.io/bearflower/trading-btc-eth:latest` 镜像（仅覆盖 `command` 为 shadow 入口），而影子采集器 `strategies/btc_eth/shadow/collector.py:29` 有 `from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as AggressiveBTCEthStrategy`。`strategies/btc_eth/Dockerfile` 原先只 `COPY strategies/btc_eth/ /app/strategies/btc_eth/`，**镜像内不含 `strategies/btc_eth_aggressive/` 目录**，故 import 在容器启动时即失败。属「复用镜像的旁路入口依赖未与镜像内容自洽」型缺陷。

## 解决

`strategies/btc_eth/Dockerfile` 第 23 行新增 `COPY strategies/btc_eth_aggressive/ /app/strategies/btc_eth_aggressive/`，使该镜像对影子入口的全部兄弟包依赖自洽。并新增静态守护测试 `tests/test_strategies/test_shadow_image_dependencies.py`：解析 Dockerfile 的所有 `COPY` 目标目录集合，与 `strategies/btc_eth/shadow/` 下扫描出的项目内 import 顶层包（`strategies.*` / `shared.*`）比对，断言覆盖，缺失即失败。

## 验证

新增测试经变异验证（FAIL→PASS）：去掉 Dockerfile 的 aggr COPY 时断言失败、补齐后通过。**容器侧重新部署验证（`docker ps` 见 `Up` + `docker logs` 无 ModuleNotFoundError）以实际 deploy Run 与运维复核为准（文档侧未独立复跑）**。

## 适用范围与修订

适用于 `strategies/btc_eth/Dockerfile` 与 `strategies/btc_eth/shadow/`。当前适用性：已修复、待重新部署验证。无替代条目。

## 教训

复用镜像的「影子/旁路入口」必须与镜像内容自洽——Dockerfile 需覆盖影子入口的**全部兄弟包依赖**；仅换 `command` 的复用方式要配套**静态守护测试**（解析 `COPY` vs `import` 顶层包）。部署后排查此类 crash-loop 最有效的是第一层：`docker ps` 看 `Restarting` + `docker logs` 看 `ModuleNotFoundError`，**勿因 CI Run 全绿就判定容器健康**。AI 归纳，仅供参考。

## 关联

- 本地补充来源: [handoff](../../handoffs/xau-mtcps-shadow-pilot_2026-10-19.md)（不随 Git 同步）
- 持久证据: `strategies/btc_eth/Dockerfile`（`COPY strategies/btc_eth_aggressive/`）、`strategies/btc_eth/shadow/collector.py:29`、`tests/test_strategies/test_shadow_image_dependencies.py`、`.trae/rules/deployment.md` §2.5「复用前提」
- 必要证据摘要: btc-eth 镜像内容现含 `strategies/btc_eth_aggressive/`；**遗留隐患（知悉项，待独立评估）**：CI 增量构建按「各 job 只看自己目录是否变更」判定，未来仅改 `strategies/btc_eth_aggressive/` 时该镜像不重建 → 影子容器可能跑旧 aggr 代码，即「镜像依赖与 CI diff 判定不一致」。
- 相关: [Binance_quantitative_trading-20261016-ci-concurrency-cancelled-tearing-and-pending-drop](./Binance_quantitative_trading-20261016-ci-concurrency-cancelled-tearing-and-pending-drop.md)（同属「CI diff 判定与镜像集一致性问题」范畴）
