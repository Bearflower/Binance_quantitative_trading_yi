"""R8 [P1] 新币重取价后必须重算额度/数量/精度/最小名义金额 —— 回归用例

覆盖验收断言 1-4（对应 requirements_R7-R9_2026-10-16.md R8 节）：

  1. 最终价变化触发二次核算（上涨场景，未越反弹参照）
  2. 下跌场景：新价跌破 min_notional → 拒开
  3. 报价未变化（新鲜或重取后同价） → 首核算一次，行为不变（防回归）

复现脚本原型：docs/reviews/strategy_reaudit_2026-10-16_round2/reproduce_findings.py
  price_probe（修复前：budget_checks=1 且下单 quantity=1×price=120 组合 —— 断言反转后红）。

全部用 mock/合成行情，不连交易所/DB。
"""

from __future__ import annotations

import os
import sys
import time as _time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict, List, Tuple

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from strategies.new_coin.strategy import FreshQuote
from strategies.new_coin.tests.conftest import (
    make_minimal_executor,
    _format_down_to_step,
)


# ============================================================
# 辅助：构造 FreshQuote（R5 新路径）
# ============================================================

def _fresh_quote(
    *,
    price: Decimal,
    source_time_ms: int | None = None,
    request_started_ms: int = 1000,
    request_finished_ms: int = 1000,
) -> FreshQuote:
    return FreshQuote(
        price=price,
        source_time_ms=source_time_ms,
        request_started_ms=request_started_ms,
        request_finished_ms=request_finished_ms,
        source_time_kind=(
            'server_time' if source_time_ms is not None else 'request_span'
        ),
    )


# ============================================================
# 场景 1：上涨触发二次核算（核心断言）
# ============================================================

class TestR8PriceRisingReprice:
    """R8-1：重取价上涨但未越反弹参照 → 必须二次核算，不得用旧量下单。

    合成：杠杆 2、可用保证金 50、旧价 100 → budget 返回 position_size=1
    （名义保证金 = 100×1/2 = 50，刚好打满额度）。entry_guard 超龄重取价 120
    （未越反弹参照），此时：
      - 修复前：继续 quantity=1 × price=120 → 名义保证金 60 > 50，越过已校验额度
      - 修复后：以 120 重算 → 二次 budget 应返回 position_size ≤ 100/120 ≈ 0.833
        → 缩仓后格式化 ≈ 0.833（step 0.001）；若仍 ≥ min_notional 则缩仓下单；
        若缩仓后低于 min_notional 则拒开。两种结果都算通过——关键是不再「旧量 1 × 新价 120」。
    """

    async def test_reprice_rising_triggers_second_budget_check_no_old_qty(self):
        cfg = {
            'scoring': {
                'rebound_check': {'max_price_age_seconds': 1, 'shadow': {'enabled': False}},
            },
        }
        exe = make_minimal_executor(cfg)

        # 让 _resolve_open_margin_budget 记录每次调用的 price，按价返回对应 position_size
        budget_calls: List[Dict[str, Any]] = []

        async def budget_side_effect(symbol: str, price: float):
            budget_calls.append({'symbol': symbol, 'price': price})
            # 旧价 100：position_size=1，名义保证金 = 100×1/2 = 50 = avail
            if price <= 100.0:
                return Decimal('1'), ""
            # 新价 120：按同样 avail=50，position_size = 50×2/120 ≈ 0.8333...
            avail_usdt = 50.0
            pos_size = Decimal(str(avail_usdt * exe.leverage / price))
            return pos_size, ""

        exe._resolve_open_margin_budget.side_effect = budget_side_effect

        # 记录下单参数
        place_calls: List[Dict[str, Any]] = []
        async def _place_recorder(sym, qty, price, **kw):
            place_calls.append({'symbol': sym, 'quantity': qty, 'price': price})
            return {'orderId': 42}
        exe._place_short_order.side_effect = _place_recorder

        # 初始 quote 价 100，request_span=0（proxy 路径），clock 推进让 age 超 max_age=1s
        # executor 内 now_ms = int(time.time()*1000)，用 now_s=3.0 → now_ms=3000，
        # age_ms = 3000 - request_started_ms(1000) = 2000ms > 1000ms → 超龄触发 entry_guard
        now_s = [3.0]  # seconds，time.time() 返回秒
        old_quote = _fresh_quote(price=Decimal('100'), request_started_ms=1000, request_finished_ms=1000)

        # entry_guard 被调 → 返回新价 120（未越反弹参照，只超龄重取）
        new_quote = _fresh_quote(price=Decimal('120'), request_started_ms=2000, request_finished_ms=2000)
        guard_calls: List[int] = []

        async def guard():
            guard_calls.append(1)
            return new_quote, None

        with patch('strategies.new_coin.executor.time.time', side_effect=lambda: now_s[0]):
            order, reason = await exe.execute_short(
                symbol='TESTUSDT',
                score_result={'total_score': 8.5},
                current_price=100.0,
                quote=old_quote,
                entry_guard=guard,
            )

        # —— 关键断言 1：budget 至少被调 2 次，且第二次传入新价 120 ——
        assert len(budget_calls) >= 2, (
            f"修复后预算函数必须至少被调 2 次（首核算 + 重算），实际 {len(budget_calls)} 次"
        )
        prices_seen = [c['price'] for c in budget_calls]
        assert 120.0 in prices_seen, (
            f"二次核算必须以 final_price=120 重算，但 budget 只看到 prices={prices_seen}"
        )

        # —— 关键断言 2：不得用旧 quantity=1 × price=120 下单 ——
        assert len(place_calls) == 1, "修复后应能下单（缩仓后仍达 min_notional）"
        qty = place_calls[0]['quantity']
        px = place_calls[0]['price']
        assert not (qty == Decimal('1') and px == Decimal('120')), (
            f"禁止以旧 quantity=1 × 新价 120 下单——实际 quantity={qty}, price={px}"
        )

        # —— 可选补充：下单 quantity 应基于新价 120 重算（约 0.833）— 且 ≥ min_notional/price ——
        expected_max_qty_at_120 = Decimal(str(50.0 * 2 / 120))  # ≈ 0.8333
        # 缩仓后 quantity 应 ≤ 可用额度上限
        assert qty <= expected_max_qty_at_120 + Decimal('0.002'), (
            f"缩仓后 quantity={qty} 应不超过可用额度上限 {expected_max_qty_at_120}"
        )
        # 名义金额 ≥ min_notional=5
        notional = qty * Decimal(str(px))
        assert notional >= exe.min_notional, (
            f"缩仓后名义金额 {notional} 不应低于 min_notional={exe.min_notional}"
        )


# ============================================================
# 场景 2：下跌导致名义金额 < min_notional → 拒开
# ============================================================

class TestR8PriceFallsBelowMinNotional:
    """R8-2：重取价下跌，缩仓后名义金额仍 < min_notional → 拒开（不下单）。

    合成：可用保证金 5、旧价 10（position_size=2）→ 名义保证金 10 打满额度。
    新价重取到 0.15 USDT（mock API 返回 0.15），杠杆 2、可用保证金 5、
    min_notional=5 → 缩仓后 quantity ≈ 5*2/0.15 ≈ 66，但 step_size=0.001，
    notional = 66 × 0.15 = 9.9 ≥ 5（仍达 min_notional）。
    故改用：可用保证金 0.2 → 旧价 10 → position_size ≈ 0.04 → notional=0.4，
    首核算就可能被 min_notional 拒掉。为聚焦「重算拒开」，我们让首核算通过
    （旧价较高时可过 min_notional），新价极低时重算触发 min_notional 拒开。
    """

    async def test_reprice_falls_triggers_min_notional_reject(self):
        cfg = {
            'scoring': {
                'rebound_check': {'max_price_age_seconds': 1, 'shadow': {'enabled': False}},
            },
        }
        exe = make_minimal_executor(cfg, min_notional=Decimal('5'))
        # leverage 已由 conftest 默认值 2 设置
        exe.leverage = 2

        budget_calls: List[Dict[str, Any]] = []

        async def budget_side_effect(symbol: str, price: float):
            budget_calls.append({'symbol': symbol, 'price': price})
            # 旧价 10：返回 position_size=1
            # 新价 500：仍返回 position_size=1，但名义保证金 250，应缩仓；
            # 这里为聚焦 min_notional 路径，直接让 _resolve_open_margin_budget
            # 对新价返回一个缩仓后的小 quantity（仍过 budget 门槛），
            # 然后由 executor 的 min_notional 校验拦截。
            if price <= 10.0:
                return Decimal('1'), ""
            # 缩仓后仍通过 budget 门槛（reject_reason 为空）
            return Decimal('0.001'), ""

        exe._resolve_open_margin_budget.side_effect = budget_side_effect

        # 下单钩子：断言不应被调
        async def _place_should_not_be_called(*a, **kw):
            raise AssertionError("min_notional 被突破时应拒开，_place_short_order 不应被调")
        exe._place_short_order.side_effect = _place_should_not_be_called

        now_s = [3.0]
        old_quote = _fresh_quote(price=Decimal('10'), request_started_ms=1000, request_finished_ms=1000)
        # entry_guard 返回新价 500（比 10 高，名义保证金被放大）
        new_quote = _fresh_quote(price=Decimal('500'), request_started_ms=2000, request_finished_ms=2000)

        async def guard():
            return new_quote, None

        with patch('strategies.new_coin.executor.time.time', side_effect=lambda: now_s[0]):
            order, reason = await exe.execute_short(
                symbol='TESTUSDT',
                score_result={'total_score': 8.5},
                current_price=10.0,
                quote=old_quote,
                entry_guard=guard,
            )

        # —— 修复后：budget 至少调 2 次，新价=500 被传入，最终拒开 ——
        assert len(budget_calls) >= 2, (
            f"修复后预算函数必须至少被调 2 次，实际 {len(budget_calls)} 次"
        )
        prices_seen = [c['price'] for c in budget_calls]
        assert 500.0 in prices_seen, (
            f"二次核算必须以新价 500 重算，但 budget 只看到 {prices_seen}"
        )
        # 拒开路径：释放 claim + 返回非空 reason
        assert order is None, f"min_notional 被突破时应拒开，实际 order={order}"
        assert reason, "拒开时返回原因不可为空"
        assert exe._release_claim_quietly.await_count >= 1, "拒开必须释放 claim"


# ============================================================
# 场景 3：报价未变化（新鲜） → 仅首次核算，行为不变（防回归）
# ============================================================

class TestR8NoRepriceBudgetOnce:
    """R8-3：报价新鲜 → 不触发 entry_guard → budget 仅 1 次，下单 quantity 按旧价。

    防回归断言：R5 的「报价不超龄」路径不能被 R8 改动破坏。
    """

    async def test_fresh_quote_skips_reprice_budget_once(self):
        cfg = {
            'scoring': {
                'rebound_check': {'max_price_age_seconds': 60, 'shadow': {'enabled': False}},
            },
        }
        exe = make_minimal_executor(cfg)

        budget_calls: List[Dict[str, Any]] = []

        async def budget_side_effect(symbol: str, price: float):
            budget_calls.append({'symbol': symbol, 'price': price})
            return Decimal('1'), ""

        exe._resolve_open_margin_budget.side_effect = budget_side_effect
        exe._resolve_open_margin_budget.return_value = None  # 让 side_effect 永远生效

        place_calls: List[Dict[str, Any]] = []

        async def _place_recorder(sym, qty, price, **kw):
            place_calls.append({'symbol': sym, 'quantity': qty, 'price': price})
            return {'orderId': 42}

        exe._place_short_order.side_effect = _place_recorder
        exe._place_short_order.return_value = None

        # 新鲜 quote：request_span=0，max_age=60s → age=0 << 60000ms，不触发 entry_guard
        now_s = [1.001]  # 几乎没推进
        old_quote = _fresh_quote(price=Decimal('100'), request_started_ms=1000, request_finished_ms=1000)

        guard_called = {'n': 0}

        async def guard():
            guard_called['n'] += 1
            raise AssertionError("新鲜报价不应触发 entry_guard")

        with patch('strategies.new_coin.executor.time.time', side_effect=lambda: now_s[0]):
            order, reason = await exe.execute_short(
                symbol='TESTUSDT',
                score_result={'total_score': 8.5},
                current_price=100.0,
                quote=old_quote,
                entry_guard=guard,
            )

        # 核心：budget 仅被调 1 次，且下单 quantity=1 × price=100
        assert len(budget_calls) == 1, (
            f"新鲜报价不应触发二次核算，budget 应仅 1 次，实际 {len(budget_calls)}"
        )
        assert budget_calls[0]['price'] == 100.0
        assert guard_called['n'] == 0, "新鲜报价不应调 entry_guard"
        assert len(place_calls) == 1
        assert place_calls[0]['price'] == Decimal('100'), "下单价应保持旧价"


# ============================================================
# 场景 4：数量精度截断为 0 → 拒开（边界值，覆盖 _resolve_quantity_at_price
#         的 quantity <= 0 分支）
# ============================================================

class TestR8QuantityTruncatesToZero:
    """首核算 position_size 极小 → _format_quantity 向下取整为 0 → 拒开，不下单。

    走 legacy 路径（quote=None）：final_price = current_price = budget_price，
    不触发二次重算，聚焦覆盖 _resolve_quantity_at_price 内 quantity <= 0 分支。
    """

    async def test_first_check_quantity_truncates_to_zero_rejects(self):
        cfg = {
            'scoring': {
                'rebound_check': {'max_price_age_seconds': 60, 'shadow': {'enabled': False}},
            },
        }
        exe = make_minimal_executor(cfg, step_size=Decimal('0.001'))

        async def budget_side_effect(symbol: str, price: float):
            # position_size=0.0005，step=0.001 → 向下取整 = 0 → 触发「精度截断后数量为 0」
            return Decimal('0.0005'), ""

        exe._resolve_open_margin_budget.side_effect = budget_side_effect

        async def _place_should_not_be_called(*a, **kw):
            raise AssertionError("精度截断为 0 时应拒开，_place_short_order 不应被调")
        exe._place_short_order.side_effect = _place_should_not_be_called

        order, reason = await exe.execute_short(
            symbol='TESTUSDT',
            score_result={'total_score': 8.5},
            current_price=100.0,
        )

        assert order is None, "quantity 截断为 0 时应拒开"
        assert "精度截断" in reason, f"拒开原因应包含精度截断，实际 '{reason}'"
        assert exe._release_claim_quietly.await_count >= 1, "拒开必须释放 claim"
