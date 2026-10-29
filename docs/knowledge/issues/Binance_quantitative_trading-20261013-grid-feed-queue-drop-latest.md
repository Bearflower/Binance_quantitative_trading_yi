---
id: Binance_quantitative_trading-20261013-grid-feed-queue-drop-latest
类型: issue
项目: Binance_quantitative_trading
标题: 队列溢出丢弃「最新」成交致整秒采样取到滞后价格
日期: 2026-10-13
标签: [bug, grid, 实时行情]
任务标识: grid_realtime_feed_and_queue_2026-10-13.md
状态: 已解决
审查: 已审查
---

# 队列溢出丢弃「最新」成交致整秒采样取到滞后价格

## 现象
切到 `@trade` 后突发成交峰值达 **1762 笔/秒**，超过队列容量 `queue_capacity=1000`；队列溢出时原 `_pump_feed` 逻辑丢弃**最新**成交 → 整秒采样（取「不晚于整秒的最后一笔」）取到的是**滞后价格**，在清算瀑布时削弱预警灵敏度。

## 根因
队列溢出的处置端错误：消费端采样依赖「末笔」，而溢出处置却丢弃**最新**一笔。丢最新即丢末笔，于是「数据没少、看着没丢」，但采样价实际滞后于真实盘口。

## 解决
commit `5137be1`（队列溢出改「丢最旧」）：
- `strategies/grid/realtime/main.py` 的 `_pump_feed` 在队列满时 `get_nowait()` 逐出最旧一笔再入队（保住最新成交）：
```python
async def _pump_feed(self) -> None:
    """从 feed 搬运成交入队；队列满时逐出最旧一笔，保住最新成交。"""
    async for trade in self._feed.stream():  # pragma: no branch
        if self._trade_queue.full():
            self._storage_degraded = True
            self._trade_queue.get_nowait()
        self._trade_queue.put_nowait(trade)
```
- 测试同步改为 `test_pump_feed_queue_full_evicts_oldest_keeps_newest`。

## 验证
- `5137be1` 后不再丢最新成交，仅用 `get_nowait()` 逐出最旧，整秒采样价仍取真实末笔；
- 当时观察到 `storage_degraded` 瞬态出现（≈**4.7 次/时**，p50 **32s**、max **59.4s**），后来证实是队列溢出被错误归类为存储容量问题，并被 60s maintenance 覆写清零；详见 2026-10-29 排查记录。此现象不应再视为正常存储降级。
- commit `5137be1` run #65 已推送部署。

## 适用范围与修订
`strategies/grid/realtime/main.py`（`_pump_feed`）；commit `5137be1`（run #65）已上线。2026-10-29 的后续修复另见[排查记录](../../reports/grid-realtime-shadow-queue-overflow-triage-2026-10-29.md)。

## 教训
「队列溢出该丢哪一端，取决于消费端语义」——采样取末笔时，溢出必须丢最旧、保最新；丢错端会让「数据完整性」表面无损、实际取到滞后价。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/grid_realtime_feed_and_queue_2026-10-13.md)（不随 Git 同步）
- 持久证据: 需求文档 `docs/requirements/grid/Grid_Trading_V2.5.4.md`；commit 5137be1
- 必要证据摘要: 突发峰值 1762 笔/秒 vs `queue_capacity=1000`；修复后仅逐出最旧、采样价取真实末笔；storage_degraded ≈4.7 次/时、p50 32s、max 59.4s（60s maintenance 清零）
- 相关: [币安 @aggTrade 停推叠加未捕获 TimeoutError 致 feed 静默假健康](../issues/Binance_quantitative_trading-20261013-grid-feed-silent-unhealthy.md)
