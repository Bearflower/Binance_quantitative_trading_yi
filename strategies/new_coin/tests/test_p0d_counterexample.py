"""P0-D 增量补挂「功能测试与反例验证」补充用例（测试工程师独立编写）。

补齐既有 test_p0_replenish_and_registration.py 未覆盖的**守卫侧分支**与边界：
- S4  守卫 missing=[] → 不调用补挂、清空重试计数
- S6  守卫 strict=True 返回 None → 零补挂 + 告警
- S9  守卫补挂失败 → 记录缺口告警
- S10 守卫 entry_price<=0 → 跳过 + 告警（不调用补挂、不改标记）
- 边界：missing=[] / missing=None / DB 空 / DB 异常 / algo_ids 非 dict / tracking 非 dict

所有外部调用（Binance API、DB、通知、K 线）均 mock，不连真实交易所/数据库。
"""

import os
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import strategies.new_coin.executor as executor_module
import strategies.new_coin.strategy as strategy_module
from strategies.new_coin.executor import (
    TradingExecutor,
    _MISSING_SL,
    _MISSING_TP,
    _REPLENISH_FAILED,
)
from strategies.new_coin.strategy import NewCoinStrategy


# ============================================================
# 构造器
# ============================================================

def make_executor(*, stub_orchestration: bool = True) -> TradingExecutor:
    """构造绕过 __init__ 的执行器，注入补全流程所需配置与 mock 依赖。"""
    ex = object.__new__(TradingExecutor)
    ex.replenish_skip_symbols = set()
    ex.replenish_cancel_after_ready = True
    ex.replenish_ignore_error_codes = ['-4164', '-2011', '-2021', '-4136', '-4507']
    ex.replenish_require_take_profit = True
    ex._replenished_symbols = set()
    ex.min_notional = Decimal('5')
    ex.limit_order_slippage = Decimal('0.001')
    ex.stop_loss_percent = Decimal('0.05')
    ex.emergency_stop_trigger_percent = Decimal('0.015')
    ex.atr_stop_multiplier = Decimal('2.5')
    ex.target1_atr_multiplier = Decimal('1.5')
    ex.target1_close_percent = Decimal('0.30')
    ex.target2_atr_multiplier = Decimal('3.5')
    ex.target2_close_percent = Decimal('0.40')
    ex.ensure_active_before_use = False
    ex.ensure_active_retries = 2
    ex.ensure_active_retry_interval = 0
    ex.kline_interval = '1h'
    ex._registered_symbols = set()
    ex.position_tracking = {}
    ex._last_tracked_qty = {}
    ex.alert_throttle_seconds = 3600.0
    ex._notify_ts = {}
    ex.db = MagicMock()
    ex.binance_api = MagicMock()
    ex.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 1001})
    ex.binance_api.place_order = AsyncMock(return_value={})
    ex.kline_service = MagicMock()
    ex.kline_service.register_symbol = AsyncMock(return_value=True)
    ex._resolve_short_quantity = AsyncMock(return_value=Decimal('10'))
    ex._calculate_atr = AsyncMock(return_value=Decimal('1.0'))
    ex._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))
    ex._get_current_price = AsyncMock(return_value=Decimal('100'))
    ex._build_tracking_entry = MagicMock(return_value={'algo_ids': {}})
    ex.cancel_all_algo_orders = AsyncMock(return_value={'failed': 0})
    if stub_orchestration:
        ex._place_conditional_and_record = AsyncMock(return_value=True)
    return ex


def make_guard_strategy() -> NewCoinStrategy:
    """构造绕过 __init__ 的策略实例，隔离 _guard_symbol 的协作者。"""
    s = object.__new__(NewCoinStrategy)
    s._guard_inflight = set()
    s._protection_gap_attempts = {}
    s.trading_executor = MagicMock()
    s.trading_executor.find_missing_protection = AsyncMock()
    s.trading_executor.replenish_conditional_orders = AsyncMock(return_value=True)
    s.trading_executor.reset_replenish_flag = MagicMock()
    s._record_gap_and_alert = AsyncMock()
    s._notify_protection_issue = AsyncMock()
    return s


# ============================================================
# S4 / S6 / S9 / S10：守卫侧分支（_guard_symbol）
# ============================================================

async def test_guard_s6_undeterminable_zero_replenish_and_alert():
    """S6：find_missing_protection(strict=True) → None → 零补挂、零撤单、仅告警。"""
    s = make_guard_strategy()
    s.trading_executor.find_missing_protection = AsyncMock(return_value=None)

    await s._guard_symbol("ACNUSDT", {"entry_price": 100.0})

    s.trading_executor.replenish_conditional_orders.assert_not_awaited()
    s.trading_executor.reset_replenish_flag.assert_not_called()
    s._record_gap_and_alert.assert_awaited_once()
    args = s._record_gap_and_alert.await_args.args
    assert args[0] == "ACNUSDT"
    assert "不可判定" in args[1][0]


async def test_guard_s4_no_gap_no_replenish_and_resets_attempts():
    """S4：missing=[] → 不调用补挂，且清空该 symbol 的重试计数。"""
    s = make_guard_strategy()
    s.trading_executor.find_missing_protection = AsyncMock(return_value=[])
    s._protection_gap_attempts["ACNUSDT"] = 3

    await s._guard_symbol("ACNUSDT", {"entry_price": 100.0})

    s.trading_executor.replenish_conditional_orders.assert_not_awaited()
    s._record_gap_and_alert.assert_not_awaited()
    assert "ACNUSDT" not in s._protection_gap_attempts


async def test_guard_s10_invalid_entry_price_skips_and_notifies():
    """S10：entry_price<=0 → 跳过补挂、不改完成标记、仅告警。"""
    s = make_guard_strategy()
    s.trading_executor.find_missing_protection = AsyncMock(return_value=[_MISSING_SL])

    await s._guard_symbol("ACNUSDT", {"entry_price": 0})

    s.trading_executor.replenish_conditional_orders.assert_not_awaited()
    s.trading_executor.reset_replenish_flag.assert_not_called()
    s._notify_protection_issue.assert_awaited_once_with("ACNUSDT", [_MISSING_SL])


async def test_guard_s10_missing_entry_price_key_defaults_to_zero():
    """S10 边界：db_position 无 entry_price 键 → 视为 0 → 跳过补挂并告警。"""
    s = make_guard_strategy()
    s.trading_executor.find_missing_protection = AsyncMock(return_value=[_MISSING_TP])

    await s._guard_symbol("ACNUSDT", {})

    s.trading_executor.replenish_conditional_orders.assert_not_awaited()
    s._notify_protection_issue.assert_awaited_once()


async def test_guard_s9_partial_failure_records_gap():
    """S9：补挂返回 False → 记录缺口告警（传入原 missing）。"""
    s = make_guard_strategy()
    s.trading_executor.find_missing_protection = AsyncMock(
        return_value=[_MISSING_SL, _MISSING_TP]
    )
    s.trading_executor.replenish_conditional_orders = AsyncMock(return_value=False)

    await s._guard_symbol("ACNUSDT", {"entry_price": 100.0})

    s.trading_executor.reset_replenish_flag.assert_called_once_with("ACNUSDT")
    s._record_gap_and_alert.assert_awaited_once_with("ACNUSDT", [_MISSING_SL, _MISSING_TP])


async def test_guard_success_no_alert_and_flag_reset_before_replenish():
    """正例：缺 SL、补挂成功 → 先 reset 标记再补挂，成功后不告警。"""
    s = make_guard_strategy()
    s.trading_executor.find_missing_protection = AsyncMock(return_value=[_MISSING_SL])
    calls = []
    s.trading_executor.reset_replenish_flag = MagicMock(side_effect=lambda *_: calls.append('reset'))

    async def _rep(*a, **k):
        calls.append('rep')
        return True

    s.trading_executor.replenish_conditional_orders = _rep
    await s._guard_symbol("ACNUSDT", {"entry_price": 100.0})

    assert calls == ['reset', 'rep']
    s._record_gap_and_alert.assert_not_awaited()


# ============================================================
# _guard_protection_orders：守卫入口全分支（L1138-1160）
# ============================================================

def _make_guard_loop_strategy():
    """构造仅注入 _guard_protection_orders 循环所需属性的策略实例。"""
    s = object.__new__(NewCoinStrategy)
    s.trading_executor = MagicMock()
    s._guard_last_run_at = 0.0
    s.guard_interval_seconds = 0
    s._guard_inflight = set()
    s._compute_authoritative_symbols = AsyncMock(return_value=set())
    s._load_open_positions_from_db = AsyncMock(return_value={})
    s._guard_symbol = AsyncMock()
    return s


async def test_guard_loop_returns_immediately_without_executor():
    """L1138-1139：trading_executor 为 None → 直接 return，不取数。"""
    s = _make_guard_loop_strategy()
    s.trading_executor = None

    await s._guard_protection_orders()   # 不得抛异常

    s._compute_authoritative_symbols.assert_not_awaited()
    s._guard_symbol.assert_not_awaited()


async def test_guard_loop_skips_within_interval(monkeypatch):
    """L1141-1142：距上轮未满 guard_interval_seconds → 早退，不取数。"""
    s = _make_guard_loop_strategy()
    s.guard_interval_seconds = 300
    s._guard_last_run_at = 1000.0
    monkeypatch.setattr(strategy_module.time, "monotonic", lambda: 1100.0)  # 差 100 < 300

    await s._guard_protection_orders()

    s._compute_authoritative_symbols.assert_not_awaited()
    s._guard_symbol.assert_not_awaited()


async def test_guard_loop_unknown_authoritative_returns_and_keeps_timestamp(monkeypatch):
    """L1144-1146：authoritative 为 None（取数失败）→ return 且**不推进**时间戳。"""
    s = _make_guard_loop_strategy()
    s._guard_last_run_at = 1000.0
    s._compute_authoritative_symbols = AsyncMock(return_value=None)
    monkeypatch.setattr(strategy_module.time, "monotonic", lambda: 5000.0)

    await s._guard_protection_orders()

    assert s._guard_last_run_at == 1000.0        # fail-closed：未推进，下一轮立即可重试
    s._load_open_positions_from_db.assert_not_awaited()
    s._guard_symbol.assert_not_awaited()


async def test_guard_loop_empty_authoritative_returns_without_guard_symbol(monkeypatch):
    """L1148-1149：authoritative 为空集 → 推进时间戳后早退，不加载持仓、不核验。"""
    s = _make_guard_loop_strategy()
    s._guard_last_run_at = 1000.0
    s._compute_authoritative_symbols = AsyncMock(return_value=set())
    monkeypatch.setattr(strategy_module.time, "monotonic", lambda: 5000.0)

    await s._guard_protection_orders()

    assert s._guard_last_run_at == 5000.0        # 空集：已推进时间戳
    s._load_open_positions_from_db.assert_not_awaited()
    s._guard_symbol.assert_not_awaited()


async def test_guard_loop_processes_symbols_and_cleans_inflight():
    """L1150-1160 正常路径：逐标的调用 _guard_symbol（带 db_position），finally 清理 inflight。"""
    s = _make_guard_loop_strategy()
    s._compute_authoritative_symbols = AsyncMock(return_value={"BBBUSDT", "AAAUSDT"})
    s._load_open_positions_from_db = AsyncMock(
        return_value={"AAAUSDT": {"entry_price": 10}, "BBBUSDT": {"entry_price": 20}}
    )
    seen, inflight_during = {}, {}

    async def fake_guard(symbol, db_position):
        seen[symbol] = db_position
        inflight_during[symbol] = set(s._guard_inflight)

    s._guard_symbol = AsyncMock(side_effect=fake_guard)

    await s._guard_protection_orders()

    assert [c.args[0] for c in s._guard_symbol.await_args_list] == ["AAAUSDT", "BBBUSDT"]
    assert seen == {"AAAUSDT": {"entry_price": 10}, "BBBUSDT": {"entry_price": 20}}
    assert inflight_during["AAAUSDT"] == {"AAAUSDT"}   # add 生效（处理期间在途）
    assert inflight_during["BBBUSDT"] == {"BBBUSDT"}
    assert s._guard_inflight == set()                  # finally 已 discard


async def test_guard_loop_skips_symbol_already_inflight():
    """L1152-1153：已在途（_guard_inflight 命中）的标的被 continue 跳过、不重复核验。"""
    s = _make_guard_loop_strategy()
    s._compute_authoritative_symbols = AsyncMock(return_value={"AAAUSDT", "BBBUSDT"})
    s._load_open_positions_from_db = AsyncMock(return_value={})
    s._guard_inflight = {"AAAUSDT"}   # 模拟上一轮尚未结束的在途标的

    await s._guard_protection_orders()

    assert [c.args[0] for c in s._guard_symbol.await_args_list] == ["BBBUSDT"]
    assert s._guard_inflight == {"AAAUSDT"}   # 在途标的保持原样、未被误删


async def test_guard_loop_isolates_symbol_exception_and_cleans_inflight():
    """L1155-1160 异常隔离：单标的抛异常被 catch，其余标的仍处理，inflight 仍清理。"""
    s = _make_guard_loop_strategy()
    s._compute_authoritative_symbols = AsyncMock(return_value={"AAAUSDT", "BBBUSDT"})
    s._load_open_positions_from_db = AsyncMock(return_value={})
    called = []

    async def fake_guard(symbol, db_position):
        called.append(symbol)
        if symbol == "AAAUSDT":
            raise RuntimeError("单标的核验异常")

    s._guard_symbol = AsyncMock(side_effect=fake_guard)

    await s._guard_protection_orders()   # 不得外抛

    assert called == ["AAAUSDT", "BBBUSDT"]   # 异常未阻断后续标的
    assert s._guard_inflight == set()         # finally 已清理


# ============================================================
# 守卫侧告警：_record_gap_and_alert + _notify_protection_issue（真实实现）
# ============================================================

def _make_real_alert_strategy(*, should_notify: bool, send_raises: bool = False):
    """构造带真实告警实现的策略实例（仅 mock 通知客户端与降频判定）。"""
    s = object.__new__(NewCoinStrategy)
    s._protection_gap_attempts = {}
    s.config = {"notification": {"project": "new_coin"}}
    s.notification_client = MagicMock()
    s.notification_client.send = AsyncMock()
    if send_raises:
        s.notification_client.send = AsyncMock(side_effect=Exception("通知网关异常"))
    ex = MagicMock()
    ex.alert_throttle_seconds = 3600.0
    ex.should_notify = MagicMock(return_value=should_notify)
    s.trading_executor = ex
    return s


async def test_record_gap_increments_attempts_and_sends_alert():
    """真实实现：累计重试次数递增，文案含 symbol / 缺失类型 / 次数。"""
    s = _make_real_alert_strategy(should_notify=True)

    await s._record_gap_and_alert("ACNUSDT", [_MISSING_SL])

    assert s._protection_gap_attempts["ACNUSDT"] == 1
    s.notification_client.send.assert_awaited_once()
    msg = s.notification_client.send.await_args.kwargs["message"]
    assert "ACNUSDT" in msg
    assert _MISSING_SL in msg
    assert "累计重试次数: 1" in msg

    # 再次触发 → 计数递增（降频由 should_notify 决定，此处为 True 故仍发送）
    await s._record_gap_and_alert("ACNUSDT", [_MISSING_SL])
    assert s._protection_gap_attempts["ACNUSDT"] == 2


async def test_notify_real_throttle_suppresses_second_send():
    """降频真实现：alert_throttle_seconds 窗口内仅发送 1 次（P0-D-AC17）。"""
    s = _make_real_alert_strategy(should_notify=True)
    real_ex = object.__new__(TradingExecutor)
    real_ex._notify_ts = {}
    s.trading_executor.should_notify = real_ex.should_notify.__get__(
        real_ex, TradingExecutor
    )
    s.trading_executor.alert_throttle_seconds = 3600.0

    await s._record_gap_and_alert("ACNUSDT", [_MISSING_SL])
    await s._record_gap_and_alert("ACNUSDT", [_MISSING_SL])

    assert s.notification_client.send.await_count == 1  # 窗口内仅 1 次
    assert s._protection_gap_attempts["ACNUSDT"] == 2   # 计数仍递增


async def test_notify_throttled_skips_send():
    """降频窗口内（should_notify=False）→ 不发送通知。"""
    s = _make_real_alert_strategy(should_notify=False)

    await s._record_gap_and_alert("ACNUSDT", [_MISSING_TP])

    assert s._protection_gap_attempts["ACNUSDT"] == 1
    s.notification_client.send.assert_not_awaited()


async def test_notify_send_exception_is_swallowed():
    """通知发送抛异常 → 不外抛（主流程不受影响）。"""
    s = _make_real_alert_strategy(should_notify=True, send_raises=True)

    await s._record_gap_and_alert("ACNUSDT", [_MISSING_TP])  # 不得抛异常

    assert s._protection_gap_attempts["ACNUSDT"] == 1


# ============================================================
# S7：Phase A 四类失败 → 零撤单零挂单（参数化，端到端）
# ============================================================

@pytest.mark.parametrize(
    "inject",
    ["atr_zero", "precision_zero", "price_zero", "ensure_active_fail"],
)
async def test_executor_s7_phase_a_failures_zero_touch(inject):
    """S7：ATR=0 / 精度失败 / 现价失败 / ensure-active 失败 → 零撤单零挂单、False、不置位。"""
    ex = make_executor()
    if inject == "atr_zero":
        ex._calculate_atr = AsyncMock(return_value=Decimal('0'))
    elif inject == "precision_zero":
        ex._get_symbol_precision = AsyncMock(return_value=(Decimal('0'), Decimal('0.001')))
    elif inject == "price_zero":
        ex._get_current_price = AsyncMock(return_value=Decimal('0'))
    elif inject == "ensure_active_fail":
        ex.ensure_active_before_use = True
        ex.kline_service.register_symbol = AsyncMock(return_value=False)

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=[_MISSING_SL, _MISSING_TP]
    )

    assert result is False
    ex.cancel_all_algo_orders.assert_not_awaited()
    ex._place_conditional_and_record.assert_not_awaited()
    assert "ACNUSDT" not in ex._replenished_symbols


# ============================================================
# S2：端到端零撤单（真实编排层，核心资金安全点）
# ============================================================

async def test_executor_s2_end_to_end_zero_cancel_real_orchestration():
    """S2 核心止血：仅缺 TP、SL 已存在 → 真实挂单编排下 SL 原样、零撤单、不挂 SL。

    注：方案甲「缺 TP 即视为 TP 全缺」→ 重新挂 TP1+TP2，同名 tp1 键被新 algo_id 覆盖
    （DB 判定为无 OPEN TAKE_PROFIT，旧 tp1=222 属陈旧缓存，覆盖无害）。
    """
    ex = make_executor(stub_orchestration=False)
    ex.position_tracking["ACNUSDT"] = {"algo_ids": {"sl": 111, "tp1": 222}}
    ex.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 333})

    with patch.object(executor_module, "record_condition_order", new=AsyncMock()):
        result = await ex.replenish_conditional_orders(
            "ACNUSDT", Decimal('100'), missing=[_MISSING_TP]
        )

    assert result is True
    ex.cancel_all_algo_orders.assert_not_awaited()          # INV-1 零撤单
    ex.binance_api.place_order.assert_not_awaited()         # 无市价平仓
    algo_ids = ex.position_tracking["ACNUSDT"]["algo_ids"]
    assert algo_ids["sl"] == 111                            # SL 原样保留（INV-4，核心止血）
    # 交易所侧只收到 2 条 TAKE_PROFIT 下单，没有任何 STOP(SL) 下单
    order_types = [c.kwargs["order_type"] for c in ex.binance_api.place_conditional_order.await_args_list]
    assert order_types == ['TAKE_PROFIT', 'TAKE_PROFIT']    # 方案甲：缺 TP → 补 TP1+TP2，不挂 SL


async def test_executor_s8_idempotent_error_end_to_end_marks_replenished():
    """S8：全缺时挂单命中幂等错误码 → 视为成功、True 且置位。"""
    ex = make_executor(stub_orchestration=False)
    ex.binance_api.place_conditional_order = AsyncMock(
        side_effect=Exception("-2021 Order would immediately trigger")
    )

    with patch.object(executor_module, "record_condition_order", new=AsyncMock()):
        result = await ex.replenish_conditional_orders(
            "ACNUSDT", Decimal('100'), missing=[_MISSING_SL, _MISSING_TP]
        )

    assert result is True
    ex.cancel_all_algo_orders.assert_not_awaited()
    assert "ACNUSDT" in ex._replenished_symbols


# ============================================================
# 边界：missing 取值异常
# ============================================================

async def test_executor_missing_empty_list_fails_closed_no_mark():
    """入口防御（fail-closed）：missing=[]（调用方违约）→ 返回 False、零撤单、
    未挂任何单、**不置位** `_replenished_symbols`。

    修复前该场景会因 place_sl/place_tp 均 False → all_success=True 而误置位（fail-open）；
    新增显式入口守卫后收敛为 fail-closed。
    """
    ex = make_executor()
    result = await ex.replenish_conditional_orders("ACNUSDT", Decimal('100'), missing=[])

    assert result is False
    ex.cancel_all_algo_orders.assert_not_awaited()
    ex._place_conditional_and_record.assert_not_awaited()
    assert "ACNUSDT" not in ex._replenished_symbols


async def test_executor_missing_none_fails_closed_no_placement():
    """入口防御：missing=None（类型错误）→ 显式守卫直接返回 False、
    零挂单、零撤单、不置位（不依赖 TypeError 异常路径）。
    """
    ex = make_executor()
    result = await ex.replenish_conditional_orders("ACNUSDT", Decimal('100'), missing=None)

    assert result is False
    ex.cancel_all_algo_orders.assert_not_awaited()
    ex._place_conditional_and_record.assert_not_awaited()
    assert "ACNUSDT" not in ex._replenished_symbols


# ============================================================
# 边界：_backfill_algo_ids_from_db 四类未覆盖分支
# ============================================================

async def test_backfill_tracking_entry_non_dict_returns_early():
    """边界：position_tracking[symbol] 非 dict → 直接返回、不查 DB。"""
    ex = make_executor()
    ex.position_tracking["ACNUSDT"] = "not-a-dict"
    ex.db.fetch_all = AsyncMock(return_value=[{"algo_id": 1, "order_type": "STOP_LOSS"}])

    await ex._backfill_algo_ids_from_db("ACNUSDT")

    ex.db.fetch_all.assert_not_awaited()


async def test_backfill_algo_ids_non_dict_returns_early():
    """边界：algo_ids 非 dict → 直接返回、不查 DB（防 setdefault 崩溃）。"""
    ex = make_executor()
    ex.position_tracking["ACNUSDT"] = {"algo_ids": None}
    ex.db.fetch_all = AsyncMock(return_value=[{"algo_id": 1, "order_type": "STOP_LOSS"}])

    await ex._backfill_algo_ids_from_db("ACNUSDT")

    ex.db.fetch_all.assert_not_awaited()
    assert ex.position_tracking["ACNUSDT"]["algo_ids"] is None


async def test_backfill_db_empty_list_no_change():
    """边界：DB 返回空列表 → algo_ids 不变、不抛异常。"""
    ex = make_executor()
    ex.position_tracking["ACNUSDT"] = {"algo_ids": {}}
    ex.db.fetch_all = AsyncMock(return_value=[])

    await ex._backfill_algo_ids_from_db("ACNUSDT")

    assert ex.position_tracking["ACNUSDT"]["algo_ids"] == {}


async def test_backfill_row_without_algo_id_and_unknown_type_ignored():
    """边界：algo_id 为 None 的行被跳过；未知 order_type 被忽略；只填已知类型。"""
    ex = make_executor()
    ex.position_tracking["ACNUSDT"] = {"algo_ids": {}}
    ex.db.fetch_all = AsyncMock(return_value=[
        {"algo_id": None, "order_type": "STOP_LOSS"},   # 跳过
        {"algo_id": 88, "order_type": "LIMIT"},          # 未知类型忽略
        {"algo_id": 99, "order_type": "stop_loss"},      # 小写 → upper 命中
    ])

    await ex._backfill_algo_ids_from_db("ACNUSDT")

    assert ex.position_tracking["ACNUSDT"]["algo_ids"] == {"sl": 99}


# ============================================================
# find_missing_protection 正/反例
# ============================================================

@pytest.mark.parametrize(
    "rows,expected",
    [
        ([{"order_type": "STOP_LOSS"}, {"order_type": "TAKE_PROFIT"}], []),
        ([{"order_type": "STOP_LOSS"}], [_MISSING_TP]),
        ([{"order_type": "TAKE_PROFIT"}], [_MISSING_SL]),
        ([], [_MISSING_SL, _MISSING_TP]),
        (None, [_MISSING_SL, _MISSING_TP]),
    ],
    ids=["both", "sl_only", "tp_only", "empty", "none_rows"],
)
async def test_find_missing_protection_matrix(rows, expected):
    """矩阵：DB 中 OPEN 类型组合 → 缺失类型判定。"""
    ex = make_executor()
    ex.db.fetch_all = AsyncMock(return_value=rows)
    assert await ex.find_missing_protection("ACNUSDT") == expected


async def test_find_missing_protection_without_tp_requirement():
    """require_take_profit=False → 只关注止损单缺失。"""
    ex = make_executor()
    ex.db.fetch_all = AsyncMock(return_value=[])
    assert await ex.find_missing_protection("ACNUSDT", require_take_profit=False) == [_MISSING_SL]


async def test_find_missing_protection_strict_false_error_returns_full_without_tp():
    """strict=False + 查询异常 + require_take_profit=False → fail-closed 仅返回止损单。"""
    ex = make_executor()
    ex.db.fetch_all = AsyncMock(side_effect=Exception("DB 抖动"))
    assert await ex.find_missing_protection(
        "ACNUSDT", strict=False, require_take_profit=False
    ) == [_MISSING_SL]


async def test_find_missing_protection_strict_true_error_returns_none_without_tp():
    """strict=True + 查询异常 → None（不可判定，与是否要求 TP 无关）。"""
    ex = make_executor()
    ex.db.fetch_all = AsyncMock(side_effect=Exception("DB 抖动"))
    assert await ex.find_missing_protection(
        "ACNUSDT", strict=True, require_take_profit=False
    ) is None


# ============================================================
# _place_protection_orders 组合分支
# ============================================================

async def test_place_protection_orders_sl_only():
    """place_sl=True / place_tp=False → 只挂 SL，不进入 TP 分支。"""
    ex = make_executor()
    ex._apply_take_profit = AsyncMock(return_value=True)
    plans = {
        'quantity': Decimal('10'), 'entry_price': Decimal('100'), 'atr': Decimal('1'),
        'sl': {'price': Decimal('105'), 'limit_price': Decimal('105.1')},
        'tp1': {'skip': True}, 'tp2': {'skip': True},
    }
    assert await ex._place_protection_orders(
        "ACNUSDT", plans, place_sl=True, place_tp=False
    ) is True
    ex._place_conditional_and_record.assert_awaited_once()
    ex._apply_take_profit.assert_not_awaited()


async def test_place_protection_orders_tp_only_keeps_sl_untouched():
    """place_sl=False / place_tp=True → 不挂 SL（既有 SL 不动），只走 TP1+TP2。"""
    ex = make_executor()
    ex._apply_take_profit = AsyncMock(return_value=True)
    plans = {
        'quantity': Decimal('10'), 'entry_price': Decimal('100'), 'atr': Decimal('1'),
        'sl': {'price': Decimal('105'), 'limit_price': Decimal('105.1')},
        'tp1': {'skip': True}, 'tp2': {'skip': True},
    }
    assert await ex._place_protection_orders(
        "ACNUSDT", plans, place_sl=False, place_tp=True
    ) is True
    ex._place_conditional_and_record.assert_not_awaited()
    assert ex._apply_take_profit.await_count == 2
    levels = [c.args[2] for c in ex._apply_take_profit.await_args_list]
    assert levels == [1, 2]
