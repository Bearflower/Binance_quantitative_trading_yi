"""
MTPCS 策略开仓基数重构（分配额 × 等级比例）单元测试

背景：开仓基数由「账户可用资金」改为「策略当月分配额 × 该信号等级百分比」，
并去掉震荡市×2 放大与 max_single_position_usdt 钳制。

覆盖分支（对应测试函数）：
1. test_position_size_allocated_limit_times_ratio
   - 正常按「分配额 × 等级比例」计算（mock 分配额=100、A 级比例 0.35 → 约 35）
   - 同时验证不受 max_single_position_usdt 压缩（config 中 max=10 < 35，结果仍为 35）
2. test_position_size_fallback_to_static_when_no_limit
   - 分配额取不到（get_effective_margin_limit 返回 None / fail-open）时，
     回退使用 config.trading.total_position_margin_limit 作为基数
"""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from strategies.btc_eth.strategy import BTCEthStrategy
from shared.capital_manager import CapitalManager


def build_strategy():
    """构建测试策略实例（仅设置 _calculate_position_size 依赖的属性）"""
    config = {
        'strategy': {
            'symbols': ['BTCUSDT'],
            'timeframes': ['1h'],
            'symbol_config': {},
            'risk': {
                'position_sizing': {
                    'safety_margin_ratio': 0.0,
                    'min_margin_usdt': 0.0,
                    # 故意设小（10），用于验证放开 max 钳制后不再被压缩
                    'max_single_position_usdt': 10.0,
                },
                'position_management': {},
            },
        },
        # 分配额缺失（fail-open）时的回退基数
        'trading': {
            'total_position_margin_limit': 210.0,
        },
        'binance': {
            'leverage': {'S': 5, 'A': 4, 'B': 3, 'C': 2},
            'position_ratio': {'S': 0.5, 'A': 0.35, 'B': 0.2, 'C': 0.1},
        },
    }
    strategy = object.__new__(BTCEthStrategy)
    strategy.config = config
    strategy.binance_config = config['binance']
    strategy.risk_config = config['strategy']['risk']
    strategy.symbol_config = config['strategy']['symbol_config']
    strategy.positions = {}
    strategy.atr_filter = None
    strategy.capital_mgr = MagicMock(spec=CapitalManager)
    # get_min_position_margin 默认视为未配置（门槛跳过），用例可按需覆盖
    strategy.capital_mgr.get_min_position_margin.return_value = None
    return strategy


@pytest.mark.asyncio
async def test_position_size_allocated_limit_times_ratio():
    """分支1：分配额=100、A 级比例 0.35 → 仓位约 35，且不受 max_single_position_usdt 压缩"""
    strategy = build_strategy()
    strategy.capital_mgr.get_effective_margin_limit = AsyncMock(return_value=(100.0, 'db'))

    size, reason = await strategy._calculate_position_size('A', Decimal('1000'), 'BTCUSDT')

    assert reason == ""
    # 分配额 100 × (1 - 0) × 0.35 = 35；
    # 即便 config 中 max_single_position_usdt=10，也不再被压缩为 10，必须为 35
    assert float(size) == pytest.approx(35.0)


@pytest.mark.asyncio
async def test_position_size_fallback_to_static_when_no_limit():
    """分支2：分配额取不到（fail-open）→ 回退 config.trading.total_position_margin_limit 作为基数"""
    strategy = build_strategy()
    strategy.capital_mgr.get_effective_margin_limit = AsyncMock(return_value=(None, 'none'))

    size, reason = await strategy._calculate_position_size('A', Decimal('1000'), 'BTCUSDT')

    assert reason == ""
    # 210 × (1 - 0) × 0.35 = 73.5
    assert float(size) == pytest.approx(73.5)


@pytest.mark.asyncio
async def test_position_size_rejected_when_below_min_position_margin():
    """分支3：usable_balance 低于 trading.min_position_margin 门槛 → 拒开（决策 D3 门槛分支）"""
    strategy = build_strategy()
    strategy.capital_mgr.get_effective_margin_limit = AsyncMock(return_value=(100.0, 'db'))
    # 可用基数 = 100 × (1 - safety_margin_ratio=0.0) = 100；门槛 150 → 100 < 150 → 拒开
    strategy.capital_mgr.get_min_position_margin.return_value = 150.0

    size, reason = await strategy._calculate_position_size('A', Decimal('1000'), 'BTCUSDT')

    assert size is None
    assert "可用资金不足" in reason
    assert "100.00" in reason
    assert "150.0" in reason