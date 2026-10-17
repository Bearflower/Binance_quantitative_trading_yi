"""Offline review: actual production methods, synthetic I/O only."""
import asyncio
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.export_execution_facts import ExecutionFactExporter
from shared.binance_api import BinanceClient
from strategies.hrs.strategy import HRSStrategy
from strategies.new_coin.executor import TradingExecutor
from strategies.new_coin.strategy import FreshQuote

class DB:
    def __init__(self):
        self.rows = []
    async def fetch_one(self, sql, *args):
        assert 'RETURNING id' in sql
        row = dict(id=len(self.rows)+1, mode=args[10], status='triggered', resolution=args[7])
        self.rows.append(row)
        return dict(id=row['id'])
    async def execute(self, sql, *args):
        assert 'WHERE id = $1' in sql
        for row in self.rows:
            if row['id'] == args[0]:
                row.update(status='resolved', resolution=args[1])

async def signals():
    s = object.__new__(HRSStrategy)
    s.config = {'emm': {'execution_mode': 'shadow'}}
    s.db = DB()
    s._filter_timeout_signals = AsyncMock(side_effect=lambda rows, direction: rows)
    s.candidate_pool = SimpleNamespace(get_short_candidates=lambda: [], get_long_candidates=lambda: [])
    s._send_cycle_summary = AsyncMock()
    async def execute(signal):
        signal['shadow_blocked'] = signal['entry_mode'] == 'emm'
        return True
    s.execute_signal = execute
    rows = [dict(symbol='TESTUSDT', direction='short', score=8, current_price=100, entry_mode=m) for m in ('standard','emm')]
    await s._finalize_and_execute_signals(rows, [], ['TESTUSDT'])
    assert [r['resolution'] for r in s.db.rows] == ['executed', 'shadow']
    return s.db.rows

async def reprice():
    e = object.__new__(TradingExecutor)
    e.config = {'scoring': {'rebound_check': {'max_price_age_seconds': 5}}}
    e.baseline_ready = e._leverage_valid = True
    e.leverage, e.market_order_score_threshold, e.min_notional = 2, 7, Decimal('5')
    e._get_account_balance = AsyncMock(return_value=1000)
    e._is_symbol_occupied = AsyncMock(return_value=False)
    e._claim_symbol = AsyncMock(return_value={'claimed': True})
    e._get_symbol_precision = AsyncMock(return_value=(Decimal('.01'), Decimal('.001')))
    e._format_quantity = lambda qty, step: (qty // step) * step
    e._resolve_open_margin_budget = AsyncMock(side_effect=lambda symbol, price: (Decimal('100')/Decimal(str(price)), ''))
    e._set_leverage = AsyncMock()
    e._release_claim_quietly = AsyncMock()
    e._place_short_order = AsyncMock(return_value=None)
    old = FreshQuote(Decimal('100'), None, 0, 0, 'request_span')
    now = int(time.time()*1000)
    new = FreshQuote(Decimal('120'), None, now, now, 'request_span')
    await e.execute_short('TESTUSDT', {'total_score': 8}, current_price=100, quote=old, entry_guard=AsyncMock(return_value=(new,None)))
    args = e._place_short_order.call_args.args
    assert e._resolve_open_margin_budget.await_count == 2
    assert args[1] == Decimal('.833')
    assert args[1]*args[2]/2 <= 50
    return dict(budget_checks=2, quantity=str(args[1]), final_price=str(args[2]), margin=str(args[1]*args[2]/2))

async def fills(times, end):
    rows = [dict(id=i+1,symbol='TESTUSDT',time=t,orderId=i+1,qty='1',price='100',side='BUY',commission='0',commissionAsset='USDT',realizedPnl='0') for i,t in enumerate(times)]
    api = object.__new__(BinanceClient)
    api.use_unified_account = True
    calls = []
    async def request(method, endpoint, params, **kw):
        calls.append(dict(params))
        assert not ('fromId' in params and ('startTime' in params or 'endTime' in params))
        selected = rows
        if 'fromId' in params:
            selected = [r for r in selected if r['id'] >= params['fromId']]
        else:
            selected = [r for r in selected if params['startTime'] <= r['time'] <= params['endTime']]
        return selected[:params['limit']]
    api._request = request
    result = await ExecutionFactExporter(account_id='test',api_client=api,page_limit=2,max_pages=10).export_fills('TESTUSDT',1,end)
    return dict(expected=len(rows), actual=len(result.records), ids=[r.trade_id for r in result.records], complete=result.page_complete, calls=calls, errors=result.errors)

async def main():
    interior = await fills([500]*5,1000)
    boundary = await fills([1000]*5,1000)
    assert interior['actual'] == 5 and interior['complete']
    # 修复：边界同毫秒 5 条应完整取回，不再因时间墙提前停
    assert boundary['actual'] == 5 and boundary['complete']
    assert len(boundary['calls']) >= 2  # 至少 2 次：第一次满页，续传确认排空
    results = dict(signals=await signals(), reprice=await reprice(), interior=interior, boundary=boundary)
    Path(__file__).with_name('results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(results,ensure_ascii=False,indent=2))
asyncio.run(main())
