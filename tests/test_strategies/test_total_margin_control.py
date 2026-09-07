"""
MTPCS 策略总持仓保证金控制（_check_total_margin_ratio）单元测试

覆盖分支：
1. 正常开仓（未超限）→ True
2. 超限拒绝开仓 → False
3. 未配置阈值（None 或 <=0）→ True（不限制）
4. 账户信息获取异常 → True（降级不限制）
5. 账户权益无效（0/负）→ True（降级不限制）
6. 每次调用动态读取配置（get_account_ratio_cap 每次被调用）
7. grade 未知时取最小杠杆保守高估保证金
"""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from strategies.btc_eth.strategy import BTCEthStrategy, PositionState
from shared.capital_manager import CapitalManager
from shared.binance_api import BinanceClient


def build_strategy():
    """构建测试策略实例（仅设置 _check_total_margin_ratio 依赖的属性）"""
    config = {
        'strategy': {
            'symbols': ['BTCUSDT'],
            'timeframes': ['1h'],
            'symbol_config': {},
            'risk': {},
        },
        'binance': {
            'leverage': {'S': 5, 'A': 4, 'B': 3, 'C': 2},
        },
    }
    strategy = object.__new__(BTCEthStrategy)
    strategy.config = config
    strategy.binance_config = config['binance']
    strategy.positions = {}
    strategy.binance = MagicMock(spec=BinanceClient)
    strategy.capital_mgr = MagicMock(spec=CapitalManager)
    return strategy


def make_signal(quantity=0.02, entry_price=100.0, leverage=4):
    """构造测试信号（总持仓保证金检查只用到 symbol/quantity/entry_price/leverage）"""
    return {
        'symbol': 'BTCUSDT',
        'quantity': quantity,
        'entry_price': entry_price,
        'leverage': leverage,
    }


def make_position(quantity, entry_price, grade):
    """构造持仓状态对象"""
    pos = PositionState()
    pos.current_quantity = Decimal(str(quantity))
    pos.entry_price = Decimal(str(entry_price))
    pos.grade = grade
    return pos


@pytest.mark.asyncio
async def test_check_total_margin_ratio_normal_open():
    """分支1：正常开仓（总保证金占比未超阈值）→ True"""
    strategy = build_strategy()
    strategy.capital_mgr.get_account_ratio_cap.return_value = 0.3
    strategy.binance.get_account_info = AsyncMock(return_value={'totalMarginBalance': '1000'})

    signal = make_signal(quantity=0.02, entry_price=100.0, leverage=4)  # 新仓保证金 0.5
    result = await strategy._check_total_margin_ratio(signal)

    assert result is True


@pytest.mark.asyncio
async def test_check_total_margin_ratio_over_limit_reject():
    """分支2：超限拒绝开仓 → False"""
    strategy = build_strategy()
    strategy.capital_mgr.get_account_ratio_cap.return_value = 0.3
    strategy.binance.get_account_info = AsyncMock(return_value={'totalMarginBalance': '100'})

    # 当前持仓：数量10 × 价格10 / 杠杆2(C级) = 50 保证金
    strategy.positions['BTCUSDT'] = make_position(quantity=10, entry_price=10, grade='C')
    # 新仓：数量2 × 价格10 / 杠杆4 = 5 保证金
    signal = make_signal(quantity=2, entry_price=10.0, leverage=4)

    result = await strategy._check_total_margin_ratio(signal)

    # 总保证金 55 / 权益 100 = 0.55 > 0.3 → 拒绝
    assert result is False


@pytest.mark.asyncio
async def test_check_total_margin_ratio_no_config_no_limit():
    """分支3：未配置阈值（None）→ True（不限制）"""
    strategy = build_strategy()
    strategy.capital_mgr.get_account_ratio_cap.return_value = None
    strategy.binance.get_account_info = AsyncMock(return_value={'totalMarginBalance': '1000'})

    result = await strategy._check_total_margin_ratio(make_signal())

    assert result is True


@pytest.mark.asyncio
async def test_check_total_margin_ratio_zero_threshold_no_limit():
    """分支3b：阈值为 0 或负数 → True（不限制）"""
    for bad_value in (0, -0.1):
        strategy = build_strategy()
        strategy.capital_mgr.get_account_ratio_cap.return_value = bad_value
        strategy.binance.get_account_info = AsyncMock(return_value={'totalMarginBalance': '1000'})
        result = await strategy._check_total_margin_ratio(make_signal())
        assert result is True


@pytest.mark.asyncio
async def test_check_total_margin_ratio_account_error_degrade():
    """分支4：账户信息获取异常 → True（降级不限制）"""
    strategy = build_strategy()
    strategy.capital_mgr.get_account_ratio_cap.return_value = 0.3
    strategy.binance.get_account_info = AsyncMock(side_effect=Exception("网络异常"))

    result = await strategy._check_total_margin_ratio(make_signal())

    assert result is True


@pytest.mark.asyncio
async def test_check_total_margin_ratio_invalid_equity_degrade():
    """分支5：账户权益无效（0）→ True（降级不限制）"""
    strategy = build_strategy()
    strategy.capital_mgr.get_account_ratio_cap.return_value = 0.3
    strategy.binance.get_account_info = AsyncMock(return_value={'totalMarginBalance': '0'})

    result = await strategy._check_total_margin_ratio(make_signal())

    assert result is True


@pytest.mark.asyncio
async def test_check_total_margin_ratio_dynamic_read():
    """分支6：每次开仓检查都动态调用 get_account_ratio_cap（保证配置更新立即生效）"""
    strategy = build_strategy()
    strategy.capital_mgr.get_account_ratio_cap.return_value = 0.3
    strategy.binance.get_account_info = AsyncMock(return_value={'totalMarginBalance': '1000'})

    await strategy._check_total_margin_ratio(make_signal())
    await strategy._check_total_margin_ratio(make_signal())

    assert strategy.capital_mgr.get_account_ratio_cap.call_count == 2


@pytest.mark.asyncio
async def test_calc_current_total_margin_unknown_grade_min_leverage():
    """分支7：grade 未知时取配置中最小杠杆（C=2）保守高估保证金"""
    strategy = build_strategy()
    strategy.positions['BTCUSDT'] = make_position(quantity=10, entry_price=10, grade='')

    total_margin = strategy._calc_current_total_margin()

    # 数量10 × 价格10 / 最小杠杆2 = 50
    assert total_margin == 50.0


@pytest.mark.asyncio
async def test_calc_current_total_margin_empty_positions():
    """分支7b：无持仓时总保证金为 0"""
    strategy = build_strategy()
    assert strategy._calc_current_total_margin() == 0.0
