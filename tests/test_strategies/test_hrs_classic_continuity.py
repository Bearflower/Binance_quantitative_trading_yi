"""经典 USD-M 实盘的现金证据、保护父单和 HRS 净值回放。"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from strategies.hrs.api_client import HrsBinanceClient
from strategies.hrs.equity_evidence import EvidenceBlocked
from strategies.hrs.equity_evidence import order_key, order_owners
from strategies.hrs.margin_budget import account_cash_fields, validate_cash_fields
from strategies.hrs.protection_observer import parent_endpoint


@pytest.mark.asyncio
async def test_cash_budget_requires_actual_classic_cash_and_signed_trade_permission():
    client = HrsBinanceClient("key", "secret", use_unified_account=False)
    balances = dict(totalMarginBalance="50", totalWalletBalance="48", totalUnrealizedProfit="2",
                    availableBalance="11")
    config = dict(canTrade=True, dualSidePosition=False, multiAssetsMargin=False)
    client._request = AsyncMock(side_effect=[balances, config, balances, dict(config, canTrade=False)])
    try:
        receipt = account_cash_fields(await client.get_account_info(), False)
        assert receipt["account_available_balance_source"] == "fapi/v3/account.availableBalance"
        assert validate_cash_fields(receipt) == Decimal("11")
        with pytest.raises(ValueError):
            validate_cash_fields(account_cash_fields(await client.get_account_info(), False))
        assert [c.args[1] for c in client._request.await_args_list] == [
            "/fapi/v3/account", "/fapi/v1/accountConfig"] * 2
    finally:
        await client.close()


def test_historical_and_classic_parent_and_nav_are_bound_to_their_epoch(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "1000")
    pm = datetime.fromtimestamp(.999, timezone.utc)
    classic = datetime.fromtimestamp(1, timezone.utc)
    assert parent_endpoint({"event_time":pm}, "main") == "/papi/v1/um/algo/algoOrder"
    assert parent_endpoint({"event_time":classic}, "main") == "/fapi/v1/algoOrder"
    fill = dict(order_id="17", symbol="XUSDT", trade_time_utc=classic)
    old = dict(account_id="main",order_id="17", symbol="XUSDT", executed_at=pm, strategy="hrs")
    new = dict(old, executed_at=classic, strategy="btc_eth_aggressive")
    owners = order_owners("main", [old, new])
    assert owners[order_key("main",fill)] == {"btc_eth_aggressive"}
    assert owners[order_key("main",dict(fill,trade_time_utc=pm))] == {"hrs"}
    assert order_key("main",fill) not in order_owners("main", [old])
    with pytest.raises(EvidenceBlocked, match="epoch"):
        order_key("main",dict(fill,order_epoch="pm"))


@pytest.mark.asyncio
async def test_startup_blocks_legacy_protection_and_timeout_restore(monkeypatch):
    from types import SimpleNamespace
    from strategies.hrs.cutover_state import verify_classic_restore
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "1000")
    client = SimpleNamespace(use_unified_account=False, testnet=False)
    for pending, live, backup, error in (
        ([{"value":'{"symbol":"ETHUSDT","order_id":"17"}'}], None, None, "超时"),
        ([], {"symbol":"ETHUSDT"}, None, "持仓表"),
        ([], None, {"backup":'{"positions":{"ETHUSDT":{"opened_at":"1970-01-01T00:00:00+00:00"}}}'}, "备份"),
        ([{"value":'{"account_id":"main","account_mode":"classic","cutover_ms":1000}'}], None,
         {"backup":'{"positions":{"ETHUSDT":{"opened_at":"1970-01-01T00:00:02+00:00"}}}'}, None)):
        db = SimpleNamespace(fetch_all=AsyncMock(return_value=pending),fetch_one=AsyncMock(side_effect=[live,backup]))
        if error:
            with pytest.raises(ValueError, match=error):
                await verify_classic_restore(db,client)
        else:
            await verify_classic_restore(db,client)


@pytest.mark.asyncio
async def test_classic_tp_quantity_requires_own_child_fills(monkeypatch):
    from unittest.mock import MagicMock
    from strategies.hrs.strategy import HRSStrategy
    strategy = object.__new__(HRSStrategy)
    strategy.binance_client = MagicMock(use_unified_account=False)
    manager = strategy.position_manager = MagicMock()
    manager.get_algo_id.side_effect = lambda symbol, role: {"tp1": 11,"tp2":12}[role]
    pos = dict(entry_quantity=10, remaining_quantity=10)
    confirm = AsyncMock(side_effect=[False,False,Decimal(2),False,Decimal(2),Decimal(3)])
    monkeypatch.setattr("shared.condition_orders.confirm_classic_algo_fill",confirm)
    for exchange_quantity in (8,7):
        with pytest.raises(ValueError,match="减仓量"):
            await strategy._classic_take_profit_change("XUSDT",pos,exchange_quantity)
    manager.mark_target_reached.assert_not_called()
    assert await strategy._classic_take_profit_change("XUSDT",pos,5)==2
    assert [c.kwargs["actual_remaining_quantity"] for c in manager.mark_target_reached.call_args_list]==[8,5]
    assert [c.kwargs["tp_level"] for c in confirm.await_args_list]==[1,2]*3


def test_funding_slots_and_offline_replay_disambiguate_reused_orders(monkeypatch):
    import json
    from datetime import timedelta
    from strategies.hrs.entry_occupancy import capture_hrs_slot_inputs, prove_hrs_slot_ownership
    from strategies.hrs.equity_evidence import canonical, certify_owned_inventory_paths, attribute_funding_from_inventory
    from ai_tuner.hrs_evidence_export import _flat_unchanged_proof

    T = datetime(2026, 11, 25, tzinfo=timezone.utc)
    boundary = int(T.timestamp()*1000)
    monkeypatch.setenv('TRADE_ACCOUNT_ID', 'main')
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS', str(boundary))
    facts, owners = [], []
    for tid, oid, offset, side, qty, owner in [
        (1, 17, -10, 'BUY', '1', 'hrs'), (2, 18, -2, 'SELL', '1', 'hrs'),
        (3, 17, 0, 'BUY', '2', 'btc_eth')]:
        at = T + timedelta(seconds=offset)
        epoch = 'pm' if offset < 0 else 'classic'
        facts.append(dict(account_id='main',symbol='XUSDT',trade_id=tid,order_id=oid,side=side,qty=qty,
                          price='10',trade_time_utc=at,order_epoch=epoch))
        owners.append(dict(account_id='main',symbol='XUSDT',order_id=str(oid),strategy=owner,
                           executed_at=at,order_epoch=epoch))
    events = [dict(account_id='main',symbol='XUSDT',income_type='FUNDING_FEE',asset='USDT',tran_id=i,
                   amount='-.1',event_time_utc=(T+timedelta(seconds=dt)).isoformat(),reason='funding_owner_unproven')
              for i,dt in [(77,-5),(78,1)]]
    now = T+timedelta(seconds=4)
    result = attribute_funding_from_inventory(dict(events=events,classic_cutover_ms=boundary,start_ms=boundary-11000,
        end_ms=boundary+4001), 'main', now, facts, owners, [dict(strategy='btc_eth',symbol='XUSDT',quantity='2')],
        coverage_complete=True)
    assert [event['strategy'] for event in result['events']] == ['hrs','btc_eth']
    pm, classic = result['mode_segments']
    assert Decimal(pm['by_strategy']['hrs']['funding_usdt']) == Decimal('-.1')
    assert Decimal(classic['by_strategy']['btc_eth']['funding_usdt']) == Decimal('-.1')

    inventory=[dict(symbol='XUSDT',positionAmt='2',entryPrice='10',positionSide='BOTH',markPrice='10',leverage='5')]
    paths=certify_owned_inventory_paths('main',{'XUSDT':Decimal(2)},facts,owners,coverage_complete=True)
    def parent(at, inv, fills):
        payload=dict(account_id='main',segment_id='segment',as_of=at,available_at=at,fills=fills,order_owners=owners,
            capture=dict(inventory=inv,independent=inv,local={}), source_collection=dict(complete=True,gaps=[],position_symbols_covered=['XUSDT']),
            inventory_owner_path_proofs=paths,income_report=dict(events=[],id_match_and_coverage_complete=True))
        encoded,digest=canonical(payload)
        return dict(evidence_sha256=digest,payload=json.loads(encoded))
    latest=parent(now,inventory,facts)
    # Offline consumers can replay the retained signed epochs without a host mode variable.
    monkeypatch.delenv('BINANCE_CLASSIC_CUTOVER_MS')
    inputs=capture_hrs_slot_inputs(inventory=inventory,independent=inventory,local={},observed_at=now)
    proof=prove_hrs_slot_ownership('main',latest,inputs,[dict(id=1,claim_state='ACTIVE',symbol='XUSDT',strategy='btc_eth')],checked_at=now)
    assert proof['hrs_symbols'] == [] and proof['owners'] == {'XUSDT':'btc_eth'}
    before=T-timedelta(seconds=1)
    initial=parent(before,[],facts[:2])
    assert _flat_unchanged_proof([initial,latest],before,now,[])[0] == latest
