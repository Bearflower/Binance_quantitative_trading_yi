"""Offline review probes. No real API, database or order submission."""
import asyncio
import json
import sys
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from strategies.hrs.strategy import HRSStrategy
from strategies.new_coin.executor import TradingExecutor
from strategies.new_coin.strategy import FreshQuote
from shared.binance_api import BinanceClient
from scripts.export_execution_facts import ExecutionFactExporter


class SignalDB:
    """Model INSERT/UPDATE predicates from real SQL; no PostgreSQL required."""
    def __init__(self):
        self.rows = []

    async def execute(self, sql, *args):
        if 'INSERT INTO hrs.hrs_signals' in sql:
            self.rows.append(dict(symbol=args[0], direction=args[1], status='triggered', resolution=args[7]))
        elif 'UPDATE hrs.hrs_signals' in sql:
            assert "symbol = $1 AND direction = $2 AND status = 'triggered'" in sql
            for row in self.rows:
                if row['symbol'] == args[0] and row['direction'] == args[1] and row['status'] == 'triggered':
                    row.update(status='resolved', resolution=args[2])
        else:
            raise AssertionError(sql)


async def signal_probe():
    s = object.__new__(HRSStrategy)
    s.config = {"emm": {"execution_mode": "shadow"}}
    s.db = SignalDB()
    s._filter_timeout_signals = AsyncMock(side_effect=lambda signals, direction: signals)
    s.candidate_pool = SimpleNamespace(get_short_candidates=lambda: [], get_long_candidates=lambda: [])
    s._send_cycle_summary = AsyncMock()
    async def execute(signal):
        signal['shadow_blocked'] = signal['entry_mode'] == 'emm'
        return True
    s.execute_signal = execute
    signals = [dict(symbol='TESTUSDT', direction='short', score=8, entry_mode=mode, current_price=100) for mode in ('standard', 'emm')]
    await s._finalize_and_execute_signals(signals, [], ['TESTUSDT'])
    assert [r['resolution'] for r in s.db.rows] == ['executed', 'executed']
    return s.db.rows


async def price_probe():
    e = object.__new__(TradingExecutor)
    e.config = {'scoring': {'rebound_check': {'max_price_age_seconds': 5}}}
    e.baseline_ready = True
    e._leverage_valid = True
    e.leverage = 2
    e.market_order_score_threshold = 7
    e._get_account_balance = AsyncMock(return_value=1000)
    e._is_symbol_occupied = AsyncMock(return_value=False)
    e._claim_symbol = AsyncMock(return_value={'claimed': True})
    e._resolve_open_margin_budget = AsyncMock(return_value=(Decimal('1'), ''))
    e._get_symbol_precision = AsyncMock(return_value=(Decimal('.01'), Decimal('.01')))
    e._format_quantity = lambda qty, step: qty
    e._set_leverage = AsyncMock()
    e._release_claim_quietly = AsyncMock()
    e._place_short_order = AsyncMock(return_value=None)  # stop before fill handling
    old = FreshQuote(Decimal('100'), None, 0, 0, 'request_span')
    import time
    now = int(time.time() * 1000)
    new = FreshQuote(Decimal('120'), None, now, now, 'request_span')
    guard = AsyncMock(return_value=(new, None))
    await e.execute_short('TESTUSDT', {'total_score': 8}, current_price=100, quote=old, entry_guard=guard)
    args = e._place_short_order.call_args.args
    assert args[1:3] == (Decimal('1'), Decimal('120'))
    assert e._resolve_open_margin_budget.await_count == 1
    return dict(budget_price=e._resolve_open_margin_budget.call_args.args[1], quantity=str(args[1]), order_price=str(args[2]), old_margin=50, new_margin=60, budget_checks=1)


def fill_row(i):
    return dict(id=i, symbol='TESTUSDT', time=1000, orderId=i, qty='1', price='100', side='BUY', commission='0', commissionAsset='USDT', realizedPnl='0')


async def pagination_probe():
    api = object.__new__(BinanceClient)
    api.use_unified_account = True
    calls = []
    async def request(method, endpoint, params, **kw):
        calls.append(dict(params))
        if 'fromId' in params and ('startTime' in params or 'endTime' in params):
            raise ValueError('Official API contract: fromId cannot be sent with startTime or endTime')
        return [fill_row(1), fill_row(2)]
    api._request = request
    result = await ExecutionFactExporter(account_id='test', api_client=api, page_limit=2).export_fills('TESTUSDT', 1000, 2000)
    assert not result.page_complete and len(result.records) == 2
    assert 'fromId' in calls[-1] and 'startTime' in calls[-1]
    return dict(calls=calls, records=len(result.records), complete=result.page_complete, errors=result.errors)


async def income_probe():
    calls = []
    async def get_income_history(**kw):
        calls.append(kw)
        # Three events in same millisecond, only first two fit the first page.
        return [dict(tranId=i, symbol='TESTUSDT', incomeType='REALIZED_PNL', income='1', asset='USDT', time=1000) for i in (1, 2)]
    result = await ExecutionFactExporter(account_id='test', api_client=SimpleNamespace(get_income_history=get_income_history), page_limit=2).export_income(1000, 2000)
    assert len(result.records) == 2 and not result.page_complete
    return dict(available=3, collected=len(result.records), complete=result.page_complete, errors=result.errors, calls=calls)


async def main():
    result = dict(signal_resolution=await signal_probe(), refreshed_quantity=await price_probe(), fills_contract=await pagination_probe(), income_same_ms=await income_probe())
    Path(__file__).with_name('reproduction_results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

if __name__ == '__main__':
    asyncio.run(main())
