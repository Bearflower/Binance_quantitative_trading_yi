import sys,asyncio,pathlib,json,tempfile
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock,patch
sys.path.insert(0,str(pathlib.Path.cwd()))
from strategies.new_coin.executor import TradingExecutor
from strategies.hrs.strategy import HRSStrategy
from ai_tuner.cleanup.orphan_cleanup import OrphanCleanupJob
from ai_tuner.adapters.mtpcs_adapter import MTPCSAdapter
from ai_tuner.engine.response_parser import ResponseParser
from shared.circuit_breaker import CircuitBreaker,load_circuit_breaker_config,floor_index_hour
results={}
async def newcoin_close():
 e=TradingExecutor.__new__(TradingExecutor)
 e.config={'trading':{'close_position':{'max_retries':1,'retry_interval':0,'poll_interval':1,'timeout':1}}}
 e._get_symbol_precision=AsyncMock(return_value=(Decimal('.01'),Decimal('.1')))
 e._update_short_position_closed=AsyncMock()
 calls=[]; position=Decimal('-1')
 async def place(**kw):
  nonlocal position
  calls.append({k:str(v) for k,v in kw.items()})
  position+=Decimal('.4') if len(calls)==1 else kw['quantity']
  return {'orderId':len(calls),'status':'PARTIALLY_FILLED' if len(calls)==1 else 'FILLED'}
 e.binance_api=SimpleNamespace(get_position=AsyncMock(return_value=[{'positionAmt':'-1'}]),get_ticker=AsyncMock(return_value={'lastPrice':'100'}),get_orderbook=AsyncMock(return_value={'bids':[['100','1']]}),place_order=place,get_open_orders=AsyncMock(side_effect=[[{'orderId':1}],[]]),cancel_order=AsyncMock(return_value={'executedQty':'.4','status':'CANCELED'}))
 with patch('strategies.new_coin.executor.asyncio.sleep',new=AsyncMock()): ok=await e._close_position('TESTUSDT',Decimal('1'),'audit')
 results['newcoin_close']={'result':ok,'initial_position':'-1','final_position':str(position),'quantities':[c['quantity'] for c in calls],'reduce_only':[c.get('reduce_only',c.get('reduceOnly')) for c in calls]}
async def hrs_close():
 s=HRSStrategy.__new__(HRSStrategy)
 pos={'direction':'short','entry_price':100,'entry_quantity':1,'atr':1,'remaining_quantity':1}
 s.position_manager=MagicMock(); s.position_manager.get_all_positions.return_value={'TESTUSDT':pos};s.position_manager.detect_take_profit_fills.return_value=None;s.position_manager.check_time_stop.return_value=True;s.position_manager.cancel_all_orders=AsyncMock()
 s.binance_client=SimpleNamespace(get_ticker=AsyncMock(return_value={'lastPrice':'110'}),get_position=AsyncMock(return_value=[{'positionAmt':'-1'}]))
 s.trading_executor=SimpleNamespace(close_position=AsyncMock(return_value=None))
 s._time_stop_reanalyze_enabled=False;s._writeback_pnl=AsyncMock();s.risk_manager=SimpleNamespace(record_loss=AsyncMock());s._total_pnl=0;s._save_state=AsyncMock();s._send_position_close_notification=AsyncMock();s.mark_stop_loss=AsyncMock();s._should_unregister=lambda symbol:False
 await s._monitor_positions()
 results['hrs_close_failure']={'close_result':None,'cancelled_protection':s.position_manager.cancel_all_orders.await_count,'removed_position':s.position_manager.remove_position.call_count}
async def orphan():
 j=OrphanCleanupJob(None,None,SimpleNamespace(send=AsyncMock()))
 j._ensure_table=AsyncMock();j._get_exchange_positions=AsyncMock(return_value={'BTCUSDT'});j._query_strategy_states=AsyncMock(return_value={});j._cancel_order=AsyncMock(return_value=(True,''))
 order={'strategy_name':'btc_eth','symbol':'BTCUSDT','order_type':'STOP_LOSS','algo_id':'1'}
 with patch('ai_tuner.cleanup.orphan_cleanup.get_open_orders',new=AsyncMock(return_value=[order])):await j.execute()
 results['orphan_live_position']={'exchange_has_position':True,'cancelled_stop':j._cancel_order.await_count}
async def breaker():
 db=SimpleNamespace(fetch_one=AsyncMock(side_effect=[None,{'equal_weight':.05}]))
 b=CircuitBreaker(load_circuit_breaker_config(),db,'mtpcs');h=floor_index_hour()
 a=await b.load_index(h);z=await b.load_index(h)
 results['breaker_missing_cache']={'first':a,'after_db_available':z,'database_reads':db.fetch_one.await_count}
def validation():
 a=MTPCSAdapter(None)
 a.get_param_whitelist=lambda:['scoring.min_score'];a.get_redline_params=lambda:[];a.get_change_rate_threshold=lambda:0;a.get_param_ranges=lambda:{'scoring.min_score':[60,95]}
 changes={'scoring.min_score':{'from':70,'to':999}}
 v=ResponseParser().validate_adjustments(changes,a)
 used=v['validated'] if v['errors'] else changes
 results['ai_validation_ignored']={'validated':v['validated'],'errors':v['errors'],'weekly_job_uses':used}
async def main():
 await newcoin_close();await hrs_close();await orphan();await breaker();validation()
 print('REPRO_RESULTS='+json.dumps(results,ensure_ascii=False))
asyncio.run(main())
