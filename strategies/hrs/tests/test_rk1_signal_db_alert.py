"""
RK1: HRS 信号落库/更新失败可识别告警——测试覆盖

生产新增：strategy.py::_alert_signal_db_failure（L4493-4542），收敛三处失败路径：
  1. _log_signal_to_db 的 INSERT 异常 → kind=insert_failed
  2. _update_signal_resolution 的 signal_id=None fail-safe → kind=signal_id_missing
  3. _update_signal_resolution 的 UPDATE 异常 → kind=update_failed
开关：config.yaml::notification.events.signal_db_failure（默认 true）

本测试复用 test_r9_signal_attribution 的 TightSignalDB / _make_strategy 夹具风格，
最小补齐通知相关属性（_notif_enabled / _notif_events / notification_client）。
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from strategies.hrs.tests.test_r9_signal_attribution import (
    TightSignalDB,
    _make_strategy,
)


# ==================== 通知夹具 ====================


def _attach_notifier(strategy, enabled=True, event_switch=True):
    """注入通知相关属性到 strategy 实例。

    生产代码 _alert_signal_db_failure 检查：
      hasattr(self, '_notif_enabled') → self._notif_enabled → self._should_notify(kind)
    本夹具同时设置三层，确保开关判断路径全覆盖。
    """
    strategy._notif_enabled = enabled
    strategy._notif_events = {"signal_db_failure": event_switch}
    strategy.notification_client = AsyncMock()
    return strategy


# ==================== 直接测试 _alert_signal_db_failure 三 kind ====================


class TestAlertDirectCall:
    """直接调用 _alert_signal_db_failure，验证三个 kind 各自触发一次 send_alert。"""

    @pytest.mark.asyncio
    async def test_insert_failed_kind_sends_alert(self):
        """kind=insert_failed → send_alert 被调用 1 次，message 含关键信息。"""
        s = _attach_notifier(_make_strategy(TightSignalDB()))
        await s._alert_signal_db_failure(
            "insert_failed",
            symbol="BTCUSDT",
            direction="short",
            error="模拟DB不可用",
        )
        s.notification_client.send_alert.assert_awaited_once()
        call_kwargs = s.notification_client.send_alert.await_args.kwargs
        assert call_kwargs["title"] == "HRS信号落库失败"
        assert call_kwargs["level"] == "warning"
        msg = call_kwargs["message"]
        # message 必须携带传入的关键信息
        for token in ("kind=insert_failed", "symbol=BTCUSDT",
                      "direction=short", "error=模拟DB不可用"):
            assert token in msg, f"message 缺少 {token!r}，实际 {msg}"

    @pytest.mark.asyncio
    async def test_signal_id_missing_kind_sends_alert(self):
        """kind=signal_id_missing → message 含 resolution。"""
        s = _attach_notifier(_make_strategy(TightSignalDB()))
        await s._alert_signal_db_failure(
            "signal_id_missing",
            resolution="executed",
        )
        s.notification_client.send_alert.assert_awaited_once()
        call_kwargs = s.notification_client.send_alert.await_args.kwargs
        assert call_kwargs["title"] == "HRS信号落库失败"
        assert call_kwargs["level"] == "warning"
        msg = call_kwargs["message"]
        for token in ("kind=signal_id_missing", "resolution=executed"):
            assert token in msg, f"message 缺少 {token!r}，实际 {msg}"

    @pytest.mark.asyncio
    async def test_update_failed_kind_sends_alert(self):
        """kind=update_failed → message 含 signal_id / resolution / error。"""
        s = _attach_notifier(_make_strategy(TightSignalDB()))
        await s._alert_signal_db_failure(
            "update_failed",
            signal_id=42,
            resolution="timeout",
            error="UPDATE行数异常",
        )
        s.notification_client.send_alert.assert_awaited_once()
        call_kwargs = s.notification_client.send_alert.await_args.kwargs
        assert call_kwargs["title"] == "HRS信号落库失败"
        assert call_kwargs["level"] == "warning"
        msg = call_kwargs["message"]
        for token in ("kind=update_failed", "signal_id=42",
                      "resolution=timeout", "error=UPDATE行数异常"):
            assert token in msg, f"message 缺少 {token!r}，实际 {msg}"


# ==================== 开关判断 ====================


class TestAlertSwitch:
    """告警开关关闭时不发送。"""

    @pytest.mark.asyncio
    async def test_event_switch_off_no_alert(self):
        """notification.events.signal_db_failure=false → 不发。"""
        s = _attach_notifier(_make_strategy(TightSignalDB()),
                             enabled=True, event_switch=False)
        await s._alert_signal_db_failure(
            "insert_failed", symbol="BTCUSDT", error="x",
        )
        s.notification_client.send_alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notif_enabled_false_no_alert(self):
        """_notif_enabled=false（通知总开关关）→ 不发。"""
        s = _attach_notifier(_make_strategy(TightSignalDB()),
                             enabled=False, event_switch=True)
        await s._alert_signal_db_failure(
            "insert_failed", symbol="BTCUSDT", error="x",
        )
        s.notification_client.send_alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_notif_enabled_attr_no_alert(self):
        """hasattr(self, '_notif_enabled') 为假（最小 fixture）→ 防御性跳过。"""
        s = _make_strategy(TightSignalDB())
        # 不注入 _notif_enabled 属性，模拟"最小 fixture 未初始化通知"场景
        await s._alert_signal_db_failure(
            "insert_failed", symbol="BTCUSDT", error="x",
        )
        # 生产函数因 hasattr 检查直接 return，notification_client 未被调用
        # （若未注入 notification_client 也不会报错——函数在 hasattr 处就 return 了）


# ==================== 三处失败路径集成验证 ====================


class TestAlertCallSites:
    """验证生产三处失败路径各自能触发告警。"""

    @pytest.mark.asyncio
    async def test_insert_failure_triggers_alert_via_finalize(self):
        """路径 1：_log_signal_to_db INSERT 抛异常 → kind=insert_failed 告警。

        级联行为（生产实际）：INSERT 失败 → signal_id=None → _update_signal_resolution
        也发 kind=signal_id_missing。本测试断言「至少有一次 kind=insert_failed」，
        signal_id_missing 的级联验证已在下方 TestAlertSignalIdMissing 独立覆盖。
        """

        class BrokenInsertDB(TightSignalDB):
            """INSERT RETURNING 模拟 DB 不可用。"""
            async def fetch_one(self, sql: str, *args, **kwargs):
                if "INSERT INTO HRS.HRS_SIGNALS" in sql.upper():
                    raise RuntimeError("模拟 DB 不可用")
                return None

        s = _attach_notifier(_make_strategy(BrokenInsertDB()))
        # 注入 execute_signal：INSERT 失败不应阻断主链
        async def _execute(signal):
            signal["shadow_blocked"] = False
            return True
        s.execute_signal = _execute

        signal = dict(
            symbol="TESTUSDT", direction="short", score=9,
            entry_mode="standard", signal_origin="main", current_price=100,
        )
        await s._finalize_and_execute_signals(
            short_signals=[signal], long_signals=[],
            source_symbols=["TESTUSDT"],
        )

        # 断言：send_alert 至少被调用一次（INSERT 失败 + signal_id 缺失级联），
        # 且其中必须包含 kind=insert_failed（本用例的主验证目标）
        call_args_list = s.notification_client.send_alert.await_args_list
        assert len(call_args_list) >= 1, "失败路径应触发至少一次告警"
        all_msgs = [c.kwargs["message"] for c in call_args_list]
        assert any("kind=insert_failed" in m for m in all_msgs), (
            f"未发现 kind=insert_failed 的告警，实际 messages={all_msgs}"
        )
        # 额外断言：insert_failed 的 message 里必须有 symbol
        insert_msg = next(m for m in all_msgs if "kind=insert_failed" in m)
        assert "symbol=TESTUSDT" in insert_msg

    @pytest.mark.asyncio
    async def test_signal_id_missing_triggers_alert(self):
        """路径 2：直接调 _update_signal_resolution(signal_id=None) → kind=signal_id_missing。"""
        s = _attach_notifier(_make_strategy(TightSignalDB()))
        # 注意：此路径无 symbol（生产代码也未传 symbol）
        await s._update_signal_resolution(None, "executed")

        s.notification_client.send_alert.assert_awaited_once()
        call_kwargs = s.notification_client.send_alert.await_args.kwargs
        msg = call_kwargs["message"]
        assert "kind=signal_id_missing" in msg
        assert "resolution=executed" in msg

    @pytest.mark.asyncio
    async def test_update_failure_triggers_alert(self):
        """路径 3：_update_signal_resolution UPDATE 抛异常 → kind=update_failed。"""

        class BrokenUpdateDB(TightSignalDB):
            """UPDATE 模拟 DB 不可用。"""
            async def execute(self, sql: str, *args, **kwargs):
                if "UPDATE HRS.HRS_SIGNALS" in sql.upper():
                    raise RuntimeError("UPDATE 连接丢失")
                await super().execute(sql, *args, **kwargs)

        # 先让 INSERT 正常落库拿到 signal_id
        db = BrokenUpdateDB()
        # 先手动 INSERT 一条，确保有 signal_id 可传
        await db.fetch_one(
            "INSERT INTO hrs.hrs_signals RETURNING id",
            "TESTUSDT", "short", 8.0, 5.0, 3.0, 0.0,
            100.0, "executed", "none", 50.0,
            "standard", "main",
        )

        s = _attach_notifier(_make_strategy(db))
        # 直接调 UPDATE 路径（signal_id=1，已落库的那条）
        await s._update_signal_resolution(1, "executed")

        s.notification_client.send_alert.assert_awaited_once()
        call_kwargs = s.notification_client.send_alert.await_args.kwargs
        msg = call_kwargs["message"]
        assert "kind=update_failed" in msg
        assert "signal_id=1" in msg
        assert "resolution=executed" in msg
        assert "UPDATE 连接丢失" in msg


# ==================== 正常路径不发告警 ====================


class TestNoAlertOnSuccess:
    """成功路径不应发送告警，避免告警放大。"""

    @pytest.mark.asyncio
    async def test_normal_finalize_no_alert(self):
        """正常 _finalize_and_execute_signals → send_alert 从未被调用。"""
        s = _attach_notifier(_make_strategy(TightSignalDB()))

        async def _execute(signal):
            signal["shadow_blocked"] = False
            return True
        s.execute_signal = _execute

        signal = dict(
            symbol="TESTUSDT", direction="short", score=9,
            entry_mode="standard", signal_origin="main", current_price=100,
        )
        await s._finalize_and_execute_signals(
            short_signals=[signal], long_signals=[],
            source_symbols=["TESTUSDT"],
        )

        # 断言：send_alert 从未被调用
        s.notification_client.send_alert.assert_not_awaited()


# ==================== 告警自身失败的兜底 ====================


class TestAlertSendFailureSafelySwallowed:
    """_alert_signal_db_failure 内 send_alert 抛异常时的兜底（L4541-4542）。

    生产实现：try/except 包裹 send_alert，except 内仅 logger.warning，
    不 re-raise → 调用方不会感知到告警失败，主流程不中断。
    """

    @pytest.mark.asyncio
    async def test_send_alert_exception_swallowed(self):
        """send_alert 抛 RuntimeError → _alert_signal_db_failure 不向外抛。"""
        s = _attach_notifier(_make_strategy(TightSignalDB()))
        # 把 mock 配成抛异常（AsyncMock 默认返回 None，这里显式改）
        s.notification_client.send_alert.side_effect = RuntimeError("飞书 webhook 超时")

        # 关键断言：调用不抛异常（兜底吞掉）
        try:
            await s._alert_signal_db_failure(
                "insert_failed", symbol="TESTUSDT", error="boom",
            )
        except Exception as caught:  # noqa: BLE001 —— 这里就是要抓所有异常
            pytest.fail(
                f"send_alert 抛异常时 _alert_signal_db_failure 不应向外传播，"
                f"实际捕获到 {type(caught).__name__}: {caught}"
            )

        # 补充：send_alert 确实被调用过（验证我们的 side_effect 被触发）
        s.notification_client.send_alert.assert_awaited_once()
