# 网格实时 shadow 部署确认（2026-10-27）

## 范围与版本

- 目标：生产服务器 `/root/trading_system` 的 `grid-realtime`，与小时网格服务共用 `trading-grid` 镜像；独立 `aggtrade-collector` 未改动。
- 首次提交 `351caa1` 于北京时间 13:15 部署。生产观察发现原始成交 ID 自然跳号被误判为缺口，实时行情持续降级；随即修复并重新部署。
- 最终提交 `dc660ad84809fea92753f49fcb5a9f2841d5390f`，服务器 `deploy_logs/20261027.log` 于北京时间 17:42:32 记录 `DEPLOY_SUCCESS`，`DEPLOY_ID=00006901`。容器内 `VERSION` 的 `GIT_SHA` 与该提交一致。
- 容器内 `feed.py` SHA-256 为 `5850a628a016052e8c04307be7cc5ae20c7ff68bbe65db5572be49de5d996a52`，`main.py` 为 `6e9f4c2e18df84a8444dd42d0c1bdd5a04f2485be9922b500b5317b08f05632a`，均与本地一致。

## 验证

- 本地网格测试：`python3 -m pytest tests/test_grid_realtime -q`，314 passed；最后增加 REST 页内重复 ID 去重后，feed/main 定向测试 84 passed；`git diff --check` 通过。
- 北京时间 17:51，`grid-realtime`、`grid-strategy`、`aggtrade-collector`、`kline-monitor` 均在运行，目标容器已运行约 9 分钟。实时日志 `feed_healthy=true`、`storage_degraded=false`；跨过 300 秒窗口预热后未再出现 `feed_interrupted`。
- SQLite 最新会话为 21、`ACTIVE`，心跳约 0.5 秒；参考快照累计 91 条，事件累计 8 条，8 条 outbox 均为 `SHADOW`。配置仍为 `mode: shadow`、`shadow_research.enabled: false`。
- ETHUSDT 聚合成交研究库最新成交约 9.5 秒前、秒采样最新约 10.5 秒前；当前保留区间聚合 ID 与秒采样时间轴均未见缺行。

## 待观察与边界

- 17:51 运行态仍有 `sync_uncertain`，原因是小时网格服务随共用镜像重启后，新会话尚无成功发送的参考快照（会话 21 的 `current_seq=0`）。下一次常规巡检预计在北京时间 18:05；在参考确认前实时进程不产生当前建议边界事件。该状态须在下一次巡检后再核对。
- 原始成交 ID 数字跳号不能单独证明丢包；REST 最近一页与随后 WS 的接缝也不能证明逐笔无遗漏。本次修复以断线降级、最近可见成交尽力补入和 300 秒重新预热控制窗口风险。
- 本轮只是 shadow 设施修复。正式 `alert` 仍受 M3 独立验证和 M4 验收门槛约束，未切换。
