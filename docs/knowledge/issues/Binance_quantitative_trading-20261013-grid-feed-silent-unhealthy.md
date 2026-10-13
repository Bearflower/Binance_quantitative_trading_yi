---
id: Binance_quantitative_trading-20261013-grid-feed-silent-unhealthy
类型: issue
项目: Binance_quantitative_trading
标题: 币安 @aggTrade 停推叠加未捕获 TimeoutError 导致 feed 静默「假健康」
日期: 2026-10-13
标签: [bug, grid, 实时行情]
任务标识: grid_realtime_feed_and_queue_2026-10-13.md
状态: 已解决
审查: 已审查
---

# 币安 @aggTrade 停推叠加未捕获 TimeoutError 导致 feed 静默「假健康」

## 现象
grid 实时风险预警影子期（`trading_system-grid-realtime` 容器）运行态快照长期显示行情健康：核查窗口 17.1h / 1023 条运行态快照中 `feed_healthy` **100% true**、feed 降级 **0 次**，表面看行情通道一直在正常工作。

## 根因
双重原因，缺一不可：
1. **外部数据源停推**：币安 WS `@aggTrade` 流已停止推送（同域 `@trade` / `@bookTicker` 正常）；
2. **内部异常未捕获**：`feed.py` 未捕获 `asyncio.TimeoutError` → feed 协程静默崩溃、永不重连，`feed_healthy` 冻结在假 `true`（无数据却不报警，即「假健康」）。

## 解决
commit `9e04ee5`（切流 + 修「静默假健康」），改动 `strategies/grid/realtime/feed.py`、`tests/test_grid_realtime/test_feed.py`：
- WS 流由 `@aggTrade` 切换为 `@trade`；
- REST `/fapi/v1/aggTrades` 切换为 `/fapi/v1/trades`（字段 `id/price/qty/time/isBuyerMaker`，无 `fromId`，返回升序）；
- `_refill` 改为单次取最近一批；
- `_read_one` 捕获 `asyncio.TimeoutError` → 抛 `FeedInterrupted` 走重连（关键片段见来源）：
```python
try:
    msg = await ws.receive(timeout=timeout)
except asyncio.TimeoutError:
    raise FeedInterrupted("WS 静默无数据")
```

## 验证
- **生产解析路径实拉（决定性证据）**：容器内用生产 `parse_ws_message` + `ws_url`（`wss://fstream.binance.com/ws/ethusdt@trade`）稳定拉到连续真实成交；
- **长窗口快照**：17.1h / 1023 条运行态快照 `feed_healthy` 100% true、feed 降级 0 次；
- grid 套件 `tests/test_grid_realtime/` **308 passed**；
- commit `9e04ee5` 已推送并触发 Actions 部署（同批提交链 `5137be1` run #65、`dc2d657` run #66）。

## 适用范围与修订
`strategies/grid/realtime/feed.py`、`tests/test_grid_realtime/test_feed.py`；commit `9e04ee5` 已上线。同批提交 `dc2d657` 为 `price_source` 口径订正（`agg_trade` → `trade`，该字段仅被 `parse_profile` 校验非空、不参与运行逻辑，无行为影响），非本条目根因。无替代条目。

## 教训
「静默失败比崩溃更危险」——协程异常若被吞掉会冻结健康标志（fail-open），健康检查必须 fail-closed 并主动做超时重连；同时外部数据源停推没有错误码，不能只靠「有没有抛异常」判断通道是否健康。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/grid_realtime_feed_and_queue_2026-10-13.md)（不随 Git 同步）
- 持久证据: 需求文档 `docs/requirements/grid/Grid_Trading_V2.5.4.md`；commit 9e04ee5
- 必要证据摘要: 核查窗口 17.1h / 1023 条快照 feed_healthy 100% true、降级 0 次；生产实拉 `wss://fstream.binance.com/ws/ethusdt@trade` 拉到连续真实成交；grid 套件 308 passed
- 相关: [队列溢出丢弃最新数据致采样取滞后价](../issues/Binance_quantitative_trading-20261013-grid-feed-queue-drop-latest.md)
