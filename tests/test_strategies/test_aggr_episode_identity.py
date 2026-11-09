"""An add order must not replace the position episode's first entry order."""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from strategies.btc_eth_aggressive.strategy import BTCEthStrategy
from shared.protection_retry import merge_position
from shared.accounting.stop_intent_journal import StopIntentJournal


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


@pytest.mark.asyncio
async def test_stop_intent_is_fsynced_before_exchange_post_and_resolved_after_binding(tmp_path):
    strategy = object.__new__(BTCEthStrategy)
    strategy.db_manager = None
    strategy.strategy_name = "btc_eth_aggressive"
    strategy.positions = {"ETHUSDT": SimpleNamespace(episode_entry_order_id=100)}
    strategy._unbound_stop_algo_ids = {}
    strategy._stop_journal = StopIntentJournal(tmp_path / "stop-intents.jsonl")
    strategy._record_stop_binding = AsyncMock(return_value=True)
    strategy.binance = AsyncMock()

    async def post(*args, **kwargs):
        client_id = kwargs["clientAlgoId"]
        assert strategy._stop_journal.pending("ETHUSDT")[0]["client_algo_id"] == client_id
        return {"algoId": 200, "clientAlgoId": client_id, "quantity": "0.3"}

    strategy.binance.place_conditional_order.side_effect = post
    with patch.dict("os.environ", {"TRADE_ACCOUNT_ID": "default"}):
        result = await strategy._place_conditional_order_and_record(
            "ETHUSDT", "SELL", "STOP", Decimal("98"), Decimal("97"),
            Decimal("0.3"), "STOP_LOSS", "btc_eth_aggressive",
        )
    assert result == 200
    assert StopIntentJournal(strategy._stop_journal.path).pending("ETHUSDT") == []


@pytest.mark.asyncio
async def test_unknown_stop_post_leaves_durable_intent_and_blocks_reentry(tmp_path):
    strategy = object.__new__(BTCEthStrategy)
    strategy.db_manager = None
    strategy.strategy_name = "btc_eth_aggressive"
    strategy.positions = {"ETHUSDT": SimpleNamespace(episode_entry_order_id=100)}
    strategy._unbound_stop_algo_ids = {}
    strategy._stop_journal = StopIntentJournal(tmp_path / "stop-intents.jsonl")
    strategy.binance = AsyncMock()
    strategy.binance.place_conditional_order.side_effect = TimeoutError("unknown write")
    with patch.dict("os.environ", {"TRADE_ACCOUNT_ID": "default"}):
        result = await strategy._place_conditional_order_and_record(
            "ETHUSDT", "SELL", "STOP", Decimal("98"), Decimal("97"),
            Decimal("0.3"), "STOP_LOSS", "btc_eth_aggressive",
        )
    assert result is None
    restarted = object.__new__(BTCEthStrategy)
    restarted._stop_journal = StopIntentJournal(strategy._stop_journal.path)
    restarted.strategy_name = "btc_eth_aggressive"
    with patch.dict("os.environ", {"TRADE_ACCOUNT_ID": "default"}):
        assert await restarted._stop_entry_gate("ETHUSDT") == (
            False, "STOP journal intent unresolved",
        )


@pytest.mark.asyncio
async def test_journal_resolve_failure_after_exchange_ack_keeps_order_id(tmp_path):
    strategy = object.__new__(BTCEthStrategy)
    strategy.db_manager = None
    strategy.strategy_name = "btc_eth_aggressive"
    strategy.positions = {"ETHUSDT": SimpleNamespace(episode_entry_order_id=100)}
    strategy._unbound_stop_algo_ids = {}
    strategy._stop_journal = StopIntentJournal(tmp_path / "stop-intents.jsonl")
    strategy._record_stop_binding = AsyncMock(return_value=True)
    strategy.binance = AsyncMock()

    async def post(*args, **kwargs):
        return {"algoId": 200, "clientAlgoId": kwargs["clientAlgoId"], "quantity": "0.3"}

    strategy.binance.place_conditional_order.side_effect = post
    with patch.dict("os.environ", {"TRADE_ACCOUNT_ID": "default"}), patch.object(
        strategy._stop_journal, "resolve", side_effect=OSError("disk unavailable")
    ):
        result = await strategy._place_conditional_order_and_record(
            "ETHUSDT", "SELL", "STOP", Decimal("98"), Decimal("97"),
            Decimal("0.3"), "STOP_LOSS", "btc_eth_aggressive",
        )
    assert result == 200
    assert strategy._unbound_stop_algo_ids == {"ETHUSDT": {"200"}}
    assert len(strategy._stop_journal.pending("ETHUSDT")) == 1


@pytest.mark.asyncio
async def test_legacy_stop_without_episode_stays_in_persistent_review(tmp_path):
    strategy = object.__new__(BTCEthStrategy)
    strategy.db_manager = None
    strategy.strategy_name = "btc_eth_aggressive"
    strategy.positions = {"ETHUSDT": SimpleNamespace(episode_entry_order_id=None)}
    strategy._unbound_stop_algo_ids = {}
    strategy._stop_journal = StopIntentJournal(tmp_path / "stop-intents.jsonl")
    strategy.binance = AsyncMock()

    async def post(*args, **kwargs):
        return {"algoId": 200, "clientAlgoId": kwargs["clientAlgoId"], "quantity": "0.3"}

    strategy.binance.place_conditional_order.side_effect = post
    with patch.dict("os.environ", {"TRADE_ACCOUNT_ID": "default"}):
        assert await strategy._place_conditional_order_and_record(
            "ETHUSDT", "SELL", "STOP", Decimal("98"), Decimal("97"),
            Decimal("0.3"), "STOP_LOSS", "btc_eth_aggressive",
        ) == 200
    pending = StopIntentJournal(strategy._stop_journal.path).pending("ETHUSDT")
    assert len(pending) == 1 and pending[0]["episode_entry_order_id"] is None


@pytest.mark.asyncio
async def test_extreme_tightening_keeps_old_stop_when_journal_unavailable():
    strategy = object.__new__(BTCEthStrategy)
    strategy.risk_config = {"extreme_market": {"tighten_stop_atr": 1}}
    strategy._get_symbol_precision = AsyncMock()
    strategy._prepare_stop_intent = Mock(return_value=None)
    strategy.binance = AsyncMock()
    position = SimpleNamespace(direction="LONG", atr=Decimal("2"),
                               current_quantity=Decimal("1"), stop_loss_order_id=77,
                               episode_entry_order_id=100)

    await strategy._tighten_extreme_stop("ETHUSDT", position, Decimal("100"))

    strategy.binance.cancel_algo_order.assert_not_awaited()
    strategy.binance.place_conditional_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_journal_from_another_account_blocks_all_new_entries(tmp_path):
    journal = StopIntentJournal(tmp_path / "stop-intents.jsonl")
    journal.prepare(account_id="old-account", strategy="btc_eth_aggressive",
                    symbol="ETHUSDT", episode_entry_order_id=100,
                    stop_side="SELL", stop_quantity="0.3")
    strategy = object.__new__(BTCEthStrategy)
    strategy._stop_journal = journal
    strategy.strategy_name = "btc_eth_aggressive"
    with patch.dict("os.environ", {"TRADE_ACCOUNT_ID": "default"}):
        assert await strategy._stop_entry_gate("BTCUSDT") == (
            False, "STOP journal account mismatch",
        )
