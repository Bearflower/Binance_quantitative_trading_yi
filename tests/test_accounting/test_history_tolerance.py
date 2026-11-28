from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import pytest

from shared.accounting.history_tolerance import (
    HistoryAcceptanceError, acceptance_digest, validate_history_acceptance,
    read_history_acceptance,
)

NOW = datetime(2026, 11, 27, 8, tzinfo=timezone.utc)


def payload():
    return dict(kind='historical_risk_acceptance_v1', account_id='default', revision=1,
                enabled=True, exchange_uid_sha256='a'*64,
                classic_cutover_ms=1795582464000,
                accepted_from='2026-10-31T16:00:00+00:00',
                accepted_through='2026-11-27T07:59:00+00:00',
                effective_at='2026-11-27T08:00:00+00:00',
                classic_flat_at='2026-11-25T05:32:58.143000+00:00',
                classic_flat_evidence_sha256='b'*64, classic_open_equity='391.65638581',
                risk_limit_ratio='0.05', hrs_daily_limit='0.03', hrs_rolling_limit='0.06',
                classic_risk_method='classic_fixed_capital_cashflow_adjusted_drawdown_v1',
                automatic_allocation=True, approval_ref='operator:2026-11-27',
                allowed_gap_codes=['pm_native_income_coverage','historical_nav_sampling'],
                allocation_ceilings_usdt={'btc_eth':'100','btc_eth_aggressive':'60','hrs':'30','new_coin':'80'})


def row(p=None):
    p=payload() if p is None else p
    return dict(account_id=p['account_id'],revision=p['revision'],payload=p,receipt_sha256=acceptance_digest(p))


def test_valid_receipt_preserves_exact_limits_and_finite_history(monkeypatch):
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS','1795582464000')
    p=payload(); result=validate_history_acceptance(row(p),'default',now=NOW,expected_uid='a'*64)
    assert result.accepted_through < NOW
    assert result.classic_open_equity==Decimal('391.65638581')
    assert result.risk_limit_ratio==Decimal('.05') and result.automatic_allocation
    assert result.payload==p and result.receipt_sha256==acceptance_digest(p)
    assert result.covers('historical_nav_sampling', result.accepted_from,result.accepted_through)
    assert not result.covers('historical_nav_sampling', result.accepted_from,NOW)
    assert not result.covers('live_position_unknown', result.accepted_from,result.accepted_through)
    p['classic_open_equity']='999'
    assert result.payload['classic_open_equity']=='391.65638581'


@pytest.mark.parametrize('field,value',[
    ('kind','generic_allow_all'),('account_id','grid'),('revision',True),('revision',0),
    ('enabled','true'),('exchange_uid_sha256','invalid'),('classic_flat_evidence_sha256',''),
    ('classic_cutover_ms',1),('accepted_from','2026-11-28T00:00:00+00:00'),
    ('accepted_through','2026-11-27T08:01:00+00:00'),('accepted_through','2026-11-24T00:00:00+00:00'),
    ('effective_at','2026-11-27T08:01:00+00:00'),('classic_flat_at','2026-11-24T00:00:00+00:00'),
    ('accepted_from','2026-10-31T16:00:00'),('classic_open_equity','NaN'),('classic_open_equity','0'),
    ('risk_limit_ratio','0.06'),('hrs_daily_limit','0.04'),('hrs_rolling_limit','0.07'),
    ('classic_risk_method','pm_unit_nav'),('automatic_allocation',1),('approval_ref',''),
    ('allowed_gap_codes',['*']),('allowed_gap_codes',['current_api_failure']),
    ('allowed_gap_codes',[]),('allocation_ceilings_usdt',{'hrs':'30'}),
])
def test_invalid_receipt_cannot_authorize(field,value,monkeypatch):
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS','1795582464000')
    p=payload();p[field]=value
    with pytest.raises(HistoryAcceptanceError):validate_history_acceptance(row(p),'default',now=NOW,expected_uid='a'*64)


def test_hash_uid_and_database_identity_are_bound(monkeypatch):
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS','1795582464000')
    r=row(); r['receipt_sha256']='c'*64
    with pytest.raises(HistoryAcceptanceError):validate_history_acceptance(r,'default',now=NOW)
    with pytest.raises(HistoryAcceptanceError):validate_history_acceptance(row(),'default',now=NOW,expected_uid='d'*64)
    r=row();r['revision']=2
    with pytest.raises(HistoryAcceptanceError):validate_history_acceptance(r,'default',now=NOW)


@pytest.mark.asyncio
async def test_reader_no_policy_and_disabled_are_not_permission(monkeypatch):
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS','1795582464000')
    class DB:
        async def fetch_one(self, sql, *args):
            assert 'ORDER BY revision DESC LIMIT 1' in sql
            return None
    assert await read_history_acceptance(DB(),'default',now=NOW) is None
    p=payload();p['enabled']=False
    class Connection:
        async def fetchrow(self, sql, *args):return row(p)
    assert await read_history_acceptance(Connection(),'default',now=NOW) is None


@pytest.mark.asyncio
async def test_reader_accepts_json_payload(monkeypatch):
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS','1795582464000')
    r=row();r['payload']=json.dumps(r['payload'])
    class DB:
        async def fetch_one(self, sql, *args):return r
    assert (await read_history_acceptance(DB(),'default',now=NOW,expected_uid='a'*64)).revision==1
