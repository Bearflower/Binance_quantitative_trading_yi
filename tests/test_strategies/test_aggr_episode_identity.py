"""An add order must not replace the position episode's first entry order."""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from strategies.btc_eth_aggressive.strategy import BTCEthStrategy
from shared.protection_retry import merge_position


def test_first_entry_order_is_stable_across_add():
    signal = {
        "entry_price": 100, "timestamp": datetime(2026, 11, 9),
        "direction": "LONG", "atr": 2, "grade": "A",
    }
    first = SimpleNamespace(order_id=1001, avg_price=Decimal("100"))
    pos = BTCEthStrategy._build_position_state(signal, first, Decimal("1"))
    assert pos.episode_entry_order_id == 1001

    second = SimpleNamespace(order_id=1002, avg_price=Decimal("110"))
    merge_position(pos, signal, second, Decimal("2"))

    assert pos.entry_order_id == 1002
    assert pos.episode_entry_order_id == 1001


@pytest.mark.asyncio
async def test_stop_binding_requires_known_episode_and_does_not_block_protection():
    strategy = object.__new__(BTCEthStrategy)
    strategy.db_manager = object()
    strategy.strategy_name = "btc_eth_aggressive"
    strategy.positions = {"ETHUSDT": SimpleNamespace(episode_entry_order_id=None)}
    strategy._unbound_stop_algo_ids = {}
    with patch("strategies.btc_eth_aggressive.strategy.record_stop_binding", new=AsyncMock()) as bind:
        await strategy._record_stop_binding("ETHUSDT", "SELL", Decimal("0.3"), 2001)
        bind.assert_not_awaited()

        strategy.positions["ETHUSDT"].episode_entry_order_id = 1001
        bind.side_effect = RuntimeError("database unavailable")
        await strategy._record_stop_binding("ETHUSDT", "SELL", Decimal("0.3"), 2001)
        bind.assert_awaited_once()
        assert strategy._unbound_stop_algo_ids == {"ETHUSDT": {"2001"}}
        assert await strategy._stop_entry_gate("ETHUSDT") == (False, "STOP binding missing")


@pytest.mark.asyncio
async def test_new_stop_placement_records_candidate_binding():
    strategy = object.__new__(BTCEthStrategy)
    strategy.db_manager = None
    strategy.binance = AsyncMock()
    strategy.binance.place_conditional_order.return_value = {"algoId": 2001}
    strategy._record_stop_binding = AsyncMock()
    order_id = await strategy._place_conditional_order_and_record(
        "ETHUSDT", "SELL", "STOP", Decimal("98"), Decimal("97"),
        Decimal("0.3"), "STOP_LOSS", "btc_eth_aggressive",
    )
    assert order_id == 2001
    strategy._record_stop_binding.assert_awaited_once_with(
        "ETHUSDT", "SELL", Decimal("0.3"), 2001,
    )


@pytest.mark.asyncio
async def test_new_entry_gate_blocks_unresolved_and_uses_verified_stop_time():
    strategy = object.__new__(BTCEthStrategy)
    strategy.db_manager = AsyncMock()
    strategy.db_manager.fetch_one.return_value = None
    strategy.instance_id = "btc_eth_aggressive"
    strategy.strategy_name = "btc_eth_aggressive"
    strategy.risk_config = {"frequency_control": {"symbol_cooldown_hours": 48}}
    clock = datetime.fromtimestamp(4, tz=timezone.utc)
    with patch.dict("os.environ", {"TRADE_ACCOUNT_ID": "default"}), patch(
        "strategies.btc_eth_aggressive.strategy.unresolved_stop_binding_exists",
        new=AsyncMock(return_value=True),
    ) as unresolved, patch(
        "strategies.btc_eth_aggressive.strategy.latest_verified_stop_time_ms",
        new=AsyncMock(return_value=3000),
    ) as latest:
        assert await strategy._stop_entry_gate("ETHUSDT", clock) == (
            False, "STOP evidence pending review",
        )
        latest.assert_not_awaited()
        unresolved.return_value = False
        allowed, reason = await strategy._stop_entry_gate("ETHUSDT", clock)
        assert not allowed and "verified STOP cooldown" in reason
        allowed, reason = await strategy._stop_entry_gate(
            "ETHUSDT", datetime.fromtimestamp(3000 / 1000 + 49 * 3600, tz=timezone.utc),
        )
        assert allowed and reason == ""


@pytest.mark.asyncio
async def test_restart_gate_reads_persisted_unbound_stop_flag():
    strategy = object.__new__(BTCEthStrategy)
    strategy.db_manager = AsyncMock()
    strategy.db_manager.fetch_one.return_value = {
        "state_data": {"positions": {"ETHUSDT": {"unbound_stop_algo_ids": [2001]}}}
    }
    strategy.instance_id = "btc_eth_aggressive"
    strategy.strategy_name = "btc_eth_aggressive"
    with patch.dict("os.environ", {"TRADE_ACCOUNT_ID": "default"}):
        assert await strategy._stop_entry_gate("ETHUSDT") == (
            False, "STOP binding missing in saved position",
        )


@pytest.mark.asyncio
async def test_execute_signal_rechecks_stop_gate_before_new_order():
    strategy = object.__new__(BTCEthStrategy)
    strategy.frequency_controller = SimpleNamespace(state_ready=True)
    strategy.circuit_breaker = None
    strategy.positions = {}
    strategy._stop_entry_gate = AsyncMock(return_value=(False, "STOP evidence pending review"))
    strategy._open_new_position = AsyncMock(return_value=True)
    signal = {"symbol": "ETHUSDT", "direction": "LONG"}
    with patch("strategies.btc_eth_aggressive.strategy.entry_allowed",
               new=AsyncMock(return_value=True)):
        assert not await strategy.execute_signal(signal)
    assert signal["skip_reason"] == "STOP evidence pending review"
    strategy._open_new_position.assert_not_awaited()
