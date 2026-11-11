from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from strategies.grid.execution.exchange import (
    AccountModeError, GridExchange, UnknownOrderResultError,
)


class FakeClient:
    use_unified_account = True

    def __init__(self):
        self.calls = []
        self.responses = {}

    async def _request(self, method, path, params=None, signed=True):
        self.calls.append((method, path, params))
        response = self.responses.get((method, path), {})
        return response

    async def _request_once(self, method, path, params=None, signed=True):
        return await self._request(method, path, params, signed)


@pytest.mark.asyncio
async def test_order_and_protection_use_current_endpoints():
    client = FakeClient()
    client.responses[("GET", "/papi/v1/um/algo/openAlgoOrders")] = []
    client.responses[("DELETE", "/papi/v1/um/algo/order")] = {"complete": True}
    exchange = GridExchange(client)
    await exchange.place_limit("BUY", Decimal("0.02"), Decimal("1900"), "eg-1")
    await exchange.close_position("SELL", Decimal("0.02"), "eg-close")
    await exchange.place_stop("SELL", Decimal("1850"), "eg-stop")
    await exchange.open_stops()
    await exchange.query_stop("eg-stop")
    await exchange.cancel_stop("eg-stop")
    paths = [(method, path) for method, path, _ in client.calls]
    assert paths[:3] == [
        ("POST", "/papi/v1/um/order"), ("POST", "/papi/v1/um/order"),
        ("POST", "/papi/v1/um/algo/order")]
    assert ("GET", "/papi/v1/um/algo/openAlgoOrders") in paths
    assert ("GET", "/papi/v1/um/algo/algoOrder") in paths
    assert ("DELETE", "/papi/v1/um/algo/order") in paths
    assert client.calls[1][2]["reduceOnly"] == "true"
    assert client.calls[1][2]["newOrderRespType"] == "RESULT"
    assert client.calls[2][2]["closePosition"] == "true"
    assert client.calls[2][2]["algoType"] == "CONDITIONAL"


@pytest.mark.asyncio
async def test_pm_algo_cancel_requires_complete_ack():
    client = FakeClient()
    client.responses[("DELETE", "/papi/v1/um/algo/order")] = {"complete": False}
    with pytest.raises(ValueError, match="unconfirmed"):
        await GridExchange(client).cancel_stop("eg-stop")


@pytest.mark.asyncio
async def test_account_mode_mismatch_refuses_openings():
    client = FakeClient()
    client.responses[("GET", "/papi/v1/um/positionSide/dual")] = {"dualSidePosition": True}
    with pytest.raises(AccountModeError):
        await GridExchange(client).verify_account(10)


@pytest.mark.asyncio
async def test_timeout_does_not_reissue_write():
    client = FakeClient()
    calls = []

    async def uncertain(method, path, params=None, signed=True):
        calls.append((method, path))
        raise TimeoutError("reply lost")

    client._request_once = uncertain
    client._request = AsyncMock(return_value={"code": -2013})
    with pytest.raises(UnknownOrderResultError):
        await GridExchange(client).place_limit("BUY", Decimal("0.02"),
                                               Decimal("1900"), "eg-1")
    assert calls == [("POST", "/papi/v1/um/order")]


@pytest.mark.asyncio
async def test_income_uses_pm_endpoint():
    client = FakeClient()
    client.responses[("GET", "/papi/v1/um/income")] = []
    rows = await GridExchange(client).income(1000, 2000)
    assert rows == []
    assert client.calls[-1] == ("GET", "/papi/v1/um/income",
                                {"startTime": 1000, "endTime": 2000,
                                 "limit": 1000})


@pytest.mark.asyncio
async def test_user_stream_listen_key_uses_pm_rest():
    client = FakeClient()
    client.responses[("POST", "/papi/v1/listenKey")] = {"listenKey": "abc"}
    client.responses[("PUT", "/papi/v1/listenKey")] = {}
    exchange = GridExchange(client)
    assert await exchange.start_user_stream() == "abc"
    assert await exchange.keepalive_user_stream() == "abc"
    assert client.calls[-2:] == [("POST", "/papi/v1/listenKey", None),
                                  ("PUT", "/papi/v1/listenKey", None)]


@pytest.mark.asyncio
async def test_order_trade_backfill_is_exact_and_rejects_full_page():
    client = FakeClient()
    client.responses[("GET", "/papi/v1/um/userTrades")] = [
        {"orderId": 7, "id": 10}, {"orderId": 8, "id": 11}]
    assert await GridExchange(client).order_trades(7, 1000, 2000) == [
        {"orderId": 7, "id": 10}]
    assert client.calls[-1] == ("GET", "/papi/v1/um/userTrades",
                                {"symbol": "ETHUSDT", "startTime": 1000,
                                 "endTime": 2000, "limit": 1000})
    client.responses[("GET", "/papi/v1/um/userTrades")] = [{}] * 1000
    with pytest.raises(ValueError, match="incomplete"):
        await GridExchange(client).order_trades(7, 1000, 1000)


def test_rejects_non_pm_client():
    client = FakeClient()
    client.use_unified_account = False
    with pytest.raises(AccountModeError, match="PM"):
        GridExchange(client)


@pytest.mark.asyncio
async def test_pm_flat_position_is_normalized_only_after_success():
    client = FakeClient()
    client.responses[("GET", "/papi/v1/um/positionSide/dual")] = {
        "dualSidePosition": False}
    client.responses[("GET", "/papi/v1/um/positionRisk")] = []
    assert await GridExchange(client).position() == [
        {"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "0"}]
    client.responses[("GET", "/papi/v1/um/positionRisk")] = [
        {"symbol": "ETHUSDT", "positionSide": "LONG", "positionAmt": "0.1"}]
    with pytest.raises(AccountModeError):
        await GridExchange(client).position()
    client.responses[("GET", "/papi/v1/um/positionRisk")] = []
    client.responses[("GET", "/papi/v1/um/positionSide/dual")] = {
        "dualSidePosition": True}
    with pytest.raises(AccountModeError):
        await GridExchange(client).position()


@pytest.mark.asyncio
async def test_pm_position_poll_detects_foreign_cross_margin_exposure():
    client = FakeClient()
    client.responses[("GET", "/papi/v1/um/positionRisk")] = [
        {"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "0.02"},
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.01"}]
    with pytest.raises(AccountModeError, match="other symbol"):
        await GridExchange(client).position()
    assert client.calls[0] == ("GET", "/papi/v1/um/positionRisk", None)


@pytest.mark.asyncio
async def test_pm_account_accepts_only_one_way_crossed_expected_leverage():
    client = FakeClient()
    client.responses[("GET", "/papi/v1/um/positionSide/dual")] = {
        "dualSidePosition": False}
    client.responses[("GET", "/papi/v1/um/symbolConfig")] = [{
        "symbol": "ETHUSDT", "marginType": "CROSSED", "leverage": 5}]
    client.responses[("GET", "/papi/v1/account")] = {
        "accountStatus": "NORMAL", "accountEquity": "300",
        "totalAvailableBalance": "300"}
    client.responses[("GET", "/papi/v1/um/positionRisk")] = []
    client.responses[("GET", "/fapi/v1/exchangeInfo")] = {"symbols": [{
        "symbol": "ETHUSDT", "status": "TRADING", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}
    exchange = GridExchange(client)
    filters = await exchange.verify_account(5)
    assert filters.step_size == Decimal("0.001")
    client.responses[("GET", "/papi/v1/um/symbolConfig")][0]["leverage"] = 10
    with pytest.raises(AccountModeError, match="leverage"):
        await exchange.verify_account(5)


@pytest.mark.asyncio
async def test_pm_cross_margin_rejects_other_symbol_position():
    client = FakeClient()
    client.responses[("GET", "/papi/v1/um/positionSide/dual")] = {
        "dualSidePosition": False}
    client.responses[("GET", "/papi/v1/um/symbolConfig")] = [{
        "symbol": "ETHUSDT", "marginType": "CROSSED", "leverage": 5}]
    client.responses[("GET", "/papi/v1/account")] = {
        "accountStatus": "NORMAL", "accountEquity": "300",
        "totalAvailableBalance": "300"}
    client.responses[("GET", "/papi/v1/um/positionRisk")] = [
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.01"}]
    with pytest.raises(AccountModeError, match="other symbol"):
        await GridExchange(client).verify_account(5)
