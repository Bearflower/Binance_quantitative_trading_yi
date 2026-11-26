from unittest.mock import AsyncMock, MagicMock

import pytest

from strategies.hrs.strategy import HRSStrategy


@pytest.mark.asyncio
async def test_hrs_reconcile_invokes_exchange_guarded_store(monkeypatch):
    from shared import entry_risk_policy

    store = MagicMock()
    store.reconcile = AsyncMock()
    factory = MagicMock(return_value=store)
    monkeypatch.setattr(entry_risk_policy, "RiskPolicyStore", factory)
    strategy = object.__new__(HRSStrategy)
    strategy.db = MagicMock(pool=object())
    strategy.db.fetch_one = AsyncMock(return_value=None)
    strategy.binance_client = object()

    assert await strategy._reconcile_hrs_reservations("ARIAUSDT") is True

    factory.assert_called_once_with(strategy.db)
    store.reconcile.assert_awaited_once_with(
        "hrs", strategy.binance_client, enforce_legacy_orders=False,
        eligible_symbols={"ARIAUSDT"},
    )


@pytest.mark.asyncio
async def test_startup_retains_flat_position_until_pnl_is_proven():
    strategy = object.__new__(HRSStrategy)
    strategy.db = MagicMock(pool=object())
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_position = AsyncMock(return_value=[])
    strategy.binance_client.get_open_orders = AsyncMock(return_value=[])
    strategy.binance_client.get_open_algo_orders = AsyncMock(return_value=[])
    strategy._reconcile_hrs_reservations = AsyncMock()
    strategy._writeback_pnl_for_full_close = AsyncMock(return_value=None)
    strategy.position_manager = MagicMock()
    strategy.position_manager.cancel_all_orders = AsyncMock()
    strategy.position_manager.get_all_positions.return_value = {
        "ARIAUSDT": {"direction": "short", "entry_price": .23276,
                     "entry_quantity": 31, "atr": .01}
    }
    strategy._zero_qty_threshold = .0001

    await strategy._reconcile_positions()

    strategy.position_manager.remove_position.assert_not_called()
    strategy._reconcile_hrs_reservations.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_cleans_flat_position_after_late_pnl_proof(monkeypatch):
    import strategies.hrs.strategy as hrs_module

    release = AsyncMock()
    monkeypatch.setattr(hrs_module, "release_claim", release)
    strategy = object.__new__(HRSStrategy)
    strategy.db = MagicMock(pool=object())
    strategy.strategy_name = "hrs"
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_position = AsyncMock(return_value=[])
    strategy.binance_client.get_open_orders = AsyncMock(return_value=[])
    strategy.binance_client.get_open_algo_orders = AsyncMock(return_value=[])
    strategy._reconcile_hrs_reservations = AsyncMock(return_value=True)
    strategy._writeback_pnl_for_full_close = AsyncMock(return_value=__import__("decimal").Decimal("0.17484"))
    strategy.position_manager = MagicMock()
    strategy.position_manager.get_all_positions.return_value = {
        "ARIAUSDT": {"direction": "short", "entry_price": .23276,
                     "entry_quantity": 31, "atr": .01}
    }
    strategy._zero_qty_threshold = .0001
    strategy._should_unregister = MagicMock(return_value=False)
    strategy._save_state = AsyncMock()

    await strategy._reconcile_positions()

    strategy.position_manager.remove_position.assert_called_once_with("ARIAUSDT")
    release.assert_awaited_once()
    strategy._reconcile_hrs_reservations.assert_awaited_once_with("ARIAUSDT")
    strategy._save_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_transient_reservation_failure_keeps_position_for_retry(monkeypatch):
    import strategies.hrs.strategy as hrs_module

    release = AsyncMock()
    monkeypatch.setattr(hrs_module, "release_claim", release)
    strategy = object.__new__(HRSStrategy)
    strategy.db = MagicMock(pool=object())
    strategy.strategy_name = "hrs"
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_position = AsyncMock(return_value=[])
    strategy.binance_client.get_open_orders = AsyncMock(return_value=[])
    strategy.binance_client.get_open_algo_orders = AsyncMock(return_value=[])
    strategy._writeback_pnl_for_full_close = AsyncMock(return_value=__import__("decimal").Decimal("0.17484"))
    strategy._reconcile_hrs_reservations = AsyncMock(side_effect=[False, True])
    strategy.position_manager = MagicMock()
    strategy.position_manager.get_all_positions.return_value = {
        "ARIAUSDT": {"direction": "short", "entry_price": .23276,
                     "entry_quantity": 31, "atr": .01}
    }
    strategy._zero_qty_threshold = .0001
    strategy._should_unregister = MagicMock(return_value=False)
    strategy._save_state = AsyncMock()

    await strategy._reconcile_positions()
    strategy.position_manager.remove_position.assert_not_called()
    release.assert_not_awaited()

    await strategy._reconcile_positions()
    strategy.position_manager.remove_position.assert_called_once_with("ARIAUSDT")
    release.assert_awaited_once()
    strategy._save_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_retains_flat_position_with_leftover_protection():
    strategy = object.__new__(HRSStrategy)
    strategy.db = MagicMock(pool=object())
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_position = AsyncMock(return_value=[])
    strategy.binance_client.get_open_orders = AsyncMock(return_value=[])
    strategy.binance_client.get_open_algo_orders = AsyncMock(return_value=[
        {"symbol": "ARIAUSDT", "algoId": 100, "reduceOnly": True},
    ])
    strategy._reconcile_hrs_reservations = AsyncMock()
    strategy._writeback_pnl_for_full_close = AsyncMock(return_value=__import__("decimal").Decimal("0.17484"))
    strategy.position_manager = MagicMock()
    strategy.position_manager.get_all_positions.return_value = {
        "ARIAUSDT": {"direction": "short", "entry_price": .23276,
                     "entry_quantity": 31, "atr": .01}
    }
    strategy._zero_qty_threshold = .0001

    await strategy._reconcile_positions()

    strategy.position_manager.remove_position.assert_not_called()
    strategy._reconcile_hrs_reservations.assert_not_awaited()


@pytest.mark.asyncio
async def test_allocation_enforcement_never_recloses_flat_pending_position():
    strategy = object.__new__(HRSStrategy)
    strategy.position_manager = MagicMock()
    strategy.position_manager.cancel_all_orders = AsyncMock()
    strategy.position_manager.get_all_positions.return_value = {
        "ARIAUSDT": {"direction": "short", "entry_price": .23276,
                     "entry_quantity": 31}
    }
    strategy.trading_executor = MagicMock(leverage=2)
    strategy.trading_executor.close_position = AsyncMock()
    strategy._get_exchange_position_qty = AsyncMock(return_value=0.0)
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_ticker = AsyncMock(return_value={"lastPrice": "0.22712"})
    strategy.capital_mgr = MagicMock()
    strategy._writeback_pnl_for_full_close = AsyncMock(return_value=None)
    strategy._save_state = AsyncMock()

    async def enforce(_margins, close):
        return await close("ARIAUSDT")

    strategy.capital_mgr.enforce_existing_positions = AsyncMock(side_effect=enforce)
    assert await strategy._enforce_allocation_holdings() is False
    strategy.trading_executor.close_position.assert_not_awaited()
