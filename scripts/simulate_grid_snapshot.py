"""Offline scenario simulation through the actual grid execution engine.

No API client is constructed. Input must be a previously captured read-only
snapshot. Future paths are assumptions, not market forecasts.
"""
import argparse
import asyncio
import csv
import json
import sys
import tempfile
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.grid.execution.contracts import ExecutionSettings
from strategies.grid.execution.main import ExecutorService
from strategies.grid.execution.risk import beijing_day
from strategies.grid.execution.store import DecisionStore, migrate
from strategies.grid.execution.planner import ExchangeFilters

D = Decimal


class SimulatedExchange:
    def __init__(self, data):
        self.data = data
        self.orders = {r["client_order_id"]: {
            "clientOrderId":r["client_order_id"],"orderId":int(r["exchange_order_id"]),
            "side":r["side"],"price":r["price"],"origQty":r["quantity"],
            "executedQty":r["executed_qty"],"status":r["status"],"symbol":"ETHUSDT"}
            for r in data["local_orders"] if r["exchange_order_id"]}
        position = data["positions"][0]
        self.quantity = D(position["positionAmt"])
        self.entry = D(position.get("entryPrice", "0"))
        self.mark = D(position["markPrice"])
        self.realized = D(0)
        self.fees = sum((self.fee(D(r["executed_qty"])*D(r["price"]), False)
                         for r in data["local_orders"] if D(r["executed_qty"])>0),D(0))
        self.trades = []
        self.stops = {r["clientAlgoId"]:dict(r) for r in data["stops"]}
        self.sequence = max(o["orderId"] for o in self.orders.values())
        self.clock = data["observed_ms"]
        self.slippage = D(".0002")

    def fee(self, notional, taker):
        rate = self.data["fees"]["takerCommissionRate" if taker else "makerCommissionRate"]
        return (notional*D(rate)).quantize(D(".00000001"),rounding=ROUND_DOWN)

    def equity(self):
        return D(300)+self.realized-self.fees+self.quantity*(self.mark-self.entry)

    async def verify_account(self, leverage):
        assert leverage == 5
        return ExchangeFilters(D(".01"),D(".001"),D(".001"),D(20))

    async def ticker_price(self): return self.mark
    async def mark_price(self): return self.mark
    async def income(self, start, end): return []

    async def account_info(self):
        return {"accountEquity":str(self.equity()),
                "totalAvailableBalance":str(self.equity()-abs(self.quantity)*self.mark/5)}

    async def position(self):
        return [{"symbol":"ETHUSDT","positionSide":"BOTH","positionAmt":str(self.quantity),
                 "markPrice":str(self.mark),"entryPrice":str(self.entry)}]

    async def open_orders(self):
        return [dict(o) for o in self.orders.values() if o["status"] in ("NEW","PARTIALLY_FILLED")]

    async def query_order(self, client_id): return dict(self.orders[client_id])
    async def query_order_by_id(self, order_id):
        return dict(next(o for o in self.orders.values() if o["orderId"]==order_id))

    async def cancel_order(self, client_id):
        self.orders[client_id]["status"]="CANCELED"
        return dict(self.orders[client_id])

    async def place_limit(self, side, qty, price, client_id):
        assert (side=="BUY" and price<self.mark) or (side=="SELL" and price>self.mark)
        self.sequence+=1
        row={"symbol":"ETHUSDT","clientOrderId":client_id,"orderId":self.sequence,
             "side":side,"origQty":str(qty),"executedQty":"0","price":str(price),"status":"NEW"}
        self.orders[client_id]=row
        return dict(row)

    async def place_stop(self, side, trigger, client_id):
        row={"symbol":"ETHUSDT","clientAlgoId":client_id,"side":side,
             "triggerPrice":str(trigger),"algoStatus":"NEW","algoId":self.sequence+1,
             "closePosition":True,"workingType":"MARK_PRICE","orderType":"STOP_MARKET",
             "positionSide":"BOTH"}
        self.stops[client_id]=row
        return dict(row)

    async def open_stops(self):
        return [dict(s) for s in self.stops.values() if s["algoStatus"]=="NEW"]
    async def query_stop(self, client_id): return dict(self.stops[client_id])
    async def cancel_stop(self, client_id):
        self.stops[client_id]["algoStatus"]="CANCELED"
        return {"complete":True}

    def trade(self, row, qty, price, taker):
        delta = qty if row["side"]=="BUY" else -qty
        before = self.quantity
        if before*delta<0:
            self.realized += min(abs(before),qty)*(price-self.entry)*(1 if before>0 else -1)
        after=before+delta
        if before==0 or before*delta>0:
            self.entry=(abs(before)*self.entry+qty*price)/abs(after)
        elif after==0: self.entry=D(0)
        elif before*after<0: self.entry=price
        self.quantity=after
        self.fees+=self.fee(qty*price,taker)
        row["executedQty"]=row["origQty"];row["status"]="FILLED"
        tid=len(self.trades)+10000
        self.trades.append({"symbol":"ETHUSDT","orderId":row["orderId"],"id":tid,
                            "side":row["side"],"qty":str(qty),"price":str(price)})
        return {"e":"ORDER_TRADE_UPDATE","o":{
            "s":"ETHUSDT","c":row["clientOrderId"],"i":row["orderId"],"S":row["side"],
            "x":"TRADE","X":"FILLED","l":str(qty),"z":str(qty),"L":str(price),"t":tid}}

    async def close_position(self, side, qty, client_id):
        # Immediate taker close with two basis points of adverse slippage.
        price=self.mark*(1+self.slippage if side=="BUY" else 1-self.slippage)
        self.sequence+=1
        row={"symbol":"ETHUSDT","clientOrderId":client_id,"orderId":self.sequence,
             "side":side,"origQty":str(qty),"executedQty":"0","price":"0","status":"NEW"}
        self.orders[client_id]=row
        self.trade(row,qty,price,True)
        return dict(row)

    async def order_trades(self, oid, start, end):
        return [dict(t) for t in self.trades if t["orderId"]==oid]


async def scenario(data, path, force_exit=False):
    with tempfile.TemporaryDirectory() as temp:
        db=Path(temp)/"execution.sqlite3";migrate(db);store=DecisionStore(db)
        exchange=SimulatedExchange(data)
        with patch("time.time",lambda:exchange.clock/1000):
            payload={"symbol":"ETHUSDT","state":"OSCILLATION","executable":True,
                     "calculated_at_ms":exchange.clock,
                     "last_closed_kline_ms":{"15m":exchange.clock-60000,
                         "1h":exchange.clock-60000,"4h":exchange.clock-60000}}
            signal=store.publish(payload,"frozen-scenario")
            epoch=store.create_epoch(signal,levels=tuple(map(D,json.loads(data["epoch"]["levels_json"]))),
                stop_loss_low=D(data["epoch"]["stop_loss_low"]),
                stop_loss_high=D(data["epoch"]["stop_loss_high"]))
            store.acknowledge(signal)
            for i,r in enumerate(data["local_orders"]):
                store.prepare_order(epoch,r["level_index"],r["role"],r["side"],
                    D(r["price"]),D(r["quantity"]),r["client_order_id"])
                store.record_order(r["client_order_id"],r["exchange_order_id"],r["status"],r["executed_qty"])
                if D(r["executed_qty"])>0:
                    store.record_fill(r["client_order_id"],r["exchange_order_id"],str(i+1),
                                      D(r["executed_qty"]),D(r["price"]))
            for pair in data["pairs"]:
                store.conn.execute("""INSERT INTO pair(epoch_id,source_client_order_id,
                    reverse_client_order_id,quantity,status) VALUES(?,?,?,?,?)""",
                    (epoch,pair["source_client_order_id"],pair["reverse_client_order_id"],
                     pair["quantity"],pair["status"]))
            for stop in data["stops"]:
                store.conn.execute("INSERT INTO protection VALUES(?,?,?,?,?,?)",(
                    stop["clientAlgoId"],epoch,stop["side"],stop["triggerPrice"],"NEW",str(stop["algoId"])))
            store.set_engine_state("ACTIVE")
            store.record_equity_checkpoint(beijing_day(exchange.clock),D(300),exchange.equity(),D(300),
                                          exchange.clock,verified=True,transfer_fingerprint="none")
            settings=ExecutionSettings(mode="live",max_margin_usdt=D(240),max_notional_usdt=D(2400),
                max_daily_loss_usdt=D(15),max_drawdown_usdt=D(30),max_open_orders=13,expected_leverage=5)
            service=ExecutorService(settings,store,exchange,{"GRID_EXECUTION_HEARTBEAT":str(Path(temp)/"heartbeat")})
            service.stream.ready=True;service.filters=await exchange.verify_account(5)
            start_equity=exchange.equity();trace=[]
            for target in map(D,path):
                old=exchange.mark
                crossed=[o for o in await exchange.open_orders()
                         if (o["side"]=="SELL" and old<D(o["price"])<=target)
                         or (o["side"]=="BUY" and target<=D(o["price"])<old)]
                crossed.sort(key=lambda o:D(o["price"]),reverse=target<old)
                for saved in crossed:
                    row=exchange.orders[saved["clientOrderId"]]
                    if row["status"] not in ("NEW","PARTIALLY_FILLED"):continue
                    exchange.clock+=31000;exchange.mark=D(row["price"])
                    event=exchange.trade(row,D(row["origQty"])-D(row["executedQty"]),exchange.mark,False)
                    await service._on_event(event)
                    await service.tick()
                    trace.append({"mark":str(exchange.mark),"equity":str(exchange.equity()),
                                  "state":store.engine_state()["state"]})
                    if store.engine_state()["state"]!="ACTIVE":break
                if store.engine_state()["state"]!="ACTIVE":break
                exchange.mark=target;exchange.clock+=31000
                await service.tick()
                trace.append({"mark":str(target),"equity":str(exchange.equity()),
                              "state":store.engine_state()["state"]})
                if store.engine_state()["state"]!="ACTIVE":break
            if force_exit and store.engine_state()["state"] == "ACTIVE":
                assert await service.engine.exit_all(epoch)
            result={"realized_gross":str(exchange.realized),"commission":str(exchange.fees),
                "unrealized":str(exchange.quantity*(exchange.mark-exchange.entry)),
                "net_phase_pnl":str(exchange.equity()-300),
                "change_from_snapshot":str(exchange.equity()-start_equity),
                "position_qty":str(exchange.quantity),"last_mark":str(exchange.mark),
                "new_fills":len(exchange.trades),"remaining_orders":len(await exchange.open_orders()),
                "state":store.engine_state()["state"],"reason":store.engine_state()["lock_reason"],
                "trace":trace}
        store.close()
        return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument("input",type=Path);parser.add_argument("output",type=Path)
    args=parser.parse_args();data=json.loads(args.input.read_text());out=args.output;out.mkdir(parents=True,exist_ok=True)
    levels=list(map(D,json.loads(data["epoch"]["levels_json"])))
    maker=D(data["fees"]["makerCommissionRate"]);taker=D(data["fees"]["takerCommissionRate"])
    rows=[]
    for o in sorted(data["orders"],key=lambda r:D(r["price"])):
        price=D(o["price"]);q=D(o["origQty"])-D(o["executedQty"]);index=levels.index(price)
        target=levels[index+(1 if o["side"]=="BUY" else -1)]
        gross=abs(target-price)*q
        already_open = any(p["reverse_client_order_id"]==o["clientOrderId"] for p in data["pairs"])
        opening_price, closing_price = (target,price) if already_open else (price,target)
        rows.append({"side":o["side"],"price":str(price),"qty":str(q),"paired_price":str(target),
            "gross":str(gross),"maker_maker_fee":str((price+target)*q*maker),
            "maker_maker_net":str(gross-(price+target)*q*maker),
            "maker_taker_net":str(gross-opening_price*q*maker-closing_price*q*taker),
            "order_id":str(o["orderId"])})
    with (out/"per-order.csv").open("w") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    paths={"existing_short_take_profit":["1805.99"],
           "ten_range_round_trips":["1805.99"]+["1829.44","1805.99"]*9,
           "rise_through_grid":["1852.90","1876.36","1899.82","1923.27","1946.73","1993.66"],
           "fall_through_grid":["1805.99","1782.53","1759.07","1735.61","1712.15","1688.70","1665.24","1618.32"],
           "force_exit_near_entry":["1829.44"],
           "force_exit_at_snapshot":[data["positions"][0]["markPrice"]]}
    # Paths below are tied to the captured levels; reject other grids explicitly.
    assert levels[6:9]==list(map(D,["1805.99","1829.44","1852.90"]))
    results={name:asyncio.run(scenario(data,path,name.startswith("force_exit")))
             for name,path in paths.items()}
    expected=D(rows[6]["maker_maker_net"])
    assert abs(D(results["existing_short_take_profit"]["net_phase_pnl"])-expected)<D(".000001")
    assert abs(D(results["ten_range_round_trips"]["net_phase_pnl"])-10*expected)<D(".000001")
    for name in ("existing_short_take_profit","ten_range_round_trips"):
        assert results[name]["state"]=="ACTIVE"
    for name in ("rise_through_grid","fall_through_grid"):
        assert results[name]["reason"]=="risk: daily_pct"
        assert D(results[name]["position_qty"])==0 and results[name]["remaining_orders"]==0
    assert all(D(row["maker_maker_net"])>0 for row in rows)
    (out/"scenarios.json").write_text(json.dumps(results,indent=2))
    print(json.dumps({"orders":rows,"scenarios":{k:{a:b for a,b in v.items() if a!="trace"} for k,v in results.items()}},indent=2))

if __name__=="__main__":main()
