"""Real PostgreSQL serialization and fail-closed publication, without an exchange."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import pytest
import asyncpg
from ai_tuner.allocation.allocation_calculator import AllocationEntry, AllocationResult
from shared.accounting.history_tolerance import acceptance_digest, validate_history_acceptance
from shared.accounting.allocation_provenance import entries_digest

ROOT=Path(__file__).resolve().parents[2]
SIDS=('btc_eth','btc_eth_aggressive','hrs','new_coin')
NOW=datetime(2026,12,1,0,tzinfo=timezone.utc)

@pytest.fixture(scope='module')
def pg():
    if not all(shutil.which(x) for x in ('initdb','pg_ctl','psql')):
        pytest.skip('local PostgreSQL required')
    with tempfile.TemporaryDirectory(prefix='classic-publisher-') as tmp:
        data=Path(tmp)/'data'
        subprocess.run(['initdb','-D',str(data),'-U','test_admin','-A','trust','--no-locale','-E','UTF8'],check=True,capture_output=True)
        subprocess.run(['pg_ctl','-D',str(data),'-l',str(Path(tmp)/'log'),'-o',f"-F -k {tmp} -h '' -p 55449",'-w','start'],check=True,capture_output=True)
        env=dict(os.environ,PGHOST=tmp,PGPORT='55449',PGDATABASE='postgres',PGUSER='test_admin')
        def sql(query):
            p=subprocess.run(['psql','-XAt','-v','ON_ERROR_STOP=1'],input=query,text=True,capture_output=True,env=env)
            assert p.returncode==0,p.stderr
            return p.stdout
        try:
            sql('CREATE ROLE trading_migration_admin LOGIN; CREATE ROLE trading_user LOGIN; CREATE SCHEMA trading AUTHORIZATION trading_migration_admin;')
            sql('SET ROLE trading_migration_admin;'+(ROOT/'database/postgres/migrations/2026-11-27-historical-risk-acceptance.sql').read_text())
            # Same production fields; no fixture-specific permission widening.
            sql('''SET ROLE trading_migration_admin;
            CREATE TABLE trading.strategy_risk_policy (
              account_id text, strategy_id text, revision bigint, effective_at timestamptz, expires_at timestamptz,
              mode text, allow_add boolean, multiplier numeric, ratio_cap numeric, entry_ceiling numeric,
              freeze_allocation boolean, reason text, created_at timestamptz DEFAULT now(), PRIMARY KEY(account_id,strategy_id,revision));
            CREATE TABLE trading.strategy_risk_active(account_id text,strategy_id text,revision bigint,PRIMARY KEY(account_id,strategy_id));
            CREATE TABLE trading.hrs_loss_gate_state(account_id text PRIMARY KEY,segment_id text NOT NULL,version bigint NOT NULL,ledger_state jsonb NOT NULL,gate_state jsonb NOT NULL,updated_at timestamptz DEFAULT now());
            CREATE TABLE trading.strategy_equity_gate(account_id text,strategy_id text,policy_revision bigint,ready boolean,allowed boolean,as_of timestamptz,valid_until timestamptz,reasons jsonb,evidence_sha256 text,updated_at timestamptz DEFAULT now(),PRIMARY KEY(account_id,strategy_id));
            CREATE TABLE trading.income_ingestion_attempt(attempt_id text PRIMARY KEY,account_id text,observations jsonb,state text,created_at timestamptz DEFAULT now(),finished_at timestamptz);
            RESET ROLE;
            CREATE TABLE public.capital_allocation(id serial PRIMARY KEY,month date NOT NULL UNIQUE,total_capital numeric(20,8),strategy_count integer,is_first_month boolean,entries jsonb,status text,created_at timestamp DEFAULT now());
            ALTER TABLE public.capital_allocation OWNER TO trading_user;
            ''')
            full=json.loads((ROOT/'database/postgres/permissions.json').read_text())
            names={'historical_risk_acceptance','classic_account_risk_point','strategy_risk_policy','strategy_risk_active','capital_allocation','capital_allocation_id_seq','hrs_loss_gate_state','strategy_equity_gate','income_ingestion_attempt'}
            objs=[r for r in full['objects'] if r.get('name') in names]
            for obj in objs:obj['grants']={k:v for k,v in obj['grants'].items() if k=='trading_user'}
            manifest={'version':1,'schemas':['public','trading'],'roles':{'trading_user':{'required':True,'schemas':{'public':['USAGE'],'trading':['USAGE']}}},'profiles':full['profiles'],'objects':objs}
            f=Path(tmp)/'manifest.json';f.write_text(json.dumps(manifest))
            p=subprocess.run(['python3',str(ROOT/'scripts/database_permissions.py'),'apply','--local','--manifest',str(f)],env=env,text=True,capture_output=True)
            assert p.returncode==0,p.stdout+p.stderr
            yield {'host':tmp,'port':55449,'database':'postgres'},sql
        finally:
            subprocess.run(['pg_ctl','-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True)

async def setup_case(pg,monkeypatch):
    from ai_tuner.allocation.classic_publication import risk_policy_digest
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS','1795582464000')
    monkeypatch.delenv('ENTRY_EQUITY_GATE_STRATEGIES',raising=False)
    args,sql=pg
    sql('DROP TRIGGER historical_risk_acceptance_immutable ON trading.historical_risk_acceptance; DROP TRIGGER classic_account_risk_point_immutable ON trading.classic_account_risk_point; DELETE FROM trading.classic_account_risk_point; DELETE FROM trading.historical_risk_acceptance; DELETE FROM trading.strategy_risk_active; DELETE FROM trading.strategy_risk_policy; DELETE FROM public.capital_allocation; DELETE FROM trading.hrs_loss_gate_state; DELETE FROM trading.strategy_equity_gate; DELETE FROM trading.income_ingestion_attempt;')
    # Reapply immutable guards, then all test mutations append new revisions.
    sql('SET ROLE trading_migration_admin;'+(ROOT/'database/postgres/migrations/2026-11-27-historical-risk-acceptance.sql').read_text())
    admin=await asyncpg.connect(**args,user='test_admin')
    overrides={}
    for sid in SIDS:
        row=await admin.fetchrow("INSERT INTO trading.strategy_risk_policy VALUES ('default',$1,1,'2000-01-01Z','2100-01-01Z','capped',false,1,.2,30,true,'existing approved limits',now()) RETURNING *",sid)
        overrides[sid]={'revision':1,'policy_sha256':risk_policy_digest(dict(row))}
        await admin.execute("INSERT INTO trading.strategy_risk_active VALUES ('default',$1,1)",sid)
    previous=[dict(strategy_id=sid,allocated_amount=30) for sid in SIDS]
    anchor_id=await admin.fetchval("INSERT INTO public.capital_allocation(month,total_capital,strategy_count,is_first_month,entries,status) VALUES ('2026-11-01',150,4,false,$1::jsonb,'active') RETURNING id",json.dumps(previous))
    p=dict(kind='historical_risk_acceptance_v1',account_id='default',revision=1,enabled=True,
        exchange_uid_sha256='a'*64,classic_cutover_ms=1795582464000,accepted_from='2026-10-31T16:00:00+00:00',
        accepted_through='2026-11-27T07:59:00+00:00',effective_at='2026-11-27T08:00:00+00:00',
        classic_flat_at='2026-11-25T05:32:58.143000+00:00',classic_flat_evidence_sha256='b'*64,
        classic_open_equity='391.65638581',risk_limit_ratio='.05',hrs_daily_limit='.03',hrs_rolling_limit='.06',
        classic_risk_method='classic_fixed_capital_cashflow_adjusted_drawdown_v1',automatic_allocation=True,
        approval_ref='operator:test',allowed_gap_codes=['historical_nav_sampling'],
        allocation_ceilings_usdt={sid:'30' for sid in SIDS},entry_risk_ceilings_usdt={sid:'30' for sid in SIDS},
        allocation_freeze_overrides=overrides,hrs_segment_id='hrs-original',hrs_baseline_state_version=1,
        allocation_anchor_id=anchor_id,allocation_anchor_entries_sha256=entries_digest(previous))
    sha=acceptance_digest(p)
    await admin.execute("INSERT INTO trading.historical_risk_acceptance(account_id,revision,payload,receipt_sha256) VALUES ('default',1,$1::jsonb,$2)",json.dumps(p),sha)
    policy=validate_history_acceptance(dict(account_id='default',revision=1,payload=p,receipt_sha256=sha),'default',now=NOW)
    risk=dict(status='VERIFIED_WITH_ACCEPTED_HISTORY',account_id='default',policy_revision=1,acceptance_sha256=sha,
        as_of_ms=int(NOW.timestamp()*1000),financial_evidence_verified=True,known_breach=None,max_drawdown_ratio='.01',
        current_equity_usd='100',income_attribution={'ready':True,'account_id':'default','receipt_sha256':sha,'coverage_end_ms':int(NOW.timestamp()*1000)})
    await admin.execute("INSERT INTO trading.classic_account_risk_point(account_id,policy_revision,sample_time_ms,payload) VALUES ('default',1,$1,$2::jsonb)",risk['as_of_ms'],json.dumps(risk))
    gate={'segment_id':'hrs-original','day':'2026-11-30','day_origin_nav':'1','daily_latched':False,'rolling_latched':False,
        'rolling_latch_scope':None,'revision':2,'as_of':'2026-11-30T15:59:00+00:00'}
    await admin.execute("INSERT INTO trading.hrs_loss_gate_state(account_id,segment_id,version,ledger_state,gate_state) VALUES ('default','hrs-original',2,'{}',$1::jsonb)",json.dumps(gate))
    pool=await asyncpg.create_pool(**args,user='trading_user',min_size=1,max_size=4)
    r=AllocationResult('2026-12',100,25,75,False,[AllocationEntry(s,s,0,30,None,None,.3 if s=='hrs' else .15,30 if s=='hrs' else 15) for s in SIDS],history_acceptance_sha256=sha)
    return admin,pool,policy,risk,r

@pytest.mark.asyncio
async def test_concurrent_publish_inserts_once_and_preserves_floor(pg,monkeypatch):
    from ai_tuner.allocation.classic_publication import publish_classic_allocation
    admin,pool,policy,risk,result=await setup_case(pg,monkeypatch)
    try:
        outcomes=await asyncio.gather(*(publish_classic_allocation(SimpleNamespace(pool=pool),result,policy,risk,.15,'2026-11',now=NOW) for _ in range(2)))
        assert sorted(outcomes)==[False,True]
        rows=await admin.fetch("SELECT entries FROM public.capital_allocation WHERE month='2026-12-01'")
        assert len(rows)==1
        assert all(Decimal(e['holdings_floor_usdt'])==30 and e['allocated_amount']==(30 if e['strategy_id']=='hrs' else 15) for e in json.loads(rows[0]['entries']))
    finally:await pool.close();await admin.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['revoked','new_freeze','risk_stale','attribution_pending','over_cap','reserve','wrong_month','known_loss','entry_cap','missing_caps','scope','reserve_ratio','risk_changed','missing_previous','previous_floor'])
async def test_publication_guards_leave_ledger_unchanged(pg,monkeypatch,fault):
    from ai_tuner.allocation.classic_publication import publish_classic_allocation
    admin,pool,policy,risk,result=await setup_case(pg,monkeypatch)
    try:
        if fault=='revoked':
            p=dict(policy.payload,revision=2,enabled=False)
            await admin.execute("INSERT INTO trading.historical_risk_acceptance(account_id,revision,payload,receipt_sha256) VALUES ('default',2,$1::jsonb,$2)",json.dumps(p),acceptance_digest(p))
        elif fault=='new_freeze':
            await admin.execute("INSERT INTO trading.strategy_risk_policy SELECT account_id,strategy_id,2,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,true,'new emergency freeze',created_at FROM trading.strategy_risk_policy WHERE strategy_id='hrs'")
            await admin.execute("UPDATE trading.strategy_risk_active SET revision=2 WHERE strategy_id='hrs'")
        elif fault=='risk_stale': risk['as_of_ms']-=121000
        elif fault=='attribution_pending':risk['income_attribution']['ready']=False
        elif fault=='over_cap':result.entries[0].allocated_amount=31
        elif fault=='reserve':result.entries[0].allocated_amount=26
        elif fault=='wrong_month':result.month='2027-01'
        elif fault=='known_loss':risk['known_breach']={'loss':True}
        elif fault=='entry_cap':
            await admin.execute("UPDATE trading.strategy_risk_policy SET entry_ceiling=31,freeze_allocation=false WHERE strategy_id='hrs'")
        elif fault=='missing_caps':
            # A newly approved malformed receipt must not grant runtime fallback caps.
            p=dict(policy.payload,revision=2,entry_risk_ceilings_usdt={})
            sha=acceptance_digest(p)
            await admin.execute("INSERT INTO trading.historical_risk_acceptance(account_id,revision,payload,receipt_sha256) VALUES ('default',2,$1::jsonb,$2)",json.dumps(p),sha)
            policy=validate_history_acceptance(dict(account_id='default',revision=2,payload=p,receipt_sha256=sha),'default',now=NOW)
            result.history_acceptance_sha256=sha
            risk.update(policy_revision=2,acceptance_sha256=sha,as_of_ms=risk['as_of_ms']+1)
            risk['income_attribution']['receipt_sha256']=sha
            await admin.execute("INSERT INTO trading.classic_account_risk_point(account_id,policy_revision,sample_time_ms,payload) VALUES ('default',2,$1,$2::jsonb)",risk['as_of_ms'],json.dumps(risk))
        elif fault=='scope':result.entries.pop()
        elif fault=='reserve_ratio':pass
        elif fault=='risk_changed':risk['current_equity_usd']='101'
        elif fault=='missing_previous':await admin.execute("UPDATE public.capital_allocation SET status='inactive'")
        elif fault=='previous_floor':
            await admin.execute("UPDATE public.capital_allocation SET entries=(SELECT jsonb_agg(e || '{\"holdings_floor_usdt\":\"31\",\"publication_method\":\"classic_history_acceptance_v1\"}'::jsonb) FROM jsonb_array_elements(entries) e)")
        with pytest.raises(ValueError):
            await publish_classic_allocation(SimpleNamespace(pool=pool),result,policy,risk,.14 if fault=='reserve_ratio' else .15,'2026-11',now=NOW+timedelta(seconds=1))
        assert await admin.fetchval('SELECT count(*) FROM public.capital_allocation')==1
    finally:await pool.close();await admin.close()

@pytest.mark.asyncio
async def test_revocation_committed_while_publisher_waits_is_observed(pg,monkeypatch):
    from ai_tuner.allocation.classic_publication import publish_classic_allocation
    admin,pool,policy,risk,result=await setup_case(pg,monkeypatch)
    try:
        async with admin.transaction():
            await admin.execute("SELECT pg_advisory_xact_lock(hashtextextended('classic-account-risk-v1:default',0))")
            task=asyncio.create_task(publish_classic_allocation(SimpleNamespace(pool=pool),result,policy,risk,.15,'2026-11',now=NOW))
            await asyncio.sleep(.05)
            assert not task.done()
            p=dict(policy.payload,revision=2,enabled=False)
            await admin.execute("INSERT INTO trading.historical_risk_acceptance(account_id,revision,payload,receipt_sha256) VALUES ('default',2,$1::jsonb,$2)",json.dumps(p),acceptance_digest(p))
        with pytest.raises(ValueError):await task
        assert await admin.fetchval('SELECT count(*) FROM public.capital_allocation')==1
    finally:await pool.close();await admin.close()

@pytest.mark.asyncio
async def test_new_stored_breach_wins_over_older_caller_snapshot(pg,monkeypatch):
    from ai_tuner.allocation.classic_publication import publish_classic_allocation
    admin,pool,policy,risk,result=await setup_case(pg,monkeypatch)
    try:
        newer=dict(risk,as_of_ms=risk['as_of_ms']+1,known_breach={'source':'verified_loss'},status='SAFETY_REVIEW')
        await admin.execute("INSERT INTO trading.classic_account_risk_point(account_id,policy_revision,sample_time_ms,payload) VALUES ('default',1,$1,$2::jsonb)",newer['as_of_ms'],json.dumps(newer))
        with pytest.raises(ValueError):
            await publish_classic_allocation(SimpleNamespace(pool=pool),result,policy,risk,.15,'2026-11',now=NOW+timedelta(seconds=1))
        assert await admin.fetchval('SELECT count(*) FROM public.capital_allocation')==1
    finally:await pool.close();await admin.close()

@pytest.mark.asyncio
async def test_new_month_allocation_can_publish_before_hrs_projection_has_allocation(pg,monkeypatch):
    from ai_tuner.allocation.classic_publication import publish_classic_allocation
    admin,pool,policy,risk,result=await setup_case(pg,monkeypatch)
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES','hrs')
    try:
        await admin.execute("INSERT INTO trading.strategy_equity_gate(account_id,strategy_id,policy_revision,ready,allowed,as_of,valid_until,reasons,evidence_sha256) VALUES ('default','hrs',1,false,false,$1,$1,'[\"allocation_month_missing\"]',$2)",NOW,'b'*64)
        assert await publish_classic_allocation(SimpleNamespace(pool=pool),result,policy,risk,.15,'2026-11',now=NOW)
        assert await admin.fetchval('SELECT count(*) FROM public.capital_allocation')==2
        assert await admin.fetchval("SELECT ready FROM trading.strategy_equity_gate WHERE strategy_id='hrs'") is False
    finally:await pool.close();await admin.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['daily_latched','rolling_latched','missing','invalid_boolean','wrong_segment','old_version','future','foreign_account','foreign_receipt','pending_income','missing_latch_field','invalid_origin','future_day'])
async def test_new_month_keeps_known_hrs_losses_and_attribution_identity_guards(pg,monkeypatch,fault):
    from ai_tuner.allocation.classic_publication import publish_classic_allocation
    admin,pool,policy,risk,result=await setup_case(pg,monkeypatch)
    try:
        if fault in ('daily_latched','rolling_latched'):
            await admin.execute("UPDATE trading.hrs_loss_gate_state SET gate_state=jsonb_set(gate_state,$1::text[],'true')",[fault])
            if fault=='daily_latched':
                await admin.execute("UPDATE trading.hrs_loss_gate_state SET gate_state=jsonb_set(gate_state,'{day}','\"2026-12-01\"')")
        elif fault=='missing':await admin.execute('DELETE FROM trading.hrs_loss_gate_state')
        elif fault=='invalid_boolean':await admin.execute("UPDATE trading.hrs_loss_gate_state SET gate_state=jsonb_set(gate_state,'{daily_latched}','\"false\"')")
        elif fault=='wrong_segment':await admin.execute("UPDATE trading.hrs_loss_gate_state SET segment_id='new-reset-segment'")
        elif fault=='old_version':await admin.execute('UPDATE trading.hrs_loss_gate_state SET version=0')
        elif fault=='future':await admin.execute("UPDATE trading.hrs_loss_gate_state SET gate_state=jsonb_set(gate_state,'{as_of}','\"2026-12-02T00:00:00+00:00\"')")
        elif fault=='foreign_account':risk['income_attribution']['account_id']='grid'
        elif fault=='foreign_receipt':risk['income_attribution']['receipt_sha256']='c'*64
        elif fault=='missing_latch_field':await admin.execute("UPDATE trading.hrs_loss_gate_state SET gate_state=gate_state-'daily_latched'")
        elif fault=='future_day':await admin.execute("UPDATE trading.hrs_loss_gate_state SET gate_state=jsonb_set(gate_state,'{day}','\"2026-12-02\"')")
        elif fault=='invalid_origin':await admin.execute("UPDATE trading.hrs_loss_gate_state SET gate_state=jsonb_set(gate_state,'{day_origin_nav}','\"NaN\"')")
        elif fault=='pending_income':await admin.execute("INSERT INTO trading.income_ingestion_attempt(attempt_id,account_id,observations,state) VALUES ('pending','default','[]','pending')")
        if fault in ('foreign_account','foreign_receipt'):
            risk['as_of_ms']+=1
            await admin.execute("INSERT INTO trading.classic_account_risk_point(account_id,policy_revision,sample_time_ms,payload) VALUES ('default',1,$1,$2::jsonb)",risk['as_of_ms'],json.dumps(risk))
        with pytest.raises((ValueError,RuntimeError)):
            await publish_classic_allocation(SimpleNamespace(pool=pool),result,policy,risk,.15,'2026-11',now=NOW+timedelta(seconds=1))
        assert await admin.fetchval('SELECT count(*) FROM public.capital_allocation')==1
    finally:await pool.close();await admin.close()

@pytest.mark.asyncio
async def test_publisher_cannot_launder_a_damaged_parent_holdings_floor(pg,monkeypatch):
    from ai_tuner.allocation.classic_publication import publish_classic_allocation,publication_entries
    admin,pool,policy,risk,result=await setup_case(pg,monkeypatch)
    try:
        anchor=await admin.fetchrow('SELECT id,entries FROM public.capital_allocation')
        await admin.execute("UPDATE public.capital_allocation SET month='2026-10-01'")
        parent=publication_entries(result,anchor)
        parent[0]['holdings_floor_usdt']='0'
        await admin.execute("INSERT INTO public.capital_allocation(month,total_capital,strategy_count,is_first_month,entries,status) VALUES ('2026-11-01',100,4,false,$1::jsonb,'active')",json.dumps(parent))
        with pytest.raises(ValueError):
            await publish_classic_allocation(SimpleNamespace(pool=pool),result,policy,risk,.15,'2026-11',now=NOW)
        assert await admin.fetchval('SELECT count(*) FROM public.capital_allocation')==2
    finally:await pool.close();await admin.close()

@pytest.mark.asyncio
async def test_prior_day_daily_latch_does_not_deadlock_rollover_and_is_not_cleared(pg,monkeypatch):
    from ai_tuner.allocation.classic_publication import publish_classic_allocation
    admin,pool,policy,risk,result=await setup_case(pg,monkeypatch)
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES','hrs')
    try:
        await admin.execute("UPDATE trading.hrs_loss_gate_state SET gate_state=jsonb_set(gate_state,'{daily_latched}','true')")
        before=await admin.fetchval('SELECT gate_state FROM trading.hrs_loss_gate_state')
        await admin.execute("INSERT INTO trading.strategy_equity_gate(account_id,strategy_id,policy_revision,ready,allowed,as_of,valid_until,reasons,evidence_sha256) VALUES ('default','hrs',1,false,false,$1,$1,'[\"allocation_month_missing\"]',$2)",NOW,'b'*64)
        assert await publish_classic_allocation(SimpleNamespace(pool=pool),result,policy,risk,.15,'2026-11',now=NOW)
        after=await admin.fetchval('SELECT gate_state FROM trading.hrs_loss_gate_state')
        assert before==after and json.loads(after)['daily_latched'] is True
        gate=await admin.fetchrow("SELECT ready,allowed FROM trading.strategy_equity_gate WHERE strategy_id='hrs'")
        assert gate['ready'] is False and gate['allowed'] is False
        entries=json.loads(await admin.fetchval("SELECT entries FROM public.capital_allocation WHERE month='2026-12-01'"))
        assert all(Decimal(str(e['allocated_amount']))<=Decimal(policy.payload['allocation_ceilings_usdt'][e['strategy_id']]) for e in entries)
    finally:await pool.close();await admin.close()

@pytest.mark.asyncio
async def test_publisher_rechecks_hrs_principal_in_database_instead_of_accepting_calculator_claim(pg,monkeypatch):
    from ai_tuner.allocation.classic_publication import publish_classic_allocation
    admin,pool,policy,risk,result=await setup_case(pg,monkeypatch)
    try:
        for entry in result.entries:
            entry.allocated_amount=29 if entry.strategy_id=='hrs' else 15
        with pytest.raises(ValueError,match='hrs_allocation_changed'):
            await publish_classic_allocation(SimpleNamespace(pool=pool),result,policy,risk,.15,'2026-11',now=NOW)
        assert await admin.fetchval('SELECT count(*) FROM public.capital_allocation')==1
    finally:await pool.close();await admin.close()

@pytest.mark.asyncio
async def test_new_month_publication_keeps_hrs_capital_with_existing_hrs_position(pg,monkeypatch):
    from ai_tuner.allocation.classic_publication import publish_classic_allocation
    admin,pool,policy,risk,result=await setup_case(pg,monkeypatch)
    try:
        risk['as_of_ms']+=1
        risk['observation']={'raw_snapshot':{'positions':[{'symbol':'ETHUSDT','positionAmt':'1','entryPrice':'100'}]}}
        risk['income_attribution']['current_inventory']=[{'strategy':'hrs','symbol':'ETHUSDT','quantity':'1'}]
        await admin.execute("INSERT INTO trading.classic_account_risk_point(account_id,policy_revision,sample_time_ms,payload) VALUES ('default',1,$1,$2::jsonb)",risk['as_of_ms'],json.dumps(risk))
        before=await admin.fetchrow('SELECT * FROM trading.hrs_loss_gate_state')
        assert await publish_classic_allocation(SimpleNamespace(pool=pool),result,policy,risk,.15,'2026-11',now=NOW+timedelta(seconds=1))
        rows=await admin.fetch('SELECT month,entries FROM public.capital_allocation ORDER BY month')
        amounts=[Decimal(str(next(e for e in json.loads(r['entries']) if e['strategy_id']=='hrs')['allocated_amount'])) for r in rows]
        assert amounts==[Decimal('30'),Decimal('30')]
        assert before==await admin.fetchrow('SELECT * FROM trading.hrs_loss_gate_state')
        assert all(e['allocation_constraint']=='fixed_hrs_allocation' for e in json.loads(rows[-1]['entries']))
    finally:await pool.close();await admin.close()
