import asyncio
import json
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.simulate_grid_snapshot import SimulatedExchange
from shared.binance_api import BinanceAPIError
from strategies.grid.execution.contracts import ExecutionSettings,decode_grid_params,decode_feasibility
from strategies.grid.execution.exchange import GridExchange
from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.fills import FillCoordinator
from strategies.grid.execution.main import ExecutorService
from strategies.grid.execution.planner import HardLimits,build_plan,ExchangeFilters
from strategies.grid.execution.risk import RiskAssessment,RiskStatus
from strategies.grid.execution.store import DecisionStore,migrate

ROOT=Path(__file__).resolve().parents[2]


class ExpiryRaceExchange(SimulatedExchange):
    def __init__(self,data,expiry_lag=3):
        super().__init__(data)
        self.close_calls=0
        self.delete_calls=0
        self.stale_reads=0
        self.expiry_lag=expiry_lag
        parent=self
        class Client:
            use_unified_account=True
            async def _request_once(self,*args,**kwargs):
                return await self._request(*args,**kwargs)
            async def _request(self,method,path,params=None,signed=True):
                if method=="GET":
                    return await parent.query_stop(params["clientAlgoId"])
                assert method=="DELETE"
                parent.delete_calls+=1
                raise BinanceAPIError(-2011,"Unknown order sent.")
        self.adapter=GridExchange(Client())

    async def close_position(self,*args):
        self.close_calls+=1
        result=await super().close_position(*args)
        for stop in self.stops.values():
            if stop["algoStatus"]=="NEW":stop["algoStatus"]="EXPIRED"
        self.stale_reads=self.expiry_lag
        return result

    async def query_stop(self,client_id):
        result=dict(self.stops[client_id])
        if result["algoStatus"]=="EXPIRED" and self.stale_reads:
            self.stale_reads-=1
            result["algoStatus"]="NEW"
        return result

    async def cancel_stop(self,client_id):
        return await self.adapter.cancel_stop(client_id)


def status_event(order,status="NEW",cumulative="0"):
    return {"e":"ORDER_TRADE_UPDATE","o":{
        "s":"ETHUSDT","c":order["clientOrderId"],"i":order["orderId"],"S":order["side"],
        "x":status,"X":status,"l":"0","z":cumulative,"L":"0","t":0}}


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry_lag", [3, 12])
async def test_real_hourly_rebuild_survives_stop_expiry_and_late_status(tmp_path,monkeypatch,expiry_lag):
    monkeypatch.setattr(asyncio,"sleep",AsyncMock())
    data=json.loads((ROOT/"tests/fixtures/grid_execution/rebuild_20261116.json").read_text())
    new=json.loads((ROOT/"tests/fixtures/grid_execution/ethusdt_20261116_1105.json").read_text())
    exchange=ExpiryRaceExchange(data,expiry_lag)
    exchange.clock=new["payload"]["calculated_at_ms"]+3000
    exchange.mark=D(new["payload"]["market_analysis"]["current_price"])
    monkeypatch.setattr("time.time",lambda:exchange.clock/1000)
    path=tmp_path/"journal";migrate(path);store=DecisionStore(path)
    try:
        epoch=store.create_epoch("old",levels=tuple(map(D,json.loads(data["epoch"]["levels_json"]))),
            stop_loss_low=D(data["epoch"]["stop_loss_low"]),stop_loss_high=D(data["epoch"]["stop_loss_high"]))
        for i,r in enumerate(data["local_orders"]):
            store.prepare_order(epoch,r["level_index"],r["role"],r["side"],
                D(r["price"]),D(r["quantity"]),r["client_order_id"])
            store.record_order(r["client_order_id"],r["exchange_order_id"],r["status"],r["executed_qty"])
            if D(r["executed_qty"])>0:
                store.record_fill(r["client_order_id"],r["exchange_order_id"],str(i+1),
                                  D(r["executed_qty"]),D(r["price"]))
        for pair in data["pairs"]:
            store.conn.execute("insert into pair(epoch_id,source_client_order_id,reverse_client_order_id,quantity,status) values(?,?,?,?,?)",
                (epoch,pair["source_client_order_id"],pair["reverse_client_order_id"],pair["quantity"],pair["status"]))
        for p in data["stops"]:
            store.conn.execute("insert into protection values(?,?,?,?,?,?)",
                (p["clientAlgoId"],epoch,p["side"],p["triggerPrice"],"NEW",str(p["algoId"])))
        store.set_engine_state("ACTIVE")
        store.publish(new["payload"],new["config_hash"])
        decision=store.next_decision()
        settings=ExecutionSettings(mode="live",max_margin_usdt=D(240),max_notional_usdt=D(2400),
            max_daily_loss_usdt=D(15),max_drawdown_usdt=D(30),max_open_orders=13,expected_leverage=5)
        service=ExecutorService(settings,store,exchange,{})
        service.filters=ExchangeFilters(D(".01"),D(".001"),D(".001"),D(20))
        service.stream.ready=True
        plan=build_plan(decode_grid_params(decision["grid_params"]),decode_feasibility(decision["feasibility"]),
            exchange.mark,service.filters,HardLimits(D(240),D(2400),13,5),margin_budget_usdt=D("184.40582448"))
        async def apply():
            return await service.engine.apply_decision(decision,plan,RiskAssessment(RiskStatus.ALLOW,()),
                leverage=5,now_ms=exchange.clock,stop_loss_low=plan.stop_loss_low,stop_loss_high=plan.stop_loss_high)
        started = await apply()
        if expiry_lag > 3:
            assert not started
            assert store.engine_state()["state"] == "EXITING"
            assert store.cursor() != decision["decision_id"]
            assert not await exchange.open_orders()
            for _ in range(8):
                if await service.engine.exit_all(epoch):
                    break
            else:
                pytest.fail("terminal stop never reconciled")
            assert store.engine_state()["state"] == "FLAT"
            assert exchange.delete_calls == 1
            started = await apply()
        assert started
        assert store.engine_state()["state"]=="ACTIVE"
        assert store.active_epoch_id()!=epoch
        assert exchange.close_calls==1 and exchange.delete_calls==1
        assert store.cursor()==decision["decision_id"]
        assert store.next_decision() is None
        assert exchange.quantity==0
        # The delayed exit NEW arrives after REST has backfilled its FILLED state.
        close=next(o for cid,o in exchange.orders.items() if cid.startswith("eg-exit"))
        events=[status_event(close)]
        events += [status_event(o) for cid,o in exchange.orders.items()
                   if store.get_order(cid)["epoch_id"]==epoch and o["status"]=="CANCELED"]
        await asyncio.gather(*(service._on_event(event) for event in events))
        assert store.engine_state()["state"]=="ACTIVE"
        assert len(store.fills(close["clientOrderId"]))==1
        await service.engine.check_limits()
        assert len(await exchange.open_orders())==len(plan.orders)
        # Repeat the same delayed statuses after the new cycle is also closed.
        # The FLAT branch must keep facts without calling them foreign orders.
        assert await service.engine.exit_all(store.active_epoch_id())
        await asyncio.gather(*(service._on_event(event) for event in events))
        assert store.engine_state()["state"]=="FLAT"
        assert len(store.fills(close["clientOrderId"]))==1
    finally:store.close()


def test_late_status_ignored_but_unknown_trade_and_quantity_change_fail(tmp_path):
    path=tmp_path/"journal";migrate(path);store=DecisionStore(path)
    try:
        epoch=store.create_epoch("s")
        store.prepare_order(epoch,0,"EXIT","BUY",D(1),D(".05"),"eg-exit")
        store.record_order("eg-exit","1","FILLED",D(".05"))
        store.record_fill("eg-exit","1","1",D(".05"),D(1800))
        event=status_event({"clientOrderId":"eg-exit","orderId":1,"side":"BUY"})
        assert not FillCoordinator(store).record(event)
        assert store.get_order("eg-exit")["executed_qty"]=="0.05"
        bad=dict(event,o=dict(event["o"],i=2))
        with pytest.raises(ValueError,match="ID changed"):
            FillCoordinator(store).record(bad)
        assert len(store.fills("eg-exit"))==1
    finally:store.close()


@pytest.mark.asyncio
async def test_actual_unknown_event_remains_locked_and_is_preserved(tmp_path):
    path=tmp_path/"journal";migrate(path);store=DecisionStore(path)
    try:
        service=ExecutorService.__new__(ExecutorService)
        service.store=store;service.guard=asyncio.Lock()
        service.engine=GridEngine(store,None)
        event=status_event({"clientOrderId":"eg-foreign","orderId":99,"side":"BUY"})
        await service._on_event(event)
        await service._on_event(event)
        assert store.engine_state()["lock_reason"]=="unowned order event"
        rows=store.conn.execute("select payload_json,error from order_event_failure").fetchall()
        assert len(rows)==1
        assert json.loads(rows[0][0])["o"]["c"]=="eg-foreign"
        assert "no owned order intent" in rows[0][1]
    finally:store.close()


@pytest.mark.asyncio
async def test_flat_and_closed_epoch_commit_atomically(tmp_path):
    import sqlite3
    path=tmp_path/"journal";migrate(path);store=DecisionStore(path)
    try:
        epoch=store.create_epoch("s");store.set_engine_state("ACTIVE")
        exchange=AsyncMock()
        exchange.position.return_value=[{"positionSide":"BOTH","positionAmt":"0"}]
        exchange.open_orders.return_value=exchange.open_stops.return_value=[]
        store.conn.execute("""CREATE TEMP TRIGGER fail_close BEFORE UPDATE ON grid_epoch
            BEGIN SELECT RAISE(ABORT,'simulated interruption'); END""")
        with pytest.raises(sqlite3.IntegrityError):
            await GridEngine(store,exchange).exit_all(epoch)
        assert store.engine_state()["state"]=="EXITING"
        assert store.active_epoch_id()==epoch
        store.conn.execute("drop trigger fail_close")
        assert await GridEngine(store,exchange).exit_all(epoch)
        assert store.engine_state()["state"]=="FLAT"
        assert store.active_epoch_id() is None
    finally:store.close()
