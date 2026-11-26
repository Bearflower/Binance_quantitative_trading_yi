from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.check_account_cutover import flat_inventory
from shared.binance_account_mode import configured_pm, verify_trading_mode
from shared.binance_api import BinanceAPIError, BinanceClient
from strategies.hrs.executor import TradingExecutor
from strategies.hrs.position_manager import PositionManager


def test_legacy_and_current_account_settings(monkeypatch):
    assert configured_pm({})
    assert not configured_pm({"BINANCE_USE_PM": "false", "USE_UNIFIED_ACCOUNT": "false"})
    with pytest.raises(ValueError):
        configured_pm({"BINANCE_ACCOUNT_MODE": "classic", "BINANCE_USE_PM": "true"})
    with pytest.raises(ValueError):
        configured_pm({"BINANCE_ACCOUNT_MODE": "classsic"})
    monkeypatch.setenv("BINANCE_ACCOUNT_MODE", "classic")
    assert BinanceClient("key", "secret").base_url == "https://fapi.binance.com"
    assert BinanceClient("key", "secret", use_unified_account=True).use_unified_account


@pytest.mark.asyncio
async def test_flat_position_with_outstanding_stop_blocks_handoff():
    api = MagicMock()
    api.get_position = AsyncMock(return_value=[])
    api.get_um_account_positions = AsyncMock(return_value=[])
    api.get_open_orders = AsyncMock(return_value=[])
    api.get_open_algo_orders = AsyncMock(return_value=[{"algoId": 11}])
    result = await flat_inventory(api)
    assert not result["flat"] and result["algo_count"] == 1


@pytest.mark.asyncio
async def test_classic_startup_rejects_hedge_mode():
    api = MagicMock(use_unified_account=False)
    api._request = AsyncMock(return_value=dict(canTrade=True, dualSidePosition=True, multiAssetsMargin=False))
    with pytest.raises(RuntimeError):
        await verify_trading_mode(api)
    api._request.assert_awaited_once_with("GET", "/fapi/v1/accountConfig", signed=True)


@pytest.mark.asyncio
async def test_hrs_recovers_classic_algo_id_without_cancelling_or_replacing(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID","main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS","1000")
    api = MagicMock(use_unified_account=False)
    symbol = "ETHUSDT"
    positions = PositionManager({}, api)
    executor = TradingExecutor({}, api, None, None, positions)
    executor._entry_identities[symbol] = "hrs:ETHUSDT:42"
    tag = executor._protection_client_id(symbol, "sl")
    positions.add_position(symbol, "short", 2000, 1, 20)
    positions.get_position(symbol)["position_id"] = "hrs:ETHUSDT:42"
    assert executor._protection_client_id(symbol, "sl") == tag
    api.trade_logger.account_id = "main"
    api.trade_logger.strategy_name = "hrs"
    api.trade_logger.db.fetch_all = AsyncMock(return_value=[
        dict(symbol=symbol, algo_id="42", side="BUY", strategy="hrs", created_at=1010, order_type="STOP_LOSS",tp_level=None)])
    api.get_open_algo_orders = AsyncMock(return_value=[dict(symbol=symbol, side="BUY", clientAlgoId=tag, algoId=42,
                                                            createTime=1010,orderType="STOP_MARKET")])
    api.get_algo_order = AsyncMock(side_effect=BinanceAPIError(-2013, "absent"))
    await executor._reconcile_classic_protection(symbol, "short", False, False)
    assert positions.get_algo_id(symbol, "sl") == 42
    api.cancel_all_algo_orders.assert_not_called()
    api.place_conditional_order.assert_not_called()
    api.get_open_algo_orders.return_value[0]["clientAlgoId"] = "other-strategy"
    with pytest.raises(ValueError, match="归属"):
        await executor._reconcile_classic_protection(symbol, "short", False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["STOP", "STOP_MARKET", "TAKE_PROFIT"])
async def test_hrs_classic_exit_preserves_limit_or_market_semantics(kind):
    api = MagicMock(use_unified_account=False)
    api.place_conditional_order = AsyncMock(return_value={"algoId": 5})
    executor = TradingExecutor({}, api, None, None, PositionManager({}, api))
    await executor._submit_conditional_order("ETHUSDT", "SELL", kind, Decimal(1900),
                                             Decimal(1899), Decimal(1), True, client_id="episode")
    args = api.place_conditional_order.await_args.kwargs
    assert args["clientAlgoId"] == "episode"
    if kind == "STOP_MARKET":
        assert args["closePosition"] == "true" and args["quantity"] is None
    else:
        assert args["reduceOnly"] == "true" and args["quantity"] == 1 and "closePosition" not in args


@pytest.mark.asyncio
async def test_classic_ledger_gate_precedes_trading(monkeypatch):
    from shared.binance_account_mode import verify_cutover_ledger
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "default")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    client = MagicMock(use_unified_account=False, testnet=False)
    db = MagicMock()
    uid_reader = AsyncMock(return_value="a" * 64)
    monkeypatch.setattr("shared.accounting.equity_bridge.signed_spot_uid_sha256", uid_reader)
    db.fetch_all = AsyncMock(return_value=[{"observation": "pm_final", "total_equity": 100, "exchange_uid_sha256": "a" * 64}])
    with pytest.raises(ValueError):
        await verify_cutover_ledger(client, db)
    db.fetch_all.return_value.append({"observation": "classic_open", "total_equity": 85, "exchange_uid_sha256": "a" * 64})
    await verify_cutover_ledger(client, db)
    assert db.fetch_all.await_args.args[1:] == ("default", 100)
    db.fetch_one = AsyncMock(return_value={"reservations": False, "claims": True})
    with pytest.raises(RuntimeError, match="认领"):
        await verify_cutover_ledger(client, db, trading_writer=True)
    from shared.accounting.runtime_cutover import STRATEGIES
    clear = {"reservations": False, "claims": False}
    db.fetch_one.side_effect = [clear, None]
    with pytest.raises(RuntimeError, match="运行状态"):
        await verify_cutover_ledger(client, db, trading_writer=True)
    db.fetch_one.side_effect = [clear, {"state_data": {"account": "default", "cutover_ms": 100,
                                                      "version": 1, "strategies": list(STRATEGIES)}}]
    await verify_cutover_ledger(client, db, trading_writer=True)
    uid_reader.return_value = "b" * 64
    with pytest.raises(ValueError, match="UID"):
        await verify_cutover_ledger(client, db)

@pytest.mark.asyncio
async def test_extra_subaccount_inventory_uses_own_uid_without_main_claims(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    from scripts import check_account_cutover as cutover

    for name, value in {"ID": "grid-two", "MODE": "classic", "API_KEY": "local-key",
                        "API_SECRET": "local-secret"}.items():
        monkeypatch.setenv(f"CUTOVER_SUBACCOUNT_{name}", value)
    transport = MagicMock()
    client = transport.__aenter__.return_value
    constructor = MagicMock(return_value=transport)
    monkeypatch.setattr(cutover, "ReadOnlyBinanceClient", constructor)
    monkeypatch.setattr(cutover, "signed_spot_uid_sha256", AsyncMock(return_value="c" * 64))
    monkeypatch.setattr(cutover, "verify_trading_mode", AsyncMock())
    before = AsyncMock(return_value={"positions": []})
    monkeypatch.setattr(cutover, "before_close", before)
    target = tmp_path / "additional-subaccount.json"
    assert await cutover.inspect(SimpleNamespace(account="other", before_close=True, output=target)) == 0
    constructor.assert_called_once_with("local-key", "local-secret", use_unified_account=False, instance_id="cutover")
    before.assert_awaited_once_with(client, True)
    proof = json.loads(target.read_text())
    assert (proof["account_id"], proof["mode"], proof["exchange_uid_sha256"]) == ("grid-two", "classic", "c" * 64)


@pytest.mark.asyncio
async def test_before_close_retains_second_signed_position_inventory_for_subaccount():
    from scripts.check_account_cutover import before_close
    exchange = AsyncMock()
    exchange.get_position.return_value = []
    row = dict(symbol="ETHUSDT", positionAmt="-1")
    exchange.get_um_account_positions.return_value = [row]
    exchange.get_open_orders.return_value = exchange.get_open_algo_orders.return_value = []
    proof = await before_close(exchange, True)
    assert proof["positions"] == [] and proof["account_positions"] == [row]


@pytest.mark.asyncio
async def test_claim_cutover_uses_utc_database_clock():
    from shared.binance_account_mode import check_pm_claims
    from datetime import datetime
    cutover = 1795582464000
    classic_created = datetime(2026, 11, 25, 8, 32, 49)
    pm_created = datetime(2026, 11, 25, 4, 50)
    async def query(sql, account, stamp):
        assert "timezone('UTC', to_timestamp" in sql
        boundary = datetime.fromtimestamp(stamp / 1000, timezone.utc).replace(tzinfo=None)
        return dict(reservations=False, claims=created < boundary)
    created = classic_created
    await check_pm_claims(query, "default", cutover)
    created = pm_created
    with pytest.raises(RuntimeError, match="认领"):
        await check_pm_claims(query, "default", cutover)
