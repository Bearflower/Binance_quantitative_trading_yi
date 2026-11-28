"""经典 USDⓈ-M 与 PM 的交易协议离线回归。"""
import asyncio
import json
import sqlite3
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from shared.binance_api import (
    BinanceAPIError, BinanceClient, LocalOrderValidationError, PreSubmissionError,
    UnknownOrderResultError, symbol_is_confirmed_flat,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def client():
    api = BinanceClient("local-key", "local-secret", use_unified_account=False)
    api.get_symbol_info = AsyncMock(return_value={"stepSize": "0.1", "tickSize": Decimal("0.1")})
    api.get_ticker = AsyncMock(return_value={"lastPrice": "100"})
    return api


@pytest.mark.parametrize("strategy", ["btc_eth", "btc_eth_aggressive", "hrs", "new_coin"])
async def test_uncertain_strategy_algo_blocks_fresh_client_id_after_restart(client, tmp_path, monkeypatch, strategy):
    path = tmp_path / "algo.sqlite3"
    monkeypatch.setenv("CLASSIC_ALGO_INTENT_DB", str(path))
    owner = MagicMock(strategy_name=strategy, account_id="main", log_order=AsyncMock(return_value=True))
    client.set_trade_logger(owner)
    client._request = AsyncMock(side_effect=UnknownOrderResultError(
        "POST", "/fapi/v1/algoOrder", symbol="ETHUSDT", client_order_id="original"))
    with pytest.raises(UnknownOrderResultError):
        await client.place_conditional_order("ETHUSDT", "SELL", Decimal("95"), Decimal("1"),
                                             client_order_id="original")
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT client_id, request FROM pending").fetchone()
        assert row[0] == json.loads(row[1])["clientAlgoId"] == "original"

    restarted = BinanceClient("key", "secret", use_unified_account=False)
    restarted.set_trade_logger(owner)
    restarted.get_symbol_info, restarted.get_ticker = client.get_symbol_info, client.get_ticker
    restarted._request = AsyncMock(return_value=[])
    assert await restarted.get_open_algo_orders("ETHUSDT") == []
    # Even a strategy fallback from STOP_MARKET to STOP with a new price/ID is blocked.
    with pytest.raises(UnknownOrderResultError) as unknown:
        await restarted.place_conditional_order("ETHUSDT", "SELL", Decimal("94"), Decimal("1"), Decimal("93"),
                                                 order_type="STOP", client_order_id="fresh")
    assert unknown.value.client_order_id == "original"
    assert [c.args[0] for c in restarted._request.await_args_list] == ["GET"]
    assert client._request.await_count == 1


@pytest.mark.parametrize("logged, status, pending", [(True, "NEW", 0), (False, "NEW", 1), (True, "FINISHED", 1)])
async def test_strategy_algo_needs_positive_owned_receipt_to_release(client, tmp_path, monkeypatch, logged, status, pending):
    path = tmp_path / "algo.sqlite3"
    monkeypatch.setenv("CLASSIC_ALGO_INTENT_DB", str(path))
    client.set_trade_logger(MagicMock(strategy_name="new_coin", account_id="main",
                                      log_order=AsyncMock(return_value=logged)))
    ack = {"algoId": 123, "clientAlgoId": "entry-sl", "symbol": "ETHUSDT", "side": "SELL",
           "orderType": "STOP_MARKET", "algoStatus": status}
    client._request = AsyncMock(return_value=ack)
    submission = client.place_conditional_order("ETHUSDT", "SELL", Decimal("95"), Decimal("1"),
                                                client_order_id="entry-sl")
    if pending:
        with pytest.raises(UnknownOrderResultError):
            await submission
    else:
        assert await submission == ack
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM pending").fetchone() == (pending,)


async def test_classic_guard_write_failure_never_posts_and_definite_rejection_releases(client, tmp_path, monkeypatch):
    path = tmp_path / "algo.sqlite3"
    monkeypatch.setenv("CLASSIC_ALGO_INTENT_DB", str(tmp_path / "missing" / "algo.sqlite3"))
    client.set_trade_logger(MagicMock(strategy_name="hrs", account_id="main"))
    client._request = AsyncMock(side_effect=BinanceAPIError(-2021, "Order would immediately trigger."))
    async def submit():
        await client.place_conditional_order("ETHUSDT", "SELL", Decimal("95"), Decimal("1"))
    with pytest.raises(PreSubmissionError):
        await submit()
    client._request.assert_not_called()
    monkeypatch.setenv("CLASSIC_ALGO_INTENT_DB", str(path))
    with pytest.raises(BinanceAPIError):
        await submit()
    assert client._request.await_count == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM pending").fetchone() == (0,)


async def test_classic_account_and_exposure_routes(client):
    row = {"symbol": "ETHUSDT", "positionAmt": "-0.5", "leverage": "2"}
    account = {"assets": [], "positions": [row], "totalMarginBalance": "102",
               "totalWalletBalance": "100", "totalUnrealizedProfit": "2", "availableBalance": "70"}
    client._request = AsyncMock(side_effect=[
        account, [{"asset": "USDT", "balance": "100", "availableBalance": "70"}], [row], account,
    ])
    assert (await client.get_account_info())["totalMarginBalance"] == 102
    assert await client.get_account_balance() == {"USDT": 70}
    assert await client.get_position("ethusdt") == [row]
    assert await client.get_um_account_positions() == [row]
    assert client._request.await_args_list == [
        call("GET", "/fapi/v3/account", signed=True), call("GET", "/fapi/v3/balance", signed=True),
        call("GET", "/fapi/v2/positionRisk", {"symbol": "ETHUSDT"}), call("GET", "/fapi/v3/account", {}),
    ]


async def test_classic_algo_lifecycle_and_actual_fill(client):
    algo = {"symbol": "ETHUSDT", "algoId": 901, "clientAlgoId": "sl1", "algoStatus": "NEW"}
    triggered = {**algo, "algoStatus": "FINISHED", "actualOrderId": "51"}
    ordinary = {"symbol": "ETHUSDT", "orderId": 51, "status": "FILLED", "executedQty": "0.5"}
    fill = {"id": 8, "orderId": 51, "qty": "0.5"}
    client._request = AsyncMock(side_effect=[
        algo, [algo], triggered, ordinary, [fill], algo, {"code": "200"}, {"code": 200},
    ])
    result = await client.place_conditional_order(
        "ETHUSDT", "SELL", Decimal("95"), Decimal("0.5"), Decimal("94"),
        order_type="STOP", reduce_only=True, clientAlgoId="sl1",
    )
    assert result == algo
    assert await client.get_open_algo_orders("ethusdt") == [algo]
    result = await client.get_algo_order(client_algo_id="sl1")
    assert await client.get_order("ETHUSDT", int(result["actualOrderId"])) == ordinary
    assert await client.get_user_trades("ETHUSDT", order_id="51") == [fill]
    assert await client.cancel_algo_order("ETHUSDT", 901) == {"code": "200"}
    assert await client.cancel_all_algo_orders("ETHUSDT") == {"code": 200}
    assert client._request.await_args_list == [
        call("POST", "/fapi/v1/algoOrder", {
            "symbol": "ETHUSDT", "side": "SELL", "type": "STOP", "workingType": "CONTRACT_PRICE",
            "algoType": "CONDITIONAL", "clientAlgoId": "sl1", "triggerPrice": "95.0",
            "quantity": "0.5", "price": "94.0", "timeInForce": "GTC", "reduceOnly": "true",
        }, idempotency_key="sl1"),
        call("GET", "/fapi/v1/openAlgoOrders", {"algoType": "CONDITIONAL", "symbol": "ETHUSDT"}, signed=True),
        call("GET", "/fapi/v1/algoOrder", {"clientAlgoId": "sl1"}, signed=True),
        call("GET", "/fapi/v1/order", {"symbol": "ETHUSDT", "orderId": 51}, signed=True),
        call("GET", "/fapi/v1/userTrades", {"symbol": "ETHUSDT", "orderId": "51"}, signed=True),
        call("GET", "/fapi/v1/algoOrder", {"algoId": 901}, signed=True),
        call("DELETE", "/fapi/v1/algoOrder", {"algoId": 901}),
        call("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": "ETHUSDT"}),
    ]


@pytest.mark.parametrize("order_type,extra", [
    ("STOP", {"price": Decimal("94"), "closePosition": True}),
    ("STOP_MARKET", {"closePosition": True, "quantity": Decimal("1")}),
    ("TAKE_PROFIT_MARKET", {"closePosition": True, "reduceOnly": True}),
    ("STOP_MARKET", {"positionSide": "LONG", "quantity": Decimal("1"), "reduce_only": True}),
    ("TRAILING_STOP_MARKET", {"quantity": Decimal("1")}),
])
async def test_illegal_classic_algo_never_submitted(client, order_type, extra):
    client._request = AsyncMock()
    with pytest.raises(LocalOrderValidationError):
        await client.place_conditional_order("ETHUSDT", "SELL", Decimal("93"), order_type=order_type, **extra)
    client._request.assert_not_called()


async def test_classic_close_all_trailing_and_regular_reduce_only(client):
    client._request = AsyncMock(return_value={})
    await client.place_order("ETHUSDT", "SELL", order_type="STOP_MARKET",
                             stopPrice="95", closePosition=True, client_order_id="close")
    await client.place_conditional_order("ETHUSDT", "SELL", quantity=Decimal("1"),
                                         order_type="TRAILING_STOP_MARKET", callbackRate="1")
    await client.place_order("ETHUSDT", "SELL", quantity=Decimal("1"), reduce_only=True)
    stop, trailing, regular = client._request.await_args_list
    assert stop.args[1] == trailing.args[1] == "/fapi/v1/algoOrder"
    assert stop.args[2]["clientAlgoId"] == "close" and stop.args[2]["closePosition"] == "true"
    assert "quantity" not in stop.args[2] and "reduceOnly" not in stop.args[2]
    assert trailing.args[2]["callbackRate"] == "1"
    assert "triggerPrice" not in trailing.args[2] and "activatePrice" not in trailing.args[2]
    assert regular.args[1] == "/fapi/v1/order" and regular.args[2]["reduceOnly"] == "true"
    assert "reduce_only" not in regular.args[2]


@pytest.mark.parametrize("pm", [True, False])
@pytest.mark.parametrize("response", [asyncio.TimeoutError(), BinanceAPIError(-1007, "TIMEOUT"), {}])
async def test_unknown_algo_uses_original_algo_id_without_reposting(client, pm, response):
    client.use_unified_account = pm
    found = {"symbol": "ETHUSDT", "clientAlgoId": "saved", "algoId": 901}
    client._request_once = AsyncMock(side_effect=[response, found])
    result = await client.place_conditional_order("ETHUSDT", "SELL", Decimal("95"), Decimal("1"),
                                                  clientAlgoId="saved")
    assert result == found
    post, lookup = client._request_once.await_args_list
    assert post.args[:2] == ("POST", "/papi/v1/um/algo/order" if pm else "/fapi/v1/algoOrder")
    assert lookup.args == ("GET", "/papi/v1/um/algo/algoOrder" if pm else "/fapi/v1/algoOrder",
                           {"clientAlgoId": "saved"}, True)


async def test_failed_algo_verification_keeps_unknown_id(client):
    client._request_once = AsyncMock(side_effect=[asyncio.TimeoutError(), {"clientAlgoId": "wrong"}])
    with pytest.raises(UnknownOrderResultError) as exc:
        await client.place_conditional_order("ETHUSDT", "SELL", Decimal("95"), Decimal("1"),
                                             client_order_id="saved")
    assert exc.value.client_order_id == "saved"
    assert [c.args[0] for c in client._request_once.await_args_list] == ["POST", "GET"]


@pytest.mark.parametrize("positions,all_rows,account_rows,orders,algos,expected", [
    (["0"], ["0"], [], [], [], True),
    ([], [], [], [], [], False), (["0"], [], [], [], [], False),
    (["1"], ["0"], [], [], [], False), (["0"], ["1"], [], [], [], False),
    (["0"], ["0"], ["-1"], [], [], False),
    (["0"], ["0"], [], [{"orderId": 1}], [], False),
    (["0"], ["0"], [], [], [{"algoId": 1}], False),
])
async def test_classic_flat_requires_agreeing_position_and_order_inventories(
        positions, all_rows, account_rows, orders, algos, expected):
    def rows(amounts):
        return [dict(symbol="ETHUSDT", positionAmt=amount) for amount in amounts]
    exchange = MagicMock(use_unified_account=False)
    exchange.get_position = AsyncMock(side_effect=[rows(positions), rows(all_rows)])
    exchange.get_um_account_positions = AsyncMock(return_value=rows(account_rows))
    exchange.get_open_orders = AsyncMock(return_value=orders)
    exchange.get_open_algo_orders = AsyncMock(return_value=algos)
    assert await symbol_is_confirmed_flat(exchange, "ETHUSDT") is expected


@pytest.mark.parametrize("response", [[], {}, [{"symbol": "OTHER", "positionAmt": "0"}],
                                        [{"symbol": "ETHUSDT", "positionAmt": "NaN"}]])
async def test_classic_scoped_missing_or_invalid_position_is_unavailable(client, response):
    client._request = AsyncMock(return_value=response)
    with pytest.raises(ValueError):
        await client.get_position("ETHUSDT")


async def test_classic_history_routes_and_bad_pages(client):
    client._request = AsyncMock(side_effect=[[{"orderId": 8}], {}, {}])
    assert await client.get_order_history("ethusdt", 1, 2, 3) == [{"orderId": 8}]
    with pytest.raises(TypeError):
        await client.get_income_history(1, 2, income_type="FUNDING_FEE")
    with pytest.raises(TypeError):
        await client.get_user_trades("ethusdt")
    assert client._request.await_args_list[0] == call(
        "GET", "/fapi/v1/allOrders", {"symbol": "ETHUSDT", "limit": 3, "startTime": 1, "endTime": 2}, signed=True,
    )


async def test_classic_cancel_does_not_cancel_another_symbol(client):
    client._request = AsyncMock(return_value={"symbol": "BTCUSDT", "algoId": 99})
    with pytest.raises(ValueError, match="交易对"):
        await client.cancel_algo_order("ETHUSDT", 99)
    client._request.assert_awaited_once_with("GET", "/fapi/v1/algoOrder", {"algoId": 99}, signed=True)


async def test_http_503_is_verified_and_string_cancel_code_is_accepted(client):
    found = {"symbol": "ETHUSDT", "clientAlgoId": "stop", "algoId": 712}
    responses = [
        (503, None), (200, found), (200, found), (200, {"code": "200", "algoId": 712}),
    ]
    session = MagicMock(closed=False)

    def request(method, url, params):
        status, data = responses.pop(0)
        response = MagicMock(status=status)
        response.json = AsyncMock(return_value=data)
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=response)
        manager.__aexit__ = AsyncMock(return_value=None)
        return manager

    session.request.side_effect = request
    client.session = session
    assert await client.place_conditional_order("ETHUSDT", "SELL", Decimal("95"), Decimal("1"),
                                                clientAlgoId="stop") == found
    assert await client.cancel_algo_order("ETHUSDT", 712) == {"code": "200", "algoId": 712}
    calls = session.request.call_args_list
    assert [c.args[0] for c in calls] == ["POST", "GET", "GET", "DELETE"]
    assert all(c.args[1] == "https://fapi.binance.com/fapi/v1/algoOrder" for c in calls)
    assert all("signature" in c.kwargs["params"] for c in calls)
