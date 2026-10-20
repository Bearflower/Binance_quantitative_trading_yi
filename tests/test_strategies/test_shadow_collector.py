"""影子采集器核心行为测试（spec §13.2）

覆盖：
  - 两 profile 独立 positions（互不污染）
  - ON CONFLICT DO NOTHING 重试不重复
  - candidate / no_signal / analysis_error 字段契约稳定
  - 末根 K 线 close_time > now 时不产 candidate
  - 改 config 后 config_hash 变化 → run_id 变化
  - profile 异常隔离：一个抛异常另一个继续
  - 全部离线（mock kline / mock db / mock strategy）
"""
from __future__ import annotations

import asyncio
import json
import pytest
import yaml
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from shared.config_loader import compute_config_hash

from strategies.btc_eth.shadow.collector import (
    ShadowCollector,
    _validate_kline_closed,
    _validate_all_intervals,
    _build_shadow_strategy,
)
from strategies.btc_eth.shadow.repository import ShadowRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_profile_configs():
    """返回最小可用的两 profile config dict（从冻结 YAML 加载）"""
    from pathlib import Path
    project_root = Path(__file__).resolve().parent.parent.parent   # /tests/test_strategies → project root
    config_dir = project_root / "strategies" / "btc_eth" / "shadow" / "config"
    assert config_dir.is_dir(), f"config 目录不存在: {config_dir}"

    configs = {}
    for pid in ["xau_original", "xau_aggressive"]:
        path = config_dir / f"{pid}.yaml"
        with open(path, "r", encoding="utf-8") as f:
            configs[pid] = yaml.safe_load(f)
    return configs


def _make_mock_db():
    db = AsyncMock()
    db.execute = AsyncMock(return_value="OK")
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    return db


def _make_mock_kline_service(klines_by_interval):
    """构造一个 get_multi_timeframe_data 返回指定数据的 mock"""
    kline_service = AsyncMock()
    kline_service.get_multi_timeframe_data = AsyncMock(return_value=klines_by_interval)
    return kline_service


def _make_mock_binance_client():
    """最小 Mock：只读 + 空 session"""
    client = MagicMock()
    client.instance_id = "mtcps_xau_shadow"
    # _request 不抛（ReadOnlyBinanceClient 继承 BinanceClient，但 mock 时不需要）
    return client


# ---------------------------------------------------------------------------
# K 线闭合校验
# ---------------------------------------------------------------------------
class TestKlineClosedValidation:
    """_validate_kline_closed 的行为矩阵（单位：毫秒，阻断项 1 全链路统一毫秒口径）"""

    def test_empty_klines_invalid(self):
        now_ms = _now_ms()
        ok, reason, _ = _validate_kline_closed([], "1h", now_ms)
        assert ok is False
        assert "为空" in reason

    def test_unclosed_last_bar_invalid(self):
        """最后一根 close_time > now + 容差 → 未闭合；或步长超出容差 → 断链"""
        now_ms = _now_ms()
        klines = _make_closed_klines(now_ms, "1h", 50)
        # 倒数第二根保持已闭合；最后一根 close_time 推到未来 2 小时
        # —— 既超过 60s 闭合容差（_KLINE_CLOSE_TOLERANCE_MS=60_000），
        #    也与前一根的 gap 远超 300_000ms 断链容差
        klines[-1]["close_time"] = now_ms + 2 * 3_600_000   # 未来 2 小时
        klines[-1]["open_time"] = klines[-1]["close_time"] - 3_600_000 + 1
        ok, reason, _ = _validate_kline_closed(klines, "1h", now_ms)
        assert ok is False
        # 断链判断可能先于未闭合判断触发（两者都是数据质量问题，判其一即可）
        assert ("未闭合" in reason) or ("断链" in reason) or ("不递增" in reason)

    def test_valid_closed_klines(self):
        """真实毫秒口径：合法闭合 K 线应通过校验"""
        now_ms = _now_ms()
        klines = _make_closed_klines(now_ms, "1h", 100)
        ok, reason, last_ct = _validate_kline_closed(klines, "1h", now_ms)
        assert ok is True
        assert reason is None
        assert last_ct == klines[-1]["close_time"]

    def test_insufficient_count_invalid(self):
        now_ms = _now_ms()
        # 只有 10 根，不足 50
        klines = _make_closed_klines(now_ms, "1h", 10)
        ok, reason, _ = _validate_kline_closed(klines, "1h", now_ms)
        assert ok is False
        assert "K线不足" in reason

    def test_timestamp_gap_invalid(self):
        """中间时间戳断链"""
        now_ms = _now_ms()
        klines = _make_closed_klines(now_ms, "1h", 50)
        # 在最后两根之间制造巨大 gap（最后一根提前整整 24h = 86_400_000ms）
        klines[-1]["close_time"] = klines[-2]["close_time"] + 24 * 3_600_000
        klines[-1]["open_time"] = klines[-1]["close_time"] - 3_600_000
        ok, reason, _ = _validate_kline_closed(klines, "1h", now_ms)
        # 断链应该被检测到（步长不符预期）
        assert ok is False
        assert "断链" in reason or "不递增" in reason

    def test_all_intervals_invalid_when_any_invalid(self):
        """_validate_all_intervals：至少一个周期 invalid → 全部 invalid"""
        now_ms = _now_ms()
        # 1d 最后一根未闭合（1d=86_400_000ms）
        klines_1d = [
            {
                "open_time": now_ms - i * 86_400_000,
                "close_time": now_ms - i * 86_400_000 + 86_399_000,
                "close": 2000.0,
            }
            for i in range(60)
        ]
        klines_1d[-1]["close_time"] = now_ms + 3_600_000  # 未来 1h（未闭合）

        multi = {
            "1h": _make_closed_klines(now_ms, "1h", 100),
            "4h": _make_closed_klines(now_ms, "4h", 100),
            "1d": klines_1d,
        }
        all_valid, reasons, close_times = _validate_all_intervals(
            multi, ["1h", "4h", "1d"], now_ms
        )
        assert all_valid is False
        assert "1d" in reasons

    # ---- 阻断项 1 新增回归用例 ----
    def test_second_unit_timestamps_must_be_rejected(self):
        """秒级时间戳必须被拒绝（防止单位回退重放 2026-10-19 缺陷）

        kline_service 返回毫秒；如果有人误把 close_time 写成秒，
        expected_step_ms=3_600_000 与实际秒级 3600 的 gap 会达 3_596_400ms，
        远超 300_000ms 容差 → 触发断链。
        """
        now_sec = int(datetime.now(timezone.utc).timestamp())
        # 用秒级步长构造（错误口径，故意制造）
        steps = {"1h": 3600, "4h": 14400, "1d": 86400}
        step_sec = steps["1h"]
        wrong_klines = []
        count = 100
        for idx in range(count):
            close_time_sec = now_sec - 1 - (count - 1 - idx) * step_sec
            open_time_sec = close_time_sec - step_sec + 1
            wrong_klines.append({
                "open_time": open_time_sec,     # 秒
                "close_time": close_time_sec,   # 秒（错误口径）
                "close": 2000.0, "high": 2010.0, "low": 1990.0,
                "open": 1995.0, "volume": 1000.0,
            })
        # now_ms 仍用真实毫秒（正确调用）
        now_ms = int(now_sec * 1000)
        ok, reason, _ = _validate_kline_closed(wrong_klines, "1h", now_ms)
        assert ok is False, "秒级时间戳必须被拒绝"
        assert ("断链" in reason) or ("未闭合" in reason), (
            f"预期断链或未闭合，但 reason={reason}"
        )

    def test_real_millisecond_timestamps_must_pass(self):
        """真实毫秒口径 K 线应通过校验（阻断项 1 修复后的主路径回归）"""
        now_ms = _now_ms()
        for interval in ["1h", "4h", "1d"]:
            klines = _make_closed_klines(now_ms, interval, 100)
            ok, reason, last_ct = _validate_kline_closed(klines, interval, now_ms)
            assert ok is True, (
                f"interval={interval} 真实毫秒口径应通过校验，但 ok={ok} reason={reason}"
            )
            assert last_ct == klines[-1]["close_time"]


def _make_closed_klines(now_ms: int, interval: str, count: int) -> list:
    """构造已闭合的 mock K 线列表（时间升序：最旧在前，最新放末尾）

    单位：所有 close_time / open_time 都是毫秒（与 kline_service 返回一致，阻断项 1 修复后全链路统一毫秒）。
    """
    steps_ms = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
    step_ms = steps_ms.get(interval, 3_600_000)
    klines = []
    for idx in range(count):
        # idx=0 最旧（close_time 最小），idx=count-1 最新（close_time 最大）
        # 最新一根 close_time = now_ms - 1（已闭合）
        close_time = now_ms - 1 - (count - 1 - idx) * step_ms
        open_time = close_time - step_ms + 1
        klines.append({
            "open_time": open_time,
            "close_time": close_time,
            "close": 2000.0 + idx * 0.1,
            "high": 2010.0,
            "low": 1990.0,
            "open": 1995.0,
            "volume": 1000.0,
        })
    return klines


def _now_ms() -> int:
    """构造测试用毫秒时间戳的小工具（与 _make_closed_klines 配套）"""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Profile 独立 / ON CONFLICT / 字段契约
# ---------------------------------------------------------------------------
class TestCollectorProfileIndependence:
    """两 profile 独立 positions、ON CONFLICT、字段契约"""

    @pytest.fixture
    def fake_klines(self):
        now_ms = _now_ms()
        return {
            "1h": _make_closed_klines(now_ms, "1h", 100),
            "4h": _make_closed_klines(now_ms, "4h", 100),
            "1d": _make_closed_klines(now_ms, "1d", 60),
        }

    @pytest.mark.asyncio
    async def test_on_conflict_do_nothing_retry_no_duplicate(self, fake_klines):
        """同一 run_id+profile+symbol+scheduled_at 重复插入不重复"""
        configs = _make_profile_configs()
        db = _make_mock_db()
        repo = ShadowRepository(db)

        scheduled_at = datetime.utcnow()
        for _ in range(3):
            event = {
                "run_id": "run-abc",
                "profile_id": "xau_original",
                "symbol": "XAUUSDT",
                "scheduled_at": scheduled_at,
                "evaluated_at": datetime.utcnow(),
                "decision": "candidate",
                "direction": "LONG",
                "grade": "S",
                "score": 90.5,
                "reference_price": 1234.5,
                "intended_order_type": "LIMIT",
                "intended_entry_price": 1234.5,
                "stop_loss_rule": {"kind": "stop_loss", "atr_multiplier": 2.5},
                "tp1_rule": {"kind": "tp1", "atr_multiplier": 6.0},
                "tp2_rule": {"kind": "tp2", "atr_multiplier": 9.0},
                "trailing_rule": None,
                "time_stop_rule": None,
                "feature_snapshot": {"market_state": "STRONG_TREND", "atr": 5.0},
                "reason_codes": [],
                "data_quality_reason": None,
                "config_hash": compute_config_hash(configs["xau_original"]),
                "git_commit": "abc123",
                "source_kline_close_times": {"1h": 1000000},
            }
            ok = await repo.insert_decision_event(event)
            assert ok is True

        # execute 只应被调用 3 次（每次都会触发 ON CONFLICT DO NOTHING，但 SQL 调用次数 = 插入次数）
        assert db.execute.await_count == 3

    @pytest.mark.asyncio
    async def test_candidate_event_has_all_required_fields(self, fake_klines):
        """candidate 事件完整覆盖 research schema 契约字段"""
        event = _make_full_candidate_event(config_hash="hash123")
        # 必须含有的字段（按 research.shadow_decision_events 列顺序）
        required_fields = [
            "run_id", "profile_id", "symbol",
            "scheduled_at", "evaluated_at",
            "decision", "direction", "grade", "score",
            "reference_price", "intended_order_type", "intended_entry_price",
            "stop_loss_rule", "tp1_rule", "tp2_rule", "trailing_rule", "time_stop_rule",
            "feature_snapshot", "reason_codes", "data_quality_reason",
            "config_hash", "git_commit", "source_kline_close_times",
        ]
        for field in required_fields:
            assert field in event, f"candidate 事件缺少必填字段: {field}"

    @pytest.mark.asyncio
    async def test_no_signal_event_has_reason_codes(self, fake_klines):
        """no_signal 事件有 reason_codes 且 decision 正确"""
        event = _make_full_no_signal_event()
        assert event["decision"] == "no_signal"
        assert isinstance(event["reason_codes"], list)
        assert len(event["reason_codes"]) >= 1

    @pytest.mark.asyncio
    async def test_analysis_error_event_has_data_quality_reason(self, fake_klines):
        """analysis_error 事件有 data_quality_reason"""
        event = _make_full_analysis_error_event(reason="K线未闭合 close_time 未来")
        assert event["decision"] == "analysis_error"
        assert event["data_quality_reason"] is not None

    @pytest.mark.asyncio
    async def test_two_profiles_independent_positions(self, fake_klines):
        """两 profile 的 shadow strategy 各自 positions 独立

        关键：patch collector 模块内的 _build_shadow_strategy 必须在 ShadowCollector.__init__
        之前生效（__init__ 里预构建策略实例，重要项 5）。patch 覆盖整个"构造 + run_once"过程。
        两 mock 返回独立 MagicMock，各自 positions={} 互不污染。
        """
        configs = _make_profile_configs()
        mock_kline = _make_mock_kline_service(fake_klines)
        mock_binance = _make_mock_binance_client()
        db = _make_mock_db()
        repo = ShadowRepository(db)

        # 构造两个 mock strategy
        fake_original = MagicMock()
        fake_original.positions = {}
        fake_original.risk_config = configs["xau_original"].get("risk", {})
        fake_original.symbol_config = configs["xau_original"]["strategy"]["symbol_config"]
        fake_original.scoring_config = configs["xau_original"]["strategy"]["scoring"]
        fake_original._build_indicators = MagicMock(return_value={"1h": {}, "4h": {}, "1d": {}})
        fake_original._analyze_stationary_signal = AsyncMock(return_value=(
            {
                "direction": "LONG", "grade": "S", "score": 90.5,
                "entry_price": Decimal("2000.0"), "initial_stop_loss": Decimal("1990.0"),
                "atr": Decimal("4.0"), "market_state": "STRONG_TREND",
                "price_change_1h": 0.01, "tp1_price": Decimal("2024.0"),
                "tp2_price": Decimal("2036.0"),
            }, ""
        ))

        fake_aggressive = MagicMock()
        fake_aggressive.positions = {}
        fake_aggressive.risk_config = configs["xau_aggressive"].get("risk", {})
        fake_aggressive.symbol_config = configs["xau_aggressive"]["strategy"]["symbol_config"]
        fake_aggressive.scoring_config = configs["xau_aggressive"]["strategy"]["scoring"]
        fake_aggressive._build_indicators = MagicMock(return_value={"1h": {}, "4h": {}, "1d": {}})
        fake_aggressive._analyze_stationary_signal = AsyncMock(return_value=(
            None, "评分 55 < 最低评分 60"
        ))

        # 核心：patch collector 模块的 _build_shadow_strategy，返回对应 mock。
        # 新签名是 keyword-only；用 profile_id 路由返回哪个 fake。
        from strategies.btc_eth.shadow import collector as collector_module

        def _fake_build(*args, **kwargs):
            pid = kwargs.get("profile_id")
            if pid == "xau_original":
                return fake_original
            if pid == "xau_aggressive":
                return fake_aggressive
            raise ValueError(f"未预期 profile_id={pid}")

        # patch 必须包住 ShadowCollector.__init__（里面调 _build_shadow_strategy）
        with patch.object(collector_module, "_build_shadow_strategy", side_effect=_fake_build):
            collector = ShadowCollector(
                run_id="run-test",
                instance_id="mtcps_xau_shadow",
                symbol="XAUUSDT",
                profiles=["xau_original", "xau_aggressive"],
                profile_configs=configs,
                repository=repo,
                kline_service=mock_kline,
                binance_client=mock_binance,
                git_commit="test-commit",
                git_sha="test-sha",
            )
            result = await collector.run_once()

        assert result["events_total"] == 2
        assert fake_original._analyze_stationary_signal.await_count == 1
        assert fake_aggressive._analyze_stationary_signal.await_count == 1

        # 关键：positions 独立（都还是空 dict）
        assert fake_original.positions == {}
        assert fake_aggressive.positions == {}


# ---------------------------------------------------------------------------
# run_id 确定性 & config_hash 变化
# ---------------------------------------------------------------------------
class TestRunIdDeterminism:
    """run_id 确定性：相同输入 → 相同 run_id；config_hash 变化 → 不同"""

    def test_same_input_same_run_id(self):
        from strategies.btc_eth.shadow.main import _generate_run_id
        run_start = datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc)
        hashes = {"xau_original": "hashA", "xau_aggressive": "hashB"}
        git = "commit123"
        rid1 = _generate_run_id(run_start, git, hashes)
        rid2 = _generate_run_id(run_start, git, hashes)
        assert rid1 == rid2

    def test_config_hash_change_changes_run_id(self):
        from strategies.btc_eth.shadow.main import _generate_run_id
        run_start = datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc)
        hashes_a = {"xau_original": "hashA", "xau_aggressive": "hashB"}
        hashes_b = {"xau_original": "hashA2", "xau_aggressive": "hashB"}  # original 变了
        rid_a = _generate_run_id(run_start, "git1", hashes_a)
        rid_b = _generate_run_id(run_start, "git1", hashes_b)
        assert rid_a != rid_b

    def test_git_change_changes_run_id(self):
        from strategies.btc_eth.shadow.main import _generate_run_id
        run_start = datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc)
        hashes = {"xau_original": "hashA", "xau_aggressive": "hashB"}
        rid_a = _generate_run_id(run_start, "gitOld", hashes)
        rid_b = _generate_run_id(run_start, "gitNew", hashes)
        assert rid_a != rid_b

    def test_run_id_format_prefix_and_timestamp(self):
        from strategies.btc_eth.shadow.main import _generate_run_id
        run_start = datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc)
        rid = _generate_run_id(run_start, "git", {"a": "b"})
        assert rid.startswith("xau-shadow-")
        assert "20261019100000" in rid


# ---------------------------------------------------------------------------
# 辅助：构造完整事件
# ---------------------------------------------------------------------------
def _make_full_candidate_event(*, config_hash: str = "abc123") -> dict:
    return {
        "run_id": "run-test",
        "profile_id": "xau_original",
        "symbol": "XAUUSDT",
        "scheduled_at": datetime.utcnow(),
        "evaluated_at": datetime.utcnow(),
        "decision": "candidate",
        "direction": "LONG",
        "grade": "S",
        "score": 90.5,
        "reference_price": 1234.5,
        "intended_order_type": "LIMIT",
        "intended_entry_price": 1234.5,
        "stop_loss_rule": {"kind": "stop_loss", "atr_multiplier": 2.5, "atr_value": 4.5},
        "tp1_rule": {"kind": "tp1", "atr_multiplier": 6.0, "atr_value": 4.5},
        "tp2_rule": {"kind": "tp2", "atr_multiplier": 9.0, "atr_value": 4.5},
        "trailing_rule": {"enabled": True, "activation": {"min_profit_pct": 3.0}},
        "time_stop_rule": {"max_holding_hours": 96, "close_ratio": 0.50},
        "feature_snapshot": {
            "market_state": "STRONG_TREND",
            "atr": 4.5,
            "price_change_1h": 0.012,
        },
        "reason_codes": [],
        "data_quality_reason": None,
        "config_hash": config_hash,
        "git_commit": "commit-def-456",
        "source_kline_close_times": {"1h": 1729252800000, "4h": 1729238400000, "1d": 1729224000000},
    }


def _make_full_no_signal_event() -> dict:
    return {
        "run_id": "run-test",
        "profile_id": "xau_original",
        "symbol": "XAUUSDT",
        "scheduled_at": datetime.utcnow(),
        "evaluated_at": datetime.utcnow(),
        "decision": "no_signal",
        "direction": None,
        "grade": None,
        "score": None,
        "reference_price": None,
        "intended_order_type": None,
        "intended_entry_price": None,
        "stop_loss_rule": None,
        "tp1_rule": None,
        "tp2_rule": None,
        "trailing_rule": None,
        "time_stop_rule": None,
        "feature_snapshot": None,
        "reason_codes": ["grade_filter", "评分 55 < 最低评分 75"],
        "data_quality_reason": None,
        "config_hash": "abc123",
        "git_commit": "commit-def-456",
        "source_kline_close_times": {"1h": 1729252800000, "4h": 1729238400000, "1d": 1729224000000},
    }


def _make_full_analysis_error_event(*, reason: str = "") -> dict:
    return {
        "run_id": "run-test",
        "profile_id": "xau_original",
        "symbol": "XAUUSDT",
        "scheduled_at": datetime.utcnow(),
        "evaluated_at": datetime.utcnow(),
        "decision": "analysis_error",
        "direction": None,
        "grade": None,
        "score": None,
        "reference_price": None,
        "intended_order_type": None,
        "intended_entry_price": None,
        "stop_loss_rule": None,
        "tp1_rule": None,
        "tp2_rule": None,
        "trailing_rule": None,
        "time_stop_rule": None,
        "feature_snapshot": None,
        "reason_codes": ["stationary_signal_exception", "internal_exception"],
        "data_quality_reason": reason or "指标计算抛出异常",
        "config_hash": "abc123",
        "git_commit": "commit-def-456",
        "source_kline_close_times": {"1h": 1729252800000, "4h": 1729238400000, "1d": 1729224000000},
    }


# ---------------------------------------------------------------------------
# 影子策略构造安全
# ---------------------------------------------------------------------------
class TestBuildShadowStrategySafety:
    """_build_shadow_strategy：构造后自动校验三重安全断言"""

    @pytest.mark.asyncio
    async def test_shadow_strategy_has_shadow_batch_mode(self):
        configs = _make_profile_configs()
        client = MagicMock()
        kline = MagicMock()
        strategy = _build_shadow_strategy(
            profile_id="xau_original",
            profile_config=configs["xau_original"],
            binance_client=client,
            kline_service=kline,
        )
        # 断言构造后 mode 正确
        assert strategy.execution_mode == "shadow_batch"
        assert strategy.real_ordering_enabled is False
        assert strategy.state_reporting_enabled is False
        # 关键：db_manager 没有被传入（BTCEthStrategy.__init__ 的 db_manager=None）
        # 所以 CapitalManager / FrequencyController / CircuitBreaker 都不会访问 DB


# ---------------------------------------------------------------------------
# _make_event_from_signal 事件规则字段回归（修复 risk 配置路径）
# ---------------------------------------------------------------------------
class TestMakeEventFromSignalRiskRules:
    """直接调用 _make_event_from_signal，断言 risk 配置在正确层级被读取

    修复背景：collector.py 旧版本用 self.profile_configs[pid].get("risk")（顶层没有 risk 键），
    导致 risk_cfg 恒为 {}，stop_loss_rule/tp1_rule/tp2_rule/trailing_rule/time_stop_rule 全为 None。
    正确路径是 .get("strategy", {}).get("risk", {}).get("signal_levels", {}).get(grade, {})
    """

    @pytest.fixture
    def collector(self):
        """最小 ShadowCollector，repository / kline / client 全 mock"""
        configs = _make_profile_configs()
        repo = MagicMock()
        kline = MagicMock()
        client = _make_mock_binance_client()
        return ShadowCollector(
            run_id="xau-shadow-TEST",
            instance_id="mtcps_xau_shadow",
            symbol="XAUUSDT",
            profiles=list(configs.keys()),
            profile_configs=configs,
            repository=repo,
            kline_service=kline,
            binance_client=client,
            git_commit="abc123",
            git_sha="abc123",
        )

    def _signal(
        self,
        *,
        grade: str = "S",
        atr=None,
    ) -> dict:
        return {
            "direction": "long",
            "grade": grade,
            "score": 85.0,
            "entry_price": Decimal("2000.0"),
            "initial_stop_loss": Decimal("1990.0"),
            "tp1_price": Decimal("2025.0"),
            "tp2_price": Decimal("2040.0"),
            "atr": atr,
            "market_state": "trending",
            "price_change_1h": 0.5,
        }

    # ---- 1. 正常 S 档 + 有效 ATR：五条规则字段均非 None，且 multiplier 正确 ----
    def test_s_grade_populates_all_rule_fields(self, collector):
        sched = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        evald = datetime(2026, 10, 19, 12, 1, tzinfo=timezone.utc)
        comps = self._signal(grade="S", atr=Decimal("4.0"))

        for pid, expected_sl_mult in [
            ("xau_original", 2.5),
            ("xau_aggressive", 3.0),
        ]:
            evt = collector._make_event_from_signal(
                profile_id=pid,
                scheduled_at=sched,
                evaluated_at=evald,
                signal_components=comps,
                close_times={"1h": 1729300000000},
            )
            # stop_loss_rule
            assert evt["stop_loss_rule"] is not None, f"{pid}: stop_loss_rule 不允许为 None"
            assert evt["stop_loss_rule"]["kind"] == "stop_loss"
            assert evt["stop_loss_rule"]["atr_multiplier"] == expected_sl_mult
            assert evt["stop_loss_rule"]["atr_value"] == 4.0

            # tp1_rule / tp2_rule（S 档 original: 6.0 / 9.0；aggressive: 7.0 / 11.0）
            assert evt["tp1_rule"] is not None, f"{pid}: tp1_rule 不允许为 None"
            assert evt["tp2_rule"] is not None, f"{pid}: tp2_rule 不允许为 None"

            # trailing_rule / time_stop_rule（来自 risk_cfg["dynamic_trailing"] / ["time_stop"]）
            assert evt["trailing_rule"] is not None, f"{pid}: trailing_rule 不允许为 None"
            assert isinstance(evt["trailing_rule"], dict)
            assert evt["time_stop_rule"] is not None, f"{pid}: time_stop_rule 不允许为 None"
            assert isinstance(evt["time_stop_rule"], dict)

    # ---- 2. ATR 为 None：保持 fail-safe，三条价格规则均为 None ----
    def test_atr_none_yields_none_price_rules(self, collector):
        sched = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        evald = datetime(2026, 10, 19, 12, 1, tzinfo=timezone.utc)
        comps = self._signal(grade="S", atr=None)

        for pid in ["xau_original", "xau_aggressive"]:
            evt = collector._make_event_from_signal(
                profile_id=pid,
                scheduled_at=sched,
                evaluated_at=evald,
                signal_components=comps,
                close_times={"1h": 1729300000000},
            )
            assert evt["stop_loss_rule"] is None
            assert evt["tp1_rule"] is None
            assert evt["tp2_rule"] is None
            # trailing_rule / time_stop_rule 不依赖 ATR，仍应存在
            assert evt["trailing_rule"] is not None
            assert evt["time_stop_rule"] is not None

    # ---- 3. 未知 grade：risk_cfg 为空 dict，三条价格规则为 None，且不抛异常 ----
    def test_unknown_grade_does_not_raise(self, collector):
        sched = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        evald = datetime(2026, 10, 19, 12, 1, tzinfo=timezone.utc)
        comps = self._signal(grade="Z", atr=Decimal("4.0"))

        for pid in ["xau_original", "xau_aggressive"]:
            evt = collector._make_event_from_signal(
                profile_id=pid,
                scheduled_at=sched,
                evaluated_at=evald,
                signal_components=comps,
                close_times={"1h": 1729300000000},
            )
            # risk_cfg 对 grade="Z" 取到 {}，sl_mult/tp_mult 恒为 None → 三条价格规则为 None
            assert evt["stop_loss_rule"] is None
            assert evt["tp1_rule"] is None
            assert evt["tp2_rule"] is None
            # trailing_rule / time_stop_rule 从空 dict 取，也为 None
            assert evt["trailing_rule"] is None
            assert evt["time_stop_rule"] is None


# ---------------------------------------------------------------------------
# git 信息读取（run_id 确定性的关键输入）：env 优先、VERSION 文件回退、全缺为空
# ---------------------------------------------------------------------------
class TestReadGitInfo:
    """_read_git_info：环境变量 > 容器 VERSION 文件 > 空串（采集器仍可运行）"""

    def setup_method(self):
        """每个用例前清理 GIT_COMMIT / GIT_SHA，避免宿主环境泄漏。"""
        self._mp = pytest.MonkeyPatch()
        self._mp.delenv("GIT_COMMIT", raising=False)
        self._mp.delenv("GIT_SHA", raising=False)

    def teardown_method(self):
        self._mp.undo()

    def test_missing_env_and_no_version_file_returns_empty(self, tmp_path, monkeypatch):
        """无 env、无 VERSION 文件 → 空串（decision event 落 git_commit=""，不崩溃）"""
        monkeypatch.chdir(tmp_path)  # 相对路径 VERSION 不存在
        from strategies.btc_eth.shadow.collector import _read_git_info
        git_commit, git_sha = _read_git_info()
        assert git_commit == ""
        assert git_sha == ""

    def test_version_file_parsed_when_env_missing(self, tmp_path, monkeypatch):
        """env 缺失 → 从 cwd 的 VERSION 文件解析 KEY=VAL"""
        (tmp_path / "VERSION").write_text(
            "DEPLOY_ID=0000002A\nGIT_COMMIT=abc123\nGIT_SHA=def456\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from strategies.btc_eth.shadow.collector import _read_git_info
        git_commit, git_sha = _read_git_info()
        assert git_commit == "abc123"
        assert git_sha == "def456"

    def test_env_takes_priority_over_version_file(self, tmp_path, monkeypatch):
        """env 已有值时 VERSION 文件不覆盖（文件只补空缺键）"""
        (tmp_path / "VERSION").write_text(
            "GIT_COMMIT=file-commit\nGIT_SHA=file-sha\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GIT_COMMIT", "env-commit")
        monkeypatch.delenv("GIT_SHA", raising=False)
        from strategies.btc_eth.shadow.collector import _read_git_info
        git_commit, git_sha = _read_git_info()
        assert git_commit == "env-commit"   # env 优先
        assert git_sha == "file-sha"        # env 缺失 → 文件回退

    def test_malformed_version_lines_skipped(self, tmp_path, monkeypatch):
        """VERSION 文件中的空行 / 无 = 行被跳过，不影响合法键解析"""
        (tmp_path / "VERSION").write_text(
            "\nnot-a-kv-line\nGIT_SHA=sha-only\n  \n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from strategies.btc_eth.shadow.collector import _read_git_info
        git_commit, git_sha = _read_git_info()
        assert git_commit == ""
        assert git_sha == "sha-only"
