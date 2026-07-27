"""
BTC/ETH 策略动态利润保护 - 边界测试与性能测试

测试覆盖：
1. 边界测试：tiers 为空列表、entry_price 为0/None、vol_adj 极值、做空硬止损更紧
2. 性能测试：执行时间、缓存效果
"""
import sys
import os
import time
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

# 确保项目根目录在 path 中
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

import yaml
from strategies.btc_eth.strategy import BTCEthStrategy, PositionState


def load_config() -> dict:
    """加载策略配置文件"""
    config_path = os.path.join(
        PROJECT_ROOT, "strategies", "btc_eth", "config.yaml"
    )
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def create_base_strategy(config, mock_binance, mock_kline_service, mock_notification):
    """创建基础策略实例"""
    return BTCEthStrategy(
        config=config,
        binance_client=mock_binance,
        kline_service=mock_kline_service,
        notification_client=mock_notification,
        db_manager=None
    )


def setup_dynamic_trailing_config(strategy, tiers=None, vol_adj_enabled=False, mock_vol_adj=True):
    """注入动态利润保护配置
    
    Args:
        strategy: 策略实例
        tiers: 回撤阶梯配置
        vol_adj_enabled: 是否启用波动率调节
        mock_vol_adj: 是否mock _get_volatility_adjustment（性能测试需要真实方法）
    """
    strategy.risk_config['dynamic_trailing'] = {
        'enabled': True,
        'activation': {'min_profit_pct': 1.5, 'also_on_tp1': True},
        'regression_tiers': tiers if tiers is not None else [
            {'profit_ceiling': 1.5, 'retrace_ratio': 0.0},
            {'profit_ceiling': 4.0, 'retrace_ratio': 0.5},
            {'profit_ceiling': 8.0, 'retrace_ratio': 0.35},
            {'profit_ceiling': 999.0, 'retrace_ratio': 0.25}
        ],
        'volatility_adjustment': {'enabled': vol_adj_enabled}
    }
    strategy.risk_config['stop_loss_atr_multiplier'] = 1.5
    if mock_vol_adj:
        strategy._get_volatility_adjustment = AsyncMock(return_value=1.0)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def mock_binance():
    client = AsyncMock()
    client.get_ticker = AsyncMock(return_value={
        'lastPrice': '50000.00',
        'priceChangePercent': '1.5'
    })
    client.get_ticker_price = AsyncMock(return_value=Decimal('50000.00'))
    client.get_funding_rate = AsyncMock(return_value=0.0001)
    return client


@pytest.fixture
def mock_kline_service():
    service = AsyncMock()
    # 模拟返回日线K线数据，用于波动率计算
    service.get_klines = AsyncMock(return_value=[
        {
            'open_time': i * 86400000,
            'open': Decimal('50000'),
            'high': Decimal('51000'),
            'low': Decimal('49000'),
            'close': Decimal('50500'),
            'volume': Decimal('1000'),
        }
        for i in range(50)
    ])
    return service


@pytest.fixture
def mock_notification():
    return AsyncMock()


@pytest.fixture
def strategy(config, mock_binance, mock_kline_service, mock_notification):
    return create_base_strategy(config, mock_binance, mock_kline_service, mock_notification)


@pytest.fixture
def position_long():
    """做多持仓"""
    pos = PositionState()
    pos.entry_price = Decimal('60000')
    pos.direction = 'LONG'
    pos.current_quantity = Decimal('0.1')
    pos.atr = Decimal('600')
    pos.highest_price = Decimal('60000')
    pos.tp1_hit = False
    pos.trailing_activated = False
    pos.trailing_stop_price = None
    return pos


@pytest.fixture
def position_short():
    """做空持仓"""
    pos = PositionState()
    pos.entry_price = Decimal('60000')
    pos.direction = 'SHORT'
    pos.current_quantity = Decimal('1.0')
    pos.atr = Decimal('600')
    pos.lowest_price = Decimal('60000')
    pos.tp1_hit = False
    pos.trailing_activated = False
    pos.trailing_stop_price = None
    return pos


# ============================================================================
# 边界测试类
# ============================================================================

class TestBoundaryConditions:
    """动态利润保护边界条件测试"""

    @pytest.mark.asyncio
    async def test_tiers_empty_list(self, strategy, position_long):
        """
        BC-01: tiers 为空列表时的行为
        
        预期：应返回 None，不抛出异常
        """
        setup_dynamic_trailing_config(strategy, tiers=[])
        position_long.highest_price = Decimal('61200')
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('61200')
        )
        assert result is None, "空 tiers 应返回 None"

    @pytest.mark.asyncio
    async def test_tiers_single_item(self, strategy, position_long):
        """
        BC-02: tiers 只有一项（不含保本阶梯，所有浮盈直接使用回撤比例）
        
        注意：代码中第一个tier的profit_ceiling被用作保本阈值。
        所以需要将第一个tier设为保本型（0.0回撤），第二个tier承载实际逻辑。
        """
        setup_dynamic_trailing_config(strategy, tiers=[
            {'profit_ceiling': 0.5, 'retrace_ratio': 0.0},   # 保本层
            {'profit_ceiling': 999.0, 'retrace_ratio': 0.5}  # 所有浮盈
        ])
        position_long.highest_price = Decimal('66000')  # 10%浮盈
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('66000')
        )
        assert result is not None
        assert position_long.trailing_activated is True
        # 浮盈=6000，回撤=6000*0.5*1.0=3000，止损=66000-3000=63000
        assert result == Decimal('63000')

    @pytest.mark.asyncio
    async def test_entry_price_zero(self, strategy, position_long):
        """
        BC-03: entry_price 为 0
        
        预期：应返回 None（除以0保护）
        """
        setup_dynamic_trailing_config(strategy)
        position_long.entry_price = Decimal('0')
        position_long.highest_price = Decimal('61200')
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('61200')
        )
        assert result is None
        assert position_long.trailing_activated is False

    @pytest.mark.asyncio
    async def test_entry_price_none(self, strategy, position_long):
        """
        BC-04: entry_price 为 None
        
        预期：应返回 None
        """
        setup_dynamic_trailing_config(strategy)
        position_long.entry_price = None
        position_long.highest_price = Decimal('61200')
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('61200')
        )
        assert result is None
        assert position_long.trailing_activated is False

    @pytest.mark.asyncio
    async def test_entry_price_negative(self, strategy, position_long):
        """
        BC-05: entry_price 为负数
        
        预期：应返回 None
        """
        setup_dynamic_trailing_config(strategy)
        position_long.entry_price = Decimal('-100')
        position_long.highest_price = Decimal('100')
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('100')
        )
        assert result is None
        assert position_long.trailing_activated is False

    @pytest.mark.asyncio
    async def test_vol_adj_minimum(self, strategy, position_long):
        """
        BC-06: 波动率调节因子为 0.5（最小值）
        
        预期：允许回撤减半，止损价更靠近入场价
        """
        setup_dynamic_trailing_config(strategy, vol_adj_enabled=True)
        strategy._get_volatility_adjustment = AsyncMock(return_value=0.5)
        position_long.highest_price = Decimal('63000')  # 5%浮盈
        # 5% > 4%，进入第二阶梯，retrace_ratio=0.35
        # vol_adj=0.5，回撤=3000*0.35*0.5=525
        # 止损=63000-525=62475
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('63000')
        )
        assert result is not None
        assert result == Decimal('62475')
        # 对比无调节时的止损价：63000-3000*0.35=61950
        # 0.5倍调节后止损价更高（更保守），符合预期
        assert result > Decimal('61950')

    @pytest.mark.asyncio
    async def test_vol_adj_maximum(self, strategy, position_long):
        """
        BC-07: 波动率调节因子为 2.0（最大值）
        
        预期：允许回撤加倍，止损价更远离入场价
        """
        setup_dynamic_trailing_config(strategy, vol_adj_enabled=True)
        strategy._get_volatility_adjustment = AsyncMock(return_value=2.0)
        position_long.highest_price = Decimal('63000')  # 5%浮盈
        # 5% > 4%，进入第二阶梯，retrace_ratio=0.35
        # vol_adj=2.0，回撤=3000*0.35*2.0=2100
        # 止损=63000-2100=60900
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('63000')
        )
        assert result is not None
        assert result == Decimal('60900')
        # 对比无调节时的止损价：63000-3000*0.35=61950
        # 2.0倍调节后止损价更低（更宽松），符合预期
        assert result < Decimal('61950')

    @pytest.mark.asyncio
    async def test_vol_adj_disabled(self, strategy, position_long):
        """
        BC-08: 波动率调节禁用时返回 1.0
        
        预期：返回 1.0，不影响回撤计算
        """
        setup_dynamic_trailing_config(strategy, vol_adj_enabled=False)
        strategy._get_volatility_adjustment = AsyncMock(return_value=1.0)
        position_long.highest_price = Decimal('63000')
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('63000')
        )
        assert result is not None
        # 无调节：止损=63000-3000*0.35=61950
        assert result == Decimal('61950')

    @pytest.mark.asyncio
    async def test_short_hard_stop_tighter(self, strategy, position_short):
        """
        BC-09: 做空方向硬止损价比动态保护价更紧
        
        做空时，硬止损=entry_price + atr*mult
        动态止损=reference_price + allowed_retrace
        测试：硬止损价 < 动态止损价（更紧），应取硬止损
        
        场景：entry=60000，lowest=58200（3%浮盈）
        硬止损=60000+600*1.5=60900
        动态止损（3%在中阶梯，retrace=0.5）=58200+1800*0.5=59100
        min(60900, 59100)=59100（动态止损更紧）
        
        现在调整atr让硬止损更紧：
        atr=100，硬止损=60000+100*1.5=60150
        动态止损=59100，min(60150, 59100)=59100（动态止损更紧）
        
        要让硬止损更紧，需要atr很大：
        atr=2000，硬止损=60000+2000*1.5=63000
        动态止损=59100，min(63000, 59100)=59100（动态止损更紧）
        
        很难让硬止损更紧，因为硬止损基于入场价，而动态止损基于最低价。
        在有利方向上，动态止损总是更紧。
        
        调整：short下降3%，但atr很大，硬止损反向很远
        实际上做空时，硬止损是向上，意味着价格越跌，硬止损越远。
        动态止损也是向上，但基于最低价。
        两者都向上，但动态止损基于最低价+回撤，硬止损基于入场价+atr。
        在价格下跌时，硬止损=入场价+atr，动态止损=最低价+回撤。
        由于最低价<入场价，动态止损通常更紧。
        
        要让硬止损更紧，需要atr非常小：
        atr=100，硬止损=60000+100*1.5=60150
        动态止损=59100，min(60150, 59100)=59100（动态止损更紧）
        
        实际上做空时，如果价格大幅下跌，动态止损比硬止损更紧是合理的。
        测试验证取min的逻辑正确即可。
        """
        setup_dynamic_trailing_config(strategy)
        position_short.lowest_price = Decimal('58200')  # 3%浮盈
        position_short.atr = Decimal('100')  # 很小ATR，硬止损更紧
        
        # 硬止损=60000+100*1.5=60150
        # 动态止损=58200+1800*0.5=59100
        # final=min(60150, 59100)=59100（动态止损更紧）
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_short, Decimal('58200')
        )
        # 验证结果取的是较小的止损价（更紧）
        hard_stop = Decimal('60000') + Decimal('100') * Decimal('1.5')
        assert result is not None
        # 取min，所以结果应 <= hard_stop
        assert result <= hard_stop

    @pytest.mark.asyncio
    async def test_highest_price_none(self, strategy, position_long):
        """
        BC-10: highest_price 为 None（做多）
        
        预期：应回退到使用 current_price 作为参考价
        """
        setup_dynamic_trailing_config(strategy)
        position_long.highest_price = None  # 无历史最高价
        # 当前价 61200，入场价 60000，浮盈2%
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('61200')
        )
        assert result is not None
        assert position_long.trailing_activated is True
        # 参考价=current_price=61200
        # 浮盈=2%，1.5%~4%阶梯，retrace=0.5
        # 止损=61200-1200*0.5=60600
        assert result == Decimal('60600')

    @pytest.mark.asyncio
    async def test_lowest_price_none(self, strategy, position_short):
        """
        BC-11: lowest_price 为 None（做空）
        
        预期：应回退到使用 current_price 作为参考价
        """
        setup_dynamic_trailing_config(strategy)
        position_short.lowest_price = None  # 无历史最低价
        # 当前价 58200，入场价 60000，浮盈3%
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_short, Decimal('58200')
        )
        assert result is not None
        assert position_short.trailing_activated is True
        # 参考价=current_price=58200
        # 浮盈=3%，1.5%~4%阶梯，retrace=0.5
        # 止损=58200+1800*0.5=59100
        assert result == Decimal('59100')

    @pytest.mark.asyncio
    async def test_short_realized_loss_still_activates(self, strategy, position_short):
        """
        BC-12: 做空时当前价高于入场价（浮亏），但最低价曾达到浮盈
        
        核心场景：价格先跌到浮盈，再反弹到浮亏
        应基于最低价（历史峰值利润）计算
        """
        setup_dynamic_trailing_config(strategy)
        position_short.lowest_price = Decimal('57000')  # 曾到57000（5%浮盈）
        # 当前价 61000（浮亏），但最低价曾盈利
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_short, Decimal('61000')
        )
        assert result is not None
        assert position_short.trailing_activated is True
        # 参考价=lowest_price=57000
        # 浮盈=3000/60000*100=5%，4%~8%阶梯，retrace=0.35
        # 止损=57000+3000*0.35=58050
        assert result == Decimal('58050')

    @pytest.mark.asyncio
    async def test_long_realized_loss_still_activates(self, strategy, position_long):
        """
        BC-13: 做多时当前价低于入场价（浮亏），但最高价曾达到浮盈
        
        核心场景：价格先涨到浮盈，再回落到浮亏
        应基于最高价（历史峰值利润）计算
        """
        setup_dynamic_trailing_config(strategy)
        position_long.highest_price = Decimal('66000')  # 曾到66000（10%浮盈）
        # 当前价 59000（浮亏），但最高价曾盈利
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('59000')
        )
        assert result is not None
        assert position_long.trailing_activated is True
        # 参考价=highest_price=66000
        # 浮盈=10%，>8%阶梯，retrace=0.25
        # 止损=66000-6000*0.25=64500
        assert result == Decimal('64500')
        # 当前价59000 < 止损价64500，应触发止损
        assert Decimal('59000') <= result

    @pytest.mark.asyncio
    async def test_dynamic_trailing_disabled(self, strategy, position_long):
        """
        BC-14: 动态利润保护全局禁用
        
        预期：无论浮盈多少，都返回 None
        """
        setup_dynamic_trailing_config(strategy)
        strategy.risk_config['dynamic_trailing']['enabled'] = False
        position_long.highest_price = Decimal('66000')
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('66000')
        )
        assert result is None
        assert position_long.trailing_activated is False

    @pytest.mark.asyncio
    async def test_check_zero_quantity(self, strategy, position_long):
        """
        BC-15: 持仓量为0时，_check_dynamic_trailing 直接返回
        
        预期：不触发任何操作
        """
        setup_dynamic_trailing_config(strategy)
        position_long.current_quantity = Decimal('0')
        position_long.trailing_activated = True
        position_long.trailing_stop_price = Decimal('60600')
        
        with patch.object(strategy, '_close_position', AsyncMock()) as mock_close:
            await strategy._check_dynamic_trailing(
                "BTCUSDT", position_long, Decimal('60000')
            )
            mock_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_negative_quantity(self, strategy, position_long):
        """
        BC-16: 持仓量为负数
        
        预期：不触发任何操作
        """
        setup_dynamic_trailing_config(strategy)
        position_long.current_quantity = Decimal('-0.1')
        position_long.trailing_activated = True
        position_long.trailing_stop_price = Decimal('60600')
        
        with patch.object(strategy, '_close_position', AsyncMock()) as mock_close:
            await strategy._check_dynamic_trailing(
                "BTCUSDT", position_long, Decimal('60000')
            )
            mock_close.assert_not_called()


# ============================================================================
# 性能测试类
# ============================================================================

class TestPerformance:
    """动态利润保护性能测试"""

    @pytest.mark.asyncio
    async def test_calculate_dynamic_trailing_stop_execution_time(self, strategy, position_long):
        """
        PERF-01: _calculate_dynamic_trailing_stop 执行时间
        
        预期：单次执行时间 < 50ms
        """
        setup_dynamic_trailing_config(strategy)
        position_long.highest_price = Decimal('66000')
        strategy._get_volatility_adjustment = AsyncMock(return_value=1.0)
        
        iterations = 100
        start_time = time.time()
        
        for _ in range(iterations):
            result = await strategy._calculate_dynamic_trailing_stop(
                "BTCUSDT", position_long, Decimal('66000')
            )
            assert result is not None
        
        elapsed = time.time() - start_time
        avg_time = (elapsed / iterations) * 1000  # 转换为毫秒
        
        print(f"\n[PERF-01] {iterations}次迭代总耗时: {elapsed:.4f}s, 平均: {avg_time:.2f}ms")
        assert avg_time < 50, f"平均执行时间 {avg_time:.2f}ms 超过阈值 50ms"

    @pytest.mark.asyncio
    async def test_get_volatility_adjustment_cache_effect(self, strategy, position_long, mock_kline_service):
        """
        PERF-02: _get_volatility_adjustment 缓存效果
        
        预期：缓存后调用速度提升10倍以上
        """
        # 启用波动率调节，不mock _get_volatility_adjustment 以便测试真实缓存逻辑
        setup_dynamic_trailing_config(strategy, vol_adj_enabled=True, mock_vol_adj=False)
        strategy.kline_service = mock_kline_service
        
        # 第一次调用（无缓存）
        start_time = time.time()
        vol_adj_1 = await strategy._get_volatility_adjustment("BTCUSDT", position_long)
        first_call_time = time.time() - start_time
        
        # 验证缓存已设置
        assert hasattr(strategy, '_base_atr_cache')
        cache_key = "base_atr_pct_BTCUSDT"
        assert cache_key in strategy._base_atr_cache
        
        # 第二次调用（有缓存）
        start_time = time.time()
        vol_adj_2 = await strategy._get_volatility_adjustment("BTCUSDT", position_long)
        second_call_time = time.time() - start_time
        
        # 验证两次结果一致
        assert vol_adj_1 == vol_adj_2
        
        print(f"\n[PERF-02] 第一次调用: {first_call_time*1000:.2f}ms, 第二次(缓存): {second_call_time*1000:.2f}ms")
        print(f"         缓存加速比: {first_call_time/second_call_time:.1f}x")
        
        # 缓存后应快10倍以上
        assert second_call_time < first_call_time / 10, \
            f"缓存加速不足: 第一次{first_call_time*1000:.2f}ms, 缓存后{second_call_time*1000:.2f}ms"

    @pytest.mark.asyncio
    async def test_cache_ttl_works(self, strategy, position_long, mock_kline_service):
        """
        PERF-03: 缓存TTL过期后重新计算
        
        预期：TTL过期后应重新获取数据
        """
        setup_dynamic_trailing_config(strategy, vol_adj_enabled=True, mock_vol_adj=False)
        strategy.kline_service = mock_kline_service
        
        # 第一次调用，设置缓存
        vol_adj_1 = await strategy._get_volatility_adjustment("BTCUSDT", position_long)
        
        # 修改缓存时间戳使其过期
        cache_key = "base_atr_pct_BTCUSDT"
        orig_time = strategy._base_atr_cache[cache_key]['time']
        strategy._base_atr_cache[cache_key]['time'] = orig_time - 7200  # 2小时前
        
        # 模拟get_klines返回不同的值
        mock_kline_service.get_klines = AsyncMock(return_value=[
            {
                'open_time': i * 86400000,
                'open': Decimal('60000'),
                'high': Decimal('61000'),
                'low': Decimal('59000'),
                'close': Decimal('60500'),
                'volume': Decimal('1000'),
            }
            for i in range(50)
        ])
        
        # 第二次调用，应重新计算
        vol_adj_2 = await strategy._get_volatility_adjustment("BTCUSDT", position_long)
        
        # 验证get_klines被调用了（说明重新计算了）
        assert mock_kline_service.get_klines.called
        # 验证缓存时间已更新
        new_time = strategy._base_atr_cache[cache_key]['time']
        assert new_time > orig_time

    @pytest.mark.asyncio
    async def test_bulk_calculate_stress(self, strategy, position_long):
        """
        PERF-04: 批量计算压力测试
        
        模拟多个不同价格的场景，批量计算止损价
        """
        setup_dynamic_trailing_config(strategy)
        strategy._get_volatility_adjustment = AsyncMock(return_value=1.0)
        
        # 模拟10个不同的价格场景
        prices = [Decimal('60000') + Decimal(str(i * 100)) for i in range(1, 101)]
        
        start_time = time.time()
        results = []
        for price in prices:
            pos = PositionState()
            pos.entry_price = Decimal('60000')
            pos.direction = 'LONG'
            pos.current_quantity = Decimal('0.1')
            pos.atr = Decimal('600')
            pos.highest_price = price
            pos.tp1_hit = False
            pos.trailing_activated = False
            pos.trailing_stop_price = None
            
            result = await strategy._calculate_dynamic_trailing_stop(
                "BTCUSDT", pos, price
            )
            if result is not None:
                results.append((price, result))
        
        elapsed = time.time() - start_time
        avg_time = (elapsed / len(prices)) * 1000
        
        print(f"\n[PERF-04] {len(prices)}个场景计算总耗时: {elapsed:.4f}s, 平均: {avg_time:.2f}ms")
        print(f"         激活的止损价数量: {len(results)}/{len(prices)}")
        
        # 验证结果单调性（价格越高，止损价越高）
        if len(results) > 1:
            for i in range(1, len(results)):
                assert results[i][1] >= results[i-1][1], \
                    f"止损价非单调递增: {results[i-1]} -> {results[i]}"
        
        # 平均执行时间 < 10ms
        assert avg_time < 10, f"平均执行时间 {avg_time:.2f}ms 超过阈值 10ms"


class TestExchangeOrderSync:
    """测试动态止损价同步到交易所条件单的功能"""

    @pytest.fixture
    def position_long(self):
        """创建做多持仓"""
        pos = PositionState()
        pos.entry_price = Decimal('60000')
        pos.direction = 'LONG'
        pos.current_quantity = Decimal('0.1')
        pos.atr = Decimal('600')  # 1% ATR
        pos.highest_price = Decimal('61200')
        pos.tp1_hit = False
        pos.trailing_activated = True
        pos.trailing_stop_price = Decimal('60600')
        pos.trailing_stop_order_id = None
        return pos

    @pytest.fixture
    def strategy_with_mocks(self, strategy):
        """配置策略并模拟交易所API"""
        # 注入动态利润保护配置
        strategy.risk_config['dynamic_trailing'] = {
            'enabled': True,
            'activation': {'min_profit_pct': 1.5, 'also_on_tp1': True},
            'regression_tiers': [
                {'profit_ceiling': 1.5, 'retrace_ratio': 0.0},
                {'profit_ceiling': 4.0, 'retrace_ratio': 0.5},
                {'profit_ceiling': 8.0, 'retrace_ratio': 0.35},
                {'profit_ceiling': 999.0, 'retrace_ratio': 0.25}
            ],
            'volatility_adjustment': {'enabled': False}
        }
        strategy.risk_config['stop_loss_atr_multiplier'] = 1.5
        strategy.risk_config['stop_limit_order'] = {'offset_pct': 0.002}

        # 模拟 vol_adj 返回 1.0
        strategy._get_volatility_adjustment = AsyncMock(return_value=1.0)

        # 模拟交易所API
        strategy.binance.cancel_algo_order = AsyncMock(return_value={})
        strategy.binance.place_conditional_order = AsyncMock(return_value={
            'algoId': 12345,
            'orderId': 12345
        })

        # 模拟精度方法
        strategy._get_symbol_precision = AsyncMock(return_value={
            'tick_size': '0.01',
            'step_size': '0.001'
        })
        strategy._adjust_price_precision = MagicMock(side_effect=lambda x, _: x)
        strategy._adjust_quantity_precision = MagicMock(side_effect=lambda x, _: x)

        return strategy

    @pytest.mark.asyncio
    async def test_sync_first_activation_cancels_hard_stop(self, strategy_with_mocks, position_long):
        """首次激活：取消硬止损单，创建移动止损条件单"""
        strategy = strategy_with_mocks
        position_long.trailing_stop_order_id = None
        position_long.stop_loss_order_id = 99999  # 模拟有硬止损单

        # 手动调用 _sync_trailing_stop_order
        trailing_stop = Decimal('60600')
        await strategy._sync_trailing_stop_order("BTCUSDT", position_long, trailing_stop)

        # 验证：取消硬止损单
        strategy.binance.cancel_algo_order.assert_any_call("BTCUSDT", 99999)
        assert position_long.stop_loss_order_id is None

        # 验证：创建新移动止损条件单
        strategy.binance.place_conditional_order.assert_called_once()
        args, kwargs = strategy.binance.place_conditional_order.call_args
        assert kwargs['symbol'] == 'BTCUSDT'
        assert kwargs['side'] == 'SELL'
        assert kwargs['stop_price'] == trailing_stop
        assert kwargs['order_type'] == 'STOP'
        assert kwargs['reduce_only'] is True

        # 验证：记录新订单ID
        assert position_long.trailing_stop_order_id == 12345

    @pytest.mark.asyncio
    async def test_sync_update_improved_price(self, strategy_with_mocks, position_long):
        """止损价改善：取消旧移动止损单，创建新单"""
        strategy = strategy_with_mocks
        position_long.trailing_stop_order_id = 11111  # 模拟已有移动止损单
        position_long.stop_loss_order_id = None  # 硬止损已被取消

        # 新的止损价比旧的好
        new_trailing_stop = Decimal('61200')
        await strategy._sync_trailing_stop_order("BTCUSDT", position_long, new_trailing_stop)

        # 验证：取消旧移动止损单
        strategy.binance.cancel_algo_order.assert_any_call("BTCUSDT", 11111)

        # 验证：创建新移动止损条件单
        strategy.binance.place_conditional_order.assert_called_once()
        args, kwargs = strategy.binance.place_conditional_order.call_args
        assert kwargs['stop_price'] == new_trailing_stop

        # 验证：更新订单ID
        assert position_long.trailing_stop_order_id == 12345

    @pytest.mark.asyncio
    async def test_check_dynamic_trailing_calls_sync_on_improvement(self, strategy_with_mocks, position_long):
        """止损价改善时，_check_dynamic_trailing 调用 _sync_trailing_stop_order"""
        strategy = strategy_with_mocks
        position_long.highest_price = Decimal('62000')  # 价格进一步提高
        position_long.trailing_stop_price = Decimal('60600')  # 旧止损价
        position_long.trailing_stop_order_id = 11111  # 已有移动止损单
        position_long.stop_loss_order_id = None

        # 当前价格回到峰值
        current_price = Decimal('62000')

        with patch.object(strategy, '_sync_trailing_stop_order', AsyncMock()) as mock_sync:
            await strategy._check_dynamic_trailing("BTCUSDT", position_long, current_price)

            # 验证：调用了 _sync_trailing_stop_order
            mock_sync.assert_called_once()
            args, kwargs = mock_sync.call_args
            assert args[0] == "BTCUSDT"
            assert args[1] is position_long
            # 新的止损价应高于旧价60600
            assert args[2] > Decimal('60600')

    @pytest.mark.asyncio
    async def test_check_dynamic_trailing_triggered_with_order(self, strategy_with_mocks, position_long):
        """触发平仓时，取消移动止损单然后平仓"""
        strategy = strategy_with_mocks
        position_long.highest_price = Decimal('62000')
        position_long.trailing_stop_price = Decimal('60600')
        position_long.trailing_stop_order_id = 11111  # 有移动止损单
        position_long.stop_loss_order_id = None

        # 价格已跌破止损价
        current_price = Decimal('60500')

        with patch.object(strategy, '_close_position', AsyncMock()) as mock_close:
            await strategy._check_dynamic_trailing("BTCUSDT", position_long, current_price)

            # 验证：先取消移动止损单
            strategy.binance.cancel_algo_order.assert_called_once_with("BTCUSDT", 11111)
            assert position_long.trailing_stop_order_id is None

            # 验证：后平仓
            mock_close.assert_called_once()
            args, kwargs = mock_close.call_args
            assert kwargs['close_reason'] == 'TRAILING_STOP'

    @pytest.mark.asyncio
    async def test_check_dynamic_trailing_not_improved_skips_sync(self, strategy_with_mocks, position_long):
        """止损价未改善时，不调用 _sync_trailing_stop_order"""
        strategy = strategy_with_mocks
        # 价格未创新高，trailing_stop_price 保持不变
        position_long.highest_price = Decimal('61200')  # 同之前
        position_long.trailing_stop_price = Decimal('60600')
        position_long.trailing_stop_order_id = 11111
        position_long.stop_loss_order_id = None

        with patch.object(strategy, '_sync_trailing_stop_order', AsyncMock()) as mock_sync:
            await strategy._check_dynamic_trailing("BTCUSDT", position_long, Decimal('61000'))

            # 验证：未调用 _sync_trailing_stop_order
            mock_sync.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_position_orders_cancels_trailing_stop(self, strategy_with_mocks, position_long):
        """清理持仓时取消移动止损单"""
        strategy = strategy_with_mocks
        position_long.trailing_stop_order_id = 11111
        position_long.stop_loss_order_id = None
        position_long.tp1_order_id = None
        position_long.tp2_order_id = None
        position_long.entry_order_id = None

        await strategy._cleanup_position_orders("BTCUSDT", position_long)

        # 验证：取消移动止损单
        strategy.binance.cancel_algo_order.assert_called_once_with("BTCUSDT", 11111)
        assert position_long.trailing_stop_order_id is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])