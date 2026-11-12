from decimal import Decimal
import json
import sqlite3

import pytest

from strategies.grid.execution.dashboard_snapshot import calculate_snapshot
from strategies.grid.execution.dashboard_snapshot import cached_funding
from strategies.grid.execution.dashboard_snapshot import collect
from strategies.grid.execution.store import migrate


def base():
    return dict(
        epochs=[{"id": 7, "created_at_ms": 1000}],
        fills=[{"exchange_order_id": "11", "trade_id": "21", "epoch_id": 7,
                "side": "BUY", "quantity": "0.02", "price": "2000"}],
        facts=[{"order_id": "11", "trade_id": "21", "epoch_id": 7,
                "side": "BUY", "quantity": "0.02", "price": "2000",
                "realized_pnl": "0", "commission": "0.02", "commission_asset": "USDT",
                "created_at_ms": 1200}],
        funding=[{"tranId": 31, "time": 1100, "symbol": "ETHUSDT",
                  "asset": "USDT", "incomeType": "FUNDING_FEE", "income": "-0.01"}],
        position={"positionAmt": "0.02", "entryPrice": "2000", "markPrice": "2010",
                  "unRealizedProfit": "0.2", "positionSide": "BOTH"},
        engine={"state": "ACTIVE", "risk_status": "ALLOW", "stream_ready": True},
        open_orders=2, now_ms=2000,
    )


def test_subaccount_snapshot_keeps_funding_fees_and_unrealized_separate():
    snapshot = calculate_snapshot(**base())
    assert snapshot["account_scope"] == "grid_subaccount_pm"
    assert snapshot["stage_pnl_usdt"] == "0.17"
    assert snapshot["cumulative_pnl_usdt"] == "0.17"
    assert snapshot["realized_net_usdt"] == "-0.02"
    assert snapshot["funding_usdt"] == "-0.01"
    assert snapshot["unrealized_pnl_usdt"] == "0.2"
    assert snapshot["fill_count"] == 1
    assert snapshot["open_order_count"] == 2
    assert snapshot["last_trade"]["trade_id"] == "21"


def test_snapshot_rejects_unmatched_pm_fact_and_position_drift():
    data = base()
    data["facts"] = []
    with pytest.raises(ValueError, match="unmatched"):
        calculate_snapshot(**data)
    data = base()
    data["position"]["positionAmt"] = "0"
    with pytest.raises(ValueError, match="position"):
        calculate_snapshot(**data)


def test_flat_no_grid_trade_is_verified_zero_not_main_account_zero():
    data = base()
    data.update(epochs=[], fills=[], facts=[], funding=[], open_orders=0,
                position={"positionAmt": "0", "positionSide": "BOTH"})
    result = calculate_snapshot(**data)
    assert result["cumulative_pnl_usdt"] == "0"
    assert result["fill_count"] == 0
    assert result["active_epoch_id"] is None


def test_foreign_funding_and_non_usdt_commission_fail_closed():
    data = base()
    data["funding"][0]["symbol"] = "BTCUSDT"
    with pytest.raises(ValueError, match="funding"):
        calculate_snapshot(**data)
    data = base()
    data["facts"][0]["commission_asset"] = "BNB"
    with pytest.raises(ValueError, match="commission"):
        calculate_snapshot(**data)


@pytest.mark.asyncio
async def test_funding_cache_advances_without_replaying_full_history(tmp_path):
    class Exchange:
        def __init__(self): self.calls = []
        async def income(self, start, end):
            self.calls.append((start, end))
            return ([{"tranId": 1, "time": 1100, "symbol": "ETHUSDT",
                      "asset": "USDT", "incomeType": "FUNDING_FEE", "income": "-0.01"}]
                    if start <= 1100 <= end else [])
    exchange = Exchange()
    path = tmp_path / "funding.json"
    assert len(await cached_funding(exchange, path, 1000, 5_000_000)) == 1
    assert len(await cached_funding(exchange, path, 1000, 7_000_000)) == 1
    assert exchange.calls[1][0] > 1000


@pytest.mark.asyncio
async def test_collect_reads_empty_live_schema_without_trade_writes(tmp_path):
    execution = tmp_path / "execution.sqlite3"
    notices = tmp_path / "notices.sqlite3"
    heartbeat = tmp_path / "heartbeat.json"
    migrate(execution)
    with sqlite3.connect(notices) as db:
        db.execute("CREATE TABLE fact (order_id TEXT, trade_id TEXT, epoch_id INTEGER, "
                   "side TEXT, quantity TEXT, price TEXT, realized_pnl TEXT, commission TEXT, "
                   "commission_asset TEXT, created_at_ms INTEGER)")
    heartbeat.write_text(json.dumps({"updated_at_ms": 1000, "state": "FLAT",
                                     "risk_status": "ALLOW", "stream_ready": True}))
    class Exchange:
        async def position(self): return [{"positionAmt": "0", "positionSide": "BOTH"}]
        async def open_orders(self): return []
    result = await collect(execution, notices, heartbeat, Exchange(), 2000)
    assert result["cumulative_pnl_usdt"] == "0"
    assert result["engine_state"] == "FLAT"
    assert result["open_order_count"] == 0
