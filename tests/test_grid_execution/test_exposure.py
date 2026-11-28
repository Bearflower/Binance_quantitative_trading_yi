import asyncio
import json
import runpy
import time
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.exposure import projected_exposure, validate_exposure
from strategies.grid.execution.main import ExecutorService
from strategies.grid.execution.contracts import ExecutionSettings
from strategies.grid.execution.planner import HardLimits, PlanLimitExceeded, ExchangeFilters
from strategies.grid.execution.store import DecisionStore, migrate


LIMITS = HardLimits(D(240), D(2400), 13, 5)


def order(side, qty, executed="0", role="ENTRY"):
    return {"side": side, "quantity": qty, "executed_qty": executed,
            "price": "2000", "role": role}


def positions(qty):
    return [{"positionSide": "BOTH", "positionAmt": qty}]


def test_partial_fill_and_closing_order_share_one_risk_envelope():
    result = projected_exposure(
        positions(".3"), [order("BUY", ".5", ".3"), order("SELL", ".3", role="REVERSE")],
        D(2000), D(".3"))
    assert result.notional == D(1000)
    validate_exposure(result, LIMITS)


def test_mark_move_and_journal_gap_cannot_hide_exposure():
    result = projected_exposure(positions("0"), [], D(2100), D(".6"))
    with pytest.raises(PlanLimitExceeded):
        validate_exposure(result, LIMITS)
    assert result.position_notional == D(1260)


def test_protective_stops_are_separate_from_ordinary_order_limit():
    orders = [order("BUY", ".01") for _ in range(13)] + [order("SELL", "1", role="STOP")]
    result = projected_exposure(positions("0"), orders, D(2000), D(0))
    assert result.order_count == 13
    validate_exposure(result, LIMITS)
    with pytest.raises(PlanLimitExceeded, match="count"):
        validate_exposure(projected_exposure(positions("0"),
            orders + [order("BUY", ".01")], D(2000), D(0)), LIMITS)


@pytest.mark.asyncio
@pytest.mark.parametrize("ticker, expected_orders", [("1943.65", 12), ("1943.66", 13)])
async def test_original_0305_signal_opens_under_production_budget(
        tmp_path, monkeypatch, ticker, expected_orders):
    sample = json.loads((Path(__file__).parents[1] / "fixtures/grid_execution/"
                         "ethusdt_20261114_0305.json").read_text())
    payload = sample["payload"]
    now = payload["calculated_at_ms"] + 3000
    monkeypatch.setattr(time, "time", lambda: now / 1000)
    exchange = runpy.run_path("tests/test_grid_execution/test_end_to_end.py")["FakeExchange"]()
    filters = ExchangeFilters(D(".01"), D(".001"), D(".001"), D(20))
    exchange.verify_account = AsyncMock(return_value=filters)
    exchange.ticker_price = AsyncMock(return_value=D(ticker))
    exchange.mark_price = AsyncMock(return_value=D("1943.65"))
    exchange.account_info = AsyncMock(return_value={
        "accountEquity": "300", "totalAvailableBalance": "300"})
    exchange.income = AsyncMock(return_value=[])
    database = tmp_path / "live.sqlite3"
    migrate(database)
    store = DecisionStore(database)
    settings = ExecutionSettings(
        mode="live", max_margin_usdt=D(240), max_notional_usdt=D(2400),
        max_daily_loss_usdt=D(15), max_drawdown_usdt=D(30),
        max_open_orders=13, expected_leverage=5)
    try:
        store.publish(payload, sample["config_hash"])
        service = ExecutorService(settings, store, exchange,
                                  {"GRID_EXECUTION_HEARTBEAT": str(tmp_path / "heartbeat")})
        service.stream.ready = True
        await service.tick()
        assert store.engine_state()["state"] == "ACTIVE"
        assert len(await exchange.open_orders()) == expected_orders
        assert all(D(o["origQty"]) == D(".047") for o in exchange.orders.values())
        assert sum(D(o["origQty"])*D(o["price"]) for o in exchange.orders.values())/5 <= 240
        epoch = store.active_epoch_id()
        first = next(k for k,v in exchange.orders.items() if v["side"]=="BUY")
        event = exchange.fill(first, 101)
        await service._on_event(event)
        assert store.engine_state()["state"] == "ACTIVE"
        await service.engine.check_limits()
        assert len(await exchange.open_orders()) <= 13
        # Duplicate deliveries serialize through the real service guard.
        await asyncio.gather(service._on_event(event), service._on_event(event))
        assert len(store.fills(first)) == 1
        assert len(await exchange.open_orders()) <= 13
        assert exchange.stops
        # Two different orders fill before either callback reaches the service.
        next_buys = [k for k,v in exchange.orders.items()
                     if v["side"] == "BUY" and v["status"] == "NEW"][:2]
        events = [exchange.fill(cid, tid) for cid,tid in zip(next_buys,(102,103))]
        trades = [{"symbol":"ETHUSDT", "side":e["o"]["S"], "orderId":e["o"]["i"],
                   "id":e["o"]["t"], "qty":e["o"]["l"], "price":e["o"]["L"]}
                  for e in events]
        exchange.order_trades = AsyncMock(side_effect=lambda oid,start,end:
                                          [t for t in trades if t["orderId"]==oid])
        await asyncio.gather(*(service._on_event(e) for e in events))
        assert store.engine_state()["state"] == "ACTIVE"
        assert all(len(store.fills(cid))==1 for cid in next_buys)
        await service.engine.check_limits()

        # A single order fills in two pieces, with a reverse obligation after
        # each fragment and no duplicate allocation on replay.
        partial = next(k for k,v in exchange.orders.items()
                       if v["side"]=="BUY" and v["status"]=="NEW")
        row = exchange.orders[partial]
        for qty,cumulative,status,tid in [
                (D(".02"),D(".02"),"PARTIALLY_FILLED",104),
                (D(".027"),D(".047"),"FILLED",105)]:
            exchange.position_amt += qty
            row["status"],row["executedQty"] = status,str(cumulative)
            e = {"e":"ORDER_TRADE_UPDATE","o":{
                "s":"ETHUSDT","c":partial,"i":row["orderId"],"S":"BUY","x":"TRADE",
                "X":status,"l":str(qty),"z":str(cumulative),"L":row["price"],"t":tid}}
            await service._on_event(e)
            assert store.engine_state()["state"] == "ACTIVE"
            await service.engine.check_limits()
        assert len(store.fills(partial))==2
        # Restart/reconnect verifies the same durable journal and resumes checks.
        restarted = ExecutorService(settings, store, exchange,
                                   {"GRID_EXECUTION_HEARTBEAT": str(tmp_path / "heartbeat")})
        await restarted._on_reconnect()
        assert store.active_epoch_id() == epoch
        assert await restarted.engine.enforce_limits(epoch)
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_exposure", [True, False])
async def test_rejected_reverse_releases_draft_and_preserves_filled_obligation(
        tmp_path, reject_exposure):
    path=tmp_path/"budget.sqlite3"; migrate(path); store=DecisionStore(path)
    try:
        epoch=store.create_epoch("s",levels=(D(1900),D(2000)),
                                 stop_loss_low=D(1800),stop_loss_high=D(2100))
        store.prepare_order(epoch,0,"ENTRY","BUY",D(1900),D(".02"),"eg-entry")
        store.record_order("eg-entry","1","FILLED",D(".02"))
        store.record_fill("eg-entry","1","1",D(".02"),D(1900))
        store.set_engine_state("ACTIVE")
        exchange=Mock()
        engine=GridEngine(store,exchange,LIMITS)
        if reject_exposure:
            engine.check_limits=AsyncMock(side_effect=PlanLimitExceeded("budget"))
        else:
            store.prepare_order(epoch,2,"ENTRY","SELL",D(1900),D(".06"),"eg-other")
            store.record_order("eg-other","2","FILLED",D(".06"))
            store.record_fill("eg-other","2","2",D(".06"),D(1900))
            engine.check_limits=AsyncMock(return_value=None)
            exchange.position=AsyncMock(return_value=positions("-.04"))
            exchange.account_info=AsyncMock(return_value={"totalAvailableBalance":"0"})
        async def pause(*args,**kwargs):
            store.set_engine_state("PAUSED")
            return True
        engine.pause=AsyncMock(side_effect=pause)
        exchange.place_limit=AsyncMock()
        assert await engine._replenish("eg-entry",(D(1900),D(2000)),
                                      ExchangeFilters(D(".01"),D(".001"),D(".001"),D(20)))
        assert store.engine_state()["state"]=="PAUSED"
        assert len(store.all_orders())==(1 if reject_exposure else 2)
        assert len(store.fills("eg-entry"))==1
        assert store.conn.execute("select count(*) from pair").fetchone()[0]==0
        exchange.place_limit.assert_not_awaited()
    finally:
        store.close()



@pytest.mark.asyncio
async def test_paused_position_over_budget_exits_instead_of_disabling_checks(tmp_path):
    path=tmp_path/"paused.sqlite3"; migrate(path); store=DecisionStore(path)
    try:
        epoch=store.create_epoch("s",stop_loss_low=D(1800),stop_loss_high=D(2100))
        store.set_engine_state("PAUSED")
        engine=GridEngine(store,Mock(),LIMITS)
        engine.check_limits=AsyncMock(side_effect=PlanLimitExceeded("position"))
        engine.pause=AsyncMock(return_value=True)
        engine.exit_all=AsyncMock(return_value=True)
        assert not await engine.enforce_limits(epoch)
        assert store.engine_state()["state"]=="LOCKED"
        engine.exit_all.assert_awaited_once_with(epoch)
    finally:
        store.close()



@pytest.mark.asyncio
async def test_unknown_exchange_order_prevents_risk_expansion(tmp_path):
    path=tmp_path/"foreign.sqlite3"; migrate(path); store=DecisionStore(path)
    try:
        exchange=Mock()
        exchange.position=AsyncMock(return_value=positions("0"))
        exchange.mark_price=AsyncMock(return_value=D(2000))
        exchange.open_orders=AsyncMock(return_value=[{"clientOrderId":"foreign"}])
        with pytest.raises(ValueError,match="unowned"):
            await GridEngine(store,exchange,LIMITS).check_limits()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_exposure_check_keeps_larger_unconfirmed_remainder(tmp_path):
    path=tmp_path/"remaining.sqlite3"; migrate(path); store=DecisionStore(path)
    try:
        epoch=store.create_epoch("s")
        store.prepare_order(epoch,0,"ENTRY","BUY",D(2000),D(".7"),"eg-1")
        store.record_order("eg-1","1","PARTIALLY_FILLED",D(".2"))
        exchange=Mock()
        exchange.position=AsyncMock(return_value=positions("0"))
        exchange.mark_price=AsyncMock(return_value=D(2000))
        exchange.open_orders=AsyncMock(return_value=[{
            "clientOrderId":"eg-1","symbol":"ETHUSDT","side":"BUY",
            "origQty":".7","executedQty":"0","price":"2000"}])
        with pytest.raises(PlanLimitExceeded):
            await GridEngine(store,exchange,LIMITS).check_limits()
    finally:
        store.close()



@pytest.mark.asyncio
async def test_failed_recheck_after_pause_keeps_cleanup_in_execution_loop(tmp_path):
    path=tmp_path/"failed-recheck.sqlite3"; migrate(path); store=DecisionStore(path)
    try:
        epoch=store.create_epoch("s",stop_loss_low=D(1800),stop_loss_high=D(2100))
        store.set_engine_state("ACTIVE")
        engine=GridEngine(store,Mock(),LIMITS)
        engine.check_limits=AsyncMock(side_effect=[PlanLimitExceeded("orders"),ValueError("bad evidence")])
        engine.pause=AsyncMock(return_value=True)
        engine.exit_all=AsyncMock(return_value=True)
        assert not await engine.enforce_limits(epoch)
        assert store.engine_state()["state"]=="LOCKED"
        engine.exit_all.assert_awaited_once_with(epoch)
    finally:
        store.close()
