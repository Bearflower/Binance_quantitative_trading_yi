"""
MTPCS 重启后接管「已有完整保护单」持仓 修复功能测试

背景（2026-10-10）：btc_eth / btc_eth_aggressive 启动时不从持久化状态恢复持仓，
且 _ensure_symbol_protection 对"已有完整保护单"的交易所持仓直接 return、不登记，
导致这类仓位重启后永久掉出 self.positions（不再监控、不进 strategy_states /
strategy_open_positions，看板缺失）。本次修复新增 _adopt_untracked_exchange_positions()
在启动时把「交易所存在、归属本策略、已挂满保护单」的仓位重新纳入内存跟踪。

覆盖用例（对应修复方案 §测试）：
  1. 满保护单 + 未跟踪 → 被接管；entry_price 取自交易所、entry_time = 最早保护单
     created_at、stop/tp1/tp2 三个 id 齐全
  2. 归属 owner=对家 或 None → 不接管
  3. 已在 self.positions → 不接管（幂等）
  4. 非托管 symbol / |amt| ≤ min_position_amt → 跳过
  5. entryPrice = 0 → 回退当前价
  6. 两张 STOP_LOSS → 首张 stop_loss_order_id、第二张 trailing_stop_order_id
  7. 安全闸：current_price 已在重建 TP1 之外 → 跳过接管
  8. 接管过程中未调用任何下单/撤单 API
  9. 接管后 build_positions_report() 含该 symbol 且 margin > 0
  + 保护单不完整（TP 不足 2 张）→ 不接管

btc_eth 侧覆盖第 1、2、8 项（改法一致，验证等价行为）。
"""
import os
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

import yaml
import pytest
from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as AggressiveStrategy
from strategies.btc_eth_aggressive.strategy import PositionState
from strategies.btc_eth.strategy import BTCEthStrategy as BaseStrategy

_AGGR_MODULE = 'strategies.btc_eth_aggressive.strategy'
_BASE_MODULE = 'strategies.btc_eth.strategy'

# kind -> (策略类, 配置子目录, 模块路径, 归属名)
_SPECS = {
    'aggressive': (AggressiveStrategy, 'btc_eth_aggressive', _AGGR_MODULE, 'btc_eth_aggressive'),
    'base': (BaseStrategy, 'btc_eth', _BASE_MODULE, 'btc_eth'),
}

# 保护单 createdAt（近似原入场时间，用于 entry_time 断言）
_T0 = datetime(2026, 10, 9, 5, 10, 19)

# 当前价（_get_current_price mock 返回值）与 ATR（_calc_protection_atr mock 返回值）
_CURRENT_PRICE = Decimal('2500')
_ATR = Decimal('20')
# 默认 TP1 价（远高于当前价，令 LONG 安全闸放行）
_TP1_SAFE = Decimal('99999')


def build_strategy(kind: str):
    """构造策略实例并注入 mock（不触碰真实交易所/数据库）"""
    cls, subdir, _, _ = _SPECS[kind]
    cfg_path = os.path.join(PROJECT_ROOT, 'strategies', subdir, 'config.yaml')
    with open(cfg_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    s = cls(
        config=config,
        binance_client=AsyncMock(),
        kline_service=AsyncMock(),
        notification_client=AsyncMock(),
        db_manager=None,
    )
    # 接管涉及的取价/ATR/止盈价方法统一 mock，避免触网，并让安全闸默认放行
    s._get_current_price = AsyncMock(return_value=_CURRENT_PRICE)
    s._calc_protection_atr = AsyncMock(return_value=_ATR)
    # _calculate_tp_price 为同步方法，须用 MagicMock（AsyncMock 会返回 coroutine）
    s._calculate_tp_price = MagicMock(return_value=_TP1_SAFE)
    return s


def make_pos(symbol: str, amt: str = '0.026', entry: str = '2489.82') -> dict:
    """构造交易所 positionRisk 原始项"""
    return {'symbol': symbol, 'positionAmt': amt, 'entryPrice': entry}


def make_orders(symbol: str, stop_count: int = 1, tp_count: int = 2) -> list:
    """构造 get_open_orders 返回的 OPEN 保护单列表（STOP_LOSS + TAKE_PROFIT）"""
    orders = []
    for i in range(stop_count):
        orders.append({
            'symbol': symbol, 'order_type': 'STOP_LOSS',
            'algo_id': 2000 + i, 'created_at': _T0 + timedelta(minutes=i),
        })
    for i in range(tp_count):
        orders.append({
            'symbol': symbol, 'order_type': 'TAKE_PROFIT',
            'algo_id': 2100 + i, 'created_at': _T0,
        })
    return orders


def patch_env(module: str, orders: list, owner, owner_mock=None):
    """patch 掉模块级 get_open_orders / resolve_position_owner（避免真实 DB 查询）"""
    if owner_mock is None:
        owner_mock = AsyncMock(return_value=owner)
    stack = ExitStack()
    stack.enter_context(patch(
        f'{module}.get_open_orders', new=AsyncMock(return_value=orders)))
    stack.enter_context(patch(
        f'{module}.resolve_position_owner', new=owner_mock))
    return stack


def assert_no_trade_api(strategy):
    """断言接管过程未调用任何下单/撤单 API（接管只重建内存状态）"""
    strategy.binance.place_order.assert_not_awaited()
    strategy.binance.cancel_all_algo_orders.assert_not_awaited()
    strategy.binance.cancel_order.assert_not_awaited()


# ============================================================================
# btc_eth_aggressive：9 项用例 + 保护单不完整
# ============================================================================

class TestAdoptPositionsAggressive:
    """btc_eth_aggressive._adopt_untracked_exchange_positions 分支"""

    async def test_case1_full_protection_untracked_is_adopted(self):
        """用例1：满保护单 + 未跟踪 → 被接管，字段取自交易所/保护单"""
        s = build_strategy('aggressive')
        sym = s.symbols[0]
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym)])
        owner_mock = AsyncMock(return_value='btc_eth_aggressive')

        with patch_env(_AGGR_MODULE, make_orders(sym), 'btc_eth_aggressive', owner_mock=owner_mock):
            await s._adopt_untracked_exchange_positions()

        # 归属守卫必须用 status_filter=False（开仓记录 status 恒为 NEW）
        owner_mock.assert_awaited_once_with(s.db_manager, sym, status_filter=False)
        assert sym in s.positions
        ps = s.positions[sym]
        assert ps.direction == 'LONG'
        assert ps.entry_price == Decimal('2489.82')
        assert ps.initial_quantity == Decimal('0.026')
        assert ps.current_quantity == Decimal('0.026')
        assert ps.entry_time == _T0              # 取最早保护单 created_at
        assert ps.stop_loss_order_id == 2000
        assert ps.tp1_order_id == 2100
        assert ps.tp2_order_id == 2101

    @pytest.mark.parametrize('owner', ['btc_eth', None])
    async def test_case2_owner_mismatch_not_adopted(self, owner):
        """用例2：归属为对家或无法判定 → 不接管"""
        s = build_strategy('aggressive')
        sym = s.symbols[0]
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym)])

        with patch_env(_AGGR_MODULE, make_orders(sym), owner):
            await s._adopt_untracked_exchange_positions()

        assert sym not in s.positions

    async def test_case3_already_tracked_is_skipped(self):
        """用例3：已在 self.positions → 幂等跳过，不覆盖现有状态"""
        s = build_strategy('aggressive')
        sym = s.symbols[0]
        existing = PositionState()
        existing.direction = 'SHORT'
        s.positions[sym] = existing
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym)])

        with patch_env(_AGGR_MODULE, make_orders(sym), 'btc_eth_aggressive'):
            await s._adopt_untracked_exchange_positions()

        assert s.positions[sym] is existing

    async def test_case4a_non_managed_symbol_skipped(self):
        """用例4a：非托管 symbol → 跳过"""
        s = build_strategy('aggressive')
        sym = 'DOGEUSDT'
        assert sym not in s.symbols
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym)])

        with patch_env(_AGGR_MODULE, make_orders(sym), 'btc_eth_aggressive'):
            await s._adopt_untracked_exchange_positions()

        assert sym not in s.positions

    async def test_case4b_below_min_amt_skipped(self):
        """用例4b：|positionAmt| ≤ min_position_amt → 跳过"""
        s = build_strategy('aggressive')
        sym = s.symbols[0]
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym, amt='0.000001')])

        with patch_env(_AGGR_MODULE, make_orders(sym), 'btc_eth_aggressive'):
            await s._adopt_untracked_exchange_positions()

        assert sym not in s.positions

    async def test_case5_entry_price_zero_falls_back_to_current(self):
        """用例5：entryPrice = 0 → 回退当前价"""
        s = build_strategy('aggressive')
        sym = s.symbols[0]
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym, entry='0')])

        with patch_env(_AGGR_MODULE, make_orders(sym), 'btc_eth_aggressive'):
            await s._adopt_untracked_exchange_positions()

        assert s.positions[sym].entry_price == _CURRENT_PRICE

    async def test_case6_two_stop_loss_mapping(self):
        """用例6：两张 STOP_LOSS → 首张 stop_loss、第二张 trailing_stop"""
        s = build_strategy('aggressive')
        sym = s.symbols[0]
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym)])

        with patch_env(_AGGR_MODULE, make_orders(sym, stop_count=2), 'btc_eth_aggressive'):
            await s._adopt_untracked_exchange_positions()

        ps = s.positions[sym]
        assert ps.stop_loss_order_id == 2000
        assert ps.trailing_stop_order_id == 2001

    async def test_case7_safety_gate_blocks_when_tp1_reached(self):
        """用例7：当前价已在重建 TP1 之外（LONG: price >= tp1）→ 放弃接管"""
        s = build_strategy('aggressive')
        sym = s.symbols[0]
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym)])
        # TP1 价 2400 < 当前价 2500，模拟 ATR 被高估 → 接管会立即误市价止盈
        s._calculate_tp_price = MagicMock(return_value=Decimal('2400'))

        with patch_env(_AGGR_MODULE, make_orders(sym), 'btc_eth_aggressive'):
            await s._adopt_untracked_exchange_positions()

        assert sym not in s.positions

    async def test_case8_no_trade_api_called(self):
        """用例8：接管过程中未调用任何下单/撤单 API"""
        s = build_strategy('aggressive')
        sym = s.symbols[0]
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym)])

        with patch_env(_AGGR_MODULE, make_orders(sym), 'btc_eth_aggressive'):
            await s._adopt_untracked_exchange_positions()

        assert sym in s.positions
        assert_no_trade_api(s)

    async def test_case9_report_contains_adopted_symbol_with_margin(self):
        """用例9：接管后 build_positions_report() 含该 symbol 且 margin > 0"""
        s = build_strategy('aggressive')
        sym = s.symbols[0]
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym)])

        with patch_env(_AGGR_MODULE, make_orders(sym), 'btc_eth_aggressive'):
            await s._adopt_untracked_exchange_positions()

        positions, margin_dict, qty_dict = s.build_positions_report()
        assert sym in positions
        assert margin_dict[sym] > 0
        assert qty_dict[sym] == pytest.approx(0.026)

    async def test_incomplete_protection_not_adopted(self):
        """保护单不完整（TAKE_PROFIT 仅 1 张）→ 不接管（交由补挂逻辑处理）"""
        s = build_strategy('aggressive')
        sym = s.symbols[0]
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym)])

        with patch_env(_AGGR_MODULE, make_orders(sym, tp_count=1), 'btc_eth_aggressive'):
            await s._adopt_untracked_exchange_positions()

        assert sym not in s.positions


# ============================================================================
# btc_eth：等价用例（1、2、8）
# ============================================================================

class TestAdoptPositionsBase:
    """btc_eth._adopt_untracked_exchange_positions 等价用例"""

    async def test_case1_full_protection_untracked_is_adopted(self):
        """用例1：满保护单 + 未跟踪 → 被接管"""
        s = build_strategy('base')
        sym = s.symbols[0]
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym)])
        owner_mock = AsyncMock(return_value='btc_eth')

        with patch_env(_BASE_MODULE, make_orders(sym), 'btc_eth', owner_mock=owner_mock):
            await s._adopt_untracked_exchange_positions()

        owner_mock.assert_awaited_once_with(s.db_manager, sym, status_filter=False)
        assert sym in s.positions
        ps = s.positions[sym]
        assert ps.direction == 'LONG'
        assert ps.entry_price == Decimal('2489.82')
        assert ps.entry_time == _T0
        assert ps.stop_loss_order_id == 2000
        assert ps.tp1_order_id == 2100
        assert ps.tp2_order_id == 2101

    @pytest.mark.parametrize('owner', ['btc_eth_aggressive', None])
    async def test_case2_owner_mismatch_not_adopted(self, owner):
        """用例2：归属为对家或无法判定 → 不接管"""
        s = build_strategy('base')
        sym = s.symbols[0]
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym)])

        with patch_env(_BASE_MODULE, make_orders(sym), owner):
            await s._adopt_untracked_exchange_positions()

        assert sym not in s.positions

    async def test_case8_no_trade_api_called(self):
        """用例8：接管过程中未调用任何下单/撤单 API"""
        s = build_strategy('base')
        sym = s.symbols[0]
        s.binance.get_position = AsyncMock(return_value=[make_pos(sym)])

        with patch_env(_BASE_MODULE, make_orders(sym), 'btc_eth'):
            await s._adopt_untracked_exchange_positions()

        assert sym in s.positions
        assert_no_trade_api(s)