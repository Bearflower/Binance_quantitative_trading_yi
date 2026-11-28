"""2026-11-26 调优与看板审查的离线回归测试。"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import socket
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard/backend"))

from ai_tuner.adapters.hrs_adapter import HRSAdapter
from ai_tuner.adapters.mtpcs_adapter import MTPCSAdapter
from ai_tuner.cleanup.orphan_cleanup import OrphanCleanupJob
from ai_tuner.deploy.config_operator import ConfigOperator
from ai_tuner.deploy.rollback_manager import RollbackManager
from ai_tuner.deploy.version_manager import VersionManager
from ai_tuner.feedback.effect_tracker import EffectTracker
from ai_tuner.main import StratTuneAI
from ai_tuner.memory.db_handler import MemoryDBHandler
from ai_tuner.scheduler.weekly_job import WeeklyTuningJob
from ai_tuner.parameters.manifest import ManifestRegistry
from ai_tuner.readiness.auditor import ReadinessAuditor
from shared.config_loader import load_strategy_config
import main_docker
import api.routes_docker as routes
import services.data_service_docker as services
from services.equity_snapshot_job import run_snapshot


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("regression tests must not connect to external services")
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect)


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("scoring:\n  a: 10\n  b: 20\n")
    return path


def test_partial_overrides_preserve_prior_approved_values(config_file):
    op = ConfigOperator()
    assert op.apply_overrides(str(config_file), {"scoring.a": 11})
    assert op.apply_overrides(str(config_file), {"scoring.b": {"from": 20, "to": 22}})
    assert load_strategy_config(str(config_file.parent))["scoring"] == {"a": 11, "b": 22}


@pytest.mark.parametrize("adjustments", [{}, {"scoring.a": None}, {"scoring..a": 12}, {"": 12}])
def test_invalid_overlay_does_not_replace_approved_version(config_file, adjustments):
    op = ConfigOperator()
    version = op.publish_overrides(str(config_file), {"scoring.a": 11})
    directory = config_file.parent / "tuning_overrides"
    before = sorted(path.name for path in directory.iterdir())
    assert not op.publish_overrides(str(config_file), adjustments, expected_version=version)
    assert (directory / ".active").read_text().strip() == version
    assert sorted(path.name for path in directory.iterdir()) == before
    assert load_strategy_config(str(config_file.parent))["scoring"]["a"] == 11


@pytest.mark.asyncio
@pytest.mark.parametrize("versions", [1, 2])
@pytest.mark.parametrize("failure", [None, "metadata", "notification"])
async def test_rollback_endpoint_restores_previous_active_layer(config_file, versions, failure):
    app = StratTuneAI()
    app.config = {"strategies": [{"strategy_id": "hrs", "config_path": str(config_file)}]}
    app.config_operator = ConfigOperator()
    app.rollback_manager = RollbackManager()
    app.db_handler = AsyncMock()
    app.db_handler.find_memory_by_version.return_value = {"id": 7}
    app.messenger = AsyncMock()
    if failure == "metadata":
        app.db_handler.record_overlay_rollback.side_effect = RuntimeError("offline metadata failure")
    if failure == "notification":
        app.messenger.send_rollback_notification.side_effect = RuntimeError("offline notification failure")
    assert app.config_operator.apply_overrides(str(config_file), {"scoring.a": 11})
    if versions == 2:
        assert app.config_operator.apply_overrides(str(config_file), {"scoring.b": 22})
    request = AsyncMock()
    request.json.return_value = {"strategy_id": "hrs", "expected_version": (config_file.parent / "tuning_overrides/.active").read_text().strip()}
    response = await app._handle_rollback(request)
    assert response.status == 200
    expected = {"a": 11 if versions == 2 else 10, "b": 20}
    assert load_strategy_config(str(config_file.parent))["scoring"] == expected
    app.db_handler.record_overlay_rollback.assert_awaited_once()
    payload = json.loads(response.text)
    assert payload["metadata_updated"] is (failure != "metadata")
    assert payload["notification_sent"] is (failure != "notification")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,selected", [("audit", None), ("shadow", "hrs"), ("live", "btc_eth_aggressive")])
async def test_approval_rechecks_current_mvp_gate(config_file, mode, selected):
    app = StratTuneAI()
    app.config = {"purpose_driven_mvp": {"mode": mode, "selected_strategy": selected, "candidates": ["hrs"]},
                  "strategies": [{"strategy_id": "hrs", "config_path": str(config_file)}]}
    app.config_operator = ConfigOperator()
    app.db_handler = AsyncMock()
    app.messenger = AsyncMock()
    app._find_pending_approval = AsyncMock(return_value={"id": 1, "ai_suggestions": {"adjustments": {"scoring.a": {"from": 10, "to": 11}}}})
    request = AsyncMock()
    request.json.return_value = {"strategy_id": "hrs", "action": "confirm"}
    response = await app._handle_approval(request)
    assert response.status == 409
    assert not (config_file.parent / "tuning_overrides/.active").exists()
    app.db_handler.mark_applied.assert_not_awaited()
    app.messenger.send_applied_notification.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mvp,allowed", [({"mode": "live", "selected_strategy": "hrs", "candidates": ["hrs"]}, False), ({}, True)])
async def test_approval_requires_qualification_for_candidate_and_keeps_legacy_path(config_file, mvp, allowed):
    app = StratTuneAI()
    app.config = {"purpose_driven_mvp": mvp, "strategies": [{"strategy_id": "hrs", "config_path": str(config_file)}]}
    app.config_operator = ConfigOperator()
    app.db_handler = AsyncMock()
    app.messenger = AsyncMock()
    app._find_pending_approval = AsyncMock(return_value={"id": 1, "ai_suggestions": {"adjustments": {"scoring.a": {"from": 10, "to": 11}}}})
    request = AsyncMock()
    request.json.return_value = {"strategy_id": "hrs", "action": "confirm"}
    response = await app._handle_approval(request)
    assert response.status == (200 if allowed else 409)
    assert load_strategy_config(str(config_file.parent))["scoring"]["a"] == (11 if allowed else 10)
    assert app.db_handler.mark_published.await_count == int(allowed)
    app.db_handler.mark_applied.assert_not_awaited()  # 策略 ACK 后才确认生效。


def test_manifest_from_matches_active_override(config_file):
    config_file.write_text("scoring:\n  entry_threshold: 6\n  short_entry_threshold: 5\n  technical:\n    min_total_score: 4\n    short_min_total_score: 2\n")
    hrs = HRSAdapter(None)
    hrs.config_path = str(config_file)
    hrs._strategy_cfg = {"parameter_manifest": "ai_tuner/parameters/manifests/hrs.yaml"}
    hrs._manifest_cache = ManifestRegistry(ROOT / "ai_tuner/parameters/manifests").load("hrs")
    assert ConfigOperator().apply_overrides(str(config_file), {"scoring.entry_threshold": 6.3})
    result = hrs.validate_params({"scoring.entry_threshold": {"from": 6.3, "to": 6.4}})
    assert result["valid"], result


@pytest.mark.asyncio
async def test_static_files_reject_encoded_traversal_and_symlink_escape(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("safe homepage")
    (frontend / "asset.js").write_text("safe asset")
    (tmp_path / "outside.txt").write_text("private")
    (frontend / "link.txt").symlink_to(tmp_path / "outside.txt")
    monkeypatch.setattr(main_docker, "FRONTEND_DIR", frontend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main_docker.app), base_url="http://offline") as client:
        assert (await client.get("/asset.js")).text == "safe asset"
        assert (await client.get("/app/route")).text == "safe homepage"
        for path in ("/..%2Foutside.txt", "/link.txt"):
            response = await client.get(path)
            assert response.status_code == 404
            assert "private" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange_positions,local_position,should_cancel", [
    (None, False, False), ({"BTCUSDT"}, False, False), (set(), False, True), (set(), True, False),
])
async def test_stale_state_needs_confirmed_absent_position(exchange_positions, local_position, should_cancel):
    job = OrphanCleanupJob(AsyncMock(), AsyncMock(), AsyncMock(), require_exchange_confirmation=True)
    order = {"strategy_name": "hrs", "symbol": "BTCUSDT", "order_type": "STOP_LOSS", "algo_id": 123, "order_id": None}
    job._ensure_table = AsyncMock()
    job._get_exchange_positions = AsyncMock(return_value=exchange_positions)
    job._query_strategy_states = AsyncMock(return_value={"hrs": {"symbols": {"BTCUSDT"}, "updated_at": datetime.utcnow() - timedelta(hours=3)}})
    job._strategy_has_position = AsyncMock(return_value=local_position)
    job._cancel_order = AsyncMock(return_value=(True, None))
    with patch("ai_tuner.cleanup.orphan_cleanup.get_open_orders", AsyncMock(return_value=[order])):
        await job.execute()
    assert job._cancel_order.await_count == int(should_cancel)


@pytest.mark.asyncio
async def test_dashboard_shares_pool_and_closes_at_lifespan_exit():
    db = MagicMock(connect=AsyncMock(), disconnect=AsyncMock())
    client = MagicMock(close=AsyncMock())
    with patch.object(services, "DatabaseManager", return_value=db) as factory, patch.object(services, "BinanceClient", return_value=client), patch.object(services, "TradeLogger"), patch.object(services, "verify_trading_mode", AsyncMock()), patch.object(services, "verify_collector_ledger", AsyncMock(return_value=None)):
        async with main_docker.lifespan(main_docker.app):
            request = Request({"type": "http", "app": main_docker.app})
            instances = [routes.get_data_service(request) for _ in range(3)]
            await asyncio.gather(*(service._ensure_initialized() for service in instances))
            assert instances[0] is instances[1] is instances[2]
            assert factory.call_count == 1
            db.disconnect.assert_not_awaited()
        db.disconnect.assert_awaited_once()
        client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_equity_api_failure_does_not_replace_snapshot():
    service = services.DataService()
    service._ensure_initialized = AsyncMock()
    service._binance_client = AsyncMock()
    service._binance_client.get_account_info.side_effect = TimeoutError("offline injected timeout")
    service._db_manager = AsyncMock()
    result = await run_snapshot(service)
    assert "error" in result
    service._db_manager.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("account,expected", [
    ({}, None),
    ({"totalWalletBalance": "100", "availableBalance": "30"}, None),
    ({"totalMarginBalance": "NaN", "availableBalance": "30"}, None),
    ({"totalMarginBalance": "inf", "availableBalance": "30"}, None),
    ({"totalMarginBalance": "10", "availableBalance": None}, None),
    ({"totalMarginBalance": "0", "availableBalance": "0"}, "0.00"),
    ({"totalWalletBalance": "100", "totalUnrealizedProfit": "5", "availableBalance": "30"}, "105.00"),
])
async def test_equity_snapshot_requires_valid_account_evidence(account, expected):
    service = services.DataService()
    service._ensure_initialized = AsyncMock()
    service._binance_client = AsyncMock()
    service._binance_client.get_account_info.return_value = account
    service._binance_client.get_position.return_value = []
    service._db_manager = AsyncMock()
    result = await run_snapshot(service)
    if expected is None:
        assert "error" in result
        service._db_manager.execute.assert_not_awaited()
    else:
        from decimal import Decimal
        assert Decimal(result["total_equity"]) == Decimal(expected)
        service._db_manager.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_raw_empty_pm_account_response_never_becomes_zero_snapshot():
    from shared.binance_api import BinanceClient
    service = services.DataService()
    service._ensure_initialized = AsyncMock()
    service._binance_client = object.__new__(BinanceClient)
    service._binance_client.use_unified_account = True
    service._binance_client._request = AsyncMock(return_value={})
    service._binance_client.get_position = AsyncMock(return_value=[])
    service._db_manager = AsyncMock()
    assert "error" in await run_snapshot(service)
    service._db_manager.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_readiness_preserves_zero_completeness():
    auditor = object.__new__(ReadinessAuditor)
    auditor._fetch_trading_window = AsyncMock(return_value={"closed_trades": 100, "complete_ratio": 0.0})
    count, ratio, identity_ratio = await auditor._query_trade_records("hrs", datetime.now(timezone.utc)-timedelta(days=90), datetime.now(timezone.utc))
    assert (count, ratio, identity_ratio) == (100, 0.0, 0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("approved_at,should_fill", [(datetime(2026, 11, 15, 23, 55), True), (datetime(2026, 11, 18, 12), False)])
async def test_feedback_observes_week_after_application(approved_at, should_fill):
    class Monday(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 23, 0, 5, tzinfo=tz)
    db = AsyncMock()
    db.fetch_all.return_value = [{"realized_pnl": 10, "symbol": "BTCUSDT"}]
    adapter = MTPCSAdapter(db)
    adapter._read_config = lambda: {}
    adapter._system_config = {}
    memory = AsyncMock()
    memory.find_memory_by_version.return_value = {"id": 1, "approved_at": approved_at, "full_report": {"performance": {"win_rate": 1.0, "total_pnl": 10}}, "post_win_rate": None, "post_total_pnl": None}
    version = MagicMock()
    version.get_active_version.return_value = "V20261115"
    with patch("ai_tuner.adapters.mtpcs_adapter.datetime", Monday), patch("ai_tuner.feedback.effect_tracker._now_shanghai", return_value=Monday.now(timezone(timedelta(hours=8)))):
        await EffectTracker({}, version).track_and_fill("btc_eth", adapter, memory)
    queried = db.fetch_all.await_args.args
    assert queried[2:4] == (datetime(2026, 11, 16), datetime(2026, 11, 23))
    assert memory.update_effect_tracking.await_count == int(should_fill)


def test_missing_active_version_blocks_new_override_without_losing_pointer(config_file):
    folder = config_file.parent / "tuning_overrides"
    folder.mkdir()
    (folder / ".active").write_text("V20260101")
    assert not ConfigOperator().apply_overrides(str(config_file), {"scoring.a": 11})
    assert (folder / ".active").read_text() == "V20260101"


def test_legacy_override_requires_explicit_rollback_target(config_file):
    folder = config_file.parent / "tuning_overrides"
    folder.mkdir()
    (folder / ".active").write_text("V20260101")
    (folder / "V20260101.yaml").write_text("scoring:\n  a: 11\n")
    op = ConfigOperator()
    with pytest.raises(FileNotFoundError):
        op.rollback_overlay(str(config_file))
    with pytest.raises(ValueError):
        op.rollback_overlay(str(config_file), "../config")
    assert op.rollback_overlay(str(config_file), "base") == "base"
    assert load_strategy_config(str(config_file.parent))["scoring"]["a"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_initialization_failure_closes_pool_before_retry(failure_type):
    db = MagicMock(connect=AsyncMock(), disconnect=AsyncMock())
    service = services.DataService()
    with patch.object(services, "DatabaseManager", return_value=db), patch.object(services, "BinanceClient", side_effect=failure_type("invalid client config")):
        with pytest.raises(failure_type):
            await service._ensure_initialized()
        db.disconnect.assert_awaited_once()
        assert service._db_manager is None


class MemoryStore:
    """由真实 SQL 驱动的发布、策略 ACK、回滚状态替身。"""
    def __init__(self):
        self.record = None

    async def fetch_one(self, query, *args):
        if "INSERT INTO trading.strategy_memory" in query:
            self.record = {"id": 7, "strategy_id": args[0], "active_version": args[8],
                           "ai_suggestions": json.loads(args[7]), "is_applied": False,
                           "is_rolled_back": False, "approved_by": None}
            return {"id": 7}
        if "approved_by='publishing'" in query:
            if self.record["approved_by"] is None:
                self.record["approved_by"] = "publishing"
                return {"id": self.record["id"]}
        if "AND active_version = $2" in query:
            if self.record and self.record["strategy_id"] == args[0] and self.record["active_version"] == args[1] and self.record["is_applied"] and not self.record["is_rolled_back"]:
                return dict(self.record)
        return None

    async def execute(self, query, *args):
        if "SET active_version=$2" in query:
            assert args[0] == self.record["id"]
            self.record.update(active_version=args[1], approved_by=args[2])
        elif "SET is_applied=TRUE" in query:
            assert args[:2] == (self.record["strategy_id"], self.record["active_version"])
            self.record["is_applied"] = True
        elif "SET is_rolled_back=(active_version=$2)" in query:
            assert args[0] == self.record["strategy_id"]
            self.record["is_rolled_back"] = self.record["active_version"] == args[1]
            self.record["is_applied"] = False
        else:
            raise AssertionError(f"unexpected memory SQL: {query}")


@pytest.mark.asyncio
@pytest.mark.parametrize("same_day", [False, True])
async def test_manual_approval_binds_actual_version_and_rollback_finds_memory(config_file, same_day):
    store = MemoryStore()
    handler = MemoryDBHandler(store)
    handler._schema = "trading"
    op = ConfigOperator()
    class ApplyDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 26, 12)
    with patch("ai_tuner.deploy.config_operator.datetime", ApplyDay):
        if same_day:
            assert op.apply_overrides(str(config_file), {"scoring.b": 22})
        predicted = "V20261126" if same_day else "V20261125"
        await handler.save_memory("hrs", "HRS", {}, {"adjustments": {"scoring.a": {"from": 10, "to": 11}}}, active_version=predicted)
        app = StratTuneAI()
        app.config = {"strategies": [{"strategy_id": "hrs", "config_path": str(config_file)}]}
        app.config_operator, app.db_handler, app.messenger = op, handler, AsyncMock()
        app._find_pending_approval = AsyncMock(return_value=dict(store.record))
        request = AsyncMock()
        request.json.return_value = {"strategy_id": "hrs", "action": "confirm"}
        assert (await app._handle_approval(request)).status == 200
    actual = (config_file.parent / "tuning_overrides/.active").read_text().strip()
    assert actual == ("V20261126_02" if same_day else "V20261126")
    assert store.record["active_version"] == actual
    assert not await handler.find_memory_by_version("hrs", actual)
    await handler.acknowledge_version("hrs", actual)
    assert await handler.find_memory_by_version("hrs", actual)
    request.json.return_value = {"strategy_id": "hrs", "expected_version": (config_file.parent / "tuning_overrides/.active").read_text().strip()}
    response = await app._handle_rollback(request)
    assert response.status == 200
    assert json.loads(response.text)["metadata_updated"] is True
    assert store.record["is_rolled_back"] is True


@pytest.mark.asyncio
async def test_weekly_auto_apply_binds_actual_incremented_version(config_file):
    import importlib.util
    # 复用已有流水线前置替身，仅覆盖这里需要验证的真实配置和记忆写入。
    spec = importlib.util.spec_from_file_location("auto_apply_fixture", ROOT / "ai_tuner/tests/test_auto_apply.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    job, _ = fixture._create_job({"approval": {"auto_apply": {"enabled": True}}}, adjustments={"scoring.a": 11})
    store = MemoryStore()
    job.db_handler = MemoryDBHandler(store)
    job.db_handler._schema = "trading"
    job.config_operator = ConfigOperator()
    job.version_manager = VersionManager({})
    class ApplyDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 26, 12)
    cfg = {"strategy_id": "btc_eth", "name": "MTPCS", "adapter_class": "ai_tuner.adapters.mtpcs_adapter.MTPCSAdapter", "config_path": str(config_file)}
    with patch("ai_tuner.deploy.config_operator.datetime", ApplyDay), patch("ai_tuner.deploy.version_manager.datetime", ApplyDay):
        assert job.config_operator.apply_overrides(str(config_file), {"scoring.b": 22})
        assert await job._tune_single_strategy(cfg) == "success"
    actual = (config_file.parent / "tuning_overrides/.active").read_text().strip()
    assert actual == "V20261126_02"
    assert store.record["active_version"] == actual
    assert not await job.db_handler.find_memory_by_version("btc_eth", actual)
    await job.db_handler.acknowledge_version("btc_eth", actual)
    assert await job.db_handler.find_memory_by_version("btc_eth", actual)
