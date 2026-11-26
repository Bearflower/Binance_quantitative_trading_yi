from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from strategies.grid.execution.contracts import ExecutionSettings
from strategies.grid.execution.exchange import AccountModeError, ClassicGridExchange, GridExchange
from strategies.grid.execution.risk import RiskLimits, RiskStatus, beijing_day
from strategies.grid.execution.risk_tracker import RiskTracker
from strategies.grid.execution.store import DecisionStore, migrate
from strategies.grid.execution.stream import user_stream_url


@pytest.fixture
def exchange():
    client = AsyncMock()
    client.use_unified_account = False
    return ClassicGridExchange(client)


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "journal.sqlite"
    migrate(path)
    store = DecisionStore(path)
    yield store
    store.close()


def test_live_subaccount_mode_independent_from_main_account():
    env = {"GRID_EXECUTION_MODE": "live", "GRID_SUBACCOUNT_MODE": "classic",
           "BINANCE_USE_PM": "true", "GRID_SUBACCOUNT_API_KEY": "subkey",
           "GRID_SUBACCOUNT_API_SECRET": "subsecret", "GRID_MAX_MARGIN_USDT": "100",
           "GRID_MAX_NOTIONAL_USDT": "500", "GRID_MAX_DAILY_LOSS_USDT": "10",
           "GRID_MAX_DRAWDOWN_USDT": "20", "GRID_MAX_OPEN_ORDERS": "12",
           "GRID_EXPECTED_LEVERAGE": "10"}
    adapter = GridExchange.from_settings(ExecutionSettings.from_env(env))
    assert isinstance(adapter, ClassicGridExchange)
    assert adapter.client.base_url == "https://fapi.binance.com"
    env["GRID_SUBACCOUNT_MODE"] = "typo"
    with pytest.raises(ValueError, match="GRID_SUBACCOUNT_MODE"):
        ExecutionSettings.from_env(env)


@pytest.mark.asyncio
async def test_classic_rest_order_lifecycle(exchange):
    writes = exchange.client._request_once
    reads = exchange.client._request
    reads.side_effect = [{}, {}, [], [], {}, {"algoStatus": "NEW", "clientAlgoId": "sl"}]
    writes.side_effect = [{}, {}, {}, {"code": "200", "clientAlgoId": "sl"}]
    await exchange.place_limit("BUY", Decimal("0.1"), Decimal(2000), "limit")
    await exchange.close_position("SELL", Decimal("0.1"), "close")
    await exchange.place_stop("SELL", Decimal(1800), "sl")
    await exchange.query_order("limit")
    await exchange.cancel_order("limit")
    await exchange.open_orders()
    await exchange.open_stops()
    await exchange.query_stop("sl")
    await exchange.cancel_stop("sl")
    assert [(c.args[0], c.args[1]) for c in writes.await_args_list] == [
        ("POST", "/fapi/v1/order"), ("POST", "/fapi/v1/order"),
        ("POST", "/fapi/v1/algoOrder"), ("DELETE", "/fapi/v1/algoOrder")]
    assert writes.await_args_list[0].args[2]["timeInForce"] == "GTX"
    assert writes.await_args_list[1].args[2]["reduceOnly"] == "true"
    stop = writes.await_args_list[2].args[2]
    assert stop["clientAlgoId"] == "sl" and stop["closePosition"] == "true"
    assert stop["triggerPrice"] == "1800" and stop["workingType"] == "MARK_PRICE"
    assert [c.args for c in reads.await_args_list] == [
        ("GET", "/fapi/v1/order", {"symbol": "ETHUSDT", "origClientOrderId": "limit"}),
        ("DELETE", "/fapi/v1/order", {"symbol": "ETHUSDT", "origClientOrderId": "limit"}),
        ("GET", "/fapi/v1/openOrders", None), ("GET", "/fapi/v1/openAlgoOrders", None),
        ("GET", "/fapi/v1/algoOrder", {"clientAlgoId": "sl"}),
        ("GET", "/fapi/v1/algoOrder", {"clientAlgoId": "sl"})]


@pytest.mark.asyncio
async def test_classic_stream_renewal_and_trade_backfill(exchange):
    exchange.client._request_once.side_effect = [{"listenKey": "abc"}, {"listenKey": "abc"}]
    assert await exchange.start_user_stream() == await exchange.keepalive_user_stream()
    assert [c.args for c in exchange.client._request_once.await_args_list] == [
        ("POST", "/fapi/v1/listenKey"), ("PUT", "/fapi/v1/listenKey")]
    assert user_stream_url("live", "abc", pm=False) == "wss://fstream.binance.com/private/ws/abc"
    exchange.client._request.return_value = [{"orderId": 5}, {"orderId": 6}]
    assert await exchange.order_trades(5, 1000, 2000) == [{"orderId": 5}]
    assert exchange.client._request.await_args.args[1] == "/fapi/v1/userTrades"


CLASSIC_CONFIG = {"canTrade": True, "multiAssetsMargin": False, "dualSidePosition": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("canTrade", False), ("multiAssetsMargin", True),
                                         ("dualSidePosition", True), ("canTrade", "true")])
async def test_classic_account_fails_closed_on_mode_or_permission(exchange, field, value):
    exchange.client._request.return_value = {**CLASSIC_CONFIG, field: value}
    with pytest.raises(AccountModeError):
        await exchange.account_info()
    exchange.client._request.assert_awaited_once_with("GET", "/fapi/v1/accountConfig")


@pytest.mark.asyncio
async def test_classic_equity_not_wallet_or_available_balance(exchange, store):
    now = 1_800_000_000_000
    exchange.client._request.side_effect = [
        CLASSIC_CONFIG, {"totalMarginBalance": "120", "availableBalance": "31",
                         "totalWalletBalance": "90"}, [], [], {"dualSidePosition": False}]
    tracker = RiskTracker(store, exchange, RiskLimits(Decimal(10), Decimal(20)))
    assert (await tracker.assess(now)).status == RiskStatus.ALLOW
    assert store.latest_equity_checkpoint(beijing_day(now))["equity"] == "120"
    assert [c.args[1] for c in exchange.client._request.await_args_list] == [
        "/fapi/v1/accountConfig", "/fapi/v3/account", "/fapi/v1/income",
        "/fapi/v3/positionRisk", "/fapi/v1/positionSide/dual"]


@pytest.mark.asyncio
async def test_classic_journal_alias_is_stable_and_cannot_cross_accounts(exchange, store):
    exchange.client._request.return_value = [{"asset": "USDT", "accountAlias": "sub1"}]
    identity = await exchange.journal_identity()
    exchange.client._request.assert_awaited_once_with("GET", "/fapi/v3/balance")
    assert identity == "classic:sub1"
    decision = {"symbol": "ETHUSDT", "state": "OSCILLATION", "calculated_at_ms": 1000,
                "last_closed_kline_ms": {"15m": 999, "1h": 999, "4h": 999}}
    old_id = store.publish(decision, "hash")
    store.bind_account(identity)
    assert store.cursor() == old_id
    next_id = store.publish({**decision, "calculated_at_ms": 2000}, "hash")
    store.bind_account(identity)
    assert store.next_decision()["decision_id"] == next_id
    for wrong in ("classic:sub2", "pm"):
        with pytest.raises(RuntimeError, match="another account"):
            store.bind_account(wrong)


def test_legacy_pm_epoch_prevents_classic_reuse(store):
    store.create_epoch("old")
    with pytest.raises(RuntimeError, match="fresh"):
        store.bind_account("classic:sub")
    store.bind_account("pm")


@pytest.mark.asyncio
async def test_classic_transfer_or_unclassified_income_locks_existing_baseline(exchange, store):
    tracker = RiskTracker(store, exchange, RiskLimits(Decimal(10), Decimal(20)))
    now = 1_800_000_000_000
    store.record_equity_checkpoint(beijing_day(now), Decimal(100), Decimal(100), Decimal(100),
                                   now - 1000, verified=True, transfer_fingerprint="none")
    for kind in ("TRANSFER", "INTERNAL_TRANSFER", None):
        exchange.client._request.side_effect = [
            CLASSIC_CONFIG, {"totalMarginBalance": "101", "availableBalance": "100"},
            [{"asset": "USDT", "time": now, "incomeType": kind, "income": "1"}]]
        assert (await tracker.assess(now)).status == RiskStatus.UNKNOWN_LOCKED



def test_notifier_cannot_attach_pm_facts_to_classic_account(store, tmp_path):
    import sqlite3
    from strategies.grid.execution.observer_journal import bind_notices, notice_identity, notice_path

    env = {"GRID_EXECUTION_DB": "/app/data/grid-classic.sqlite3"}
    assert notice_path(env, False) == "/app/data/grid-classic_notifications.sqlite3"
    with sqlite3.connect(tmp_path / "notices.sqlite") as db:
        db.execute("CREATE TABLE fact(id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO fact VALUES(1)")
        db.commit()
        with pytest.raises(ValueError, match="fresh"):
            bind_notices(db, "classic:sub")
        assert notice_identity(db) is None
        bind_notices(db, "pm")
        with pytest.raises(ValueError, match="mismatch"):
            bind_notices(db, "classic:sub")
