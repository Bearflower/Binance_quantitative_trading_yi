"""Every published holdings floor must resolve to the approved legacy anchor."""
from copy import deepcopy
from decimal import Decimal
import json
import pytest

from shared.accounting.allocation_provenance import METHOD, entries_digest
from shared.accounting.history_tolerance import acceptance_digest, validate_history_acceptance

SCOPE=('btc_eth','btc_eth_aggressive','hrs','new_coin')


class Chain:
    def __init__(self):
        self.rows={1:dict(id=1,entries=[dict(strategy_id=s,allocated_amount=40) for s in SCOPE])}
        p=dict(kind='historical_risk_acceptance_v1',account_id='default',revision=1,enabled=True,
            exchange_uid_sha256='a'*64,classic_cutover_ms=1795582464000,
            accepted_from='2026-10-31T16:00:00+00:00',accepted_through='2026-11-26T07:59:00+00:00',
            effective_at='2026-11-26T08:00:00+00:00',classic_flat_at='2026-11-25T05:32:58.143000+00:00',
            classic_flat_evidence_sha256='b'*64,classic_open_equity='391.65638581',risk_limit_ratio='.05',
            hrs_daily_limit='.03',hrs_rolling_limit='.06',classic_risk_method='classic_fixed_capital_cashflow_adjusted_drawdown_v1',
            automatic_allocation=True,approval_ref='operator',allowed_gap_codes=['historical_nav_sampling'],
            allocation_ceilings_usdt={s:'100' for s in SCOPE},allocation_anchor_id=1,
            allocation_anchor_entries_sha256=entries_digest(self.rows[1]['entries']))
        self.receipt=dict(account_id='default',revision=1,payload=p,receipt_sha256=acceptance_digest(p))
        self.receipts={self.receipt['receipt_sha256']:self.receipt}
        self.append(2,1,30)
        self.append(3,2,20)

    def append(self,row_id,parent_id,new_amount):
        old=self.rows[parent_id]
        self.rows[row_id]=dict(id=row_id,entries=[dict(strategy_id=e['strategy_id'],allocated_amount=new_amount,
            holdings_floor_usdt=str(max(Decimal(str(e['allocated_amount'])),Decimal(str(e.get('holdings_floor_usdt',0))))),
            publication_method=METHOD,previous_allocation_id=parent_id,previous_entries_sha256=entries_digest(old['entries']),
            history_acceptance_sha256=self.receipt['receipt_sha256']) for e in old['entries']])

    async def fetchrow(self,sql,*args):
        if 'historical_risk_acceptance' in sql:return deepcopy(self.receipts.get(args[-1]))
        assert 'WHERE id' in sql
        return deepcopy(self.rows.get(args[0]))


@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS','1795582464000')
    return Chain()


@pytest.mark.asyncio
async def test_complete_chain_preserves_original_floor_and_entry_budget(chain):
    from shared.accounting.allocation_provenance import validate_allocation_chain
    assert await validate_allocation_chain(chain,chain.rows[3])=={s:Decimal(40) for s in SCOPE}
    assert all(e['allocated_amount']==20 for e in chain.rows[3]['entries'])
    policy=validate_history_acceptance(chain.receipt,'default')
    assert await validate_allocation_chain(chain,chain.rows[1],policy=policy)=={s:Decimal(40) for s in SCOPE}


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['floor_zero','strip_all_metadata','missing_receipt','unknown_method',
    'missing_field','parent_missing','duplicate_strategy','cycle','parent_hash'])
async def test_rehashing_a_corrupt_ancestor_does_not_make_the_chain_valid(chain,damage):
    from shared.accounting.allocation_provenance import validate_allocation_chain
    ancestor=chain.rows[2]
    if damage=='floor_zero':
        for e in ancestor['entries']:e['holdings_floor_usdt']='0'
    elif damage=='strip_all_metadata':
        ancestor['entries']=[dict(strategy_id=e['strategy_id'],allocated_amount=e['allocated_amount']) for e in ancestor['entries']]
    elif damage=='missing_receipt':
        for e in ancestor['entries']:e['history_acceptance_sha256']='e'*64
    elif damage=='unknown_method':ancestor['entries'][0]['publication_method']='other'
    elif damage=='missing_field':del ancestor['entries'][0]['history_acceptance_sha256']
    elif damage=='parent_missing':del chain.rows[1]
    elif damage=='duplicate_strategy':ancestor['entries'].append(dict(ancestor['entries'][0]))
    elif damage=='cycle':
        for e in ancestor['entries']:e['previous_allocation_id']=3
    elif damage=='parent_hash':ancestor['entries'][0]['previous_entries_sha256']='e'*64
    chain.append(3,2,20)
    with pytest.raises(ValueError):await validate_allocation_chain(chain,chain.rows[3])


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('allocation_anchor_id',2),('allocation_anchor_id',True),
    ('allocation_anchor_entries_sha256','e'*64),('allocation_anchor_entries_sha256',None)])
async def test_receipt_anchor_is_mandatory_and_cannot_be_substituted(chain,field,value):
    from shared.accounting.allocation_provenance import validate_allocation_chain
    p=chain.receipt['payload'];p[field]=value
    chain.receipt['receipt_sha256']=acceptance_digest(p)
    chain.receipts={chain.receipt['receipt_sha256']:chain.receipt}
    chain.append(2,1,30);chain.append(3,2,20)
    with pytest.raises(ValueError):await validate_allocation_chain(chain,chain.rows[3])


@pytest.mark.asyncio
async def test_every_receipt_in_chain_must_use_the_same_anchor(chain):
    from shared.accounting.allocation_provenance import validate_allocation_chain
    altered=deepcopy(chain.receipt);altered['revision']=altered['payload']['revision']=2
    altered['payload']['allocation_anchor_id']=2
    altered['receipt_sha256']=acceptance_digest(altered['payload'])
    chain.receipts[altered['receipt_sha256']]=altered
    for e in chain.rows[2]['entries']:e['history_acceptance_sha256']=altered['receipt_sha256']
    chain.append(3,2,20)
    with pytest.raises(ValueError,match='anchor'):await validate_allocation_chain(chain,chain.rows[3])


@pytest.mark.asyncio
async def test_chain_depth_budget_is_bounded(chain,monkeypatch):
    from shared.accounting import allocation_provenance as module
    monkeypatch.setattr(module,'MAX_CHAIN_DEPTH',2)
    with pytest.raises(ValueError,match='depth'):await module.validate_allocation_chain(chain,chain.rows[3])


@pytest.mark.asyncio
async def test_json_entries_and_fetch_one_adapter_are_supported(chain):
    from shared.accounting.allocation_provenance import validate_allocation_chain
    for row in chain.rows.values():row['entries']=json.dumps(row['entries'])
    class DB:fetch_one=chain.fetchrow
    assert await validate_allocation_chain(DB(),chain.rows[3])=={s:Decimal(40) for s in SCOPE}


@pytest.mark.asyncio
async def test_chain_requires_real_positive_database_row_identity(chain):
    from shared.accounting.allocation_provenance import validate_allocation_chain
    row=deepcopy(chain.rows[3]);row['id']=True
    with pytest.raises(ValueError,match='row_identity'):await validate_allocation_chain(chain,row)


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[{'enabled':False},{'automatic_allocation':False}])
async def test_original_receipt_must_have_authorized_the_historical_publication(chain,change):
    from shared.accounting.allocation_provenance import validate_allocation_chain
    chain.receipt['payload'].update(change)
    chain.receipt['receipt_sha256']=acceptance_digest(chain.receipt['payload'])
    chain.receipts={chain.receipt['receipt_sha256']:chain.receipt}
    chain.append(2,1,30);chain.append(3,2,20)
    with pytest.raises(ValueError,match='original_approval_invalid'):
        await validate_allocation_chain(chain,chain.rows[3])
