import pytest

def test_unknown_or_unprotected_position_blocks_and_close_position_stop_counts():
    from scripts.verify_ledger_risk_release import verify_exchange,GateError
    position={'symbol':'ARIAUSDT','positionAmt':'-42','entryPrice':'1','positionSide':'BOTH'}
    stop={'symbol':'ARIAUSDT','side':'BUY','type':'STOP_MARKET','closePosition':True,'reduceOnly':True,'stopPrice':'1.2','positionSide':'BOTH'}
    verify_exchange([position],[],[stop])
    for pos,ordinary,algo in [(None,[],[]),([position],[],[]),([dict(position,positionAmt='NaN')],[],[stop]),([position],[{'reduceOnly':False}],[stop]),([position],[],[dict(stop,side='SELL')])]:
        with pytest.raises(GateError):verify_exchange(pos,ordinary,algo)


def test_real_pm_algo_shape():
    from scripts.verify_ledger_risk_release import verify_exchange
    verify_exchange([dict(symbol='ARIAUSDT',positionAmt='-42',entryPrice='.12',markPrice='.12',positionSide='BOTH')],[],[dict(symbol='ARIAUSDT',side='BUY',positionSide='BOTH',orderType='STOP_MARKET',triggerPrice='.12977',closePosition=True,reduceOnly=True)])

def test_readonly_report_identity_never_labels_missing_as_fresh():
    import pytest
    from scripts.verify_ledger_risk_release import verify_reports,GateError
    assert verify_reports([], 'main')=={'status':'unavailable','reports':[]}
    row=dict(account_id='main',window_key='monthly:1000',report_version='exchange-income-v1',report_id='id',start_ms=1000,end_ms=2000,
        report=dict(account_id='main',window_key='monthly:1000',report_version='exchange-income-v1',report_id='id',start_ms=1000,end_ms=2000,account_coverage_complete=True))
    assert verify_reports([row],'main')['status']=='stored_verified'
    for key in ('account_id','window_key','report_version','report_id','start_ms','end_ms'):
        bad=dict(row,report=dict(row['report'],**{key:'wrong'}))
        with pytest.raises(GateError):verify_reports([bad],'main')

@pytest.mark.asyncio
async def test_publish_proof_uses_canonical_month_and_preserves_unknown(monkeypatch):
    from scripts.verify_ledger_risk_release import publish_report_proof,GateError
    from unittest.mock import AsyncMock
    import shared.accounting.published_report as module
    evidence=[dict(account_id='main',start_ms=1000,end_ms=2000,net_usdt='-1')]
    body=dict(account_id='main',start_ms=1000,end_ms=2000,requested_end_ms=2000,window_key='monthly:1000',report_version=module.REPORT_VERSION,report_id='abc',publication_status='published',refresh_error=None,account_coverage_complete=True,strategy_attribution_complete=False,account_components={'net_usdt':'-1'},by_strategy={'hrs':{'net_usdt':'2'}})
    reader=AsyncMock(return_value=body);monkeypatch.setattr(module,'read_published_report',reader)
    proof=await publish_report_proof('db',evidence,'main')
    reader.assert_awaited_once_with('db','main',1000,2000,'monthly:1000',current=False,refresh_seconds=0)
    assert proof[0]['strategy_values']['hrs'] is None
    for key,value in [('publication_status','stale'),('refresh_error','IncomeIntegrityBlocked'),('account_coverage_complete',False),('strategy_attribution_complete',True),('account_components',{'net_usdt':'-2'})]:
        reader.return_value=dict(body,**{key:value})
        with pytest.raises(GateError):await publish_report_proof('db',evidence,'main')

def test_split_protective_stops_cover_remaining_quantity():
    from scripts.verify_ledger_risk_release import verify_exchange,GateError
    p=dict(symbol='X',positionAmt='5',entryPrice='10',markPrice='10')
    stop=dict(symbol='X',side='SELL',orderType='STOP',triggerPrice='9',reduceOnly=True)
    verify_exchange([p],[],[dict(stop,algoId=1,quantity='2'),dict(stop,algoId=2,quantity='3')])
    with pytest.raises(GateError):verify_exchange([p],[],[dict(stop,algoId=1,quantity='3'),dict(stop,algoId=1,quantity='3')])
    with pytest.raises(GateError):verify_exchange([p],[],[dict(stop,algoId=1,quantity='3',executedQty='2'),dict(stop,algoId=2,quantity='3')])

def test_dashboard_http_matches_published_exact_identity_and_net():
    from scripts.scoped_ledger_risk_release import verify_dashboard_reports
    from scripts.ledger_risk_manifest import ReleaseBlocked
    proof=dict(account_id='main',start_ms=1,end_ms=2,report_id='id',report_version='exchange-income-v1',publication_status='published',strategy_attribution_complete=False,net_usdt='-1')
    row=dict(proof,total_pnl='-1',account_coverage_complete=True,refresh_error=None)
    verify_dashboard_reports({'code':0,'data':{'trends':[row]}},[proof])
    for key,value in [('total_pnl','0'),('report_id','other'),('publication_status','stale'),('strategy_attribution_complete',True)]:
        with pytest.raises(ReleaseBlocked):verify_dashboard_reports({'code':0,'data':{'trends':[dict(row,**{key:value})]}},[proof])
    with pytest.raises(ReleaseBlocked):verify_dashboard_reports({'code':0,'data':{'trends':[]}},[proof])
