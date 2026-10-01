"""P0 修复验收测试：new_coin 补全条件单先算后撤 + 持仓标的保留 K 线注册。

覆盖验收标准：
- P0-3-AC1：ATR=0 → 不撤单、告警、返回 False、不置位
- P0-3-AC2：ATR 正常 → 先算后撤 → 挂齐 → True 且置位
- P0-3-AC3：撤单失败 → 不挂新单、返回 False
- P0-3-AC4：撤成功但 TP 挂单失败 → False、不置位、下周期重试
- P0-3-AC5：无空头持仓 → 不撤不挂、返回 True
- P0-3-AC6：已补全过 / 托管清单 → 跳过
- P0-1-AC1/AC4/AC6：有持仓不注销；无持仓才注销；注销失败保留注册态
- P0-1-AC5：ensure-active 失败不进 ATR、不撤单
说明：全部使用假客户端/mock，不连接真实数据库与交易所。
"""

import os
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 保证 `strategies.*` 可导入（与既有用例一致的运行方式）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import strategies.new_coin.executor as executor_module
from strategies.new_coin.executor import (
    TradingExecutor,
    _REPLENISH_FAILED,
    _REPLENISH_READY,
)
from strategies.new_coin.strategy import NewCoinStrategy


# ============================================================
# 执行器构造（绕过 __init__，仅注入补全流程所需属性）
# ============================================================

def make_executor(*, stub_orchestration: bool = True) -> TradingExecutor:
    """构造绕过 __init__ 的执行器，注入补全流程所需的配置与 mock 依赖。

    Args:
        stub_orchestration: True（默认）时把编排层 `_place_conditional_and_record`
            整体桩掉，供只关注调用顺序/失败语义的用例；False 时保留其真实实现，
            仅桩掉 Binance API 与 DB 记录调用，供覆盖真实挂单/记录逻辑的用例。
    """
    ex = object.__new__(TradingExecutor)
    # P0-3 配置
    ex.replenish_skip_symbols = set()
    ex.replenish_cancel_after_ready = True
    ex.replenish_ignore_error_codes = ['-4164', '-2011', '-2021', '-4136', '-4507']
    ex._replenished_symbols = set()
    ex.min_notional = Decimal('5')
    ex.limit_order_slippage = Decimal('0.001')
    # 止盈止损系数
    ex.stop_loss_percent = Decimal('0.05')
    ex.emergency_stop_trigger_percent = Decimal('0.015')
    ex.atr_stop_multiplier = Decimal('2.5')
    ex.target1_atr_multiplier = Decimal('1.5')
    ex.target1_close_percent = Decimal('0.30')
    ex.target2_atr_multiplier = Decimal('3.5')
    ex.target2_close_percent = Decimal('0.40')
    # P0-1 ensure-active 配置（默认关闭，按需在用例中打开）
    ex.ensure_active_before_use = False
    ex.ensure_active_retries = 2
    ex.ensure_active_retry_interval = 0
    ex.kline_interval = '1h'
    ex._registered_symbols = set()
    ex.position_tracking = {}
    ex._last_tracked_qty = {}
    # 依赖 mock：仅桩交易所调用，编排层真实实现默认保留
    ex.db = MagicMock()
    ex.binance_api = MagicMock()
    ex.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 1001})
    ex.binance_api.place_order = AsyncMock(return_value={})
    ex.kline_service = MagicMock()
    ex.kline_service.register_symbol = AsyncMock(return_value=True)
    # 默认可用的只读准备结果
    ex._resolve_short_quantity = AsyncMock(return_value=Decimal('10'))
    ex._calculate_atr = AsyncMock(return_value=Decimal('1.0'))
    ex._get_symbol_precision = AsyncMock(
        return_value=(Decimal('0.01'), Decimal('0.001'))
    )
    ex._get_current_price = AsyncMock(return_value=Decimal('100'))
    ex._build_tracking_entry = MagicMock(return_value={'algo_ids': {}})
    ex.cancel_all_algo_orders = AsyncMock(return_value={'failed': 0})
    if stub_orchestration:
        ex._place_conditional_and_record = AsyncMock(return_value=True)
    return ex


# ============================================================
# P0-3：先算后撤（顺序与失败语义）
# ============================================================

async def test_p0_3_ac1_atr_failure_does_not_cancel():
    """ATR=0 → 不撤单、不挂单、返回 False、不置位（核心止血）。"""
    ex = make_executor()
    ex._calculate_atr = AsyncMock(return_value=Decimal('0'))

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单', '止盈单']
    )

    assert result is False
    ex.cancel_all_algo_orders.assert_not_awaited()
    ex._place_conditional_and_record.assert_not_awaited()
    assert "ACNUSDT" not in ex._replenished_symbols


async def test_p0_3_ac2_incremental_prepare_then_place_no_cancel():
    """P0-D 迁移：默认路径为「只读准备 → 增量挂单」，全缺时挂 3 条、**零撤单**、置位。

    原断言「先撤后挂」已被 P0-D 增量语义取代（默认路径不再撤单，消除 SL 空窗）。
    """
    ex = make_executor()
    order = []

    async def _atr(symbol):
        order.append('atr')
        return Decimal('1.0')

    async def _cancel(symbol):
        order.append('cancel')
        return {'failed': 0}

    async def _place(symbol, **kwargs):
        order.append('place')
        return True

    ex._calculate_atr = _atr
    ex.cancel_all_algo_orders = _cancel
    ex._place_conditional_and_record = _place

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单', '止盈单']
    )

    assert result is True
    assert order[0] == 'atr'          # 先算 ATR（只读）
    assert 'cancel' not in order      # P0-D：默认路径零撤单
    assert order.count('place') == 3  # SL/TP1/TP2
    assert "ACNUSDT" in ex._replenished_symbols


async def test_p0_3_ac3_cancel_failure_blocks_placement():
    """P0-D 迁移：撤单失败阻断挂新单的语义**仅存于回退分支**（cancel_after_ready=False）。"""
    ex = make_executor()
    ex.replenish_cancel_after_ready = False
    ex.cancel_all_algo_orders = AsyncMock(return_value={'failed': 1})

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单', '止盈单']
    )

    assert result is False
    ex._place_conditional_and_record.assert_not_awaited()
    assert "ACNUSDT" not in ex._replenished_symbols


async def test_p0_3_ac4_partial_place_failure_not_marked():
    """全缺时 TP1 挂单失败 → False、不置位（下周期重试）。"""
    ex = make_executor()
    ex._place_conditional_and_record = AsyncMock(side_effect=[True, False, True])

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单', '止盈单']
    )

    assert result is False
    assert "ACNUSDT" not in ex._replenished_symbols


async def test_p0_3_ac5_no_position_returns_true_without_touch():
    """无空头持仓 → 不撤不挂、返回 True（NO_POSITION 视同成功）。"""
    ex = make_executor()
    ex._resolve_short_quantity = AsyncMock(return_value=Decimal('0'))

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单']
    )

    assert result is True
    ex.cancel_all_algo_orders.assert_not_awaited()
    ex._place_conditional_and_record.assert_not_awaited()


async def test_p0_3_ac6_already_replenished_skips():
    """已补全过的币种直接跳过（幂等）。"""
    ex = make_executor()
    ex._replenished_symbols.add("ACNUSDT")

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单']
    )

    assert result is True
    ex.cancel_all_algo_orders.assert_not_awaited()


async def test_p0_3_managed_symbol_skipped_from_config():
    """托管清单（配置化 skip_symbols）命中 → 跳过，不再触碰交易所。"""
    ex = make_executor()
    ex.replenish_skip_symbols = {"BTCUSDT"}

    result = await ex.replenish_conditional_orders(
        "BTCUSDT", Decimal('100'), missing=['止损单']
    )

    assert result is True
    ex.cancel_all_algo_orders.assert_not_awaited()


# ============================================================
# P0-1 策略侧（执行器）：ensure-active
# ============================================================

async def test_p0_1_ac5_ensure_active_failure_skips_atr_and_cancel():
    """ensure-active 全部重试失败 → 不进 ATR、不撤单、返回 False。"""
    ex = make_executor()
    ex.ensure_active_before_use = True
    ex.kline_service.register_symbol = AsyncMock(return_value=False)

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单']
    )

    assert result is False
    assert ex.kline_service.register_symbol.await_count == 2  # 按配置重试 2 次
    ex._calculate_atr.assert_not_awaited()
    ex.cancel_all_algo_orders.assert_not_awaited()


async def test_p0_1_ensure_active_idempotent_when_already_registered():
    """已在本策略注册集合内 → 直接返回 True，不再调用注册接口。"""
    ex = make_executor()
    ex.ensure_active_before_use = True
    ex._registered_symbols.add("ACNUSDT")

    ok = await ex._ensure_symbol_active("ACNUSDT")

    assert ok is True
    ex.kline_service.register_symbol.assert_not_awaited()


async def test_p0_1_ensure_active_registers_once_then_cached():
    """注册成功后写入本地集合，后续调用不再重复注册。"""
    ex = make_executor()
    ex.ensure_active_before_use = True

    assert await ex._ensure_symbol_active("ACNUSDT") is True
    assert "ACNUSDT" in ex._registered_symbols
    assert await ex._ensure_symbol_active("ACNUSDT") is True
    ex.kline_service.register_symbol.assert_awaited_once()


# ============================================================
# P0-1 策略侧：有持仓不注销
# ============================================================

def make_strategy() -> NewCoinStrategy:
    """构造绕过 __init__ 的策略实例，仅注入注销守卫所需属性。"""
    s = object.__new__(NewCoinStrategy)
    s.keep_registration_when_position_open = True
    s.positions = {}
    s._registered_symbols = set()
    s.kline_service = MagicMock()
    s.kline_service.unregister_symbol = AsyncMock(return_value=True)
    s.trading_executor = MagicMock()
    s.trading_executor._has_open_short_position = AsyncMock(return_value=False)
    return s


async def test_p0_1_ac1_keep_registration_when_position_open():
    """内存中有未平持仓 → 不注销、保留注册集合。"""
    s = make_strategy()
    s.positions["ACNUSDT"] = {"entry_price": Decimal('100')}
    s._registered_symbols = {"ACNUSDT"}

    assert await s._unregister_if_safe("ACNUSDT") == (True, False)
    s.kline_service.unregister_symbol.assert_not_awaited()
    assert "ACNUSDT" in s._registered_symbols


async def test_p0_1_ac4_unregister_when_no_position():
    """无持仓（内存与 DB 均无）→ 照旧注销并同步本地缓存。"""
    s = make_strategy()
    s._registered_symbols = {"ACNUSDT"}

    assert await s._unregister_if_safe("ACNUSDT") == (True, True)
    s.kline_service.unregister_symbol.assert_awaited_once()
    assert "ACNUSDT" not in s._registered_symbols


async def test_p0_1_ac6_unregister_failure_keeps_registration():
    """注销失败 → 保留注册集合（不误判），返回 False。"""
    s = make_strategy()
    s._registered_symbols = {"ACNUSDT"}
    s.kline_service.unregister_symbol = AsyncMock(return_value=False)

    assert await s._unregister_if_safe("ACNUSDT") == (False, False)
    assert "ACNUSDT" in s._registered_symbols


async def test_p0_1_has_open_position_db_error_is_conservative():
    """DB 查询异常（default_on_error=True）→ 保守视为有持仓，不注销。"""
    s = make_strategy()
    s.trading_executor._has_open_short_position = AsyncMock(return_value=True)
    s._registered_symbols = {"ACNUSDT"}

    assert await s._has_open_position("ACNUSDT") is True
    assert await s._unregister_if_safe("ACNUSDT") == (True, False)
    s.kline_service.unregister_symbol.assert_not_awaited()


# ============================================================
# P0-3 配置健壮性：ignore_error_codes 写成空值（None）不崩溃
# ============================================================

def test_p0_3_ignore_error_codes_none_falls_back_to_default(monkeypatch):
    """config.yaml 的 ignore_error_codes 写成空值（None）→ 回退默认幂等错误码，不抛 TypeError。

    对齐同函数内 skip_symbols 的 `or []` 兜底写法，避免 `for code in None` 崩溃。
    通过真实调用 __init__ 覆盖配置解析分支。
    """
    # 隔离 CapitalManager 的构造（避免读写真实配置/DB）
    monkeypatch.setattr(
        executor_module, "CapitalManager", MagicMock(return_value=MagicMock())
    )

    config = {
        "strategy": {"name": "new_coin"},
        "trading": {
            "leverage": 2,
            "replenish": {"ignore_error_codes": None, "skip_symbols": None},
        },
    }
    ex = object.__new__(TradingExecutor)
    TradingExecutor.__init__(ex, MagicMock(), MagicMock(), MagicMock(), config)

    assert ex.replenish_ignore_error_codes == [
        '-4164', '-2011', '-2021', '-4136', '-4507'
    ]
    assert ex.replenish_skip_symbols == set()


# ============================================================
# P0-3 资金路径：_place_conditional_and_record 真实实现
# （make_executor(stub_orchestration=False)，仅桩 Binance API 与 DB 记录）
# ============================================================

def _patch_record_condition_order():
    """拦截真实实现内的 record_condition_order（避免落库），并返回该替身。"""
    return patch.object(executor_module, "record_condition_order", new=AsyncMock())


async def test_place_conditional_success_writes_algo_id_and_records():
    """挂单成功（含 algoId）→ True，写 algo_ids 并 await 记录条件单。"""
    ex = make_executor(stub_orchestration=False)
    ex.position_tracking["ACNUSDT"] = {'algo_ids': {}}
    ex.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 123})
    with _patch_record_condition_order() as rec:
        ok = await ex._place_conditional_and_record(
            "ACNUSDT", algo_key='sl', stop_price=Decimal('105'),
            limit_price=Decimal('105.1'), quantity=Decimal('10'),
        )
    assert ok is True
    assert ex.position_tracking["ACNUSDT"]['algo_ids']['sl'] == 123
    rec.assert_awaited_once_with(
        ex.db, "new_coin", "ACNUSDT", algo_id=123, order_type='STOP_LOSS'
    )


async def test_place_conditional_existing_order_code_returns_true():
    """幂等错误码命中 replenish_ignore_error_codes → True（不记录、不写 algo_id）。"""
    ex = make_executor(stub_orchestration=False)
    ex.position_tracking["ACNUSDT"] = {'algo_ids': {}}
    ex.binance_api.place_conditional_order = AsyncMock(
        side_effect=Exception("-2021 Order would immediately trigger")
    )
    with _patch_record_condition_order() as rec:
        ok = await ex._place_conditional_and_record(
            "ACNUSDT", algo_key='tp1', stop_price=Decimal('98'),
            limit_price=Decimal('98.1'), quantity=Decimal('3'),
        )
    assert ok is True
    assert ex.position_tracking["ACNUSDT"]['algo_ids'] == {}
    rec.assert_not_awaited()


async def test_place_conditional_unknown_error_returns_false():
    """非幂等错误码 → False（真实失败）。"""
    ex = make_executor(stub_orchestration=False)
    ex.position_tracking["ACNUSDT"] = {'algo_ids': {}}
    ex.binance_api.place_conditional_order = AsyncMock(
        side_effect=Exception("-9999 boom")
    )
    with _patch_record_condition_order() as rec:
        ok = await ex._place_conditional_and_record(
            "ACNUSDT", algo_key='tp2', stop_price=Decimal('96'),
            limit_price=Decimal('96.1'), quantity=Decimal('4'),
        )
    assert ok is False
    rec.assert_not_awaited()


@pytest.mark.parametrize("result", [{}, {'orderId': 1}], ids=["empty", "no_algo_id"])
async def test_place_conditional_without_algo_id_returns_true_without_write(result):
    """返回结果无 algoId → 不写 algo_ids、不记录，但仍返回 True。"""
    ex = make_executor(stub_orchestration=False)
    ex.position_tracking["ACNUSDT"] = {'algo_ids': {}}
    ex.binance_api.place_conditional_order = AsyncMock(return_value=result)
    with _patch_record_condition_order() as rec:
        ok = await ex._place_conditional_and_record(
            "ACNUSDT", algo_key='sl', stop_price=Decimal('105'),
            limit_price=Decimal('105.1'), quantity=Decimal('10'),
        )
    assert ok is True
    assert ex.position_tracking["ACNUSDT"]['algo_ids'] == {}
    rec.assert_not_awaited()


async def test_place_conditional_symbol_untracked_returns_true_without_write():
    """symbol 不在 position_tracking → 不写 algo_ids、不记录，但仍返回 True。"""
    ex = make_executor(stub_orchestration=False)
    ex.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 9})
    with _patch_record_condition_order() as rec:
        ok = await ex._place_conditional_and_record(
            "ACNUSDT", algo_key='sl', stop_price=Decimal('105'),
            limit_price=Decimal('105.1'), quantity=Decimal('10'),
        )
    assert ok is True
    rec.assert_not_awaited()


# ============================================================
# P0-3：撤单严格/尽力语义
# ============================================================

@pytest.mark.parametrize(
    "outcome,expected",
    [
        (Exception("撤单网络异常"), False),
        ({'failed': 2}, False),
        ({'failed': 0}, True),
        (None, True),  # 非 dict（无 failed）→ 视为 0 失败项
    ],
    ids=["exception", "failed_2", "failed_0", "non_dict"],
)
async def test_cancel_orders_strict(outcome, expected):
    """严格撤单：异常/存在失败项 → False；无失败项 → True。"""
    ex = make_executor()
    if isinstance(outcome, Exception):
        ex.cancel_all_algo_orders = AsyncMock(side_effect=outcome)
    else:
        ex.cancel_all_algo_orders = AsyncMock(return_value=outcome)
    assert await ex._cancel_orders_strict("ACNUSDT") is expected


# ============================================================
# P0-1：ensure-active 重试语义（异常分支）
# ============================================================

async def test_ensure_symbol_active_retry_exception_then_success():
    """首次注册抛异常、重试成功 → True，注册调用 2 次并写入本地集合。"""
    ex = make_executor()
    ex.ensure_active_before_use = True
    ex.kline_service.register_symbol = AsyncMock(
        side_effect=[Exception("瞬时失败"), True]
    )
    assert await ex._ensure_symbol_active("ACNUSDT") is True
    assert ex.kline_service.register_symbol.await_count == 2
    assert "ACNUSDT" in ex._registered_symbols


async def test_ensure_symbol_active_all_exceptions_returns_false():
    """全部重试均抛异常 → False，调用次数按配置（2 次）。"""
    ex = make_executor()
    ex.ensure_active_before_use = True
    ex.kline_service.register_symbol = AsyncMock(side_effect=Exception("持续失败"))
    assert await ex._ensure_symbol_active("ACNUSDT") is False
    assert ex.kline_service.register_symbol.await_count == 2


async def test_ensure_symbol_active_all_exceptions_skips_atr_and_cancel():
    """ensure-active 全异常 → 不进 ATR、不撤单、返回 False。"""
    ex = make_executor()
    ex.ensure_active_before_use = True
    ex.kline_service.register_symbol = AsyncMock(side_effect=Exception("持续失败"))
    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单']
    )
    assert result is False
    ex._calculate_atr.assert_not_awaited()
    ex.cancel_all_algo_orders.assert_not_awaited()


# ============================================================
# P0-3：回退开关（旧全量重建）与严格模式异常阻断
# ============================================================

async def test_replenish_fallback_prepare_then_strict_cancel_then_place_all():
    """P0-D 迁移：回退分支（cancel_after_ready=False）为 只读准备 → strict 撤单 → 全量挂。"""
    ex = make_executor()
    ex.replenish_cancel_after_ready = False
    events = []

    async def _cancel(symbol):
        events.append('cancel')
        return {'failed': 0}

    async def _atr(symbol):
        events.append('atr')
        return Decimal('1.0')

    ex.cancel_all_algo_orders = _cancel
    ex._calculate_atr = _atr

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单']
    )

    assert result is True
    assert events == ['atr', 'cancel']              # 先算后撤
    assert ex._place_conditional_and_record.await_count == 3  # 全量 SL/TP1/TP2
    assert "ACNUSDT" in ex._replenished_symbols


async def test_replenish_strict_cancel_exception_blocks_placement():
    """P0-D 迁移：回退分支（cancel_after_ready=False）撤单抛异常 → 阻断挂新单、返回 False。"""
    ex = make_executor()
    ex.replenish_cancel_after_ready = False
    ex.cancel_all_algo_orders = AsyncMock(side_effect=Exception("撤单异常"))
    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单']
    )
    assert result is False
    ex._place_conditional_and_record.assert_not_awaited()


async def test_replenish_top_level_exception_converges_via_handler():
    """只读准备抛出幂等错误码异常 → 顶层收敛为 True 并置位（不无限重试）。"""
    ex = make_executor()
    ex._resolve_short_quantity = AsyncMock(
        side_effect=Exception("-2021 触发条件不满足")
    )
    assert await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单']
    ) is True
    assert "ACNUSDT" in ex._replenished_symbols


async def test_execute_replenish_plans_sl_failure_not_marked():
    """SL 挂单失败 → 返回 False 且不置位（缺口留待下周期收敛）。"""
    ex = make_executor()
    ex._place_conditional_and_record = AsyncMock(side_effect=[False, True, True])
    plans = {
        'quantity': Decimal('10'), 'entry_price': Decimal('100'), 'atr': Decimal('1'),
        'sl': {'price': Decimal('105'), 'limit_price': Decimal('105.1')},
        'tp1': {'skip': False, 'market_close': False, 'price': Decimal('98'),
                'limit_price': Decimal('98.1'), 'quantity': Decimal('3')},
        'tp2': {'skip': True},
    }
    assert await ex._execute_replenish_plans("ACNUSDT", plans) is False
    assert "ACNUSDT" not in ex._replenished_symbols


# ============================================================
# P0-3：止盈计划执行与市价部分平仓
# ============================================================

async def test_apply_take_profit_skip_returns_true_without_placing():
    """plan.skip=True → 直接 True，不挂单、不市价平仓。"""
    ex = make_executor()
    ex._market_close_partial = AsyncMock()
    plan = {'skip': True, 'skip_reason': '名义价值不足'}
    assert await ex._apply_take_profit("ACNUSDT", plan, 1) is True
    ex._place_conditional_and_record.assert_not_awaited()
    ex._market_close_partial.assert_not_awaited()


async def test_apply_take_profit_market_close_delegates():
    """plan.market_close=True → 调 _market_close_partial 并返回 True。"""
    ex = make_executor()
    ex._market_close_partial = AsyncMock()
    plan = {'skip': False, 'market_close': True, 'price': Decimal('99'),
            'quantity': Decimal('3')}
    assert await ex._apply_take_profit("ACNUSDT", plan, 2) is True
    ex._market_close_partial.assert_awaited_once_with("ACNUSDT", Decimal('3'), 2)
    ex._place_conditional_and_record.assert_not_awaited()


async def test_apply_take_profit_places_conditional_order():
    """普通 plan → 委托 _place_conditional_and_record（algo_key=tpN）。"""
    ex = make_executor()
    plan = {'skip': False, 'market_close': False, 'price': Decimal('98'),
            'limit_price': Decimal('98.1'), 'quantity': Decimal('3')}
    assert await ex._apply_take_profit("ACNUSDT", plan, 1) is True
    ex._place_conditional_and_record.assert_awaited_once_with(
        "ACNUSDT", algo_key='tp1', stop_price=Decimal('98'),
        limit_price=Decimal('98.1'), quantity=Decimal('3'),
    )


async def test_market_close_partial_non_positive_quantity_skips_api():
    """quantity<=0 → 直接返回，不调用下单 API。"""
    ex = make_executor()
    ex.binance_api.place_order = AsyncMock()
    await ex._market_close_partial("ACNUSDT", Decimal('0'), 1)
    ex.binance_api.place_order.assert_not_awaited()


async def test_market_close_partial_api_error_is_swallowed():
    """下单 API 抛异常 → 仅告警，不外抛。"""
    ex = make_executor()
    ex.binance_api.place_order = AsyncMock(side_effect=Exception("下单失败"))
    assert await ex._market_close_partial("ACNUSDT", Decimal('1'), 1) is None


async def test_market_close_partial_success_updates_tracking():
    """市价平仓成功 → 调用 API 并回写剩余数量与目标达成标记。"""
    ex = make_executor()
    ex.binance_api.place_order = AsyncMock(return_value={})
    ex.position_tracking["ACNUSDT"] = {
        'algo_ids': {}, 'remaining_quantity': 10.0, 'entry_quantity': 10.0,
    }
    await ex._market_close_partial("ACNUSDT", Decimal('3'), 1)
    ex.binance_api.place_order.assert_awaited_once_with(
        symbol="ACNUSDT", side='BUY', order_type='MARKET',
        quantity=Decimal('3'), reduce_only=True,
    )
    assert ex.position_tracking["ACNUSDT"]['remaining_quantity'] == 7.0
    assert ex.position_tracking["ACNUSDT"]['target1_reached'] is True


# ============================================================
# P0-3：顶层异常收敛与止盈计划边界
# ============================================================

def test_handle_replenish_exception_idempotent_code_marks_handled():
    """幂等错误码命中 → 加入 _replenished_symbols 且返回 True。"""
    ex = make_executor()
    assert ex._handle_replenish_exception(
        "ACNUSDT", Exception("-4136 订单数量超出上限")
    ) is True
    assert "ACNUSDT" in ex._replenished_symbols


def test_handle_replenish_exception_unknown_code_returns_false():
    """错误码未命中 → 返回 False，且不置位。"""
    ex = make_executor()
    assert ex._handle_replenish_exception("ACNUSDT", Exception("-9999 未知错误")) is False
    assert "ACNUSDT" not in ex._replenished_symbols


_TP_BASE = {
    "entry_price": Decimal('100'),
    "atr": Decimal('1'),
    "multiplier": Decimal('1'),
    "close_percent": Decimal('1'),
    "quantity": Decimal('100'),
    "tick_size": Decimal('0.01'),
    "step_size": Decimal('0.01'),
    "current_price": Decimal('200'),
    "slippage": Decimal('0.001'),
}


@pytest.mark.parametrize(
    "overrides,reason_part",
    [
        ({"entry_price": Decimal('1'), "atr": Decimal('1'), "multiplier": Decimal('1')}, "目标价"),
        ({"quantity": Decimal('0.01'), "close_percent": Decimal('0.3'),
          "step_size": Decimal('1')}, "数量"),
        ({"quantity": Decimal('0.01'), "close_percent": Decimal('1'),
          "step_size": Decimal('0.01')}, "名义价值"),
    ],
    ids=["target_price_non_positive", "quantity_non_positive", "notional_below_min"],
)
def test_plan_take_profit_skip_branches(overrides, reason_part):
    """目标价<=0 / 数量<=0 / 名义价值<min_notional → 均 skip 且原因命中。"""
    ex = make_executor()
    plan = ex._plan_take_profit(**{**_TP_BASE, **overrides})
    assert plan["skip"] is True
    assert reason_part in plan["skip_reason"]


def test_plan_take_profit_market_close_when_price_reached():
    """现价 <= 目标价 → market_close=True 且计划数量为正（不再挂限价单）。"""
    ex = make_executor()
    plan = ex._plan_take_profit(**{**_TP_BASE, "current_price": Decimal('99')})
    assert plan["skip"] is False
    assert plan["market_close"] is True
    assert plan["quantity"] > 0


def test_plan_take_profit_normal_returns_limit_order():
    """常规情形 → skip=False、market_close=False，且限价高于目标价。"""
    ex = make_executor()
    plan = ex._plan_take_profit(**_TP_BASE)
    assert plan["skip"] is False
    assert plan["market_close"] is False
    assert plan["limit_price"] > plan["price"] > 0


# ============================================================
# P0-3：组价组量的三个失败早退
# ============================================================

async def test_build_replenish_plans_precision_failure_returns_failed():
    """精度获取失败（tick/step<=0）→ (FAILED, None)。"""
    ex = make_executor()
    ex._get_symbol_precision = AsyncMock(return_value=(Decimal('0'), Decimal('0.001')))
    status, plans = await ex._build_replenish_plans(
        "ACNUSDT", Decimal('100'), Decimal('10'), Decimal('1')
    )
    assert status == _REPLENISH_FAILED
    assert plans is None


async def test_build_replenish_plans_price_failure_returns_failed():
    """现价获取失败（<=0）→ (FAILED, None)。"""
    ex = make_executor()
    ex._get_current_price = AsyncMock(return_value=Decimal('0'))
    status, plans = await ex._build_replenish_plans(
        "ACNUSDT", Decimal('100'), Decimal('10'), Decimal('1')
    )
    assert status == _REPLENISH_FAILED
    assert plans is None


async def test_build_replenish_plans_stop_loss_unavailable_returns_failed():
    """止损计划不可用（价格格式化为 0）→ (FAILED, None)。"""
    ex = make_executor()
    status, plans = await ex._build_replenish_plans(
        "ACNUSDT", Decimal('0'), Decimal('10'), Decimal('0')
    )
    assert status == _REPLENISH_FAILED
    assert plans is None


async def test_build_replenish_plans_ready_returns_plans():
    """全部就绪 → (READY, plans)，plans 含 quantity/sl/tp1/tp2。"""
    ex = make_executor()
    status, plans = await ex._build_replenish_plans(
        "ACNUSDT", Decimal('100'), Decimal('10'), Decimal('1')
    )
    assert status == _REPLENISH_READY
    assert plans['quantity'] == Decimal('10')
    assert {'sl', 'tp1', 'tp2'} <= set(plans)


async def test_build_replenish_plans_is_readonly_no_tracking():
    """Phase A 纯只读：组价组量完成后不得建立持仓跟踪（撤单失败不留半成品）。"""
    ex = make_executor()
    status, _plans = await ex._build_replenish_plans(
        "ACNUSDT", Decimal('100'), Decimal('10'), Decimal('1')
    )
    assert status == _REPLENISH_READY
    assert ex.position_tracking == {}


async def test_execute_replenish_plans_builds_tracking_before_placing():
    """Phase C 首步建立持仓跟踪：挂单前 tracking 已存在（挂单路径依赖它）。"""
    ex = make_executor()
    plans = {
        'quantity': Decimal('10'), 'entry_price': Decimal('100'), 'atr': Decimal('1'),
        'sl': {'price': Decimal('105'), 'limit_price': Decimal('105.1')},
        'tp1': {'skip': True}, 'tp2': {'skip': True},
    }
    assert await ex._execute_replenish_plans("ACNUSDT", plans) is True
    ex._build_tracking_entry.assert_called_once()
    assert "ACNUSDT" in ex.position_tracking


# ============================================================
# P0-1 策略侧：_has_open_position 与注销异常
# ============================================================

async def test_has_open_position_memory_hit_skips_db():
    """内存持仓命中 → True，且不查询 DB。"""
    s = make_strategy()
    s.positions["ACNUSDT"] = {"entry_price": Decimal('100')}
    assert await s._has_open_position("ACNUSDT") is True
    s.trading_executor._has_open_short_position.assert_not_awaited()


async def test_has_open_position_without_executor_is_conservative():
    """交易执行器为 None → 保守返回 True。"""
    s = make_strategy()
    s.trading_executor = None
    assert await s._has_open_position("ACNUSDT") is True


async def test_unregister_if_safe_exception_returns_false_and_keeps_cache():
    """注销抛异常 → (False, False) 且不 discard 本地注册缓存。"""
    s = make_strategy()
    s._registered_symbols = {"ACNUSDT"}
    s.kline_service.unregister_symbol = AsyncMock(side_effect=Exception("注销网络异常"))
    assert await s._unregister_if_safe("ACNUSDT") == (False, False)
    assert "ACNUSDT" in s._registered_symbols


# ============================================================
# P0-D：增量补挂（只补缺失类型、绝不撤/改有效保护单）
# ============================================================

def _logged_messages(mock_logger) -> list:
    """收集 mock logger 全部调用（位置参数 + 关键字）转成的字符串，供日志断言。"""
    messages = []
    for method in (mock_logger.info, mock_logger.warning, mock_logger.error):
        for call in method.call_args_list:
            messages.extend(str(a) for a in call.args)
            messages.extend(str(v) for v in call.kwargs.values())
    return messages


async def test_find_missing_protection_strict_query_error_returns_none():
    """P0-D-AC7（前置）：strict=True 且查询异常 → None（不可判定）。

    默认（非 strict）仍 fail-closed 返回全类型（P0-A-AC3 契约不变）。
    """
    ex = make_executor()
    ex.replenish_require_take_profit = True
    ex.db = MagicMock()
    ex.db.fetch_all = AsyncMock(side_effect=Exception("DB 不可用"))
    assert await ex.find_missing_protection("ACNUSDT", strict=True) is None
    assert await ex.find_missing_protection("ACNUSDT") == ['止损单', '止盈单']


async def test_incremental_zero_cancel_when_sl_exists():
    """P0-D-AC1/AC20：仅缺 TP 且 SL 已存在 → 零撤单、SL 未被触碰、日志无「撤销旧条件单」。"""
    ex = make_executor()
    ex.position_tracking["ACNUSDT"] = {"algo_ids": {"sl": 111}}

    with patch.object(executor_module, "logger", new=MagicMock()) as mock_logger:
        result = await ex.replenish_conditional_orders(
            "ACNUSDT", Decimal('100'), missing=['止盈单']
        )

    assert result is True
    ex.cancel_all_algo_orders.assert_not_awaited()                    # 零撤单（INV-1）
    assert ex.position_tracking["ACNUSDT"]["algo_ids"]["sl"] == 111   # SL 原样保留
    assert not any("撤销旧条件单" in m for m in _logged_messages(mock_logger))  # AC20


async def test_incremental_only_missing_sl_places_sl():
    """P0-D-AC2：仅缺 SL → 只挂 1 条 SL，不挂 TP。"""
    ex = make_executor()

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单']
    )

    assert result is True
    assert ex._place_conditional_and_record.await_count == 1
    assert ex._place_conditional_and_record.await_args.kwargs["algo_key"] == 'sl'


async def test_incremental_only_missing_tp_keeps_sl():
    """P0-D-AC3（核心止血）：仅缺 TP → 补 TP1+TP2，SL 分支不进入、零撤单。"""
    ex = make_executor()
    ex.position_tracking["ACNUSDT"] = {"algo_ids": {"sl": 111}}

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止盈单']
    )

    assert result is True
    ex.cancel_all_algo_orders.assert_not_awaited()
    keys = [c.kwargs["algo_key"] for c in ex._place_conditional_and_record.await_args_list]
    assert keys == ['tp1', 'tp2']
    assert ex.position_tracking["ACNUSDT"]["algo_ids"]["sl"] == 111


async def test_incremental_all_missing_places_all():
    """P0-D-AC4：全缺 → 挂 SL+TP1+TP2，零撤单（无单可撤）、置位。"""
    ex = make_executor()

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单', '止盈单']
    )

    assert result is True
    ex.cancel_all_algo_orders.assert_not_awaited()
    keys = [c.kwargs["algo_key"] for c in ex._place_conditional_and_record.await_args_list]
    assert keys == ['sl', 'tp1', 'tp2']
    assert "ACNUSDT" in ex._replenished_symbols


async def test_incremental_partial_failure_not_marked():
    """P0-D-AC10：全缺且 TP1 失败 → False、不置位，保留已成功项。"""
    ex = make_executor()
    ex._place_conditional_and_record = AsyncMock(side_effect=[True, False, True])

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单', '止盈单']
    )

    assert result is False
    assert "ACNUSDT" not in ex._replenished_symbols
    assert ex._place_conditional_and_record.await_count == 3


async def test_incremental_retry_only_missing_second_round():
    """P0-D-AC11：首轮 SL 失败不置位；次轮只补 SL，零撤单、不触发全量重建。"""
    ex = make_executor()
    ex._place_conditional_and_record = AsyncMock(side_effect=[False, True])

    first = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单']
    )
    assert first is False
    assert "ACNUSDT" not in ex._replenished_symbols

    second = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止损单']
    )
    assert second is True
    assert "ACNUSDT" in ex._replenished_symbols
    ex.cancel_all_algo_orders.assert_not_awaited()
    assert ex._place_conditional_and_record.await_count == 2


async def test_incremental_preserves_existing_algo_ids_and_writes_new():
    """P0-D-AC13：保留既有 algo_ids（sl 不被清空/覆盖）；新挂 TP 条目正确写入。"""
    ex = make_executor(stub_orchestration=False)
    ex.position_tracking["ACNUSDT"] = {"algo_ids": {"sl": 111}}
    ex.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 2001})

    with _patch_record_condition_order():
        result = await ex.replenish_conditional_orders(
            "ACNUSDT", Decimal('100'), missing=['止盈单']
        )

    assert result is True
    algo_ids = ex.position_tracking["ACNUSDT"]["algo_ids"]
    assert algo_ids["sl"] == 111          # 既有键保留（INV-4）
    assert algo_ids["tp1"] == 2001        # 新挂条目写入
    assert algo_ids["tp2"] == 2001
    ex.cancel_all_algo_orders.assert_not_awaited()


async def test_market_close_does_not_cancel_sl():
    """P0-D-AC14：TP 计划 market_close → 只做市价部分平仓，零撤 SL，SL 保持。"""
    ex = make_executor()
    ex._get_current_price = AsyncMock(return_value=Decimal('90'))  # 现价已过 TP 目标价
    ex._market_close_partial = AsyncMock()
    ex.position_tracking["ACNUSDT"] = {"algo_ids": {"sl": 111}}

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止盈单']
    )

    assert result is True
    ex.cancel_all_algo_orders.assert_not_awaited()
    assert ex._market_close_partial.await_count == 2   # TP1/TP2 各一次
    assert ex.position_tracking["ACNUSDT"]["algo_ids"]["sl"] == 111


def test_new_config_defaults_without_crash():
    """P0-D-AC18：配置缺省（无 cancel_after_ready）→ 默认 true（增量补挂），不崩溃。"""
    ex = object.__new__(TradingExecutor)
    with patch.object(executor_module, "CapitalManager", MagicMock(return_value=MagicMock())):
        TradingExecutor.__init__(
            ex, MagicMock(), MagicMock(), MagicMock(),
            {"strategy": {"name": "new_coin"}, "trading": {}},
        )
    assert ex.replenish_cancel_after_ready is True
    assert ex.replenish_require_take_profit is True


# ============================================================
# P0-D：algo_id 回填（DB 为权威、本地缓存尽力补齐；只填空缺不覆盖）
# ============================================================

async def test_backfill_algo_ids_from_db_fills_missing_sl():
    """回填：DB 有 OPEN STOP_LOSS → 新建立 tracking 的 algo_ids['sl'] 填为该 algo_id。"""
    ex = make_executor()
    ex.position_tracking["ACNUSDT"] = {"algo_ids": {}}
    ex.db.fetch_all = AsyncMock(
        return_value=[{"algo_id": 555, "order_type": "STOP_LOSS"}]
    )

    await ex._backfill_algo_ids_from_db("ACNUSDT")

    assert ex.position_tracking["ACNUSDT"]["algo_ids"]["sl"] == 555
    args = ex.db.fetch_all.await_args.args
    assert args[1] == "ACNUSDT"              # 参数化传 symbol（勿拼字符串）
    assert "status='OPEN'" in args[0]        # 仅取 OPEN 记录


async def test_backfill_algo_ids_from_db_fills_tp_slots_in_row_order():
    """回填：两行 TAKE_PROFIT → 按行序填 tp1、tp2（DB 不区分档位，尽力而为）。"""
    ex = make_executor()
    ex.position_tracking["ACNUSDT"] = {"algo_ids": {}}
    ex.db.fetch_all = AsyncMock(return_value=[
        {"algo_id": 11, "order_type": "TAKE_PROFIT"},
        {"algo_id": 22, "order_type": "TAKE_PROFIT"},
    ])

    await ex._backfill_algo_ids_from_db("ACNUSDT")

    assert ex.position_tracking["ACNUSDT"]["algo_ids"] == {"tp1": 11, "tp2": 22}


async def test_backfill_algo_ids_from_db_does_not_overwrite_existing():
    """不覆盖（INV-4）：既有 algo_ids['sl']=111 保持 111，不被 DB 的不同 algo_id 覆盖。"""
    ex = make_executor()
    ex.position_tracking["ACNUSDT"] = {"algo_ids": {"sl": 111}}
    ex.db.fetch_all = AsyncMock(
        return_value=[{"algo_id": 999, "order_type": "STOP_LOSS"}]
    )

    await ex._backfill_algo_ids_from_db("ACNUSDT")

    assert ex.position_tracking["ACNUSDT"]["algo_ids"]["sl"] == 111


async def test_backfill_algo_ids_from_db_error_is_swallowed():
    """容错：DB 查询抛异常 → 不抛出、algo_ids 不变，且补挂照常进行。"""
    ex = make_executor()
    ex.position_tracking["ACNUSDT"] = {"algo_ids": {"sl": 111}}
    ex.db.fetch_all = AsyncMock(side_effect=Exception("DB 不可用"))

    await ex._backfill_algo_ids_from_db("ACNUSDT")          # 不得抛异常
    assert ex.position_tracking["ACNUSDT"]["algo_ids"] == {"sl": 111}

    result = await ex.replenish_conditional_orders(
        "ACNUSDT", Decimal('100'), missing=['止盈单']
    )
    assert result is True                                   # 回填失败不阻断补挂


async def test_backfill_algo_ids_from_db_skips_untracked_symbol():
    """边界：symbol 不在 position_tracking → 直接返回，不查 DB。"""
    ex = make_executor()
    ex.db.fetch_all = AsyncMock(return_value=[])

    await ex._backfill_algo_ids_from_db("NOPEUSDT")

    ex.db.fetch_all.assert_not_awaited()


async def test_incremental_backfills_sl_then_places_tp_zero_cancel():
    """端到端：tracking 缺失 + 仅缺 TP + DB 有 OPEN STOP_LOSS →
    回填 sl、新挂 tp1/tp2，且零撤单。"""
    ex = make_executor(stub_orchestration=False)
    ex.db.fetch_all = AsyncMock(
        return_value=[{"algo_id": 777, "order_type": "STOP_LOSS"}]
    )
    ex.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 2002})

    with _patch_record_condition_order():
        result = await ex.replenish_conditional_orders(
            "ACNUSDT", Decimal('100'), missing=['止盈单']
        )

    assert result is True
    algo_ids = ex.position_tracking["ACNUSDT"]["algo_ids"]
    assert algo_ids["sl"] == 777            # DB 回填（既有保护单的取消依据）
    assert algo_ids["tp1"] == 2002          # 本轮新挂
    assert algo_ids["tp2"] == 2002
    ex.cancel_all_algo_orders.assert_not_awaited()          # 零撤单（INV-1）
