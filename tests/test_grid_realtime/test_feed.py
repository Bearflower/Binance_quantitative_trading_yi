"""feed.py 测试：消息解析、端点、WS 流重连/去重、REST 补齐、默认工厂失败路径。"""
import json
from types import SimpleNamespace

import aiohttp
import pytest

from strategies.grid.realtime import feed as feed_mod
from strategies.grid.realtime.feed import (FeedInterrupted, TradeFeed,
                                           now_ms, parse_rest_row,
                                           parse_ws_message, rest_url, ws_url)
from strategies.grid.realtime.rules import parse_profile

SYMBOL = "ETHUSDT"


def _ws_payload(tid, price="2700.0", qty="0.1", t=1_000_000, maker=False):
    return {"e": "aggTrade", "s": SYMBOL, "a": tid, "p": price, "q": qty,
            "T": t, "m": maker}


def _msg_text(payload):
    return SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                           data=json.dumps(payload))


def _msg(kind):
    return SimpleNamespace(type=kind, data=None)


class FakeWs:
    """脚本化 WS：按序 receive；记录 close 调用。"""

    def __init__(self, messages):
        self._messages = messages
        self.closed = False

    async def receive(self, timeout=None):
        if not self._messages:
            return _msg(aiohttp.WSMsgType.CLOSED)
        return self._messages.pop(0)

    async def close(self):
        self.closed = True


@pytest.fixture
def cfg(profile_raw):
    parsed = parse_profile(profile_raw)
    return parsed


class _NoSleep:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


# ───────────────────── 解析 ─────────────────────

def test_parse_ws_valid():
    trade = parse_ws_message(_ws_payload(10, maker=True), SYMBOL)
    assert trade.agg_trade_id == 10
    assert str(trade.price) == "2700.0"
    assert trade.is_buyer_maker == 1 and trade.trade_time_ms == 1_000_000


@pytest.mark.parametrize("payload", [
    {"e": "trade", "s": SYMBOL},               # 事件类型错
    {"e": "aggTrade", "s": "BTCUSDT"},         # 品种错
    {"e": "aggTrade", "s": SYMBOL},            # 缺字段
    {"e": "aggTrade", "s": SYMBOL, "a": "x", "p": "1", "q": "1", "T": 1},
    {"e": "aggTrade", "s": SYMBOL, "a": 0, "p": "1", "q": "1", "T": 1},
    {"e": "aggTrade", "s": SYMBOL, "a": 1, "p": "0", "q": "1", "T": 1},
    {"e": "aggTrade", "s": SYMBOL, "a": 1, "p": "1", "q": "1", "T": 0},
    {"e": "aggTrade", "s": SYMBOL, "a": 1, "p": "1", "q": "-1", "T": 1},
])
def test_parse_ws_invalid(payload):
    assert parse_ws_message(payload, SYMBOL) is None


def test_parse_rest_row_valid():
    row = {"a": 5, "p": "100", "q": "2", "T": 9, "m": False}
    trade = parse_rest_row(row, SYMBOL)
    assert trade.agg_trade_id == 5 and trade.is_buyer_maker == 0


def test_parse_rest_row_missing_maker_defaults():
    trade = parse_rest_row({"a": 5, "p": "100", "q": "2", "T": 9}, SYMBOL)
    assert trade is not None and trade.is_buyer_maker == 0


@pytest.mark.parametrize("row", [
    {"a": "x"}, {"a": 0, "p": "1", "q": "1", "T": 1},
    {"a": 1, "p": "1", "q": "1", "T": -1}],
)
def test_parse_rest_row_invalid(row):
    assert parse_rest_row(row, SYMBOL) is None


# ───────────────────── 端点 ─────────────────────

def test_ws_url_default():
    assert ws_url("ETHUSDT") == (
        "wss://fstream.binance.com/ws/ethusdt@aggTrade")


def test_ws_url_env_override(monkeypatch):
    monkeypatch.setenv("BINANCE_WS_BASE_URL", "wss://example.com/")
    assert ws_url("ETHUSDT") == "wss://example.com/ws/ethusdt@aggTrade"


def test_rest_url_default():
    assert rest_url() == "https://fapi.binance.com/fapi/v1/aggTrades"


def test_rest_url_env_override(monkeypatch):
    monkeypatch.setenv("BINANCE_REST_BASE_URL", "https://example.com")
    assert rest_url() == "https://example.com/fapi/v1/aggTrades"


def test_now_ms():
    import time
    value = now_ms()
    assert isinstance(value, int)
    assert abs(value / 1000 - time.time()) < 5


# ───────────────────── WS 流 ─────────────────────

async def test_stream_reconnect_dedup_and_health(cfg):
    ws1 = FakeWs([_msg_text(_ws_payload(10)), _msg_text(_ws_payload(11)),
                  _msg(aiohttp.WSMsgType.CLOSED)])
    ws2 = FakeWs([_msg_text(_ws_payload(12)),
                  _msg(aiohttp.WSMsgType.CLOSED)])
    created = []
    items = []

    async def factory(url):
        assert url == ws_url(SYMBOL)
        if not created:
            created.append(ws1)
            return ws1
        if len(created) == 1:
            created.append(ws2)
            return ws2
        raise RuntimeError("测试终止")

    health_events = []

    async def on_health(healthy, reason):
        health_events.append((healthy, reason))

    sleep = _NoSleep()

    async def rest_fetcher(cursor):
        return []

    feed = TradeFeed(cfg, on_health=on_health, ws_factory=factory,
                     rest_fetcher=rest_fetcher, sleep=sleep)
    with pytest.raises(RuntimeError, match="测试终止"):
        async for trade in feed.stream():
            items.append(trade)
    assert [t.agg_trade_id for t in items] == [10, 11, 12]
    assert feed.last_id == 12
    assert ws1.closed and ws2.closed
    assert (False, "WS 连接关闭或出错") in health_events
    assert (True, "") in health_events
    # 两次断线退避递增：initial=1 → 2
    assert feed._backoff == cfg["transport"]["reconnect_initial_seconds"] * 2


async def test_stream_backoff_increases_after_first_disconnect(cfg):
    """断连无补齐时退避递增封顶（initial=1 → cap=30）。"""
    ws = FakeWs([_msg(aiohttp.WSMsgType.CLOSED)])

    async def factory(url):
        factory.count += 1
        if factory.count > 1:
            raise RuntimeError("测试终止")
        return ws
    factory.count = 0

    feed = TradeFeed(cfg, ws_factory=factory,
                     rest_fetcher=lambda c: _async([]), sleep=_NoSleep())
    with pytest.raises(RuntimeError):
        async for _ in feed.stream():
            pass
    assert feed._backoff == cfg["transport"]["reconnect_initial_seconds"] * 2


async def test_dedup_old_id_skipped(cfg):
    ws = FakeWs([_msg_text(_ws_payload(10)), _msg_text(_ws_payload(10)),
                 _msg_text(_ws_payload(11)),
                 _msg(aiohttp.WSMsgType.CLOSED)])

    async def factory(url):
        if factory.count > 0:
            raise RuntimeError("测试终止")
        factory.count += 1
        return ws
    factory.count = 0

    sleep = _NoSleep()

    async def rest_fetcher(cursor):
        return []

    feed = TradeFeed(cfg, ws_factory=factory, rest_fetcher=rest_fetcher,
                     sleep=sleep)
    items = []
    with pytest.raises(RuntimeError, match="测试终止"):
        async for trade in feed.stream():
            items.append(trade)
    assert [t.agg_trade_id for t in items] == [10, 11]


async def test_non_text_and_bad_json_skipped(cfg):
    ws = FakeWs([_msg(aiohttp.WSMsgType.BINARY),
                 SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                                 data="{bad json"),
                 _msg_text(_ws_payload(10)),
                 _msg(aiohttp.WSMsgType.CLOSED)])

    async def factory(url):
        if factory.count > 0:
            raise RuntimeError("测试终止")
        factory.count += 1
        return ws
    factory.count = 0

    feed = TradeFeed(cfg, ws_factory=factory,
                     rest_fetcher=lambda c: _async([]), sleep=_NoSleep())
    items = []
    with pytest.raises(RuntimeError, match="测试终止"):
        async for trade in feed.stream():
            items.append(trade)
    assert items[0].agg_trade_id == 10


async def _async(value):
    return value


# ───────────────────── REST 补齐 ─────────────────────

def _rest_trade(tid):
    return parse_rest_row(
        {"a": tid, "p": "100", "q": "1", "T": tid, "m": False}, SYMBOL)


async def test_refill_no_cursor_yields_nothing(cfg):
    feed = TradeFeed(cfg, rest_fetcher=lambda c: _async([]))
    items = [t async for t in feed._refill()]
    assert items == []


async def test_refill_short_page(cfg):
    requests = []

    async def fetcher(cursor):
        requests.append(cursor)
        return [_rest_trade(cursor), _rest_trade(cursor + 1)]

    sleep = _NoSleep()
    feed = TradeFeed(cfg, rest_fetcher=fetcher, sleep=sleep)
    feed._last_id = 100
    items = [t async for t in feed._refill()]
    assert [t.agg_trade_id for t in items] == [101, 102]
    assert feed.last_id == 102 and requests == [101]
    assert sleep.calls == []  # 短页不限速等待


async def test_refill_full_then_short(cfg):
    pages = {101: [_rest_trade(i) for i in range(101, 1101)],
             1101: [_rest_trade(1101)]}

    async def fetcher(cursor):
        return pages[cursor]

    sleep = _NoSleep()
    feed = TradeFeed(cfg, rest_fetcher=fetcher, sleep=sleep)
    feed._last_id = 100
    items = [t async for t in feed._refill()]
    assert len(items) == 1001 and items[-1].agg_trade_id == 1101
    assert sleep.calls == [1 / cfg["transport"]["refill_requests_per_second"]]


async def test_refill_stale_ids_filtered(cfg):
    async def fetcher(cursor):
        return [_rest_trade(50), _rest_trade(51)]  # 全部 ≤ last_id

    feed = TradeFeed(cfg, rest_fetcher=fetcher, sleep=_NoSleep())
    feed._last_id = 100
    items = [t async for t in feed._refill()]
    assert items == [] and feed.last_id == 100  # 游标不回退


# ───────────────────── 默认工厂 ─────────────────────

async def test_default_ws_factory_failure_raises_feed_interrupted(
        cfg, monkeypatch):
    class FakeSession:
        async def ws_connect(self, url, heartbeat):
            raise ConnectionError("boom")

        async def close(self):
            self.closed = True

    session = FakeSession()
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    feed = TradeFeed(cfg)
    with pytest.raises(FeedInterrupted):
        await feed._default_ws_factory(ws_url(SYMBOL))
    assert session.closed


async def test_default_ws_factory_success(cfg, monkeypatch):
    class FakeWsObj:
        async def receive(self, timeout):
            return "WS"

        async def close(self):
            pass

    class FakeSession:
        async def ws_connect(self, url, heartbeat):
            return FakeWsObj()

        async def close(self):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    feed = TradeFeed(cfg)
    conn = await feed._default_ws_factory(ws_url(SYMBOL))
    assert isinstance(conn, feed_mod._WsConnection)
    assert await conn.receive(timeout=1) == "WS"
    await conn.close()


async def test_default_rest_fetch_non_200(cfg, monkeypatch):
    class FakeResponse:
        status = 500

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def get(self, url, params):
            return FakeResponse()

        async def close(self):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: FakeSession())
    feed = TradeFeed(cfg)
    assert await feed._default_rest_fetch(1) == []


async def test_default_rest_fetch_200(cfg, monkeypatch):
    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, content_type):
            return [{"a": 1, "p": "100", "q": "1", "T": 1, "m": False}]

    class FakeSession:
        def get(self, url, params):
            return FakeResponse()

        async def close(self):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: FakeSession())
    feed = TradeFeed(cfg)
    rows = await feed._default_rest_fetch(1)
    assert len(rows) == 1 and rows[0].agg_trade_id == 1


def test_ws_connection_close_error_still_closes_session():
    class BrokenWs:
        async def close(self):
            raise RuntimeError("ws close 失败")

    class Session:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    session = Session()
    conn = feed_mod._WsConnection(BrokenWs(), session)
    with pytest.raises(RuntimeError):
        import asyncio
        asyncio.get_event_loop().run_until_complete(conn.close())
    assert session.closed
