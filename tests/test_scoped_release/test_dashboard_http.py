import subprocess
import sys
from pathlib import Path
import pytest

@pytest.mark.parametrize('module',['api.routes_docker'])
def test_real_trend_http_response_preserves_ledger_metadata(module):
    code=r'''
import sys,asyncio,importlib
from pathlib import Path
sys.path.insert(0,str(Path('dashboard/backend').resolve()))
from fastapi import FastAPI
import httpx
from unittest.mock import AsyncMock,Mock
routes=importlib.import_module(sys.argv[1])
row=dict(date='2026-08-01',total_pnl='-115.45319304',report_id='verified-id',report_version='exchange-income-v1',account_id='default',start_ms=1,end_ms=2,publication_status='published',account_coverage_complete=True,strategy_attribution_complete=False,win_rate=None,order_count=None)
service=Mock();service.get_trend_data=AsyncMock(return_value=[row])
cache=Mock();cache.get.return_value={'legacy':'must not be used'}
app=importlib.import_module('main_docker').app
active=[route for route in app.routes if getattr(route,'path',None)=='/api/trend']
assert len(active)==1 and active[0].endpoint is routes.get_trend
app.dependency_overrides[routes.get_data_service]=lambda:service
app.dependency_overrides[routes.get_cache_service]=lambda:cache
async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.get('/api/trend?type=monthly&days=4')
        assert response.status_code==200,response.text
        data=response.json()['data'];assert data['report_type']=='monthly'
        point=data['trends'][0]
        for key in ('report_id','report_version','account_id','start_ms','end_ms','publication_status','account_coverage_complete','strategy_attribution_complete','total_pnl'):
            assert point[key]==row[key],(key,point)
        assert point['win_rate'] is None
asyncio.run(run())
'''
    result=subprocess.run([sys.executable,'-c',code,module],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
