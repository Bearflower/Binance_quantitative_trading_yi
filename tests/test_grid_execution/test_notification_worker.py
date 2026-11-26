from decimal import Decimal
import time
from unittest.mock import AsyncMock

import pytest

from strategies.grid.execution.notification_worker import NotificationWorker, healthcheck
from strategies.grid.execution.store import DecisionStore, migrate


def journal_with_fill(path):
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("signal", levels=(Decimal("2000"),),
                               stop_loss_low=Decimal("1900"),
                               stop_loss_high=Decimal("2100"))
    store.prepare_order(epoch, 0, "ENTRY", "BUY", Decimal("2000"),
                        Decimal("0.01"), "eg-test-1")
    store.record_order("eg-test-1", "101", "FILLED", Decimal("0.01"))
    store.record_fill("eg-test-1", "101", "501", Decimal("0.01"),
                      Decimal("2000"))
    store.close()


class FakeExchange:
    def __init__(self, trades):
        self.trades = trades
        self.order_trades = AsyncMock(side_effect=lambda *_: self.trades)
        self.income = AsyncMock(return_value=[])
        self.position = AsyncMock(return_value=[{
            "symbol": "ETHUSDT", "positionSide": "BOTH",
            "positionAmt": "0.01", "entryPrice": "2000",
            "markPrice": "2010", "unRealizedProfit": "0.1"}])


@pytest.mark.asyncio
async def test_owned_fill_sends_once_and_survives_restart(tmp_path):
    execution = tmp_path / "execution.sqlite3"
    notices = tmp_path / "notices.sqlite3"
    journal_with_fill(execution)
    trade = {"symbol": "ETHUSDT", "orderId": 101, "id": 501,
             "side": "BUY", "qty": "0.01", "price": "2000",
             "realizedPnl": "0", "commission": "0.01",
             "commissionAsset": "USDT"}
    exchange = FakeExchange([trade, dict(trade, orderId=999, id=999)])
    sender = AsyncMock(return_value=True)
    worker = NotificationWorker(execution, notices, exchange, sender)
    assert await worker.run_once() == 1
    assert sender.await_count == 1
    assert "当前网格阶段盈亏: +0.0900 USDT" in sender.await_args.args[0]
    worker.close()
    again = NotificationWorker(execution, notices, exchange, sender)
    assert await again.run_once() == 0
    assert sender.await_count == 1
    again.close()


@pytest.mark.asyncio
async def test_missing_pm_trade_keeps_fill_pending(tmp_path):
    execution = tmp_path / "execution.sqlite3"
    journal_with_fill(execution)
    sender = AsyncMock(return_value=True)
    worker = NotificationWorker(execution, tmp_path / "notices.sqlite3",
                                FakeExchange([]), sender)
    assert await worker.run_once() == 0
    sender.assert_not_awaited()
    worker.close()


@pytest.mark.asyncio
async def test_incomplete_pm_facts_block_other_fill_report(tmp_path):
    execution = tmp_path / "execution.sqlite3"
    journal_with_fill(execution)
    store = DecisionStore(execution)
    store.prepare_order(1, 1, "ENTRY", "SELL", Decimal("2010"),
                        Decimal("0.01"), "eg-test-2")
    store.record_order("eg-test-2", "102", "FILLED", Decimal("0.01"))
    store.record_fill("eg-test-2", "102", "502", Decimal("0.01"),
                      Decimal("2010"))
    store.close()
    first = {"symbol": "ETHUSDT", "orderId": 101, "id": 501,
             "side": "BUY", "qty": "0.01", "price": "2000",
             "realizedPnl": "0", "commission": "0.01",
             "commissionAsset": "USDT"}
    sender = AsyncMock(return_value=True)
    worker = NotificationWorker(execution, tmp_path / "notices.sqlite3",
                                FakeExchange([first]), sender)
    assert await worker.run_once() == 0
    sender.assert_not_awaited()
    worker.close()


@pytest.mark.asyncio
async def test_unknown_delivery_retries_same_trade_without_new_fact(tmp_path):
    execution = tmp_path / "execution.sqlite3"
    journal_with_fill(execution)
    trade = {"symbol": "ETHUSDT", "orderId": 101, "id": 501,
             "side": "BUY", "qty": "0.01", "price": "2000",
             "realizedPnl": "0", "commission": "0.01",
             "commissionAsset": "USDT"}
    sender = AsyncMock(side_effect=[None, True])
    worker = NotificationWorker(execution, tmp_path / "notices.sqlite3",
                                FakeExchange([trade]), sender)
    now = int(time.time() * 1000)
    assert await worker.run_once(now_ms=now) == 0
    assert await worker.run_once(now_ms=now + 31_000) == 1
    assert sender.await_count == 2
    worker.close()


@pytest.mark.asyncio
async def test_failed_earlier_report_blocks_newer_report_until_retry(tmp_path):
    execution = tmp_path / "execution.sqlite3"
    journal_with_fill(execution)
    trades = [{"symbol": "ETHUSDT", "orderId": 101, "id": 501,
               "side": "BUY", "qty": "0.01", "price": "2000",
               "realizedPnl": "0", "commission": "0.01",
               "commissionAsset": "USDT"},
              {"symbol": "ETHUSDT", "orderId": 102, "id": 502,
               "side": "SELL", "qty": "0.01", "price": "2010",
               "realizedPnl": "0.1", "commission": "0.01",
               "commissionAsset": "USDT"}]
    sender = AsyncMock(side_effect=[None, True, True])
    worker = NotificationWorker(execution, tmp_path / "notices.sqlite3",
                                FakeExchange(trades), sender)
    now = int(time.time() * 1000)
    assert await worker.run_once(now_ms=now) == 0
    store = DecisionStore(execution)
    store.prepare_order(1, 1, "ENTRY", "SELL", Decimal("2010"),
                        Decimal("0.01"), "eg-test-2")
    store.record_order("eg-test-2", "102", "FILLED", Decimal("0.01"))
    store.record_fill("eg-test-2", "102", "502", Decimal("0.01"),
                      Decimal("2010"))
    store.close()
    assert await worker.run_once(now_ms=now + 1000) == 0
    assert sender.await_count == 1
    assert await worker.run_once(now_ms=now + 31_000) == 2
    assert sender.await_count == 3
    worker.close()


@pytest.mark.asyncio
async def test_funding_is_included_once_and_foreign_asset_fails_closed(tmp_path):
    execution = tmp_path / "execution.sqlite3"
    journal_with_fill(execution)
    trade = {"symbol": "ETHUSDT", "orderId": 101, "id": 501,
             "side": "BUY", "qty": "0.01", "price": "2000",
             "realizedPnl": "0", "commission": "0.01",
             "commissionAsset": "USDT"}
    exchange = FakeExchange([trade])
    now = int(time.time() * 1000)
    exchange.income.return_value = [{"time": now, "incomeType": "FUNDING_FEE",
                                     "symbol": "ETHUSDT", "asset": "USDT",
                                     "income": "-0.02"}]
    sender = AsyncMock(return_value=True)
    worker = NotificationWorker(execution, tmp_path / "notices.sqlite3",
                                exchange, sender)
    assert await worker.run_once(now_ms=now) == 1
    assert "当前网格阶段盈亏: +0.0700 USDT" in sender.await_args.args[0]
    worker.close()

    exchange.income.return_value[0]["asset"] = "BNB"
    worker = NotificationWorker(execution, tmp_path / "another.sqlite3",
                                exchange, AsyncMock(return_value=True))
    with pytest.raises(ValueError, match="foreign funding"):
        await worker.run_once(now_ms=now)
    worker.close()


def test_notification_health_requires_recent_successful_cycle(tmp_path):
    path = tmp_path / "heartbeat.json"
    assert healthcheck(path, now_ms=100_000) == 1
    path.write_text('{"updated_at_ms":90000,"status":"ok"}')
    assert healthcheck(path, now_ms=100_000) == 0
    path.write_text('{"updated_at_ms":90000,"status":"error"}')
    assert healthcheck(path, now_ms=100_000) == 1
