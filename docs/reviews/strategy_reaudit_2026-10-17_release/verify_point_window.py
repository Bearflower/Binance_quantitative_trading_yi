"""Independent offline probes for closed-interval guard and income completeness."""
import asyncio,json,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from scripts.export_execution_facts import ExecutionFactExporter

async def main():
    row=dict(id=1,symbol='TESTUSDT',time=1000,orderId=1,qty='1',price='100',side='BUY',commission='0',commissionAsset='USDT',realizedPnl='0')
    api=SimpleNamespace(get_user_trades=AsyncMock(return_value=[row]))
    exp=ExecutionFactExporter(account_id='test',api_client=api,page_limit=2)
    result=await exp.export_fills('TESTUSDT',1000,1000)
    assert api.get_user_trades.await_count==1 and len(result.records)==1 and result.page_complete
    single=dict(expected=1,actual=len(result.records),api_calls=api.get_user_trades.await_count,complete=result.page_complete)
    income_rows=[dict(tranId=i,symbol='TESTUSDT',incomeType='REALIZED_PNL',income='1',asset='USDT',time=1000) for i in (1,2,3)]
    api2=SimpleNamespace(get_income_history=AsyncMock(return_value=income_rows[:2]))
    exp2=ExecutionFactExporter(account_id='test',api_client=api2,page_limit=2)
    a=await exp2.export_income(1,1000)
    b=await exp2.export_income(1,1000)
    assert not a.page_complete and not b.page_complete and a.errors and b.errors
    out=dict(single_millisecond=single,income=dict(first_status=a.cursor.request_status,second_status=b.cursor.request_status,errors=b.errors,actual=len(b.records),expected=3))
    Path(__file__).with_name('remaining_results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(out,ensure_ascii=False,indent=2))
asyncio.run(main())
