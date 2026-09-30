import sys,asyncio,tempfile,pathlib,json,ast
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock,patch
sys.path.insert(0,str(pathlib.Path.cwd()))
from shared.binance_api import BinanceClient
from shared.database import DatabaseManager
from shared.position_ownership import is_symbol_owned_by_other
from strategies.btc_eth.strategy import BTCEthStrategy
from ai_tuner.deploy.config_operator import ConfigOperator
from ai_tuner.deploy.rollback_manager import RollbackManager
from shared.config_loader import load_strategy_config
from strategies.grid.position_manager import PositionManager
results={}
async def request_retry():
 c=BinanceClient('local_fake_key','local_fake_secret'); accepted=[]
 class Response:
  status=200; reason='OK'
  async def __aenter__(self):
   accepted.append(len(accepted)+1)
   if len(accepted)==1: raise asyncio.TimeoutError('accepted remotely; response lost')
   return self
  async def __aexit__(self,*a): pass
  async def json(self): return {'orderId':accepted[-1],'status':'NEW'}
 c.session=SimpleNamespace(closed=False,request=lambda *a,**k:Response())
 with patch('shared.utils.asyncio.sleep',new=AsyncMock()):
  r=await c._request('POST','/papi/v1/um/order',{'symbol':'BTCUSDT','side':'BUY','type':'MARKET','quantity':'1'})
 results['retry']={'exchange_accepted_orders':accepted,'caller_sees_order':r['orderId']}
async def ownership_race():
 lock=asyncio.Lock(); rows=[]; checked=0; gate=asyncio.Event()
 class Transaction:
  async def __aenter__(self): await lock.acquire()
  async def __aexit__(self,*a): lock.release()
 class Conn:
  def transaction(self): return Transaction()
  async def execute(self,*a): pass
  async def fetchrow(self,*a): return {'strategy':rows[-1]} if rows else None
 class Acquire:
  async def __aenter__(self): return Conn()
  async def __aexit__(self,*a): pass
 db=DatabaseManager('local',5432,'test','fake','fake'); db.pool=SimpleNamespace(acquire=lambda:Acquire())
 async def open_position(name):
  nonlocal checked
  blocked=await is_symbol_owned_by_other(db,'BTCUSDT',name)
  checked+=1
  if checked==2: gate.set()
  await gate.wait()
  if not blocked: rows.append(name)
  return {'strategy':name,'blocked':blocked}
 results['ownership_race']=await asyncio.gather(open_position('A'),open_position('B'))
async def protection_failure():
 s=BTCEthStrategy.__new__(BTCEthStrategy)
 s.db_manager=None; s.my_record_name='A'; s._competing_record_names=[]; s.positions={}
 s.frequency_controller=SimpleNamespace(record_trade=AsyncMock())
 s._place_entry_order=AsyncMock(return_value={'orderId':1,'status':'FILLED'})
 s._place_entry_protection_orders=AsyncMock(return_value=(None,False))
 s._send_signal_error_notification=AsyncMock()
 ok=await s._open_new_position({'symbol':'BTCUSDT','direction':'LONG','grade':'A','score':90,'timestamp':None})
 results['protection_failure']={'entry_filled':True,'result':ok,'managed_positions':list(s.positions)}
async def partial_fill():
 s=BTCEthStrategy.__new__(BTCEthStrategy)
 partial={'orderId':1,'status':'CANCELED','executedQty':'0.4','origQty':'1'}
 s.binance=SimpleNamespace(get_order=AsyncMock(return_value=partial),place_order=AsyncMock(return_value={'orderId':1}),cancel_order=AsyncMock(return_value=partial))
 s.risk_config={}
 with patch('strategies.btc_eth.strategy.asyncio.sleep',new=AsyncMock()):
  r=await s._place_and_wait_entry_order('BTCUSDT',{'direction':'LONG','quantity':Decimal('1'),'entry_price':Decimal('100')})
 results['partial_fill']={'executed_quantity':'0.4','entry_result':r,'cancel_called':s.binance.cancel_order.await_count}
def config_probes():
 with tempfile.TemporaryDirectory() as td:
  d=pathlib.Path(td); p=d/'config.yaml'; p.write_text('scoring:\n  min_score: 70\nrisk:\n  max_loss: 20\n')
  op=ConfigOperator(); rb=RollbackManager(); backup=rb.create_backup(str(p))
  op.apply_overrides(str(p),{'scoring.min_score':{'from':70,'to':80}})
  results['auto_apply_type']={'effective_value':load_strategy_config(td)['scoring']['min_score']}
  op.apply_overrides(str(p),{'scoring.min_score':80})
  rb.rollback(str(p),backup)
  results['rollback']={'reported_success':True,'effective_value':load_strategy_config(td)['scoring']['min_score'],'expected':70}
  op.apply_overrides(str(p),{'risk.max_loss':10})
  results['incremental_overrides']={'scoring_after_second_change':load_strategy_config(td)['scoring']['min_score'],'expected_if_cumulative':80}
def grid_cost():
 p=PositionManager(None,None,{})
 for side,q,price in [('BUY','2','100'),('SELL','1','110'),('BUY','1','100')]: p.update_position('ETHUSDT',side,Decimal(q),Decimal(price))
 results['grid_cost']={'actual_average':str(p.positions['ETHUSDT']['avg_price']),'expected_average':'100'}
async def sql_route():
 source=pathlib.Path('services/kline_service/api/routes.py').read_text(); t=ast.parse(source)
 fn=next(n for n in t.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='get_latest_klines'); fn.decorator_list=[]
 class Conn:
  async def __aenter__(self): return self
  async def __aexit__(self,*a): pass
  async def fetch_all(self,q,values): results['sql_injection']={'reached_query':q.strip()}; return []
 scope={'Query':lambda *a,**k:None,'db':SimpleNamespace(get_connection=lambda:Conn()),'_table_exists':AsyncMock(return_value=False),'collector':SimpleNamespace(ensure_table=AsyncMock(return_value=False)),'logger':MagicMock(),'HTTPException':Exception}
 exec(compile(ast.Module(body=[fn],type_ignores=[]),'services/kline_service/api/routes.py','exec'),scope)
 await scope['get_latest_klines']('BTCUSDT','1h CROSS JOIN (SELECT pg_sleep(0)) AS injected',10)
async def main():
 await request_retry();await ownership_race();await protection_failure();await partial_fill();await sql_route();config_probes();grid_cost()
 print('REPRO_RESULTS='+json.dumps(results,ensure_ascii=False))
asyncio.run(main())
