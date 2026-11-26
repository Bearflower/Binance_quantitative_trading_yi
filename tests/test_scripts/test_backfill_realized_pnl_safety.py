"""CR-008 使用数据库和 API 模拟对象；运行入口前替换所有联网调用。"""
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock
import time
import pytest
from scripts import backfill_realized_pnl as script


class Database:
    def __init__(self, fail_update=False):
        self.writes = []
        self.existing_pnl = Decimal('12.34')
        self.new_pnl = None
        self.fail_update = fail_update
        self.transactions = 0
        self.queries = []
    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return [dict(id=1, symbol='BTCUSDT', order_id='123', side='SELL',
                     account_id='offline', strategy='hrs', order_type='MARKET',
                     executed_at=datetime.now(), commission=0)]
    async def fetchrow(self, *args):
        return dict(total=2, with_pnl=1, non_zero_pnl=1, total_pnl=self.existing_pnl)
    async def execute(self, query, *args):
        self.writes.append((query, args))
        if 'SET realized_pnl = NULL' in query: self.existing_pnl = None
        else:
            self.new_pnl = args[0]
            if self.fail_update: raise RuntimeError('模拟写入失败')
        return 'UPDATE 1'
    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        previous = self.new_pnl
        try: yield
        except Exception:
            self.new_pnl = previous
            raise
    async def close(self): pass


@pytest.fixture
def offline(monkeypatch):
    for key in ("BINANCE_ACCOUNT_MODE", "BINANCE_USE_PM", "USE_UNIFIED_ACCOUNT", "BINANCE_CLASSIC_CUTOVER_MS"):
        monkeypatch.delenv(key, raising=False)
    db = Database()
    monkeypatch.setenv('BINANCE_API_KEY', 'offline')
    monkeypatch.setenv('BINANCE_API_SECRET', 'offline')
    monkeypatch.setenv('TRADE_ACCOUNT_ID', 'offline')
    monkeypatch.setattr(script, 'get_db_connection', AsyncMock(return_value=db))
    monkeypatch.setattr(script, 'log', lambda *a, **k: None)
    monkeypatch.setattr(script, 'START_TIME_MS', int(time.time() * 1000) - 1000)
    order = dict(symbol='BTCUSDT', orderId=123, side='SELL', status='FILLED', origQty='0.3', executedQty='0.3')
    trades = [dict(symbol='BTCUSDT', orderId=123, side='SELL', id=1, qty='0.1', price='100', realizedPnl='1'),
              dict(symbol='BTCUSDT', orderId=123, side='SELL', id=2, qty='0.2', price='103', realizedPnl='-0.4')]
    async def request(self, method, path, params=None):
        if path.endswith('/userTrades'): return trades
        if path.endswith('/order'): return order
        raise RuntimeError('测试不允许启发式匹配或真实请求')
    monkeypatch.setattr(script.BinanceBackfillClient, '_request', request)
    return db, order, trades


@pytest.mark.asyncio
async def test_outage_preserves_existing_pnl_and_fails(offline, monkeypatch):
    db, _, _ = offline
    monkeypatch.setattr(script.BinanceBackfillClient, '_request', AsyncMock(side_effect=RuntimeError('offline outage')))
    assert await script.main() == 1
    assert db.existing_pnl == Decimal('12.34')
    assert db.writes == []


@pytest.mark.asyncio
async def test_only_exact_order_fills_can_backfill_null_pnl(offline):
    db, _, _ = offline
    assert await script.main() == 0
    assert db.existing_pnl == Decimal('12.34')
    assert db.new_pnl == Decimal('0.6')
    assert db.transactions == 1
    assert len(db.writes) == 1
    sql, args = db.writes[0]
    assert 'realized_pnl IS NULL' in sql
    assert 'account_id' in sql and 'order_id' in sql and 'exchange_order_matched' in sql


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['wrong_order', 'incomplete', 'full_page', 'nonfinite'])
async def test_unverified_order_evidence_writes_nothing(offline, bad):
    db, order, trades = offline
    if bad == 'wrong_order': trades[0]['orderId'] = 999
    if bad == 'incomplete': trades.pop()
    if bad == 'full_page': trades *= 500
    if bad == 'nonfinite': trades[0]['realizedPnl'] = 'NaN'
    assert await script.main() == 1
    assert db.writes == []
    assert db.existing_pnl == Decimal('12.34')


@pytest.mark.asyncio
async def test_write_failure_rolls_back_backfill(offline):
    db, _, _ = offline
    db.fail_update = True
    assert await script.main() == 1
    assert db.transactions == 1
    assert db.new_pnl is None
    assert db.existing_pnl == Decimal('12.34')


@pytest.mark.parametrize('mode,prefix,base', [
    ('pm', '/papi/v1/um', 'https://papi.binance.com'),
    ('classic', '/fapi/v1', 'https://fapi.binance.com'),
])
def test_backfill_client_uses_configured_account_mode(monkeypatch, mode, prefix, base):
    for key in ('BINANCE_USE_PM', 'USE_UNIFIED_ACCOUNT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('BINANCE_ACCOUNT_MODE', mode)
    client = script.BinanceBackfillClient('offline', 'offline')
    assert client.base_url == base
    assert client.api_prefix == prefix


@pytest.mark.asyncio
async def test_classic_backfill_cannot_cross_mode_boundary(offline, monkeypatch):
    db, _, _ = offline
    monkeypatch.setenv('BINANCE_ACCOUNT_MODE', 'classic')
    assert await script.main() == 1
    assert db.queries == db.writes == []
    cutover = 1795536000000
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS', str(cutover))
    assert await script.main() == 0
    query, args = db.queries[0]
    assert 'AND executed_at >= $3' in query
    assert args[2] == datetime(2026, 11, 25, 0, 0)


@pytest.mark.asyncio
async def test_pm_backfill_rejects_classic_rows_even_if_database_returns_one(offline, monkeypatch):
    db, _, _ = offline
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS', '1795536000000')
    assert await script.main() == 1
    assert 'AND executed_at < $3' in db.queries[0][0]
    assert db.writes == []
