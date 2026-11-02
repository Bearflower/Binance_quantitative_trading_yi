"""Regressions for live strategies' monthly allocation boundary."""

from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from shared.capital_manager import CapitalManager
from strategies.hrs.strategy import HRSStrategy
from strategies.btc_eth.strategy import BTCEthStrategy, PositionState
from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as AggressiveStrategy
from strategies.new_coin.strategy import NewCoinStrategy
from decimal import Decimal


@pytest.mark.asyncio
async def test_live_strategy_rejects_entry_without_current_month_allocation(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"trading": {"total_position_margin_limit": 500}}))
    db = MagicMock(fetch_one=AsyncMock(return_value=None))
    manager = CapitalManager(str(path), db=db, strategy_id="hrs", require_current_allocation=True)

    assert (await manager.can_open_within_limit(0, 10))[0] is False


@pytest.mark.asyncio
async def test_live_strategy_does_not_use_previous_month_config(tmp_path):
    path = tmp_path / "config.yaml"
    previous_month = "2020-01"
    path.write_text(yaml.safe_dump({
        "capital_limits": {"monthly_limit": 500, "allocation_month": previous_month},
        "trading": {"total_position_margin_limit": 1000},
    }))
    db = MagicMock(fetch_one=AsyncMock(return_value=None))
    manager = CapitalManager(str(path), db=db, strategy_id="hrs", require_current_allocation=True)

    assert (await manager.can_open_within_limit(0, 10))[0] is False


@pytest.mark.asyncio
async def test_live_strategy_does_not_use_current_config_when_db_has_no_allocation(tmp_path):
    path = tmp_path / "config.yaml"
    current_month = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")
    path.write_text(yaml.safe_dump({
        "capital_limits": {"monthly_limit": 500, "allocation_month": current_month},
        "trading": {"total_position_margin_limit": 1000},
    }))
    db = MagicMock(fetch_one=AsyncMock(return_value=None))
    manager = CapitalManager(str(path), db=db, strategy_id="hrs", require_current_allocation=True)

    assert (await manager.can_open_within_limit(0, 10))[0] is False


@pytest.mark.asyncio
async def test_live_strategy_rejects_nonfinite_allocation(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"trading": {"total_position_margin_limit": 1000}}))
    db = MagicMock(fetch_one=AsyncMock(return_value={
        "entries": [{"strategy_id": "hrs", "allocated_amount": "NaN"}]
    }))
    manager = CapitalManager(str(path), db=db, strategy_id="hrs", require_current_allocation=True)

    assert (await manager.can_open_within_limit(0, 10))[0] is False


@pytest.mark.asyncio
async def test_live_strategy_uses_fresh_db_amount_after_daily_refresh(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{}")
    db = MagicMock(fetch_one=AsyncMock(side_effect=[
        {"entries": [{"strategy_id": "hrs", "allocated_amount": 100}]},
        {"entries": [{"strategy_id": "hrs", "allocated_amount": 50}]},
    ]))
    manager = CapitalManager(str(path), db=db, strategy_id="hrs", require_current_allocation=True)

    assert (await manager.can_open_within_limit(0, 75))[0] is True
    assert (await manager.can_open_within_limit(0, 75))[0] is False
    assert db.fetch_one.await_count == 2


@pytest.mark.asyncio
async def test_live_strategy_rejects_new_order_during_db_outage(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{}")
    db = MagicMock(fetch_one=AsyncMock(side_effect=[
        {"entries": [{"strategy_id": "hrs", "allocated_amount": 100}]},
        RuntimeError("database offline"),
    ]))
    manager = CapitalManager(str(path), db=db, strategy_id="hrs", require_current_allocation=True)

    assert (await manager.can_open_within_limit(0, 75))[0] is True
    assert (await manager.can_open_within_limit(0, 75))[0] is False


@pytest.mark.asyncio
async def test_hrs_add_position_checks_allocation_before_order():
    strategy = object.__new__(HRSStrategy)
    strategy.position_manager = MagicMock()
    position = {"direction": "short", "entry_price": 100, "entry_quantity": 2, "atr": 1}
    strategy.position_manager.get_position.return_value = position
    strategy.position_manager.get_all_positions.return_value = {"BTCUSDT": position}
    strategy._add_position_count = {}
    strategy.add_position_max_times = 1
    strategy.add_position_profit_threshold = 0.01
    strategy.add_position_size_ratio = 0.5
    strategy.trading_executor = MagicMock()
    strategy.trading_executor.leverage = 2
    strategy.trading_executor.calculate_atr = AsyncMock(return_value=1)
    strategy.trading_executor.add_to_position = AsyncMock(return_value={"orderId": 1})
    strategy.capital_mgr = MagicMock()
    strategy.capital_mgr.can_open_within_limit = AsyncMock(return_value=(False, "超额"))

    result = await strategy._handle_add_position("BTCUSDT", "short", 8, 90, [])

    assert result is False
    strategy.capital_mgr.can_open_within_limit.assert_awaited_once()
    strategy.trading_executor.add_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_positions_are_closed_until_within_reduced_allocation(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{}")
    manager = CapitalManager(str(path), strategy_id="hrs", require_current_allocation=True)
    manager.get_effective_margin_limit = AsyncMock(return_value=(70.0, "db"))
    closed = []

    async def close(symbol):
        closed.append(symbol)
        return True

    settled = await manager.enforce_existing_positions(
        {"BTCUSDT": 50.0, "ETHUSDT": 40.0}, close
    )

    assert settled is True
    assert closed == ["ETHUSDT"]


@pytest.mark.asyncio
async def test_existing_positions_remain_unsettled_if_close_fails(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{}")
    manager = CapitalManager(str(path), strategy_id="hrs", require_current_allocation=True)
    manager.get_effective_margin_limit = AsyncMock(return_value=(30.0, "db"))
    close = AsyncMock(return_value=False)

    settled = await manager.enforce_existing_positions({"BTCUSDT": 50.0}, close)

    assert settled is False
    close.assert_awaited_once_with("BTCUSDT")


@pytest.mark.asyncio
async def test_close_exception_does_not_abort_position_monitor(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{}")
    manager = CapitalManager(str(path), strategy_id="hrs", require_current_allocation=True)
    manager.get_effective_margin_limit = AsyncMock(return_value=(30.0, "db"))
    close = AsyncMock(side_effect=RuntimeError("exchange unavailable"))

    assert await manager.enforce_existing_positions({"BTCUSDT": 50.0}, close) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_type", [BTCEthStrategy, AggressiveStrategy])
async def test_mtpcs_reconciles_existing_position_over_reduced_limit(strategy_type):
    strategy = object.__new__(strategy_type)
    position = PositionState()
    position.current_quantity = Decimal("2")
    position.entry_price = Decimal("100")
    position.grade = "C"
    strategy.positions = {"BTCUSDT": position}
    strategy.symbols = ["BTCUSDT"]
    strategy.my_record_name = "btc_eth_aggressive" if strategy_type is AggressiveStrategy else "btc_eth"
    strategy.db_manager = None
    strategy.min_position_amt = 0.00001
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[{
        "symbol": "BTCUSDT", "positionAmt": "2", "markPrice": "100", "leverage": "2",
        "entryPrice": "100",
    }])
    strategy.binance_config = {"leverage": {"C": 2}}
    strategy.capital_mgr = MagicMock()
    strategy.capital_mgr.get_effective_margin_limit = AsyncMock(return_value=(50.0, "db"))
    strategy.capital_mgr.enforce_existing_positions = CapitalManager.enforce_existing_positions.__get__(strategy.capital_mgr)

    async def close(symbol, pos, qty, reason):
        pos.current_quantity = Decimal("0")
        return True

    strategy._close_position = AsyncMock(side_effect=close)
    strategy._sync_position_with_exchange = AsyncMock(return_value={"closed": True})
    module = strategy_type.__module__
    with patch(f"{module}.resolve_position_owner", new=AsyncMock(return_value=strategy.my_record_name)):
        assert await strategy._enforce_allocation_holdings() is True
    strategy._close_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_mtpcs_does_not_claim_overage_resolved_until_exchange_is_flat():
    strategy = object.__new__(BTCEthStrategy)
    position = PositionState()
    position.current_quantity = Decimal("2")
    position.entry_price = Decimal("100")
    position.grade = "C"
    strategy.positions = {"BTCUSDT": position}
    strategy.symbols = ["BTCUSDT"]
    strategy.my_record_name = "btc_eth"
    strategy.db_manager = None
    strategy.min_position_amt = 0.00001
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[{
        "symbol": "BTCUSDT", "positionAmt": "2", "markPrice": "100", "leverage": "2",
        "entryPrice": "100",
    }])
    strategy.binance_config = {"leverage": {"C": 2}}
    strategy.capital_mgr = MagicMock()
    strategy.capital_mgr.get_effective_margin_limit = AsyncMock(return_value=(50.0, "db"))
    strategy.capital_mgr.enforce_existing_positions = CapitalManager.enforce_existing_positions.__get__(strategy.capital_mgr)
    strategy._close_position = AsyncMock(return_value=True)
    strategy._sync_position_with_exchange = AsyncMock(return_value={"closed": False})

    with patch(f"{BTCEthStrategy.__module__}.resolve_position_owner", new=AsyncMock(return_value="btc_eth")):
        assert await strategy._enforce_allocation_holdings() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_type", [BTCEthStrategy, AggressiveStrategy])
async def test_mtpcs_reduces_owned_unadopted_exchange_position(strategy_type):
    strategy = object.__new__(strategy_type)
    strategy.positions = {}
    strategy.symbols = ["ETHUSDT"]
    strategy.my_record_name = "btc_eth_aggressive" if strategy_type is AggressiveStrategy else "btc_eth"
    strategy.db_manager = MagicMock(execute=AsyncMock())
    strategy.strategy_name = strategy.my_record_name
    strategy.min_position_amt = 0.00001
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[{
        "symbol": "ETHUSDT", "positionAmt": "1", "markPrice": "100", "leverage": "2",
        "entryPrice": "90",
    }])
    strategy.capital_mgr = MagicMock()
    strategy.capital_mgr.get_effective_margin_limit = AsyncMock(return_value=(30.0, "db"))
    strategy.capital_mgr.enforce_existing_positions = CapitalManager.enforce_existing_positions.__get__(strategy.capital_mgr)
    strategy._close_position = AsyncMock(return_value=True)
    strategy._sync_position_with_exchange = AsyncMock(return_value={"closed": True})
    strategy._cancel_symbol_conditional_orders = AsyncMock(return_value=True)
    if strategy_type is AggressiveStrategy:
        strategy.binance.get_user_trades = AsyncMock(return_value=[
            {"id": 1, "time": 1793409086845, "side": "BUY", "qty": "1"},
        ])
        strategy._close_untracked_exchange_position = AsyncMock(return_value=True)
    module = strategy_type.__module__
    with patch(f"{module}.resolve_position_owner", new=AsyncMock(return_value=strategy.my_record_name)) as owner:
        with patch(f"{module}.release_claim", new=AsyncMock()) as release:
            assert await strategy._enforce_allocation_holdings() is True
    assert owner.await_count >= 2  # snapshot and immediately before close
    if strategy_type is AggressiveStrategy:
        strategy._close_untracked_exchange_position.assert_awaited_once()
        assert strategy._close_untracked_exchange_position.await_args.args[2] == "ALLOCATION_LIMIT"
        strategy._close_position.assert_not_awaited()
        strategy._cancel_symbol_conditional_orders.assert_not_awaited()
        strategy.db_manager.execute.assert_not_awaited()
        release.assert_not_awaited()
    else:
        strategy._close_position.assert_awaited_once()
        assert strategy._close_position.await_args.args[3] == "ALLOCATION_LIMIT"
        strategy._cancel_symbol_conditional_orders.assert_awaited_once()
        strategy.db_manager.execute.assert_awaited_once()
        release.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_type", [BTCEthStrategy, AggressiveStrategy])
async def test_mtpcs_never_closes_other_strategy_exchange_position(strategy_type):
    strategy = object.__new__(strategy_type)
    strategy.positions = {}
    strategy.symbols = ["ETHUSDT"]
    strategy.my_record_name = "btc_eth_aggressive" if strategy_type is AggressiveStrategy else "btc_eth"
    strategy.db_manager = None
    strategy.min_position_amt = 0.00001
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[{
        "symbol": "ETHUSDT", "positionAmt": "1", "markPrice": "100", "leverage": "2",
        "entryPrice": "90",
    }])
    strategy.capital_mgr = MagicMock()
    strategy.capital_mgr.get_effective_margin_limit = AsyncMock(return_value=(0.0, "db"))
    strategy.capital_mgr.enforce_existing_positions = CapitalManager.enforce_existing_positions.__get__(strategy.capital_mgr)
    strategy._close_position = AsyncMock()
    module = strategy_type.__module__
    other = "btc_eth" if strategy_type is AggressiveStrategy else "btc_eth_aggressive"
    with patch(f"{module}.resolve_position_owner", new=AsyncMock(return_value=other)):
        assert await strategy._enforce_allocation_holdings() is True
    strategy._close_position.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_type", [BTCEthStrategy, AggressiveStrategy])
async def test_mtpcs_owner_change_before_close_blocks_order(strategy_type):
    strategy = object.__new__(strategy_type)
    strategy.positions = {}
    strategy.my_record_name = "btc_eth_aggressive" if strategy_type is AggressiveStrategy else "btc_eth"
    strategy.db_manager = None
    strategy.min_position_amt = 0.00001
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[{
        "symbol": "ETHUSDT", "positionAmt": "1", "markPrice": "100", "leverage": "2",
    }])
    strategy.capital_mgr = MagicMock()
    strategy.capital_mgr.get_effective_margin_limit = AsyncMock(return_value=(30.0, "db"))
    strategy.capital_mgr.enforce_existing_positions = CapitalManager.enforce_existing_positions.__get__(strategy.capital_mgr)
    strategy._close_position = AsyncMock()
    other = "btc_eth" if strategy_type is AggressiveStrategy else "btc_eth_aggressive"
    with patch(f"{strategy_type.__module__}.resolve_position_owner",
               new=AsyncMock(side_effect=[strategy.my_record_name, other])):
        assert await strategy._enforce_allocation_holdings() is False
    strategy._close_position.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_type", [BTCEthStrategy, AggressiveStrategy])
async def test_mtpcs_exchange_read_failure_cannot_claim_compliance(strategy_type):
    strategy = object.__new__(strategy_type)
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(side_effect=RuntimeError("exchange unavailable"))
    strategy.capital_mgr = MagicMock()
    strategy.capital_mgr.enforce_existing_positions = AsyncMock()
    assert await strategy._enforce_allocation_holdings() is False
    strategy.capital_mgr.enforce_existing_positions.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_type", [BTCEthStrategy, AggressiveStrategy])
async def test_mtpcs_keeps_position_monitor_running_when_allocation_check_errors(strategy_type):
    strategy = object.__new__(strategy_type)
    position = PositionState()
    position.current_quantity = Decimal("1")
    strategy.positions = {"BTCUSDT": position}
    strategy._enforce_allocation_holdings = AsyncMock(side_effect=RuntimeError("db error"))
    strategy._get_current_price = AsyncMock(return_value=None)
    strategy._cycle_count = 0
    strategy._retry_pending_cancellations = AsyncMock()
    strategy._cleanup_residual_orders = AsyncMock()
    strategy._retry_rebuild_pending = AsyncMock()
    strategy._retry_protection_pending = AsyncMock()
    strategy._maybe_cleanup_expired_claims = AsyncMock()
    strategy._unverified_entry_time_symbols = set()
    strategy._untracked_stop_retry_symbols = set()

    await strategy.update_positions()

    strategy._get_current_price.assert_awaited_once_with("BTCUSDT")


@pytest.mark.asyncio
async def test_new_coin_reconciles_existing_position_over_reduced_limit():
    strategy = object.__new__(NewCoinStrategy)
    strategy.positions = {"ABCUSDT": {"entry_price": 100}}
    strategy.trading_executor = MagicMock()
    strategy.trading_executor.calc_current_occupied_margin = AsyncMock(return_value=100.0)
    strategy.trading_executor._close_position = AsyncMock(return_value=True)
    strategy.trading_executor.capital_mgr = MagicMock()
    manager = strategy.trading_executor.capital_mgr
    manager.get_effective_margin_limit = AsyncMock(return_value=(50.0, "db"))
    manager.enforce_existing_positions = CapitalManager.enforce_existing_positions.__get__(manager)

    assert await strategy._enforce_allocation_holdings() is True
    strategy.trading_executor._close_position.assert_awaited_once_with(
        "ABCUSDT", Decimal("1"), "月度分配额度超额"
    )


@pytest.mark.asyncio
async def test_new_coin_keeps_position_monitor_running_when_allocation_check_errors():
    strategy = object.__new__(NewCoinStrategy)
    strategy._enforce_allocation_holdings = AsyncMock(side_effect=RuntimeError("db error"))
    strategy._monitor_positions = AsyncMock()
    strategy._save_state = AsyncMock()
    strategy._weekly_review = AsyncMock()

    await strategy._run_cycle_tail()

    strategy._monitor_positions.assert_awaited_once()


@pytest.mark.asyncio
async def test_hrs_reconciles_existing_position_over_reduced_limit():
    strategy = object.__new__(HRSStrategy)
    position = {"direction": "short", "entry_price": 100, "entry_quantity": 2,
                "remaining_quantity": 2, "atr": 1}
    strategy.position_manager = MagicMock()
    strategy.position_manager.get_all_positions.return_value = {"BTCUSDT": position}
    strategy.trading_executor = MagicMock()
    strategy.trading_executor.leverage = 2
    strategy.trading_executor.close_position = AsyncMock(return_value=MagicMock(success=True))
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_ticker = AsyncMock(return_value={"lastPrice": "95"})
    strategy._finalize_close_if_filled = AsyncMock(return_value=True)
    strategy.capital_mgr = MagicMock()
    manager = strategy.capital_mgr
    manager.get_effective_margin_limit = AsyncMock(return_value=(50.0, "db"))
    manager.enforce_existing_positions = CapitalManager.enforce_existing_positions.__get__(manager)

    assert await strategy._enforce_allocation_holdings() is True
    strategy.trading_executor.close_position.assert_awaited_once()
    strategy._finalize_close_if_filled.assert_awaited_once()


@pytest.mark.asyncio
async def test_hrs_monitor_survives_allocation_check_error():
    strategy = object.__new__(HRSStrategy)
    strategy._enforce_allocation_holdings = AsyncMock(side_effect=RuntimeError("db error"))
    strategy.position_manager = MagicMock()
    strategy.position_manager.get_all_positions.return_value = {}

    await strategy._monitor_positions()


def test_hrs_margin_counts_remaining_quantity_after_take_profit():
    strategy = object.__new__(HRSStrategy)
    strategy.position_manager = MagicMock()
    strategy.position_manager.get_all_positions.return_value = {
        "BTCUSDT": {"entry_quantity": 4, "remaining_quantity": 1, "entry_price": 100}
    }
    strategy.trading_executor = MagicMock(leverage=2)

    assert strategy._calc_current_total_margin() == pytest.approx(50.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy_cls", "module_name", "strategy_id"),
    [
        (BTCEthStrategy, "strategies.btc_eth.strategy", "btc_eth"),
        (AggressiveStrategy, "strategies.btc_eth_aggressive.strategy", "btc_eth_aggressive"),
    ],
)
async def test_mtpcs_counts_owned_exchange_positions_missing_from_local_state(
    monkeypatch, strategy_cls, module_name, strategy_id
):
    import importlib

    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "resolve_position_owner", AsyncMock(return_value=strategy_id))
    strategy = object.__new__(strategy_cls)
    strategy.positions = {}
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[
        {"symbol": "ETHUSDT", "positionAmt": "1", "markPrice": "100", "leverage": "2"},
    ])
    strategy.db_manager = MagicMock()
    strategy.my_record_name = strategy_id
    strategy.min_position_amt = 0.00001
    strategy._calc_current_total_margin = lambda: 0.0
    strategy.capital_mgr = MagicMock()
    strategy.capital_mgr.can_open_within_limit = AsyncMock(return_value=(False, "limit"))
    strategy.capital_mgr.get_account_ratio_cap.return_value = None

    allowed = await strategy._check_total_margin_ratio(
        {"symbol": "BTCUSDT", "quantity": 1, "entry_price": 100, "leverage": 2}
    )

    assert allowed is False
    assert strategy.capital_mgr.can_open_within_limit.await_args.args == (50.0, 50.0)
