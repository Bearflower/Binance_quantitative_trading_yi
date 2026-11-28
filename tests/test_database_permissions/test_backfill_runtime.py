"""CR-008：用现行建表 SQL 和普通运行角色验证真实 asyncpg 参数类型。"""
from decimal import Decimal
import asyncpg
from unittest.mock import AsyncMock
from pathlib import Path
import pytest
from scripts import backfill_realized_pnl as script
from tests.test_database_permissions.test_contract import pg


@pytest.mark.asyncio
async def test_backfill_current_ddl_as_runtime_role(pg, monkeypatch):
    env, sql, _ = pg
    ddl = (Path(__file__).resolve().parents[2] / 'database/postgres/init-scripts/03-create-trade-records.sql').read_text()
    # 仅临时库复刻清单内 trade_records 的存量 trading_user owner。
    assert sql(ddl + 'ALTER TABLE trading.trade_records OWNER TO trading_user;').returncode == 0
    conn = await asyncpg.connect(host=env['PGHOST'], port=int(env['PGPORT']), database='postgres', user='trading_user')
    await conn.execute("INSERT INTO trading.trade_records (account_id,strategy,symbol,order_id,side,order_type,status,realized_pnl) VALUES ('offline','hrs','BTCUSDT','123','SELL','MARKET','FILLED',NULL),('offline','hrs','BTCUSDT','124','SELL','MARKET','FILLED',12.34)")
    monkeypatch.setenv('BINANCE_API_KEY', 'offline')
    monkeypatch.setenv('BINANCE_API_SECRET', 'offline')
    monkeypatch.setenv('TRADE_ACCOUNT_ID', 'offline')
    monkeypatch.setattr(script, 'get_db_connection', AsyncMock(return_value=conn))
    async def request(self, method, endpoint, params=None):
        if endpoint.endswith('/order'):
            return dict(symbol='BTCUSDT', orderId=123, side='SELL', status='FILLED', origQty='0.3', executedQty='0.3')
        assert endpoint.endswith('/userTrades')
        return [dict(symbol='BTCUSDT', orderId=123, side='SELL', id=1, qty='0.3', price='100', realizedPnl='0.6')]
    monkeypatch.setattr(script.BinanceBackfillClient, '_request', request)
    assert await script.main() == 0
    verify = await asyncpg.connect(host=env['PGHOST'], port=int(env['PGPORT']), database='postgres', user='trading_user')
    try:
        rows = await verify.fetch('SELECT order_id,realized_pnl,pnl_source,pg_typeof(realized_pnl)::text AS type FROM trading.trade_records ORDER BY order_id')
        assert rows[0]['realized_pnl'] == Decimal('0.6')
        assert rows[0]['pnl_source'] == 'exchange_order_matched'
        assert rows[0]['type'] == 'numeric'
        assert rows[1]['realized_pnl'] == Decimal('12.34')
    finally:
        await verify.close()
