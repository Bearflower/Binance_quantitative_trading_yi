"""
main.py 决策证据旁路接线测试（C1）。

覆盖：
  - initialize()：decision_evidence.enabled=True → 构造 recorder 挂到 strategy；
                  enabled=False / 配置非 dict → 不构造；
                  recorder 构造异常（非法 db_timeout_seconds）→ 降级关闭、不抛
  - run_strategy()：recorder 存在 → 每周期每 symbol 调用一次 record；
                    recorder=None → 空跑，主流程不受影响

所有外部依赖（DB/交易所/通知/配置加载）全部 mock，绝不连真实服务。
"""
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

# 注意：strategies/btc_eth/__init__.py 里 `from .main import main` 把包属性
# `main` 覆盖成了函数，`import strategies.btc_eth.main as m` 会拿到函数而非
# 模块；这里必须用 importlib 显式取真实模块。
main_mod = importlib.import_module("strategies.btc_eth.main")
from strategies.btc_eth.evidence_recorder import DecisionEvidenceRecorder


# ── 假件 ───────────────────────────────────────────────────────

class FakeAsyncCtx:
    """支持 async with 的假客户端。"""

    async def __aenter__(self) -> "FakeAsyncCtx":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeNotification(FakeAsyncCtx):
    """记录 send/send_alert 调用的假通知客户端。"""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.alerts: List[Dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)

    async def send_alert(self, **kwargs: Any) -> None:
        self.alerts.append(kwargs)


class FakeRecorder:
    """记录 record 调用的假 recorder。"""

    def __init__(self) -> None:
        self.calls: List[Tuple[Dict[str, Any], str]] = []
        self.account_id = "fake_account"

    async def record(self, result: Dict[str, Any], symbol: str) -> None:
        self.calls.append((result, symbol))


class FakeStrategy:
    """假策略：不预设 _evidence_recorder 属性（getattr 默认 None 可验证空跑）。"""

    def __init__(self) -> None:
        self.symbols = ["BTCUSDT"]
        self.db_manager = SimpleNamespace()
        self.frequency_controller = SimpleNamespace(
            ensure_table_exists=AsyncMock(),
            load_state=AsyncMock(),
        )

    async def analyze(self, symbol: str) -> Dict[str, Any]:
        # 返回拒绝分支（无 direction），不触发 execute_signal
        return {"symbol": symbol, "score": 50, "grade": "C", "reason": "测试拒绝"}

    async def update_positions(self) -> None:
        pass

    async def _sync_positions_with_exchange(self) -> None:
        pass

    async def _startup_orphan_cleanup(self) -> None:
        pass

    async def _adopt_untracked_exchange_positions(self) -> None:
        pass

    async def _ensure_position_protection(self) -> None:
        pass

    def build_positions_report(self) -> Tuple[list, dict, dict]:
        return [], {}, {}


def _base_config(enabled: bool) -> Dict[str, Any]:
    return {
        "decision_evidence": {
            "enabled": enabled,
            "account_id": "wiring_account",
            "db_timeout_seconds": 1.0,
        },
        "notification": {"max_retries": 1, "retry_interval_seconds": 0},
    }


@pytest.fixture
def clean_env(monkeypatch):
    """initialize() 要求的 4 个环境变量。"""
    for var in ("BINANCE_API_KEY", "BINANCE_API_SECRET",
                "KLINE_SERVICE_URL", "NOTIFICATION_SERVICE_URL"):
        monkeypatch.setenv(var, "test-value")


@pytest.fixture
def patched_deps(monkeypatch):
    """把 initialize() 依赖的外部客户端/配置加载全部替换为假件。"""
    config_holder: Dict[str, Any] = {}
    strategy_holder: Dict[str, Any] = {}

    monkeypatch.setattr(main_mod, "load_strategy_config", lambda _dir: config_holder["config"])

    fake_db_instance = MagicMock(name="db_instance")
    fake_db_instance.connect = AsyncMock()
    fake_db_cls = MagicMock(name="DatabaseManager", return_value=fake_db_instance)
    monkeypatch.setattr(main_mod, "DatabaseManager", fake_db_cls)

    fake_trade_logger = MagicMock(name="trade_logger")
    fake_trade_logger.ensure_table_exists = AsyncMock()
    monkeypatch.setattr(main_mod, "TradeLogger", MagicMock(return_value=fake_trade_logger))

    monkeypatch.setattr(main_mod, "BinanceClient", MagicMock(name="BinanceClient"))
    monkeypatch.setattr(main_mod, "KLineService", MagicMock(name="KLineService"))
    monkeypatch.setattr(main_mod, "NotificationClient", MagicMock(name="NotificationClient"))
    monkeypatch.setattr(
        main_mod, "load_ownership_config",
        lambda cfg: {"my_record_name": "test"},
    )
    monkeypatch.setattr(main_mod.condition_orders, "ensure_table", AsyncMock())
    monkeypatch.setattr(
        main_mod, "BTCEthStrategy",
        lambda *args, **kwargs: strategy_holder["strategy"],
    )

    # 污染防护：main 模块全局变量由 monkeypatch 管理，测试后自动还原
    for name in ("config", "binance_client", "kline_service",
                 "notification_client", "strategy"):
        monkeypatch.setattr(main_mod, name, None)

    return SimpleNamespace(
        config_holder=config_holder,
        strategy_holder=strategy_holder,
        db_instance=fake_db_instance,
    )


# ══════════════════════════════════════════════════════════════════
# initialize() 的 recorder 接线
# ══════════════════════════════════════════════════════════════════

def test_initialize_creates_recorder_when_enabled(patched_deps, clean_env):
    """enabled=True → 构造 DecisionEvidenceRecorder 挂到 strategy，绑定 PG sink。"""
    patched_deps.config_holder["config"] = _base_config(enabled=True)
    strategy = FakeStrategy()
    patched_deps.strategy_holder["strategy"] = strategy

    asyncio.run(main_mod.initialize())

    rec = getattr(strategy, "_evidence_recorder", None)
    assert rec is not None
    assert isinstance(rec, DecisionEvidenceRecorder)
    assert rec.enabled is True
    assert rec.account_id == "wiring_account"
    assert rec.sink is not None                    # PG sink 已注入
    assert rec.sink.db is patched_deps.db_instance  # sink 持有 db_manager


def test_initialize_skips_recorder_when_disabled(patched_deps, clean_env):
    """enabled=False → 不构造 recorder，strategy 上无该属性。"""
    patched_deps.config_holder["config"] = _base_config(enabled=False)
    strategy = FakeStrategy()
    patched_deps.strategy_holder["strategy"] = strategy

    asyncio.run(main_mod.initialize())
    assert getattr(strategy, "_evidence_recorder", None) is None


def test_initialize_skips_recorder_when_cfg_not_dict(patched_deps, clean_env):
    """decision_evidence 配置不是 dict → 跳过构造（isinstance 守卫分支）。"""
    cfg = _base_config(enabled=True)
    cfg["decision_evidence"] = "broken"
    patched_deps.config_holder["config"] = cfg
    strategy = FakeStrategy()
    patched_deps.strategy_holder["strategy"] = strategy

    asyncio.run(main_mod.initialize())
    assert getattr(strategy, "_evidence_recorder", None) is None


def test_initialize_degrades_when_recorder_construction_fails(patched_deps, clean_env):
    """recorder 构造抛异常（非法 db_timeout_seconds）→ 降级关闭、initialize 不抛。"""
    cfg = _base_config(enabled=True)
    cfg["decision_evidence"]["db_timeout_seconds"] = "not-a-number"  # float() → ValueError
    patched_deps.config_holder["config"] = cfg
    strategy = FakeStrategy()
    patched_deps.strategy_holder["strategy"] = strategy

    asyncio.run(main_mod.initialize())  # 不应抛
    assert getattr(strategy, "_evidence_recorder", None) is None


# ══════════════════════════════════════════════════════════════════
# run_strategy() 的 recorder 空跑 / 调用
# ══════════════════════════════════════════════════════════════════

def _setup_run_globals(monkeypatch, strategy: FakeStrategy, notification: FakeNotification):
    """替换 run_strategy() 依赖的模块级全局变量与外部函数。"""
    monkeypatch.setattr(main_mod, "config", {
        "notification": {"max_retries": 1, "retry_interval_seconds": 0},
    })
    monkeypatch.setattr(main_mod, "binance_client", FakeAsyncCtx())
    monkeypatch.setattr(main_mod, "kline_service", FakeAsyncCtx())
    monkeypatch.setattr(main_mod, "notification_client", notification)
    monkeypatch.setattr(main_mod, "strategy", strategy)
    monkeypatch.setattr(
        main_mod, "load_ownership_config",
        lambda cfg: {"my_record_name": "test"},
    )
    monkeypatch.setattr(
        main_mod, "filter_owned_positions", AsyncMock(return_value=({}, {})),
    )
    monkeypatch.setattr(main_mod, "save_strategy_state", AsyncMock())
    monkeypatch.setattr(main_mod, "sync_open_positions", AsyncMock())


def test_run_strategy_records_evidence_when_recorder_present(monkeypatch):
    """recorder 存在 → analyze 后对每个 symbol 调一次 record，且未走异常分支。"""
    recorder = FakeRecorder()
    strategy = FakeStrategy()
    strategy._evidence_recorder = recorder
    notification = FakeNotification()
    _setup_run_globals(monkeypatch, strategy, notification)

    asyncio.run(main_mod.run_strategy())

    assert len(recorder.calls) == 1
    result, symbol = recorder.calls[0]
    assert symbol == "BTCUSDT"
    assert result["symbol"] == "BTCUSDT"
    assert notification.alerts == []   # 主流程未走异常分支


def test_run_strategy_no_recorder_is_noop(monkeypatch):
    """recorder=None（getattr 默认）→ 空跑；主流程照常完成、通知正常发送。"""
    strategy = FakeStrategy()  # 无 _evidence_recorder 属性
    notification = FakeNotification()
    _setup_run_globals(monkeypatch, strategy, notification)

    asyncio.run(main_mod.run_strategy())  # 不应抛

    assert len(notification.sent) == 1   # 汇总通知照发，主流程未受影响
    assert notification.alerts == []
