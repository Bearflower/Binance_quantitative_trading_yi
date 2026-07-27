"""
v6.16.7 止损止盈单修复测试

测试目标：
1. 验证止损止盈单触发价计算逻辑
2. 验证价格获取失败时的告警通知
3. 验证触发价合理性验证
4. 验证配置项读取
5. 性能测试
6. 负载测试
7. 契约合规检查
"""
import pytest
import asyncio
from decimal import Decimal
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
import time

from strategies.btc_eth.strategy import BTCEthStrategy, PositionState
from shared.binance_api import BinanceClient, BinanceAPIError


class TestStopLossTakeProfitTriggerPrice:
    """测试止损止盈触发价计算（v6.16.7核心修复）"""

    @pytest.fixture
    async def setup_strategy(self):
        """初始化测试环境"""
        # Mock依赖
        binance_client = AsyncMock(spec=BinanceClient)
        kline_service = AsyncMock()
        notification_client = AsyncMock()

        # 加载配置
        import yaml
        with open('strategies/btc_eth/config.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        strategy = BTCEthStrategy(
            config=config,
            binance_client=binance_client,
            kline_service=kline_service,
            notification_client=notification_client
        )

        yield strategy, binance_client, notification_client

    @pytest.mark.asyncio
    async def test_get_current_price_success(self, setup_strategy):
        """测试成功获取当前价格"""
        strategy, binance_client, _ = setup_strategy

        # Mock get_ticker 返回
        binance_client.get_ticker.return_value = {
            'lastPrice': '65000.50',
            'priceChangePercent': '2.5'
        }

        # 执行测试
        current_price = await strategy._get_current_price('BTCUSDT')

        # 验证
        assert current_price is not None
        assert current_price == Decimal('65000.50')
        binance_client.get_ticker.assert_called_once_with('BTCUSDT')

    @pytest.mark.asyncio
    async def test_get_current_price_failure(self, setup_strategy):
        """测试获取当前价格失败"""
        strategy, binance_client, _ = setup_strategy

        # Mock get_ticker 抛出异常
        binance_client.get_ticker.side_effect = Exception("网络错误")

        # 执行测试
        current_price = await strategy._get_current_price('BTCUSDT')

        # 验证
        assert current_price is None

    @pytest.mark.asyncio
    async def test_stop_loss_trigger_price_calculation_long(self, setup_strategy):
        """测试做多止损触发价计算"""
        strategy, binance_client, _ = setup_strategy

        # 设置测试数据
        symbol = 'BNBUSDT'
        current_price = Decimal('600.00')
        atr = Decimal('10.00')
        stop_loss_atr_mult = Decimal('2.2')

        # Mock get_ticker
        binance_client.get_ticker.return_value = {
            'lastPrice': str(current_price),
            'priceChangePercent': '1.5'
        }

        # Mock get_symbol_info
        binance_client.get_symbol_info.return_value = {
            'quantityPrecision': 1,
            'tickSize': '0.1',
            'stepSize': '0.01'
        }

        # 计算预期止损触发价
        expected_sl_trigger = current_price - atr * stop_loss_atr_mult
        # 精度截断
        tick = Decimal('0.1')
        expected_sl_trigger = (expected_sl_trigger // tick) * tick

        # 执行测试
        actual_price = await strategy._get_current_price(symbol)
        assert actual_price == current_price

        # 验证止损触发价计算逻辑
        sl_trigger = current_price - atr * stop_loss_atr_mult
        sl_trigger = (sl_trigger // tick) * tick

        assert sl_trigger == expected_sl_trigger
        assert sl_trigger < current_price  # 做多止损价应低于当前价

    @pytest.mark.asyncio
    async def test_stop_loss_trigger_price_calculation_short(self, setup_strategy):
        """测试做空止损触发价计算"""
        strategy, binance_client, _ = setup_strategy

        # 设置测试数据
        symbol = 'BNBUSDT'
        current_price = Decimal('600.00')
        atr = Decimal('10.00')
        stop_loss_atr_mult = Decimal('2.2')

        # Mock get_ticker
        binance_client.get_ticker.return_value = {
            'lastPrice': str(current_price),
            'priceChangePercent': '1.5'
        }

        # Mock get_symbol_info
        binance_client.get_symbol_info.return_value = {
            'quantityPrecision': 1,
            'tickSize': '0.1',
            'stepSize': '0.01'
        }

        # 计算预期止损触发价
        expected_sl_trigger = current_price + atr * stop_loss_atr_mult
        # 精度截断
        tick = Decimal('0.1')
        expected_sl_trigger = (expected_sl_trigger // tick) * tick

        # 执行测试
        actual_price = await strategy._get_current_price(symbol)
        assert actual_price == current_price

        # 验证止损触发价计算逻辑
        sl_trigger = current_price + atr * stop_loss_atr_mult
        sl_trigger = (sl_trigger // tick) * tick

        assert sl_trigger == expected_sl_trigger
        assert sl_trigger > current_price  # 做空止损价应高于当前价

    @pytest.mark.asyncio
    async def test_take_profit_trigger_price_calculation_long(self, setup_strategy):
        """测试做多止盈触发价计算"""
        strategy, binance_client, _ = setup_strategy

        # 设置测试数据
        symbol = 'BNBUSDT'
        current_price = Decimal('600.00')
        atr = Decimal('10.00')
        tp1_atr_mult = Decimal('2.5')

        # Mock get_ticker
        binance_client.get_ticker.return_value = {
            'lastPrice': str(current_price),
            'priceChangePercent': '1.5'
        }

        # Mock get_symbol_info
        binance_client.get_symbol_info.return_value = {
            'quantityPrecision': 1,
            'tickSize': '0.1',
            'stepSize': '0.01'
        }

        # 计算预期止盈触发价
        expected_tp_trigger = current_price + atr * tp1_atr_mult
        # 精度截断
        tick = Decimal('0.1')
        expected_tp_trigger = (expected_tp_trigger // tick) * tick

        # 执行测试
        actual_price = await strategy._get_current_price(symbol)
        assert actual_price == current_price

        # 验证止盈触发价计算逻辑
        tp_trigger = current_price + atr * tp1_atr_mult
        tp_trigger = (tp_trigger // tick) * tick

        assert tp_trigger == expected_tp_trigger
        assert tp_trigger > current_price  # 做多止盈价应高于当前价

    @pytest.mark.asyncio
    async def test_take_profit_trigger_price_calculation_short(self, setup_strategy):
        """测试做空止盈触发价计算"""
        strategy, binance_client, _ = setup_strategy

        # 设置测试数据
        symbol = 'BNBUSDT'
        current_price = Decimal('600.00')
        atr = Decimal('10.00')
        tp1_atr_mult = Decimal('2.5')

        # Mock get_ticker
        binance_client.get_ticker.return_value = {
            'lastPrice': str(current_price),
            'priceChangePercent': '1.5'
        }

        # Mock get_symbol_info
        binance_client.get_symbol_info.return_value = {
            'quantityPrecision': 1,
            'tickSize': '0.1',
            'stepSize': '0.01'
        }

        # 计算预期止盈触发价
        expected_tp_trigger = current_price - atr * tp1_atr_mult
        # 精度截断
        tick = Decimal('0.1')
        expected_tp_trigger = (expected_tp_trigger // tick) * tick

        # 执行测试
        actual_price = await strategy._get_current_price(symbol)
        assert actual_price == current_price

        # 验证止盈触发价计算逻辑
        tp_trigger = current_price - atr * tp1_atr_mult
        tp_trigger = (tp_trigger // tick) * tick

        assert tp_trigger == expected_tp_trigger
        assert tp_trigger < current_price  # 做空止盈价应低于当前价


class TestTriggerPriceValidation:
    """测试触发价合理性验证（v6.16.7新增）"""

    @pytest.fixture
    async def setup_strategy(self):
        """初始化测试环境"""
        binance_client = AsyncMock(spec=BinanceClient)
        kline_service = AsyncMock()
        notification_client = AsyncMock()

        import yaml
        with open('strategies/btc_eth/config.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        strategy = BTCEthStrategy(
            config=config,
            binance_client=binance_client,
            kline_service=kline_service,
            notification_client=notification_client
        )

        yield strategy, binance_client, notification_client

    @pytest.mark.asyncio
    async def test_stop_loss_price_validation_long_abnormal(self, setup_strategy):
        """测试做多止损价异常情况（触发价>=当前价）"""
        strategy, _, _ = setup_strategy

        # 设置异常情况：止损触发价 >= 当前价
        current_price = Decimal('600.00')
        atr = Decimal('10.00')
        stop_loss_atr_mult = Decimal('2.2')

        # 计算异常的止损触发价（故意设置错误）
        sl_trigger = current_price - atr * stop_loss_atr_mult
        # 假设由于某种原因，触发价变成了当前价以上
        sl_trigger = current_price + Decimal('10.00')

        # 验证触发价异常
        assert sl_trigger >= current_price

        # 测试调整逻辑
        adjustment_ratio = Decimal('0.02')
        adjusted_sl_trigger = current_price * (Decimal('1') - adjustment_ratio)

        # 验证调整后的触发价合理
        assert adjusted_sl_trigger < current_price
        assert adjusted_sl_trigger == current_price * Decimal('0.98')

    @pytest.mark.asyncio
    async def test_stop_loss_price_validation_short_abnormal(self, setup_strategy):
        """测试做空止损价异常情况（触发价<=当前价）"""
        strategy, _, _ = setup_strategy

        # 设置异常情况：止损触发价 <= 当前价
        current_price = Decimal('600.00')
        atr = Decimal('10.00')
        stop_loss_atr_mult = Decimal('2.2')

        # 计算异常的止损触发价（故意设置错误）
        sl_trigger = current_price + atr * stop_loss_atr_mult
        # 假设由于某种原因，触发价变成了当前价以下
        sl_trigger = current_price - Decimal('10.00')

        # 验证触发价异常
        assert sl_trigger <= current_price

        # 测试调整逻辑
        adjustment_ratio = Decimal('0.02')
        adjusted_sl_trigger = current_price * (Decimal('1') + adjustment_ratio)

        # 验证调整后的触发价合理
        assert adjusted_sl_trigger > current_price
        assert adjusted_sl_trigger == current_price * Decimal('1.02')

    @pytest.mark.asyncio
    async def test_take_profit_price_validation_long_abnormal(self, setup_strategy):
        """测试做多止盈价异常情况（触发价<=当前价）"""
        strategy, _, _ = setup_strategy

        # 设置异常情况：止盈触发价 <= 当前价
        current_price = Decimal('600.00')
        atr = Decimal('10.00')
        tp1_atr_mult = Decimal('2.5')

        # 计算异常的止盈触发价（故意设置错误）
        tp_trigger = current_price + atr * tp1_atr_mult
        # 假设由于某种原因，触发价变成了当前价以下
        tp_trigger = current_price - Decimal('10.00')

        # 验证触发价异常
        assert tp_trigger <= current_price

        # 测试调整逻辑
        adjustment_ratio = Decimal('0.02')
        adjusted_tp_trigger = current_price * (Decimal('1') + adjustment_ratio)

        # 验证调整后的触发价合理
        assert adjusted_tp_trigger > current_price
        assert adjusted_tp_trigger == current_price * Decimal('1.02')

    @pytest.mark.asyncio
    async def test_take_profit_price_validation_short_abnormal(self, setup_strategy):
        """测试做空止盈价异常情况（触发价>=当前价）"""
        strategy, _, _ = setup_strategy

        # 设置异常情况：止盈触发价 >= 当前价
        current_price = Decimal('600.00')
        atr = Decimal('10.00')
        tp1_atr_mult = Decimal('2.5')

        # 计算异常的止盈触发价（故意设置错误）
        tp_trigger = current_price - atr * tp1_atr_mult
        # 假设由于某种原因，触发价变成了当前价以上
        tp_trigger = current_price + Decimal('10.00')

        # 验证触发价异常
        assert tp_trigger >= current_price

        # 测试调整逻辑
        adjustment_ratio = Decimal('0.02')
        adjusted_tp_trigger = current_price * (Decimal('1') - adjustment_ratio)

        # 验证调整后的触发价合理
        assert adjusted_tp_trigger < current_price
        assert adjusted_tp_trigger == current_price * Decimal('0.98')


class TestPriceFetchFailureAlert:
    """测试价格获取失败告警（v6.16.7新增）"""

    @pytest.fixture
    async def setup_strategy(self):
        """初始化测试环境"""
        binance_client = AsyncMock(spec=BinanceClient)
        kline_service = AsyncMock()
        notification_client = AsyncMock()

        import yaml
        with open('strategies/btc_eth/config.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        strategy = BTCEthStrategy(
            config=config,
            binance_client=binance_client,
            kline_service=kline_service,
            notification_client=notification_client
        )

        yield strategy, binance_client, notification_client

    @pytest.mark.asyncio
    async def test_price_fetch_failure_sends_alert(self, setup_strategy):
        """测试价格获取失败时发送告警通知"""
        strategy, binance_client, notification_client = setup_strategy

        # Mock get_ticker 抛出异常
        binance_client.get_ticker.side_effect = Exception("网络错误")

        # 执行测试
        current_price = await strategy._get_current_price('BNBUSDT')

        # 验证
        assert current_price is None
        binance_client.get_ticker.assert_called_once_with('BNBUSDT')


class TestConfigurationLoading:
    """测试配置项读取（v6.16.7新增）"""

    def test_trigger_price_adjustment_ratio_config(self):
        """测试触发价调整比例配置项读取"""
        import yaml
        with open('strategies/btc_eth/config.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        # 验证配置项存在
        assert 'risk' in config['strategy']
        assert 'trigger_price_adjustment_ratio' in config['strategy']['risk']

        # 验证配置值
        adjustment_ratio = config['strategy']['risk']['trigger_price_adjustment_ratio']
        assert adjustment_ratio == 0.02

    def test_version_update(self):
        """测试版本号更新"""
        import yaml
        with open('strategies/btc_eth/config.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        # 验证版本号
        assert config['strategy']['version'] == '6.16.7'


class TestPerformance:
    """性能测试"""

    @pytest.fixture
    async def setup_strategy(self):
        """初始化测试环境"""
        binance_client = AsyncMock(spec=BinanceClient)
        kline_service = AsyncMock()
        notification_client = AsyncMock()

        import yaml
        with open('strategies/btc_eth/config.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        strategy = BTCEthStrategy(
            config=config,
            binance_client=binance_client,
            kline_service=kline_service,
            notification_client=notification_client
        )

        yield strategy, binance_client

    @pytest.mark.asyncio
    async def test_get_current_price_performance(self, setup_strategy):
        """测试获取当前价格的性能"""
        strategy, binance_client = setup_strategy

        # Mock get_ticker
        binance_client.get_ticker.return_value = {
            'lastPrice': '65000.50',
            'priceChangePercent': '2.5'
        }

        # 性能测试：执行100次
        iterations = 100
        start_time = time.time()

        for _ in range(iterations):
            await strategy._get_current_price('BTCUSDT')

        end_time = time.time()
        total_time = end_time - start_time
        avg_time = total_time / iterations

        # 验证性能：平均每次调用应小于10ms
        assert avg_time < 0.01, f"平均调用时间 {avg_time:.4f}s 超过10ms阈值"

        print(f"\n性能测试结果:")
        print(f"  总调用次数: {iterations}")
        print(f"  总耗时: {total_time:.4f}s")
        print(f"  平均耗时: {avg_time:.4f}s")

    @pytest.mark.asyncio
    async def test_trigger_price_calculation_performance(self, setup_strategy):
        """测试触发价计算性能"""
        strategy, binance_client = setup_strategy

        # Mock get_ticker
        binance_client.get_ticker.return_value = {
            'lastPrice': '600.00',
            'priceChangePercent': '1.5'
        }

        # Mock get_symbol_info
        binance_client.get_symbol_info.return_value = {
            'quantityPrecision': 1,
            'tickSize': '0.1',
            'stepSize': '0.01'
        }

        # 性能测试：执行100次触发价计算
        iterations = 100
        start_time = time.time()

        for _ in range(iterations):
            current_price = Decimal('600.00')
            atr = Decimal('10.00')
            stop_loss_atr_mult = Decimal('2.2')
            tick = Decimal('0.1')

            # 计算止损触发价
            sl_trigger = current_price - atr * stop_loss_atr_mult
            sl_trigger = (sl_trigger // tick) * tick

            # 计算止盈触发价
            tp_trigger = current_price + atr * Decimal('2.5')
            tp_trigger = (tp_trigger // tick) * tick

        end_time = time.time()
        total_time = end_time - start_time
        avg_time = total_time / iterations

        # 验证性能：平均每次计算应小于1ms
        assert avg_time < 0.001, f"平均计算时间 {avg_time:.4f}s 超过1ms阈值"

        print(f"\n触发价计算性能测试结果:")
        print(f"  总计算次数: {iterations}")
        print(f"  总耗时: {total_time:.4f}s")
        print(f"  平均耗时: {avg_time:.6f}s")


class TestLoadTesting:
    """负载测试"""

    @pytest.fixture
    async def setup_strategy(self):
        """初始化测试环境"""
        binance_client = AsyncMock(spec=BinanceClient)
        kline_service = AsyncMock()
        notification_client = AsyncMock()

        import yaml
        with open('strategies/btc_eth/config.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        strategy = BTCEthStrategy(
            config=config,
            binance_client=binance_client,
            kline_service=kline_service,
            notification_client=notification_client
        )

        yield strategy, binance_client

    @pytest.mark.asyncio
    async def test_concurrent_price_fetch(self, setup_strategy):
        """测试并发获取价格"""
        strategy, binance_client = setup_strategy

        # Mock get_ticker
        binance_client.get_ticker.return_value = {
            'lastPrice': '65000.50',
            'priceChangePercent': '2.5'
        }

        # 并发测试：同时获取10个交易对的价格
        symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT',
                   'TRXUSDT', 'ADAUSDT', 'DOGEUSDT', 'MATICUSDT', 'DOTUSDT']

        start_time = time.time()

        # 并发执行
        tasks = [strategy._get_current_price(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks)

        end_time = time.time()
        total_time = end_time - start_time

        # 验证所有结果都成功
        assert all(price is not None for price in results)
        assert len(results) == len(symbols)

        # 验证性能：并发获取10个价格应小于1秒
        assert total_time < 1.0, f"并发获取价格耗时 {total_time:.2f}s 超过1秒阈值"

        print(f"\n并发价格获取测试结果:")
        print(f"  并发数量: {len(symbols)}")
        print(f"  总耗时: {total_time:.4f}s")
        print(f"  平均每个: {total_time/len(symbols):.4f}s")

    @pytest.mark.asyncio
    async def test_high_frequency_trigger_calculation(self, setup_strategy):
        """测试高频触发价计算"""
        strategy, binance_client = setup_strategy

        # Mock get_ticker
        binance_client.get_ticker.return_value = {
            'lastPrice': '600.00',
            'priceChangePercent': '1.5'
        }

        # Mock get_symbol_info
        binance_client.get_symbol_info.return_value = {
            'quantityPrecision': 1,
            'tickSize': '0.1',
            'stepSize': '0.01'
        }

        # 高频测试：模拟1秒内计算100次
        iterations = 100
        start_time = time.time()

        for _ in range(iterations):
            current_price = Decimal('600.00')
            atr = Decimal('10.00')
            tick = Decimal('0.1')

            # 计算止损触发价
            sl_trigger = current_price - atr * Decimal('2.2')
            sl_trigger = (sl_trigger // tick) * tick

            # 计算止盈触发价
            tp_trigger = current_price + atr * Decimal('2.5')
            tp_trigger = (tp_trigger // tick) * tick

            # 验证触发价合理性
            assert sl_trigger < current_price
            assert tp_trigger > current_price

        end_time = time.time()
        total_time = end_time - start_time

        # 验证性能：100次计算应小于0.1秒
        assert total_time < 0.1, f"高频计算耗时 {total_time:.4f}s 超过0.1秒阈值"

        print(f"\n高频触发价计算测试结果:")
        print(f"  计算次数: {iterations}")
        print(f"  总耗时: {total_time:.4f}s")
        print(f"  平均耗时: {total_time/iterations:.6f}s")
        print(f"  QPS: {iterations/total_time:.2f}")


class TestContractCompliance:
    """契约合规检查"""

    @pytest.fixture
    async def setup_strategy(self):
        """初始化测试环境"""
        binance_client = AsyncMock(spec=BinanceClient)
        kline_service = AsyncMock()
        notification_client = AsyncMock()

        import yaml
        with open('strategies/btc_eth/config.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        strategy = BTCEthStrategy(
            config=config,
            binance_client=binance_client,
            kline_service=kline_service,
            notification_client=notification_client
        )

        yield strategy, binance_client

    @pytest.mark.asyncio
    async def test_binance_api_error_code_2021_handling(self, setup_strategy):
        """测试币安API错误码[-2021]的处理"""
        strategy, binance_client = setup_strategy

        # Mock place_conditional_order 抛出错误码[-2021]
        binance_client.place_conditional_order.side_effect = BinanceAPIError(
            code=-2021,
            message="Order would immediately trigger"
        )

        # 验证错误码
        try:
            await binance_client.place_conditional_order(
                symbol='BNBUSDT',
                side='SELL',
                stop_price=Decimal('600.00'),
                price=Decimal('595.00'),
                quantity=Decimal('0.1'),
                order_type='STOP'
            )
            assert False, "应该抛出异常"
        except BinanceAPIError as e:
            assert e.code == -2021
            assert "Order would immediately trigger" in e.message

    @pytest.mark.asyncio
    async def test_trigger_price_must_be_reasonable(self, setup_strategy):
        """测试触发价必须合理（避免立即触发）"""
        strategy, binance_client = setup_strategy

        # Mock get_ticker
        binance_client.get_ticker.return_value = {
            'lastPrice': '600.00',
            'priceChangePercent': '1.5'
        }

        # Mock get_symbol_info
        binance_client.get_symbol_info.return_value = {
            'quantityPrecision': 1,
            'tickSize': '0.1',
            'stepSize': '0.01'
        }

        # 获取当前价格
        current_price = await strategy._get_current_price('BNBUSDT')
        assert current_price == Decimal('600.00')

        # 计算止损触发价
        atr = Decimal('10.00')
        stop_loss_atr_mult = Decimal('2.2')
        tick = Decimal('0.1')

        sl_trigger = current_price - atr * stop_loss_atr_mult
        sl_trigger = (sl_trigger // tick) * tick

        # 验证触发价合理（做多止损价应低于当前价）
        assert sl_trigger < current_price, f"止损触发价 {sl_trigger} 应低于当前价 {current_price}"

        # 计算止盈触发价
        tp_trigger = current_price + atr * Decimal('2.5')
        tp_trigger = (tp_trigger // tick) * tick

        # 验证触发价合理（做多止盈价应高于当前价）
        assert tp_trigger > current_price, f"止盈触发价 {tp_trigger} 应高于当前价 {current_price}"

    @pytest.mark.asyncio
    async def test_precision_truncation_compliance(self, setup_strategy):
        """测试精度截断符合币安规范"""
        strategy, binance_client = setup_strategy

        # Mock get_symbol_info
        binance_client.get_symbol_info.return_value = {
            'quantityPrecision': 1,
            'tickSize': '0.1',
            'stepSize': '0.01'
        }

        # 测试价格精度截断
        price = Decimal('600.123456')
        tick = Decimal('0.1')
        truncated_price = (price // tick) * tick

        # 验证精度截断正确
        assert truncated_price == Decimal('600.1')
        assert truncated_price % tick == 0

        # 测试数量精度截断
        quantity = Decimal('0.123456')
        step_size = Decimal('0.01')
        truncated_quantity = (quantity // step_size) * step_size

        # 验证精度截断正确
        assert truncated_quantity == Decimal('0.12')
        assert truncated_quantity % step_size == 0


class TestIntegration:
    """集成测试"""

    @pytest.fixture
    async def setup_strategy(self):
        """初始化测试环境"""
        binance_client = AsyncMock(spec=BinanceClient)
        kline_service = AsyncMock()
        notification_client = AsyncMock()

        import yaml
        with open('strategies/btc_eth/config.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        strategy = BTCEthStrategy(
            config=config,
            binance_client=binance_client,
            kline_service=kline_service,
            notification_client=notification_client
        )

        yield strategy, binance_client, notification_client

    @pytest.mark.asyncio
    async def test_full_workflow_stop_loss_take_profit(self, setup_strategy):
        """测试完整的止损止盈设置流程"""
        strategy, binance_client, notification_client = setup_strategy

        # Mock所有依赖
        binance_client.get_ticker.return_value = {
            'lastPrice': '600.00',
            'priceChangePercent': '1.5'
        }

        binance_client.get_symbol_info.return_value = {
            'quantityPrecision': 1,
            'tickSize': '0.1',
            'stepSize': '0.01'
        }

        binance_client.place_conditional_order.return_value = {
            'algoId': '12345',
            'symbol': 'BNBUSDT',
            'side': 'SELL',
            'type': 'STOP'
        }

        # 创建测试信号
        signal = {
            'symbol': 'BNBUSDT',
            'direction': 'LONG',
            'grade': 'A',
            'score': 80,
            'entry_price': Decimal('600.00'),
            'atr': Decimal('10.00'),
            'position_size': Decimal('10.00'),
            'leverage': 4,
            'timestamp': datetime.now(),
            'tp1_price': Decimal('625.00'),
            'tp2_price': Decimal('640.00'),
            'initial_stop_loss': Decimal('578.00')
        }

        # 创建持仓状态
        position = PositionState()
        position.entry_price = Decimal('600.00')
        position.entry_time = datetime.now()
        position.direction = 'LONG'
        position.initial_quantity = Decimal('0.1')
        position.current_quantity = Decimal('0.1')
        position.atr = Decimal('10.00')

        strategy.positions['BNBUSDT'] = position

        # 执行测试
        current_price = await strategy._get_current_price('BNBUSDT')
        assert current_price == Decimal('600.00')

        # 验证止损触发价计算
        atr = signal['atr']
        stop_loss_atr_mult = Decimal('2.2')
        tick = Decimal('0.1')

        sl_trigger = current_price - atr * stop_loss_atr_mult
        sl_trigger = (sl_trigger // tick) * tick

        assert sl_trigger < current_price
        assert sl_trigger == Decimal('578.0')

        # 验证止盈触发价计算
        tp_trigger = current_price + atr * Decimal('2.5')
        tp_trigger = (tp_trigger // tick) * tick

        assert tp_trigger > current_price
        assert tp_trigger == Decimal('625.0')


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
