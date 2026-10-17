import sys,asyncio,json,copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from decimal import Decimal
import yaml
sys.path.insert(0,str(Path.cwd()))
from strategies.hrs.scoring_engine import ScoringEngine
from strategies.hrs.strategy import HRSStrategy
from strategies.new_coin.strategy import NewCoinStrategy
from scripts.export_execution_facts import ExecutionFactExporter
cfg=yaml.safe_load(Path('strategies/hrs/config.yaml').read_text())
e=ScoringEngine(cfg)
e.calculate_technical_score=lambda *a,**k:(10,{'primary_pattern_score':5})
e.calculate_sentiment_score=lambda *a,**k:(10,{})
a=e.score('TESTUSDT','short',.3,{},.01,price_change_24h=None)
b=e.score('TESTUSDT','short',.3,{},.01,price_change_24h=30)
c=e.score('TESTUSDT','long',0,{},.01,has_market_cap=False,price_change_24h=None)
output={'shadow_replaces_standard':{'before_mode':a.entry_mode,'before_entry':e.should_entry(a),'after_mode':b.entry_mode,'after_entry':e.should_entry(b),'after_shadow':HRSStrategy._is_shadow_entry_mode(SimpleNamespace(config=cfg),b.entry_mode)},'missing_cap_entry':{'contract_score':c.contract_score,'should_entry':e.should_entry(c)},'lv_rm_existing_also_shadow':HRSStrategy._is_shadow_entry_mode(SimpleNamespace(config=cfg),'lv_rm')}
class API:
 def __init__(self,rows):self.rows=rows;self.calls=[]
 async def get_user_trades(self,symbol,*,start_time,end_time,limit,from_id=None):
  self.calls.append((start_time, from_id))
  # 按 id 过滤：from_id 之后的记录（同毫秒跨页）
  filtered = [r for r in self.rows if start_time<=r['time']<end_time]
  if from_id is not None:
   filtered = [r for r in filtered if r['id'] > from_id]
  return filtered[:limit]
def row(i,t):return {'id':i,'symbol':'TESTUSDT','time':t,'orderId':i,'qty':'1','price':'100','side':'BUY','commission':'0','commissionAsset':'USDT','realizedPnl':'0'}
class Sink:
 def __init__(self):self.cursor=None;self.fail=True;self.rows=[]
 async def load_cursor(self,*a):return self.cursor
 async def save_cursor(self,c):self.cursor=c
 async def upsert_fills(self,rows):
  if self.fail:raise RuntimeError('injected database failure')
  self.rows.extend(rows)
async def run():
 api=API([row(i,1000) for i in range(1,5)])
 exp=ExecutionFactExporter(account_id='test',api_client=api,page_limit=2,max_pages=3)
 r=await exp.export_fills('TESTUSDT',1,2000)
 output['same_ms_cross_page']={'expected':4,'actual':len(r.records),'complete':r.page_complete,'errors':r.errors,'requests':api.calls}
 api=API([row(1,1000),row(2,1100)]);sink=Sink()
 exp=ExecutionFactExporter(account_id='test',api_client=api,page_limit=10,sink=sink)
 first=await exp.export_fills('TESTUSDT',1,2000)
 sink.fail=False
 second=await exp.export_fills('TESTUSDT',1,2000)
 output['cursor_before_persist']={'first_marked_complete':first.page_complete,'first_errors':first.errors,'persisted_after_retry_ids':[r.trade_id for r in sink.rows],'expected_ids':[1,2]}
 class IncomeAPI:
  async def get_income_history(self,*,start_time,end_time,limit,income_type):
   rows=[{'tranId':1,'symbol':'TESTUSDT','incomeType':'REALIZED_PNL','income':'2','asset':'USDT','time':1100}, {'tranId':2,'symbol':'TESTUSDT','incomeType':'COMMISSION','income':'-0.1','asset':'USDT','time':500}]
   return [r for r in rows if r['incomeType']==income_type and start_time<=r['time']<end_time][:limit]
 exp=ExecutionFactExporter(account_id='test',api_client=IncomeAPI(),page_limit=10)
 pnl=await exp.export_income(1,2000,'REALIZED_PNL')
 fee=await exp.export_income(1,2000,'COMMISSION')
 output['income_filter_cursor_collision']={'pnl_count':len(pnl.records),'fee_expected':1,'fee_actual':len(fee.records),'fee_complete':fee.page_complete}
 clock=[1000]
 async def slow_ticker(s):clock[0]+=60;return Decimal('100')
 fake=SimpleNamespace(binance_client=SimpleNamespace(get_ticker_price=slow_ticker))
 with patch('strategies.new_coin.strategy.time.time',side_effect=lambda:clock[0]):
  price,at=await NewCoinStrategy._fetch_fresh_ticker(fake,'TESTUSDT',5)
 output['slow_ticker_accepted']={'request_seconds':60,'max_age_seconds':5,'price':str(price),'observed_at':at}
asyncio.run(run())
Path('/tmp/strategy_reaudit_probes.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
print(json.dumps(output,ensure_ascii=False,indent=2))
