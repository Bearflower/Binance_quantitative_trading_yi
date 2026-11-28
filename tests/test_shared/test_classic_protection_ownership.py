"""用四个策略的实际撤单入口核对账户、交易对、创建回执及成交证据。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.binance_api import BinanceClient
from shared.condition_orders import confirm_classic_algo_fill
from shared.trade_logger import TradeLogger
from strategies.btc_eth.strategy import BTCEthStrategy
from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as Aggressive
from strategies.hrs.position_manager import PositionManager
from strategies.new_coin.executor import TradingExecutor


def order(symbol, algo_id):
    return dict(symbol=symbol, algoId=algo_id, createTime=2000, orderType="STOP", side="SELL",
                algoStatus="NEW", reduceOnly=True)


def receipt(strategy, source):
    return dict(strategy=strategy, symbol=source["symbol"], algo_id=str(source["algoId"]),
                side=source["side"], created_at=2000, order_type="STOP_LOSS", tp_level=None)


class Exchange:
    def __init__(self, orders, ledger):
        self.orders = {row["algoId"]: row for row in orders}
        self.ledger = ledger
        self.cancelled = []

    async def request(self, method, path, params, **kwargs):
        await asyncio.sleep(0)
        if method == "GET" and path in ("/fapi/v1/openAlgoOrders", "/papi/v1/um/algo/openAlgoOrders"):
            return [row for row in self.orders.values() if row["symbol"] == params["symbol"]]
        if path in ("/fapi/v1/algoOrder", "/papi/v1/um/algo/algoOrder") and method == "GET":
            return self.orders[params["algoId"]]
        if path in ("/fapi/v1/algoOrder", "/papi/v1/um/algo/order") and method == "DELETE":
            self.cancelled.append(self.orders.pop(params["algoId"])["algoId"])
            assert set(params) == {"algoId"}
            if path.startswith("/papi/"):
                return {"complete": True}
            return {"code": "200", "algoId": params["algoId"]}
        raise AssertionError((method, path, params))

    def client(self, strategy, pm=False):
        expected_pm = pm
        async def rows(sql, account, ids, cutoff, pm):
            assert "tr.account_id = $1" in sql and "tr.order_type = 'CONDITIONAL_ORDER'" in sql
            assert "co.symbol = tr.symbol" in sql and "tr.executed_at < timezone" in sql
            assert account == "main" and cutoff == 1000 and pm == expected_pm
            return [r for r in self.ledger if r["algo_id"] in ids]

        db = MagicMock()
        db.fetch_all = AsyncMock(side_effect=rows)
        db.execute = AsyncMock(return_value="UPDATE 1")
        api = BinanceClient("local-key", "local-secret", use_unified_account=pm)
        api.trade_logger = TradeLogger(db, strategy, account_id="main")
        api._request = self.request
        return api


@pytest.fixture(autouse=True)
def cutover(monkeypatch):
    monkeypatch.setenv("BINANCE_ACCOUNT_MODE", "classic")
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "1000")


@pytest.mark.asyncio
async def test_parallel_strategy_cleanup_leaves_other_strategy_and_other_symbol_orders():
    owners = ["btc_eth", "btc_eth_aggressive", "hrs", "new_coin", "grid", "btc_eth"]
    symbols = ["SOLUSDT", "SOLUSDT", "ETHUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
    orders = [order(symbol, idx) for idx, symbol in enumerate(symbols, 1)]
    exchange = Exchange(orders, [receipt(s, r) for s, r in zip(owners, orders)])
    first = SimpleNamespace(positions={}, binance=exchange.client("btc_eth"))
    second = SimpleNamespace(positions={}, binance=exchange.client("btc_eth_aggressive"))
    hrs = object.__new__(PositionManager)
    hrs.binance_api = exchange.client("hrs")
    hrs._positions = {"ETHUSDT": {"algo_ids": {"sl": 3}}}
    new = object.__new__(TradingExecutor)
    new.binance_api = exchange.client("new_coin")
    new.position_tracking = {"ETHUSDT": {"algo_ids": {"sl": 4}}}
    results = await asyncio.gather(
        BTCEthStrategy._cancel_symbol_conditional_orders(first, "SOLUSDT"),
        Aggressive._cancel_symbol_conditional_orders(second, "SOLUSDT"),
        hrs.cancel_all_orders("ETHUSDT"), new.cancel_all_algo_orders("ETHUSDT"),
    )
    assert results[:2] == [True, True]
    assert [r["algo_ids"] for r in results[2:]] == [[3], [4]]
    assert sorted(exchange.cancelled) == [1, 2, 3, 4]
    assert sorted(exchange.orders) == [5, 6]
    for api in (first.binance, second.binance, hrs.binance_api, new.binance_api):
        sql, account, strategy, symbol, algo_id, cutoff, pm, status = api.trade_logger.db.execute.await_args.args
        assert "account_id=$1 AND strategy_name=$2 AND symbol=$3 AND algo_id=$4" in sql
        assert (account, strategy, cutoff) == ("main", owners[algo_id - 1], 1000)
        assert symbol == symbols[algo_id - 1] and not pm and status == "CANCELED"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["unknown", "conflicting", "old"])
async def test_inconsistent_evidence_blocks_before_any_cancellation(reason):
    mine, other = order("SOLUSDT", 1), order("SOLUSDT", 2)
    ledger = [receipt("hrs", mine)]
    if reason == "conflicting":
        ledger += [receipt("hrs", other), receipt("btc_eth", other)]
    if reason == "old":
        other["createTime"] = 999
        ledger += [receipt("hrs", other)]
    exchange = Exchange([mine, other], ledger)
    hrs = object.__new__(PositionManager)
    hrs.binance_api = exchange.client("hrs")
    hrs._positions = {"SOLUSDT": {"algo_ids": {"sl": 1}}}
    result = await hrs.cancel_all_orders("SOLUSDT")
    assert result["failed"] and not exchange.cancelled
    assert hrs._positions["SOLUSDT"]["algo_ids"] == {"sl": 1}


@pytest.mark.asyncio
async def test_terminal_tp_uses_actual_order_and_unique_fills_before_updating_role():
    algo = {**order("ETHUSDT", 1), "orderType": "TAKE_PROFIT", "algoStatus": "FINISHED", "actualOrderId": "44"}
    record = {**receipt("new_coin", algo), "order_type": "TAKE_PROFIT", "tp_level": 1}
    api = Exchange([algo], [record]).client("new_coin")
    api.get_order = AsyncMock(return_value={
        "symbol": "ETHUSDT", "side": "SELL", "orderId": 44, "status": "FILLED", "origQty": "2", "executedQty": "2",
    })
    fill = {"id": 9, "symbol": "ETHUSDT", "side": "SELL", "orderId": 44, "qty": "2", "price": "7"}
    api.get_user_trades = AsyncMock(return_value=[fill, fill])
    with pytest.raises(ValueError, match="duplicate"):
        await confirm_classic_algo_fill(api, "ETHUSDT", 1, role="TAKE_PROFIT", tp_level=1)
    api.trade_logger.db.execute.assert_not_awaited()
    api.get_user_trades.return_value = [fill]
    assert await confirm_classic_algo_fill(api, "ETHUSDT", 1, role="TAKE_PROFIT", tp_level=1)
    api.get_order.assert_awaited_with("ETHUSDT", 44)
    api.get_user_trades.assert_awaited_with("ETHUSDT", order_id="44", limit=1000)
    assert api.trade_logger.db.execute.await_args.args[1:] == ("main", "new_coin", "ETHUSDT", 1, 1000, False, "EXECUTED")


@pytest.mark.asyncio
async def test_algo_ack_with_child_id_is_not_an_ordinary_fill():
    db = AsyncMock()
    logger = TradeLogger(db, "hrs", account_id="main")
    assert await logger.log_order(
        {"algoId": 1, "orderId": 54, "actualOrderId": 54, "algoStatus": "FINISHED",
         "executedQty": "7", "avgPrice": "9"}, "ETHUSDT", "SELL", "STOP")
    sql, *values = db.execute.await_args.args
    assert "NULL, $3" in sql and "CONDITIONAL_ORDER" in values
    assert 54 not in values and "54" not in values
    db.execute.reset_mock()
    assert not await logger.log_order({"orderId": 54}, "ETHUSDT", "SELL", "STOP")
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_classic_tp_replenish_restores_only_missing_level():
    tp2 = {**order("ETHUSDT", 2), "orderType": "TAKE_PROFIT"}
    stop = order("ETHUSDT", 1)
    ledger = [receipt("new_coin", stop),
              {**receipt("new_coin", tp2), "order_type": "TAKE_PROFIT", "tp_level": 2}]
    new = object.__new__(TradingExecutor)
    new.binance_api = Exchange([stop, tp2], ledger).client("new_coin")
    new.position_tracking = {"ETHUSDT": {"algo_ids": {"tp2": 2}}}
    new.batch_take_profit_enabled = new.replenish_require_take_profit = True
    missing = await new.find_missing_protection("ETHUSDT", strict=True, target1_reached=False,
                                                target2_reached=False)
    assert missing == ["TP1"]
    new._ensure_replenish_tracking = MagicMock()
    new._backfill_algo_ids_from_db = AsyncMock()
    new._place_protection_orders = AsyncMock(return_value=True)
    new._apply_take_profit = AsyncMock(return_value=True)
    new._finalize_replenish = MagicMock()
    assert await new._execute_incremental_plans("ETHUSDT", {"tp1": {"price": 12}}, missing)
    new._apply_take_profit.assert_awaited_once_with("ETHUSDT", {"price": 12}, 1)
    assert not any(new._place_protection_orders.await_args.kwargs.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("pm_account", [
    {"accountEquity": "1"}, {"totalAvailableBalance": "0"},
    {"totalAvailableBalance": "0", "accountEquity": "NaN"},
    {"totalAvailableBalance": "Infinity", "accountEquity": "1"},
])
async def test_pm_equity_capture_requires_complete_finite_values(pm_account):
    client = BinanceClient("test", "test", use_unified_account=True)
    client._request = AsyncMock(return_value=pm_account)
    with pytest.raises((KeyError, ValueError)):
        await client.get_account_info()


@pytest.mark.asyncio
async def test_missing_position_row_cannot_retire_newcoin_position():
    from strategies.new_coin.strategy import NewCoinStrategy

    api = BinanceClient("test", "test", use_unified_account=False)
    api._request = AsyncMock(return_value=[])
    new = object.__new__(TradingExecutor)
    new.binance_api = api
    assert await new._get_exchange_position_qty("ETHUSDT") is None
    with pytest.raises(ValueError):
        await new._resolve_short_quantity("ETHUSDT")
    strategy = object.__new__(NewCoinStrategy)
    strategy.binance_client = api
    strategy.positions = {"ETHUSDT": {}}
    strategy._handle_position_closed = AsyncMock()
    assert await strategy._reconfirm_symbol_absent("ETHUSDT") is None
    assert not await strategy._close_stale_memory_positions(set())
    strategy._handle_position_closed.assert_not_awaited()


def test_pending_protection_adopts_owned_slots_and_refuses_unresolved_ids():
    from shared.protection_retry import reconcile_classic_protection_ids

    position = SimpleNamespace(stop_loss_order_id=None, trailing_stop_order_id=None,
                               tp1_order_id=2, tp2_order_id=None)
    live = [dict(order_type="STOP_LOSS", algo_id=1), dict(order_type="TAKE_PROFIT", tp_level=1, algo_id=2)]
    assert reconcile_classic_protection_ids(position, live)
    assert position.stop_loss_order_id == 1 and position.tp2_order_id is None
    assert not reconcile_classic_protection_ids(position, live[:1])
    assert not reconcile_classic_protection_ids(position, live + [dict(order_type="TAKE_PROFIT", tp_level=1, algo_id=4)])
    assert position.tp1_order_id == 2

@pytest.mark.asyncio
async def test_pm_recovery_cancels_only_same_strategy_before_cutover(monkeypatch):
    monkeypatch.setenv("BINANCE_ACCOUNT_MODE", "pm")
    first, second = [dict(order("ETHUSDT", n), createTime=999) for n in (1, 2)]
    exchange = Exchange([first, second], [receipt("hrs", first), receipt("btc_eth", second)])
    api = exchange.client("hrs", pm=True)
    with pytest.raises(ValueError, match="其他策略"):
        await api.cancel_algo_order("ETHUSDT", 2)
    assert not exchange.cancelled
    result = await api.cancel_all_algo_orders("ETHUSDT")
    assert result["algo_ids"] == [1] and set(exchange.orders) == {2}
    assert api.trade_logger.db.execute.await_args.args[1:] == ("main", "hrs", "ETHUSDT", 1, 1000, True, "CANCELED")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["mine", "foreign", "old", "partial", "wrong_symbol", "duplicate"])
async def test_orphan_regular_order_requires_creator_and_zero_terminal_fill(case):
    from ai_tuner.cleanup.orphan_cleanup import OrphanCleanupJob
    symbol = "ETHUSDT"
    exchange, db = AsyncMock(), AsyncMock()
    exchange.use_unified_account = False
    exchange.get_position.return_value = [{"symbol": symbol, "positionAmt": "0"}]
    exchange.get_um_account_positions.return_value = []
    opened = dict(symbol=symbol, orderId=7, time=999 if case == "old" else 1000, side="BUY", status="NEW")
    terminal = {**opened, "status": "CANCELED", "executedQty": "0.1" if case == "partial" else "0"}
    if case == "wrong_symbol":
        terminal["symbol"] = "BTCUSDT"
    exchange.get_order.side_effect = [opened, terminal]
    creator = dict(strategy="btc_eth" if case == "foreign" else "hrs", side="BUY")
    db.fetch_all.return_value = [creator, creator] if case == "duplicate" else [creator]
    db.execute.return_value = "UPDATE 1"
    job = OrphanCleanupJob(db, exchange, None)
    row = dict(id=1, symbol=symbol, strategy_name="hrs", account_id="main", order_id=7)
    result, _ = await job._cancel_order(row)
    assert result == (case == "mine")
    assert exchange.cancel_order.await_count == int(case in {"mine", "partial", "wrong_symbol"})
    assert db.execute.await_count == int(case == "mine")
    if result:
        assert db.execute.await_args.args[1:] == ("CANCELED", 1, "main", symbol, "hrs")
