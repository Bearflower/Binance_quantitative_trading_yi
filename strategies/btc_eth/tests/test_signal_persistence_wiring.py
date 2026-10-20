"""
main.py 信号持久化接线测试。

覆盖：
  - initialize()：ensure_signal_table_exists 被调用一次，schema="btc_eth"
  - run_strategy()：persist_signal 在 execute_signal 之后被调用、
                    trade_success 已写入 result、落库失败不影响主流程

所有外部依赖（DB/交易所/通知/配置加载）全部 mock，绝不连真实服务。

参考：strategies/btc_eth/tests/test_main_evidence_wiring.py（同类接线测试范式）
"""
from __future__ import annotations

import asyncio
import importlib
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

# 必须用 importlib 显式取真实模块。
# strategies/btc_eth/__init__.py 会把包属性 `main` 覆盖成函数，
# `import strategies.btc_eth.main as m` 拿到的是函数而非模块。
main_mod = importlib.import_module("strategies.btc_eth.main")


# ── 假件 ───────────────────────────────────────────────────────


class FakeAsyncCtx:
    """支持 async with 的假客户端。"""

    async def __aenter__(self) -> "FakeAsyncCtx":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeNotification(FakeAsyncCtx):
    """记录 send/send_alert 调用的假通知客户端（persist_signal 告警依赖 send_alert）。"""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.alerts: List[Dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)

    async def send_alert(self, **kwargs: Any) -> None:
        self.alerts.append(kwargs)


class FakeStrategy:
    """假策略：symbols=[BTCUSDT]，execute_signal 可按测试期望返回 True/False。"""

    def __init__(self, *, execute_signal_return: bool = True) -> None:
        self.symbols = ["BTCUSDT"]
        self.db_manager = SimpleNamespace()
        self.frequency_controller = SimpleNamespace(
            ensure_table_exists=AsyncMock(),
            load_state=AsyncMock(),
        )
        # 让 _evidence_recorder 存在但 record 是空跑（不影响 persist_signal 接线验证）
        self._evidence_recorder = None

        self._execute_signal_return = execute_signal_return
        self.execute_signal = AsyncMock(return_value=execute_signal_return)

    async def analyze(self, symbol: str) -> Dict[str, Any]:
        # 返回带 direction 的信号（触发 execute_signal + persist_signal 调用链）
        return {
            "symbol": symbol,
            "score": 85,
            "grade": "A",
            "direction": "LONG",
            "entry_price": 65000.0,
            "initial_stop_loss": 64000.0,
            "tp1_price": 67000.0,
            "created_at": datetime(2026, 10, 19, 14, 0, 0),
        }

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


# ── fixture ────────────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch):
    """initialize() 要求的 4 个环境变量。"""
    for var in ("BINANCE_API_KEY", "BINANCE_API_SECRET",
                "KLINE_SERVICE_URL", "NOTIFICATION_SERVICE_URL"):
        monkeypatch.setenv(var, "test-value")


@pytest.fixture
def patched_deps(monkeypatch):
    """把 initialize() 依赖的外部客户端/配置加载全部替换为假件；
    同时 mock 掉 mtpc_signal_persistence 的 ensure_signal_table_exists。"""
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

    # mock 掉 persistence 模块的 ensure_signal_table_exists（initialize 里直接调用）
    ensure_signal = AsyncMock()
    monkeypatch.setattr(main_mod, "ensure_signal_table_exists", ensure_signal)

    # mock persistence 模块的 persist_signal（run_strategy 里直接调用）
    persist = AsyncMock(return_value=True)
    monkeypatch.setattr(main_mod, "persist_signal", persist)

    # 污染防护：main 模块全局变量由 monkeypatch 管理，测试后自动还原
    for name in ("config", "binance_client", "kline_service",
                 "notification_client", "strategy"):
        monkeypatch.setattr(main_mod, name, None)

    return SimpleNamespace(
        config_holder=config_holder,
        strategy_holder=strategy_holder,
        db_instance=fake_db_instance,
        ensure_signal=ensure_signal,
        persist_signal=persist,
    )


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


# ══════════════════════════════════════════════════════════════════
# initialize() 的 ensure_signal_table_exists 接线
# ══════════════════════════════════════════════════════════════════


def test_initialize_calls_ensure_signal_table_exists(patched_deps, clean_env):
    """initialize() 里 ensure_signal_table_exists 被调用一次，schema="btc_eth"，
    db_manager 为刚建立的实例。"""
    patched_deps.config_holder["config"] = {}
    strategy = FakeStrategy()
    patched_deps.strategy_holder["strategy"] = strategy

    asyncio.run(main_mod.initialize())

    patched_deps.ensure_signal.assert_awaited_once()
    call_args = patched_deps.ensure_signal.call_args.args
    # 第一个参数是 db_manager，第二个是 schema
    assert call_args[0] is patched_deps.db_instance
    assert call_args[1] == "btc_eth"


# ══════════════════════════════════════════════════════════════════
# run_strategy() 的 persist_signal 接线
# ══════════════════════════════════════════════════════════════════


def test_run_strategy_calls_persist_signal_after_execute(monkeypatch, patched_deps):
    """persist_signal 在 execute_signal 之后被调用；trade_success 已写入 result；
    schema="btc_eth"；notification_client 正确传入。"""
    strategy = FakeStrategy(execute_signal_return=True)
    notification = FakeNotification()
    _setup_run_globals(monkeypatch, strategy, notification)

    asyncio.run(main_mod.run_strategy())

    # persist_signal 应被调用（对应 BTCUSDT 一个有 direction 的信号）
    patched_deps.persist_signal.assert_awaited_once()
    call_kwargs = patched_deps.persist_signal.call_args.kwargs

    # schema 正确
    assert call_kwargs["schema"] == "btc_eth"
    # notification 正确
    assert call_kwargs["notification"] is notification
    # db 是 strategy.db_manager
    assert call_kwargs["db"] is strategy.db_manager

    # signal 里 trade_success 已写入（persist_signal 读它决定 status）
    signal = call_kwargs["signal"]
    assert signal["trade_success"] is True
    assert signal["symbol"] == "BTCUSDT"


def test_run_strategy_persist_signal_cancelled_on_failure(monkeypatch, patched_deps):
    """execute_signal 返回 False → persist_signal 收到的 signal.trade_success=False。"""
    strategy = FakeStrategy(execute_signal_return=False)
    notification = FakeNotification()
    _setup_run_globals(monkeypatch, strategy, notification)

    asyncio.run(main_mod.run_strategy())

    patched_deps.persist_signal.assert_awaited_once()
    signal = patched_deps.persist_signal.call_args.kwargs["signal"]
    assert signal["trade_success"] is False


def test_run_strategy_persist_signal_failure_does_not_break_main_flow(monkeypatch, patched_deps):
    """persist_signal 抛异常 → run_strategy 不中断（主流程兜底 persist_signal 内部吞异常）。

    注：实际上 persist_signal 模块内部自己 try/except 吞异常，不会抛到 run_strategy。
    这里我们验证的是：即使 persist_signal 真的抛了，run_strategy 的 try/except 外层
    也能兜住，主循环继续处理后续 symbol（只做日志 + continue）。
    """
    # 让 persist_signal 真抛异常（模拟模块内部没兜住的极端情况）
    patched_deps.persist_signal.side_effect = RuntimeError("DB 连接断开")
    strategy = FakeStrategy(execute_signal_return=True)
    notification = FakeNotification()
    _setup_run_globals(monkeypatch, strategy, notification)

    # 不应抛；主流程走完并发送汇总通知
    asyncio.run(main_mod.run_strategy())

    # persist_signal 确实被调用（side_effect 被触发）
    patched_deps.persist_signal.assert_awaited_once()
    # 主流程汇总通知照常发出
    assert len(notification.sent) == 1
    # 告警应该**不会**被 persist_signal 的内部告警触发——因为我们让它直接抛，
    # 内部 try/except 还没来得及走就炸了；但 run_strategy 外层 try/except 会捕获
    # 这个 symbol 级异常，不会影响下一个 symbol（本测试只有一个 symbol，continue 后结束）
