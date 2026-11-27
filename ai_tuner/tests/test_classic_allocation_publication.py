from decimal import Decimal
from types import SimpleNamespace
import pytest
from ai_tuner.allocation.allocation_calculator import AllocationEntry, AllocationResult


def result():
    return AllocationResult('2026-12',100,20,80,False,[
        AllocationEntry(s,s,0,30,None,None,.2,20) for s in ('btc_eth','btc_eth_aggressive','hrs','new_coin')],
        caliber='maintain_approved_allocation_due_to_historical_gaps',history_acceptance_sha256='a'*64)


def test_publication_preserves_old_holdings_floor_without_expanding_new_entry_budget():
    from ai_tuner.allocation.classic_publication import publication_entries
    previous={'id':7,'entries':[{'strategy_id':s.strategy_id,'allocated_amount':30,
        'holdings_floor_usdt':'40','publication_method':'classic_history_acceptance_v1'} for s in result().entries]}
    entries=publication_entries(result(),previous)
    assert all(e['allocated_amount']==20 and e['holdings_floor_usdt']=='40' for e in entries)
    assert all(e['previous_allocation_id']==7 and e['publication_method']=='classic_history_acceptance_v1' for e in entries)
    assert all(e['return_rate'] is None and e['rank'] is None for e in entries)


@pytest.mark.parametrize('value',['NaN',-1,'broken'])
def test_invalid_previous_metadata_cannot_be_replaced_with_zero(value):
    from ai_tuner.allocation.classic_publication import publication_entries
    previous={'id':7,'entries':[{'strategy_id':s.strategy_id,'allocated_amount':30,
        'holdings_floor_usdt':value,'publication_method':'classic_history_acceptance_v1'} for s in result().entries]}
    with pytest.raises((ValueError,ArithmeticError)):
        publication_entries(result(),previous)


def test_clipping_rounds_down_and_respects_reserve():
    from ai_tuner.allocation.history_acceptance import cap_result
    r=result();r.total_capital=91.12345678
    p=SimpleNamespace(receipt_sha256='a'*64,payload={'allocation_ceilings_usdt':{e.strategy_id:20 for e in r.entries}})
    capped=cap_result(r,p,.15,fixed_hrs_amount=20)
    total=sum(Decimal(str(e.allocated_amount)) for e in capped.entries)
    assert total<=Decimal('91.12345678')*Decimal('.85')
    assert all(Decimal(str(e.allocated_amount)).as_tuple().exponent >= -8 for e in capped.entries)


def test_duplicate_strategy_is_rejected():
    from ai_tuner.allocation.classic_publication import publication_entries
    r=result();r.entries[-1]=r.entries[0]
    previous={'id':7,'entries':[{'strategy_id':s,'allocated_amount':30} for s in ('btc_eth','btc_eth_aggressive','hrs','new_coin')]}
    with pytest.raises(ValueError): publication_entries(r,previous)

@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_enabled',[True,False])
async def test_monthly_classic_path_uses_guarded_publisher_without_legacy_writer(monkeypatch,legacy_enabled):
    from datetime import datetime,timezone
    from unittest.mock import AsyncMock,Mock
    import ai_tuner.allocation.monthly_job as module
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,12,1,0,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(module,'datetime',Clock)
    freeze=AsyncMock(return_value=True)
    monkeypatch.setattr(module,'allocation_frozen',freeze)
    publish=AsyncMock(return_value=True)
    monkeypatch.setattr(module,'publish_classic_allocation',publish,raising=False)
    monkeypatch.setattr(module,'approved_hrs_amount',AsyncMock(return_value=Decimal('30')))
    job=module.MonthlyAllocationJob.__new__(module.MonthlyAllocationJob)
    p=SimpleNamespace(receipt_sha256='a'*64,accepted_from=Clock(2026,11,1,tzinfo=timezone.utc),
        accepted_through=Clock(2026,11,27,tzinfo=timezone.utc),payload={'allocation_ceilings_usdt':{s:30 for s in ('btc_eth','btc_eth_aggressive','hrs','new_coin')}})
    job._classic_guard=AsyncMock(return_value=(p,{'current_equity_usd':'99.99999999'}))
    job.allocation_cfg={'enabled':legacy_enabled,'mode':'shadow_only','reserve_ratio':.15,'rank_ratios':[.3,.25,.2,.1]}
    job._ensure_table=AsyncMock();job._check_idempotency=AsyncMock(return_value=False)
    job._check_is_first_month=AsyncMock(return_value=False)
    job.pnl_collector=SimpleNamespace(collect_all_realized_pnl=AsyncMock(return_value={'btc_eth':{'pnl':1}}))
    job._get_actual_balance=AsyncMock(return_value=100)
    job._strategy_configs=[{'strategy_id':s} for s in ('btc_eth','btc_eth_aggressive','hrs','new_coin')]
    job.calculator=SimpleNamespace(calculate=Mock(return_value=result()))
    job.config_updater=SimpleNamespace(update_all=AsyncMock())
    job.db_manager=object();job._send_notification=AsyncMock()
    outcome=await job.run_monthly_allocation()
    assert outcome and outcome['month']=='2026-12'
    publish.assert_awaited_once();freeze.assert_not_awaited()
    job.config_updater.update_all.assert_not_awaited();job._ensure_table.assert_not_awaited()
    assert Decimal(str(publish.await_args.args[1].total_capital))==Decimal('99.99999999')
    assert next(e for e in publish.await_args.args[1].entries if e.strategy_id=='hrs').allocated_amount==30

@pytest.mark.parametrize('previous',[
    {'id':7,'entries':{}},
    {'id':7,'entries':[{'strategy_id':'hrs','allocated_amount':30}]*2},
    {'id':0,'entries':[{'strategy_id':s,'allocated_amount':30} for s in ('btc_eth','btc_eth_aggressive','hrs','new_coin')]},
    {'id':7,'entries':[{'strategy_id':s,'allocated_amount':30,'holdings_floor_usdt':'40'} for s in ('btc_eth','btc_eth_aggressive','hrs','new_coin')]},
])
def test_prior_allocation_identity_or_unsupported_metadata_rejected(previous):
    from ai_tuner.allocation.classic_publication import publication_entries
    with pytest.raises(ValueError):publication_entries(result(),previous)


def test_publication_requires_receipt_identity():
    from ai_tuner.allocation.classic_publication import publication_entries
    r=result();r.history_acceptance_sha256=None
    previous={'id':7,'entries':[{'strategy_id':s.strategy_id,'allocated_amount':30} for s in r.entries]}
    with pytest.raises(ValueError):publication_entries(r,previous)

@pytest.mark.asyncio
@pytest.mark.parametrize('fault',[None,'outside_history','tail_gap','unknown_owner','conflict','missing_fill','missing_prior','duplicate_prior','scope','bad_pool'])
async def test_retained_allocation_accepts_only_historical_gap_and_keeps_verified_losses(monkeypatch,fault):
    from datetime import datetime,timezone
    from unittest.mock import AsyncMock
    import ai_tuner.allocation.history_acceptance as module
    from ai_tuner.allocation.pnl_collector import AllocationInputUnavailable
    start=datetime(2026,11,1,tzinfo=timezone.utc);end=datetime(2026,12,1,tzinfo=timezone.utc)
    sids=('btc_eth','btc_eth_aggressive','hrs','new_coin')
    p=SimpleNamespace(account_id='default',accepted_from=start,accepted_through=datetime(2026,11,27,tzinfo=timezone.utc),
        receipt_sha256='a'*64,payload={'allocation_ceilings_usdt':{s:30 for s in sids}})
    report={'attribution_conflicts':[],'missing_income_fills':[],'unattributed_count':0,
        'by_strategy':{'hrs':{'net_usdt':'-1.27'}},'id_match_and_coverage_complete':False}
    tail={'id_match_and_coverage_complete':True}
    entries=[{'strategy_id':s,'allocated_amount':30} for s in sids]
    if fault=='outside_history':start=end
    if fault=='tail_gap':tail['id_match_and_coverage_complete']=False
    if fault=='unknown_owner':report['unattributed_count']=1
    if fault=='conflict':report['attribution_conflicts']=[1]
    if fault=='missing_fill':report['missing_income_fills']=[1]
    if fault=='duplicate_prior':entries.append(entries[0])
    if fault=='scope':entries.pop()
    db=SimpleNamespace(fetch_one=AsyncMock(return_value=None if fault=='missing_prior' else {'entries':entries}))
    monkeypatch.setattr(module,'read_monthly_economic_report',AsyncMock(side_effect=[report,tail]))
    if fault:
        with pytest.raises((AllocationInputUnavailable,ValueError)):
            await module.retained_allocation(db,p,start,end,0 if fault=='bad_pool' else 100,.15,'2026-12',{s:s for s in sids})
    else:
        out=await module.retained_allocation(db,p,start,end,100,.15,'2026-12',{s:s for s in sids})
        assert out.caliber==module.MODE and out.allocatable_amount==84.99999999 and out.reserve_amount==15.00000001
        assert all(e.rank is None and e.return_rate is None and e.allocated_amount==(30 if e.strategy_id=='hrs' else 18.33333333) for e in out.entries)
        assert next(e for e in out.entries if e.strategy_id=='hrs').realized_pnl==-1.27

@pytest.mark.parametrize('hrs_rank_amount',[5,50])
def test_accepted_policy_keeps_hrs_principal_even_when_ranked_target_changes(hrs_rank_amount):
    from ai_tuner.allocation.history_acceptance import cap_result
    r=result();r.caliber='verified_ranked_returns'
    for e in r.entries:
        e.allocated_amount=hrs_rank_amount if e.strategy_id=='hrs' else 30
        e.rank=1;e.return_rate=.2
    p=SimpleNamespace(receipt_sha256='a'*64,payload={'allocation_ceilings_usdt':{e.strategy_id:30 for e in r.entries}})
    out=cap_result(r,p,.15,fixed_hrs_amount=Decimal('30'))
    by_id={e.strategy_id:Decimal(str(e.allocated_amount)) for e in out.entries}
    assert by_id['hrs']==30 and all(v==Decimal('18.33333333') for k,v in by_id.items() if k!='hrs')
    assert sum(by_id.values())<=Decimal('85')
    # Later equity deterioration tightens only the three adjustable allocations.
    out.total_capital=90
    lower=cap_result(out,p,.15,fixed_hrs_amount=Decimal('30'))
    assert next(e for e in lower.entries if e.strategy_id=='hrs').allocated_amount==30
    assert sum(Decimal(str(e.allocated_amount)) for e in lower.entries)<=Decimal('76.5')

@pytest.mark.parametrize('total,fixed,ceiling',[(20,'30','30'),(100,'31','30'),(100,'0','30'),(100,'0.000000001','30')])
def test_hrs_principal_is_not_silently_clipped_for_pool_or_cap(total,fixed,ceiling):
    from ai_tuner.allocation.history_acceptance import cap_result
    r=result();r.total_capital=total
    caps={e.strategy_id:30 for e in r.entries};caps['hrs']=ceiling
    p=SimpleNamespace(receipt_sha256='a'*64,payload={'allocation_ceilings_usdt':caps})
    with pytest.raises(ValueError):cap_result(r,p,.15,fixed_hrs_amount=Decimal(fixed))


def test_fixed_hrs_principal_allows_equivalent_decimal_trailing_zeros():
    from ai_tuner.allocation.history_acceptance import cap_result
    r=result()
    p=SimpleNamespace(receipt_sha256='a'*64,payload={'allocation_ceilings_usdt':{e.strategy_id:30 for e in r.entries}})
    out=cap_result(r,p,.15,fixed_hrs_amount=Decimal('30.0000000000'))
    assert next(e for e in out.entries if e.strategy_id=='hrs').allocated_amount==30
