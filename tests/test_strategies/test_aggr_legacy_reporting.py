from decimal import Decimal

import pytest

from strategies.btc_eth_aggressive.strategy import BTCEthStrategy


def test_verified_legacy_exchange_position_is_in_report_without_software_adoption():
    strategy = object.__new__(BTCEthStrategy)
    strategy.positions = {}
    strategy.binance_config = {"leverage": {"A": 5, "B": 3}}
    strategy._verified_legacy_positions = {
        "ETHUSDT": {
            "direction": "LONG", "entry_price": Decimal("2200"),
            "quantity": Decimal("0.023"), "mark_price": Decimal("2300"),
            "leverage": Decimal("5"), "entry_ms": 1793409086845,
            "hard_stop_covers": True,
        }
    }

    positions, margins, quantities = strategy.build_positions_report()

    assert "ETHUSDT" not in strategy.positions
    assert positions["ETHUSDT"]["management_mode"] == "exchange_protected_legacy"
    assert positions["ETHUSDT"]["entry_time"] == "2026-10-31T01:11:26.845000+00:00"
    assert quantities["ETHUSDT"] == 0.023
    assert margins["ETHUSDT"] == 0.023 * 2300 / 5


@pytest.mark.asyncio
async def test_live_legacy_verification_populates_report_then_flat_clears_it():
    from unittest.mock import AsyncMock, MagicMock, patch

    strategy = object.__new__(BTCEthStrategy)
    strategy.positions = {}
    strategy.binance_config = {"leverage": {"A": 5}}
    strategy.min_position_amt = 0.00001
    strategy._untracked_stop_retry_symbols = set()
    strategy.my_record_name = "btc_eth_aggressive"
    strategy.db_manager = MagicMock()
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[{
        "symbol": "ETHUSDT", "positionAmt": "0.023", "entryPrice": "2200",
        "markPrice": "2300", "leverage": "5",
    }])
    strategy.binance.get_user_trades = AsyncMock(return_value=[
        {"id": 1, "time": 1793409086845, "side": "BUY", "qty": "0.023"},
    ])
    strategy.binance.get_open_algo_orders = AsyncMock(return_value=[{
        "symbol": "ETHUSDT", "side": "SELL", "orderType": "STOP",
        "reduceOnly": True, "quantity": "0.023", "triggerPrice": "2000",
    }])
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner",
               new=AsyncMock(return_value="btc_eth_aggressive")):
        assert await strategy._check_legacy_untracked_time_stop()
    assert strategy.build_positions_report()[2]["ETHUSDT"] == 0.023

    strategy.binance.get_position.return_value = []
    assert await strategy._check_legacy_untracked_time_stop()
    assert "ETHUSDT" not in strategy.build_positions_report()[2]
