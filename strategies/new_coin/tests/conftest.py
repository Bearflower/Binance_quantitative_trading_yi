"""new_coin 测试共享夹具：构造最小 TradingExecutor 实例。

收敛三处原重复的 `object.__new__(TradingExecutor)` 工厂（R5 / R8 回归用例），
避免同一组 AsyncMock 注入逐行复制。`test_take_profit_fill_detection.py` 自建的
class `_Minimal` 属于不同策略（只暴露 detect_* 所需属性，不跑 execute_short 链路），
不在此收敛范围。

仅暴露普通 Python 函数（非 pytest fixture），调用方显式 import 以保持跨文件一致性。
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from typing import Any, Dict, Optional


def _format_down_to_step(qty: Decimal, step: Decimal) -> Decimal:
    """按 step_size 向下取整（模拟 TradingExecutor._format_quantity 基本语义）。

    同时服务 R8 重算用例（首次与二次重算的 formatter 统一）。
    """
    if qty <= 0 or step <= 0:
        return Decimal('0')
    return (qty / step).to_integral_value(rounding='ROUND_DOWN') * step


def make_minimal_executor(
    config: Dict[str, Any],
    *,
    min_notional: Decimal = Decimal('5'),
    leverage: int = 2,
    market_order_score_threshold: float = 7.0,
    budget_position_size: Decimal = Decimal('1'),
    budget_reject: str = "",
    step_size: Decimal = Decimal('0.001'),
    format_quantity_return: Optional[Decimal] = None,
    format_quantity_side_effect: Optional[Any] = None,
    place_short_order_return: Optional[Dict[str, Any]] = None,
) -> Any:
    """构造最小 TradingExecutor，注入 execute_short 前置 mock。

    参数化覆盖 R5（leverage=10、score_threshold=10、budget=(100,None)
    且 formatter 返回常数 10）与 R8（leverage=2、side_effect 格式化）两处
    原工厂的差异，保持调用方原有语义不变。

    Args:
        config: 业务配置（含 scoring.rebound_check 及 trading 段）
        min_notional: 最小名义金额（R8 首次引入开仓路径校验）
        leverage: 默认杠杆
        market_order_score_threshold: 市价单评分阈值
        budget_position_size: _resolve_open_margin_budget 默认返回的 position_size
        budget_reject: _resolve_open_margin_budget 默认返回的 reject_reason
        step_size: 数量精度（tick_size 固定 Decimal('0.01') 由精度 mock 返回）
        format_quantity_return: 若传入则 _format_quantity 为固定返回值
        format_quantity_side_effect: 若传入则 _format_quantity 用 side_effect
        place_short_order_return: _place_short_order 默认返回值；None 时 AsyncMock 无返回
    """
    from strategies.new_coin.executor import TradingExecutor

    e = object.__new__(TradingExecutor)
    e.config = config
    e.min_notional = min_notional
    e.leverage = leverage
    e.market_order_score_threshold = market_order_score_threshold
    e.baseline_ready = True
    e._leverage_valid = True
    e.binance_api = MagicMock()
    e._get_account_balance = AsyncMock(return_value=10000.0)
    e._is_symbol_occupied = AsyncMock(return_value=False)
    e._claim_symbol = AsyncMock(return_value={'claimed': True})
    e._resolve_open_margin_budget = AsyncMock(
        return_value=(budget_position_size, budget_reject)
    )
    e._get_symbol_precision = AsyncMock(
        return_value=(Decimal('0.01'), step_size)
    )

    if format_quantity_side_effect is not None:
        e._format_quantity = MagicMock(side_effect=format_quantity_side_effect)
    elif format_quantity_return is not None:
        e._format_quantity = MagicMock(return_value=format_quantity_return)
    else:
        e._format_quantity = MagicMock(side_effect=_format_down_to_step)

    e._set_leverage = AsyncMock(return_value=None)
    e._release_claim_quietly = AsyncMock(return_value=None)
    if place_short_order_return is not None:
        e._place_short_order = AsyncMock(return_value=place_short_order_return)
    else:
        e._place_short_order = AsyncMock()

    fill_mock = MagicMock()
    fill_mock.filled = True
    fill_mock.has_fill = True
    fill_mock.order_id = 99
    fill_mock.avg_price = Decimal('98.9')
    fill_mock.executed_qty = Decimal('10')
    fill_mock.status = 'FILLED'
    e._wait_for_order_fill = AsyncMock(return_value=fill_mock)
    return e
