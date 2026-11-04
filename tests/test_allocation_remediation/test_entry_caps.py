from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import pytest
from shared.capital_manager import CapitalManager


def manager(strategy='btc_eth',limit=300):
    obj=CapitalManager('/not/read',strategy_id=strategy, entry_equity_provider=AsyncMock(return_value={'totalMarginBalance':1000}))
    obj.get_effective_margin_limit=AsyncMock(return_value=(limit,'db'))
    return obj


@pytest.mark.asyncio
@pytest.mark.parametrize('strategy',['btc_eth','new_coin'])
async def test_entry_cap_does_not_shrink_holdings_limit_or_close(strategy):
    obj=manager(strategy)
    with patch('shared.entry_risk_policy.RiskPolicyStore') as store:
        store.return_value.policy=AsyncMock(return_value={'mode':'capped','entry_ceiling':100,'ratio_cap':'0.20'})
        assert not (await obj.can_open_within_limit(90,11))[0]
        assert (await obj.can_open_within_limit(90,10))[0]
    assert await obj.get_effective_margin_limit()==(300,'db')
    close=AsyncMock()
    assert await obj.enforce_existing_positions({'AAA':150},close)
    close.assert_not_called()


@pytest.mark.asyncio
async def test_missing_policy_denies_new_entry_even_with_legacy_limit():
    assert not (await manager().can_open_within_limit(0,1))[0]

@pytest.mark.asyncio
@pytest.mark.parametrize('equity,allowed',[(400,False),(500,True),(10000,True),(None,False),(float('nan'),False),(0,False),(-1,False)])
async def test_live_equity_ceiling(equity,allowed):
    obj=manager()
    obj.entry_equity_provider=AsyncMock(return_value={'totalMarginBalance':equity})
    with patch('shared.entry_risk_policy.RiskPolicyStore') as store:
        store.return_value.policy=AsyncMock(return_value={'mode':'capped','entry_ceiling':100,'ratio_cap':'0.20'})
        assert (await obj.can_open_within_limit(90,10))[0] is allowed

@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('mode','monitor_only'),('ratio_cap',2),('ratio_cap',0),('ratio_cap','NaN'),('entry_ceiling',-1),('entry_ceiling','Infinity')])
async def test_invalid_policy_denies(field,value):
    policy={'mode':'capped','entry_ceiling':100,'ratio_cap':'.2'};policy[field]=value
    with patch('shared.entry_risk_policy.RiskPolicyStore') as store:
        store.return_value.policy=AsyncMock(return_value=policy)
        assert not (await manager().can_open_within_limit(0,1))[0]

@pytest.mark.asyncio
@pytest.mark.parametrize('occupied,new,limit',[(float('nan'),1,100),(0,float('inf'),100),(-1,1,100),(0,-1,100),(0,1,None),(0,1,-1),(0,1,0),(90,10,99)])
async def test_bad_margin_or_existing_smaller_cap(occupied,new,limit):
    with patch('shared.entry_risk_policy.RiskPolicyStore') as store:
        store.return_value.policy=AsyncMock(return_value={'mode':'capped','entry_ceiling':100,'ratio_cap':'.2'})
        assert not (await manager(limit=limit).can_open_within_limit(occupied,new))[0]

@pytest.mark.asyncio
@pytest.mark.parametrize('provider',[None,AsyncMock(side_effect=TimeoutError()),AsyncMock(return_value={}),AsyncMock(return_value=None),AsyncMock(return_value={'totalMarginBalance':float('inf')})])
async def test_unavailable_equity_denies(provider):
    obj=manager();obj.entry_equity_provider=provider
    assert not (await obj.can_open_within_limit(0,1))[0]

@pytest.mark.asyncio
async def test_policy_read_follows_equity_network_wait():
    obj=manager();events=[]
    async def equity():events.append('equity');return {'totalMarginBalance':1000}
    async def expired(strategy):
        assert events==['equity'];raise RuntimeError('expired during equity request')
    obj.entry_equity_provider=equity
    with patch('shared.entry_risk_policy.RiskPolicyStore') as store:
        store.return_value.policy=expired
        assert not (await obj.can_open_within_limit(0,1))[0]

@pytest.mark.asyncio
async def test_other_strategy_does_not_use_entry_overlay():
    obj=manager('hrs');obj.entry_equity_provider=AsyncMock(side_effect=AssertionError())
    assert (await obj.can_open_within_limit(0,1))[0]
    obj.entry_equity_provider.assert_not_called()

@pytest.mark.asyncio
async def test_real_strategy_constructors_bind_equity_provider():
    # Constructors create asyncio locks; the fixture owns loop setup/cleanup.
    from pathlib import Path
    from unittest.mock import MagicMock
    import yaml
    from strategies.btc_eth.strategy import BTCEthStrategy
    from strategies.new_coin.executor import TradingExecutor
    root=Path(__file__).parents[2]
    api=MagicMock();api.get_account_info=AsyncMock()
    btc=BTCEthStrategy(yaml.safe_load((root/'strategies/btc_eth/config.yaml').read_text()),api,MagicMock(),MagicMock())
    new=TradingExecutor(api,MagicMock(),MagicMock(),yaml.safe_load((root/'strategies/new_coin/config.yaml').read_text()))
    assert btc.capital_mgr.entry_equity_provider is api.get_account_info
    assert new.capital_mgr.entry_equity_provider is api.get_account_info
    api.get_account_info.assert_not_called()

@pytest.mark.asyncio
@pytest.mark.parametrize('account_info,allowed',[
 ({'accountEquity':'523.67799237','totalAvailableBalance':'399.76623463'},True),
 ({'totalMarginBalance':'523.67799237'},True),
 ({'totalAvailableBalance':'399.76623463'},False),
 ({'accountEquity':None,'totalMarginBalance':'1000'},False),
 ({'accountEquity':'NaN','totalMarginBalance':'1000'},False),
])
async def test_actual_pm_equity_shape_and_nonpm_compatibility(account_info,allowed):
    obj=manager();obj.entry_equity_provider=AsyncMock(return_value=account_info)
    with patch('shared.entry_risk_policy.RiskPolicyStore') as store:
        store.return_value.policy=AsyncMock(return_value={'mode':'capped','entry_ceiling':100,'ratio_cap':'.2'})
        assert (await obj.can_open_within_limit(90,10))[0] is allowed
