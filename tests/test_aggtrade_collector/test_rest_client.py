"""REST 客户端测试：本地假服务器验证参数互斥、切片翻页、重试/放弃口径（§1.1）。"""
import asyncio
from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web

from services.aggtrade_collector.config import CollectorConfig
from services.aggtrade_collector.rest_client import (
    AggTradesRESTClient, RESTClientError,
)

HOUR = 3600 * 1000


@pytest.fixture()
def cfg(tmp_path):
    return CollectorConfig(
        symbol="ETHUSDT", base_url="http://127.0.0.1:0",
        agg_trades_path="/fapi/v1/aggTrades", timeout_seconds=5, page_limit=3,
        slice_hours=1, max_retries=3, backoff_base_seconds=0.01,
        page_sleep_min_seconds=0.0, page_sleep_max_seconds=0.0,
        rate_limit_wait_seconds=0.02, rate_limit_max_wait_seconds=0.05,
        db_path=tmp_path / "x.sqlite", agg_trades_retention_hours=72,
        busy_timeout_ms=1000, insert_batch_size=2000, lookback_hours=48,
        daemon_incremental_interval_seconds=10,
        daemon_cleanup_interval_seconds=3600,
        sample_path=tmp_path / "s.json", sample_sha256="x",
        manifest_path=tmp_path / "m.json",
        validation_start_beijing="2026-10-08T00:00:00+08:00",
        validation_min_full_days=30, base_dir=tmp_path)


def _trade(trade_id, t, price="100"):
    return {"a": trade_id, "p": price, "q": "1", "f": trade_id, "l": trade_id,
            "T": t, "m": False}


class FakeBinance:
    """按币安参数语义响应的假 aggTrades 服务。"""

    def __init__(self):
        self.calls = []
        self.fail_times = 0          # 前 N 次返回失败状态
        self.fail_status = 429
        self.retry_after = None      # 失败响应携带的 Retry-After 头（字符串原样返回）

    def handler(self, request):
        self.calls.append(dict(request.query))
        if len(self.calls) <= self.fail_times:
            headers = None
            if self.fail_status == 429 and self.retry_after is not None:
                headers = {"Retry-After": self.retry_after}
            return web.Response(status=self.fail_status, text="limited",
                                headers=headers)
        q = request.query
        rows = self._rows_for(q)
        return web.json_response(rows)

    def _rows_for(self, q):
        limit = int(q.get("limit", "3"))
        all_rows = [_trade(i, 1000 * i) for i in range(1, 40)]
        if "fromId" in q:
            start = int(q["fromId"])
            return [r for r in all_rows if r["a"] >= start][:limit]
        start, end = int(q["startTime"]), int(q["endTime"])
        return [r for r in all_rows if start <= r["T"] <= end][:limit]


@pytest.fixture()
async def server(cfg):
    fake = FakeBinance()
    app = web.Application()
    app.router.add_get("/fapi/v1/aggTrades", fake.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    yield replace(cfg, base_url=f"http://127.0.0.1:{port}"), fake
    await runner.cleanup()


async def _client(cfg):
    client = AggTradesRESTClient(cfg)
    await client.__aenter__()
    return client


async def test_fetch_single_page_mapping(server):
    cfg, fake = server
    client = await _client(cfg)
    try:
        rows = await client.fetch_agg_trades("ETHUSDT", start_time=0, end_time=5000)
        assert [r["agg_trade_id"] for r in rows] == [1, 2, 3]
        assert rows[0]["trade_time_ms"] == 1000
        assert "received_at_ms" in rows[0]
    finally:
        await client.__aexit__(None, None, None)


async def test_params_mutually_exclusive(server):
    cfg, _ = server
    client = await _client(cfg)
    try:
        with pytest.raises(ValueError):
            await client.fetch_agg_trades("ETHUSDT", start_time=1, from_id=2)
        with pytest.raises(ValueError):
            await client.fetch_agg_trades("ETHUSDT", start_time=1)
    finally:
        await client.__aexit__(None, None, None)


async def test_range_slice_with_fromid_paging(server, tmp_path):
    """轨道A：1h 切片满页以末条 a 作 fromId 翻页（游标含、流内可重复）；
    重复由落库层 INSERT OR IGNORE 幂等消除（§1.1/§1.2），按 [start,end) 截断。"""
    from services.aggtrade_collector import repository as repo
    cfg, fake = server
    conn = repo.open_db(tmp_path / "r.sqlite", 1000)
    client = await _client(cfg)
    raw_count = 0
    try:
        async for t in client.fetch_range_agg_trades("ETHUSDT", 0, HOUR):
            raw_count += 1
            with conn:
                repo.upsert_agg_trades(conn, [t])
    finally:
        await client.__aexit__(None, None, None)
    ids = [r[0] for r in conn.execute(
        "SELECT agg_trade_id FROM agg_trades ORDER BY agg_trade_id")]
    # 流内确有游标重复行（fromId 含末条），但落库后无重复、无越界、覆盖完整
    assert raw_count > len(ids)
    assert ids == list(range(1, 40))
    assert conn.execute(
        "SELECT COUNT(*) FROM agg_trades WHERE trade_time_ms >= ?", (HOUR,)).fetchone()[0] == 0
    assert len(fake.calls) > 1
    conn.close()


async def test_incremental_until_now(server):
    """轨道B：fromId=库内 max 起逐页，until_ms 截断。"""
    cfg, _ = server
    client = await _client(cfg)
    try:
        ids = [t["agg_trade_id"] async for t in
               client.fetch_incremental_agg_trades("ETHUSDT", 10, until_ms=12_000)]
    finally:
        await client.__aexit__(None, None, None)
    assert ids[0] == 10            # 从库内 max（含）起，重复靠幂等
    assert ids[-1] == 12
    assert ids == sorted(ids)


async def test_429_retried_then_success(server):
    cfg, fake = server
    fake.fail_times = 2
    client = await _client(cfg)
    try:
        rows = await client.fetch_agg_trades("ETHUSDT", start_time=0, end_time=5000)
    finally:
        await client.__aexit__(None, None, None)
    assert len(rows) == 3
    assert len(fake.calls) == 3    # 两次 429 + 一次成功


async def test_4xx_fails_fast_no_retry(server):
    cfg, fake = server
    fake.fail_times = 99
    fake.fail_status = 400
    client = await _client(cfg)
    try:
        with pytest.raises(RESTClientError) as exc:
            await client.fetch_agg_trades("ETHUSDT", start_time=0, end_time=5000)
        assert exc.value.status == 400
    finally:
        await client.__aexit__(None, None, None)
    assert len(fake.calls) == 1    # 4xx 直接放弃，不重试


async def _start_server(cfg, rows, fail_times=0, fail_status=500, retry_after=None):
    """按币安参数语义起一次性假服务器，返回 (替换 base_url 的 cfg, runner, 调用计数)。

    retry_after 非 None 时，失败响应携带 Retry-After 头（仅对 429 生效）。
    """
    state = {"calls": 0}

    async def handler(request):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            headers = None
            if fail_status == 429 and retry_after is not None:
                headers = {"Retry-After": retry_after}
            return web.Response(status=fail_status, text="limited", headers=headers)
        q = request.query
        limit = int(q.get("limit", "3"))
        if "fromId" in q:
            body = [r for r in rows if r["a"] >= int(q["fromId"])][:limit]
        elif "startTime" in q:
            body = [r for r in rows
                    if int(q["startTime"]) <= r["T"] <= int(q["endTime"])][:limit]
        else:
            # 裸 limit 请求：币安返回最新若干笔，测试取前 limit 笔
            body = rows[:limit]
        return web.json_response(body)

    app = web.Application()
    app.router.add_get("/fapi/v1/aggTrades", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return replace(cfg, base_url=f"http://127.0.0.1:{port}"), runner, state


async def test_aexit_without_enter_is_noop(cfg):
    """未进入 async with 就退出：session 为 None，不报错（幂等关闭）。"""
    client = AggTradesRESTClient(cfg)
    await client.__aexit__(None, None, None)


async def test_window_over_one_hour_rejected(server):
    """startTime+endTime 窗口超 1h：本地直接拒绝，不发请求（币安硬约束）。"""
    cfg, fake = server
    client = await _client(cfg)
    try:
        with pytest.raises(ValueError):
            await client.fetch_agg_trades("ETHUSDT", start_time=0, end_time=HOUR + 1)
    finally:
        await client.__aexit__(None, None, None)
    assert fake.calls == []


async def test_5xx_retry_exhausted_raises(cfg, monkeypatch):
    """5xx 可重试，退避 3 次仍失败 -> RESTClientError（共 4 次请求，指数等待）。"""
    c, runner, state = await _start_server(cfg, [_trade(1, 1000)],
                                           fail_times=99, fail_status=500)
    client = await _client(c)
    waits = _record_sleeps(client, monkeypatch)
    try:
        with pytest.raises(RESTClientError):
            await client.fetch_agg_trades("ETHUSDT", start_time=0, end_time=5000)
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    assert state["calls"] == 4
    assert waits == [0.01, 0.02, 0.04]   # 5xx 走指数退避，不走限流窗口


def _record_sleeps(client, monkeypatch):
    """替换客户端 sleep 入口记录等待秒数（实例属性不经过绑定方法，故不收 self）。"""
    waits = []

    async def rec_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(client, "_sleep", rec_sleep)
    return waits


async def test_429_waits_retry_after_header_then_succeeds(cfg, monkeypatch):
    """429 带 Retry-After：按头值等待两次后成功，等待不受指数退避影响。"""
    c, runner, state = await _start_server(
        cfg, [_trade(1, 1000)], fail_times=2, fail_status=429, retry_after="0.03")
    client = await _client(c)
    waits = _record_sleeps(client, monkeypatch)
    try:
        rows = await client.fetch_agg_trades("ETHUSDT", start_time=0, end_time=5000)
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    assert [r["agg_trade_id"] for r in rows] == [1]
    assert state["calls"] == 3
    assert waits == [0.03, 0.03]


async def test_429_retry_after_capped(cfg, monkeypatch):
    """Retry-After 超过封顶：按 rate_limit_max_wait_seconds 截断。"""
    c, runner, state = await _start_server(
        cfg, [_trade(1, 1000)], fail_times=1, fail_status=429, retry_after="999")
    client = await _client(c)
    waits = _record_sleeps(client, monkeypatch)
    try:
        await client.fetch_agg_trades("ETHUSDT", start_time=0, end_time=5000)
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    assert state["calls"] == 2
    assert waits == [0.05]


async def test_429_without_header_uses_default_window(cfg, monkeypatch):
    """429 无 Retry-After 头：回退 rate_limit_wait_seconds 默认窗口。"""
    c, runner, state = await _start_server(
        cfg, [_trade(1, 1000)], fail_times=1, fail_status=429)
    client = await _client(c)
    waits = _record_sleeps(client, monkeypatch)
    try:
        await client.fetch_agg_trades("ETHUSDT", start_time=0, end_time=5000)
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    assert state["calls"] == 2
    assert waits == [0.02]


async def test_429_malformed_header_falls_back_to_default(cfg, monkeypatch):
    """Retry-After 头非数字：无法解析，回退默认限流窗口。"""
    c, runner, state = await _start_server(
        cfg, [_trade(1, 1000)], fail_times=1, fail_status=429,
        retry_after="soon")
    client = await _client(c)
    waits = _record_sleeps(client, monkeypatch)
    try:
        await client.fetch_agg_trades("ETHUSDT", start_time=0, end_time=5000)
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    assert state["calls"] == 2
    assert waits == [0.02]


async def test_429_retry_exhausted_raises_429(cfg, monkeypatch):
    """持续 429：重试 3 次后抛 RESTClientError 且保留 status=429（共 4 次请求）。"""
    c, runner, state = await _start_server(
        cfg, [_trade(1, 1000)], fail_times=99, fail_status=429, retry_after="0.03")
    client = await _client(c)
    waits = _record_sleeps(client, monkeypatch)
    try:
        with pytest.raises(RESTClientError) as exc:
            await client.fetch_agg_trades("ETHUSDT", start_time=0, end_time=5000)
        assert exc.value.status == 429
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    assert state["calls"] == 4
    assert waits == [0.03, 0.03, 0.03]


@pytest.mark.parametrize("exc_factory", [
    lambda: aiohttp.ClientConnectionError("连接被拒"),
    lambda: aiohttp.ClientSSLError(None, OSError("SSL 握手失败")),
    lambda: asyncio.TimeoutError(),
])
async def test_retryable_transport_errors_exhaust(cfg, monkeypatch, exc_factory):
    """连接/SSL/超时三类瞬时故障走重试退避，末次后抛 RESTClientError。"""
    from unittest.mock import MagicMock

    client = await _client(cfg)
    monkeypatch.setattr(client._session, "get",
                        MagicMock(side_effect=exc_factory()))
    try:
        with pytest.raises(RESTClientError):
            await client.fetch_agg_trades("ETHUSDT", start_time=0, end_time=1000)
    finally:
        await client.__aexit__(None, None, None)


async def test_non_retryable_client_error_raises(cfg, monkeypatch):
    """非连接/SSL/响应类 ClientError 不重试，直接包成 RESTClientError。"""
    from unittest.mock import MagicMock

    client = await _client(cfg)
    monkeypatch.setattr(client._session, "get",
                        MagicMock(side_effect=aiohttp.ClientError("协议故障")))
    try:
        with pytest.raises(RESTClientError):
            await client.fetch_agg_trades("ETHUSDT", start_time=0, end_time=1000)
    finally:
        await client.__aexit__(None, None, None)


async def test_range_empty_first_page_yields_nothing(cfg):
    """切片首页为空：游标 None 不翻页，该切片零产出（134->136/137 退出分支）。"""
    c, runner, _ = await _start_server(cfg, [_trade(1, 1000)])
    client = await _client(c)
    try:
        ids = [t["agg_trade_id"] async for t in
               client.fetch_range_agg_trades("ETHUSDT", 100_000, 100_000 + HOUR)]
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    assert ids == []


async def test_range_slice_paginates_and_stops_at_slice_end(cfg):
    """切片内满页翻页：游标含末条致流内重复（落库去重），越界行见即停。"""
    rows = [_trade(1, 100), _trade(2, 1100), _trade(3, 1200),
            _trade(4, 2500), _trade(5, 3500)]
    c, runner, _ = await _start_server(cfg, rows)
    client = await _client(c)
    try:
        ids = [t["agg_trade_id"] async for t in
               client.fetch_range_agg_trades("ETHUSDT", 0, 2000)]
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    # id3 作为 fromId 游标在第二页重复出现但仍在窗内；id4/id5 越界被丢弃
    assert ids == [1, 2, 3, 3]


async def test_incremental_partial_last_page(cfg):
    """轨道B 多页推进：满页后游标+1 续拉，末页不足 limit 结束。"""
    rows = [_trade(i, 1000 * i) for i in range(1, 8)]
    c, runner, _ = await _start_server(cfg, rows)
    client = await _client(c)
    try:
        ids = [t["agg_trade_id"] async for t in
               client.fetch_incremental_agg_trades("ETHUSDT", 1)]
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    assert ids == [1, 2, 3, 4, 5, 6, 7]


async def test_incremental_empty_page_ends(cfg):
    """整页恰好满 limit 后再拉返回空页：空页结束。"""
    rows = [_trade(1, 1000), _trade(2, 2000), _trade(3, 3000)]
    c, runner, state = await _start_server(cfg, rows)
    client = await _client(c)
    try:
        ids = [t["agg_trade_id"] async for t in
               client.fetch_incremental_agg_trades("ETHUSDT", 1)]
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    assert ids == [1, 2, 3]
    assert state["calls"] == 2   # 一满页 + 一空页


async def test_incremental_until_filters_later_trades(cfg):
    """until_ms 截断：窗口外的行不产出，末行越过 until 即停（165 假分支 + 169）。"""
    rows = [_trade(i, 1000 * i) for i in range(1, 6)]
    c, runner, _ = await _start_server(cfg, rows)
    client = await _client(c)
    try:
        ids = [t["agg_trade_id"] async for t in
               client.fetch_incremental_agg_trades("ETHUSDT", 1, until_ms=2000)]
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    assert ids == [1, 2]


async def test_limit_only_request(cfg):
    """裸 limit 请求（不给时间窗/fromId）：参数仅 symbol+limit，正常返回。"""
    c, runner, _ = await _start_server(cfg, [_trade(i, 1000 * i) for i in range(1, 6)])
    client = await _client(c)
    try:
        rows = await client.fetch_agg_trades("ETHUSDT")
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    assert [r["agg_trade_id"] for r in rows] == [1, 2, 3]


async def test_range_multiple_slices(server):
    """跨两个 1h 切片：首切片翻页取完，次切片空首页零产出（126->124/135->137）。"""
    cfg, _ = server
    client = await _client(cfg)
    try:
        ids = sorted({t["agg_trade_id"] async for t in
                      client.fetch_range_agg_trades("ETHUSDT", 0, 2 * HOUR + 1)})
    finally:
        await client.__aexit__(None, None, None)
    assert ids == list(range(1, 40))


async def test_range_page_stops_when_overrunning_slice_end(cfg):
    """翻页第二页起出现越界行：界内行（含游标重复行）产出、越界行丢弃即停。"""
    rows = [_trade(1, 100), _trade(2, 1100), _trade(3, 1200),
            _trade(4, 2500), _trade(5, 2600), _trade(6, 2700)]
    c, runner, _ = await _start_server(cfg, rows)
    client = await _client(c)
    try:
        ids = [t["agg_trade_id"] async for t in
               client.fetch_range_agg_trades("ETHUSDT", 0, 2000)]
    finally:
        await client.__aexit__(None, None, None)
        await runner.cleanup()
    # 第二页 fromId=3 含游标：id3 在界内重复产出，id4/id5 越界丢弃并触发停止
    assert ids == [1, 2, 3, 3]
