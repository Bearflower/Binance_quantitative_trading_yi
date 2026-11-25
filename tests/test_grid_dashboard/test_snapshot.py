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


@pytest.mark.asyncio
async def test_classic_snapshot_requires_matching_account_and_signed_trade_facts(tmp_path):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from strategies.grid.execution.store import DecisionStore

    path = tmp_path / "classic.sqlite"
    migrate(path)
    store = DecisionStore(path)
    identity = "classic:sub"
    store.bind_account(identity)
    epoch = store.create_epoch("start")
    store.conn.execute("UPDATE grid_epoch SET created_at_ms=1000 WHERE id=?", (epoch,))
    store.prepare_order(epoch, 1, "ENTRY", "BUY", Decimal("2000"), Decimal(".02"), "eg-1")
    store.record_order("eg-1", "11", "FILLED", Decimal(".02"))
    store.record_fill("eg-1", "11", "21", Decimal(".02"), Decimal("2000"))
    store.close()
    beat = tmp_path / "heartbeat.json"
    beat.write_text(json.dumps({"account_identity": identity, "updated_at_ms": 1900}))
    get_trades = AsyncMock(return_value=[
        {"symbol": "ETHUSDT", "orderId": 11, "id": 21, "side": "BUY", "qty": ".02",
         "price": "2000", "realizedPnl": "0", "commission": "0.02", "commissionAsset": "USDT",
         "time": 1200}])
    exchange = SimpleNamespace(pm=False, journal_identity=AsyncMock(return_value=identity),
                               client=SimpleNamespace(get_user_trades=get_trades),
                               position=AsyncMock(return_value=[base()["position"]]),
                               open_orders=AsyncMock(return_value=[]), income=AsyncMock(return_value=[]))
    for now in (2000, 2500):
        snapshot = await collect(path, tmp_path / "unused-notices", beat, exchange, now,
                                 tmp_path / "funding.json")
        assert snapshot["account_scope"] == "grid_subaccount_classic"
        assert snapshot["realized_net_usdt"] == "-0.02"
    get_trades.assert_awaited_once_with("ETHUSDT", order_id="11", limit=1000)
    exchange.journal_identity.return_value = "classic:other"
    with pytest.raises(ValueError, match="identity"):
        await collect(path, None, beat, exchange, 3000)
@pytest.mark.parametrize("previous_time, expected_time", [(1150, 1150), (None, 1200)])
def test_snapshot_backfill_uses_exchange_time(previous_time, expected_time):
    data = base()
    data["facts"][0]["trade_time_ms"] = previous_time
    data["facts"].append(dict(data["facts"][0], trade_id="20", trade_time_ms=1100,
                              created_at_ms=1800))
    data["fills"].append(dict(data["fills"][0], trade_id="20"))
    data["position"]["positionAmt"] = "0.04"
    recent = calculate_snapshot(**data)["last_trade"]
    assert (recent["trade_id"], recent["time_ms"]) == ("21", expected_time)


@pytest.mark.asyncio
async def test_pending_snapshot_reports_once_then_recovers(monkeypatch,capsys):
    import asyncio
    from unittest.mock import AsyncMock,Mock
    from strategies.grid.execution import dashboard_snapshot as module
    exchange=Mock(close=AsyncMock())
    monkeypatch.setattr(module.GridExchange,"readonly_from_env",lambda settings:exchange)
    monkeypatch.setattr(module,"collect",AsyncMock(side_effect=[
        module.SnapshotPending("facts pending"),module.SnapshotPending("facts pending"),{"version":1}]))
    write=Mock();monkeypatch.setattr(module,"write_snapshot",write)
    monkeypatch.setattr(module.asyncio,"sleep",AsyncMock(side_effect=[None,None,asyncio.CancelledError()]))
    with pytest.raises(asyncio.CancelledError):
        await module.main()
    output=capsys.readouterr()
    assert output.out.count("snapshot waiting")==1
    assert output.out.count("snapshot recovered")==1
    assert "unavailable" not in output.err
    write.assert_called_once()
    exchange.close.assert_awaited_once()


def _live_evidence(tmp_path):
    """A real journal and notification DB, updated between snapshot reads."""
    from strategies.grid.execution.store import DecisionStore
    execution = tmp_path / "execution.sqlite3"
    notices = tmp_path / "notices.sqlite3"
    heartbeat = tmp_path / "heartbeat.json"
    migrate(execution)
    store = DecisionStore(execution)
    epoch = store.create_epoch("signal")
    store.prepare_order(epoch, 0, "ENTRY", "BUY", Decimal("2000"), Decimal("0.02"), "eg-buy")
    store.record_fill("eg-buy", "11", "21", Decimal("0.02"), Decimal("2000"))
    with sqlite3.connect(notices) as db:
        db.execute("CREATE TABLE fact (order_id TEXT, trade_id TEXT, epoch_id INTEGER, "
                   "side TEXT, quantity TEXT, price TEXT, realized_pnl TEXT, commission TEXT, "
                   "commission_asset TEXT, created_at_ms INTEGER)")
        db.execute("INSERT INTO fact VALUES ('11','21',?,'BUY','0.02','2000','0','0.02','USDT',1200)",
                   (epoch,))
    heartbeat.write_text(json.dumps({"updated_at_ms": store.epoch(epoch)["created_at_ms"], "state": "ACTIVE"}))

    def close_position():
        store.prepare_order(epoch, -1, "EXIT", "SELL", Decimal("2010"), Decimal("0.02"), "eg-sell")
        store.record_fill("eg-sell", "12", "22", Decimal("0.02"), Decimal("2010"))
        with sqlite3.connect(notices) as db:
            db.execute("INSERT INTO fact VALUES ('12','22',?,'SELL','0.02','2010','0.2','0.02','USDT',1300)",
                       (epoch,))

    return execution, notices, heartbeat, store, close_position


@pytest.mark.asyncio
async def test_collect_resamples_when_exit_lands_between_local_and_pm_reads(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from strategies.grid.execution import dashboard_snapshot as module
    execution, notices, heartbeat, store, close_position = _live_evidence(tmp_path)
    class Exchange:
        calls = 0
        async def position(self):
            self.calls += 1
            if self.calls == 1:
                close_position()
            return [{"positionAmt": "0", "positionSide": "BOTH"}]
        async def open_orders(self): return []
        async def income(self, start, end): return []
    exchange = Exchange()
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    try:
        result = await collect(execution, notices, heartbeat, exchange, store.epoch(1)["created_at_ms"] + 2000)
        assert result["net_qty"] == "0"
        assert result["fill_count"] == 2
        assert Decimal(result["cumulative_pnl_usdt"]) == Decimal("0.16")
        assert exchange.calls == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_collect_resamples_when_notifier_overtakes_old_fill_read(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from strategies.grid.execution import dashboard_snapshot as module
    execution, notices, heartbeat, store, close_position = _live_evidence(tmp_path)
    original_read = module._read_sqlite
    changed = False
    def interleaved_read(path, query):
        nonlocal changed
        if path == notices and not changed:
            changed = True
            close_position()
        return original_read(path, query)
    monkeypatch.setattr(module, "_read_sqlite", interleaved_read)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    class Exchange:
        async def position(self): return [{"positionAmt": "0", "positionSide": "BOTH"}]
        async def open_orders(self): return []
        async def income(self, start, end): return []
    try:
        result = await collect(execution, notices, heartbeat, Exchange(), store.epoch(1)["created_at_ms"] + 2000)
        assert result["fill_count"] == 2
        assert result["net_qty"] == "0"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_collect_handles_new_epoch_committed_between_epoch_and_fill_queries(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from strategies.grid.execution import dashboard_snapshot as module
    execution, notices, heartbeat, store, _ = _live_evidence(tmp_path)
    original_connect = sqlite3.connect
    changed = False
    def trace(query):
        nonlocal changed
        if "FROM fill f JOIN order_intent" in query and not changed:
            changed = True
            epoch = store.create_epoch("new-signal")
            store.prepare_order(epoch, 0, "ENTRY", "BUY", Decimal("2000"), Decimal("0.02"), "eg-new")
            store.record_fill("eg-new", "12", "22", Decimal("0.02"), Decimal("2000"))
            with original_connect(notices) as db:
                db.execute("INSERT INTO fact SELECT '12','22',?,'BUY',quantity,price,"
                           "realized_pnl,commission,commission_asset,created_at_ms FROM fact", (epoch,))
    def traced_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.set_trace_callback(trace)
        return conn
    monkeypatch.setattr(module.sqlite3, "connect", traced_connect)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    exchange = AsyncMock()
    exchange.position.return_value = [{"positionAmt": "0.04", "positionSide": "BOTH"}]
    exchange.open_orders.return_value = []
    exchange.income.return_value = []
    try:
        result = await collect(execution, notices, heartbeat, exchange, store.epoch(1)["created_at_ms"] + 2000)
        assert result["fill_count"] == 2
        assert result["phase_epoch_id"] == 2
        assert result["net_qty"] == "0.04"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_collect_persistent_position_drift_still_fails_after_bounded_resampling(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from strategies.grid.execution import dashboard_snapshot as module
    execution, notices, heartbeat, store, _ = _live_evidence(tmp_path)
    exchange = AsyncMock()
    exchange.position.return_value = [{"positionAmt": "0", "positionSide": "BOTH"}]
    exchange.open_orders.return_value = []
    exchange.income.return_value = []
    sleep = AsyncMock()
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    try:
        with pytest.raises(ValueError, match="position differs"):
            await collect(execution, notices, heartbeat, exchange, store.epoch(1)["created_at_ms"] + 2000)
        assert exchange.position.await_count == 3
        assert sleep.await_count == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_snapshot_failure_logs_validation_reason_and_recovery(monkeypatch, capsys):
    import asyncio
    from unittest.mock import AsyncMock, Mock
    from strategies.grid.execution import dashboard_snapshot as module
    exchange = Mock(close=AsyncMock())
    monkeypatch.setattr(module.GridExchange, "readonly_from_env", lambda settings: exchange)
    monkeypatch.setattr(module, "collect", AsyncMock(side_effect=[
        module.SnapshotConflict("PM position differs from executor fills"), {"version": 1}]))
    write = Mock()
    monkeypatch.setattr(module, "write_snapshot", write)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()]))
    with pytest.raises(asyncio.CancelledError):
        await module.main()
    output = capsys.readouterr()
    assert "PM position differs from executor fills" in output.err
    assert "ERROR:grid dashboard snapshot unavailable" in output.err
    assert output.out.count("snapshot recovered") == 1
    write.assert_called_once()
    exchange.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_evidence, expected_reads", [("extra_fact", 3), ("wrong_quantity", 1)])
async def test_unresolved_or_invalid_trade_evidence_never_becomes_valid(tmp_path, monkeypatch,
                                                                       bad_evidence, expected_reads):
    from unittest.mock import AsyncMock
    from strategies.grid.execution import dashboard_snapshot as module
    execution, notices, heartbeat, store, _ = _live_evidence(tmp_path)
    with sqlite3.connect(notices) as db:
        if bad_evidence == "extra_fact":
            db.execute("INSERT INTO fact SELECT '12','22',epoch_id,'SELL',quantity,price,"
                       "realized_pnl,commission,commission_asset,created_at_ms FROM fact")
        else:
            db.execute("UPDATE fact SET quantity='0.03'")
    exchange = AsyncMock()
    exchange.position.return_value = [{"positionAmt": "0.02", "positionSide": "BOTH"}]
    exchange.open_orders.return_value = []
    exchange.income.return_value = []
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    try:
        with pytest.raises(ValueError, match="unmatched"):
            await collect(execution, notices, heartbeat, exchange, store.epoch(1)["created_at_ms"] + 2000)
        assert exchange.position.await_count == expected_reads
    finally:
        store.close()


@pytest.mark.asyncio
async def test_arbitrary_transport_validation_message_is_not_logged(monkeypatch, capsys):
    import asyncio
    from unittest.mock import AsyncMock, Mock
    from strategies.grid.execution import dashboard_snapshot as module
    exchange = Mock(close=AsyncMock())
    monkeypatch.setattr(module.GridExchange, "readonly_from_env", lambda settings: exchange)
    monkeypatch.setattr(module, "collect", AsyncMock(side_effect=ValueError("signature=do-not-log")))
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await module.main()
    output = capsys.readouterr()
    assert "signature=" not in output.err
    assert "unavailable: ValueError" in output.err
