"""
测试技术指标计算
"""
import pytest
import pandas as pd
import numpy as np
from shared.indicators import TechnicalIndicators


class TestCalculateMA:
    """测试计算移动平均线"""
    
    def test_valid_calculation(self):
        """测试有效计算"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]
        })
        
        ma = TechnicalIndicators.calculate_ma(data, period=5)
        
        assert len(ma) == len(data)
        assert ma.iloc[-1] == (105 + 106 + 107 + 108 + 109) / 5
    
    def test_invalid_data_type(self):
        """测试无效数据类型"""
        with pytest.raises(ValueError, match="数据必须是DataFrame类型"):
            TechnicalIndicators.calculate_ma([1, 2, 3, 4, 5], period=5)
    
    def test_empty_data(self):
        """测试空数据"""
        with pytest.raises(ValueError, match="数据不能为空"):
            TechnicalIndicators.calculate_ma(pd.DataFrame(), period=5)
    
    def test_missing_close_column(self):
        """测试缺少close列"""
        data = pd.DataFrame({
            'open': [100, 101, 102, 103, 104]
        })
        
        with pytest.raises(ValueError, match="数据缺少必需的列: close"):
            TechnicalIndicators.calculate_ma(data, period=5)
    
    def test_invalid_period_zero(self):
        """测试周期为0"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 104]
        })
        
        with pytest.raises(ValueError, match="周期必须大于0"):
            TechnicalIndicators.calculate_ma(data, period=0)
    
    def test_invalid_period_negative(self):
        """测试周期为负数"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 104]
        })
        
        with pytest.raises(ValueError, match="周期必须大于0"):
            TechnicalIndicators.calculate_ma(data, period=-5)


class TestCalculateEMA:
    """测试计算指数移动平均线"""
    
    def test_valid_calculation(self):
        """测试有效计算"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]
        })
        
        ema = TechnicalIndicators.calculate_ema(data, period=5)
        
        assert len(ema) == len(data)
        assert not pd.isna(ema.iloc[-1])
    
    def test_invalid_period(self):
        """测试无效周期"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 104]
        })
        
        with pytest.raises(ValueError, match="周期必须大于0"):
            TechnicalIndicators.calculate_ema(data, period=-1)


class TestCalculateRSI:
    """测试计算RSI"""
    
    def test_valid_calculation(self):
        """测试有效计算"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 102, 101, 100, 101, 102, 103,
                      104, 105, 106, 107, 108]
        })
        
        rsi = TechnicalIndicators.calculate_rsi(data, period=14)
        
        assert len(rsi) == len(data)
        assert 0 <= rsi.iloc[-1] <= 100
    
    def test_invalid_period(self):
        """测试无效周期"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 104]
        })
        
        with pytest.raises(ValueError, match="周期必须大于0"):
            TechnicalIndicators.calculate_rsi(data, period=0)


class TestCalculateMACD:
    """测试计算MACD"""
    
    def test_valid_calculation(self):
        """测试有效计算"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
                      110, 111, 112, 113, 114, 115, 116, 117, 118, 119,
                      120, 121, 122, 123, 124, 125, 126, 127, 128, 129]
        })
        
        macd, signal, hist = TechnicalIndicators.calculate_macd(
            data,
            fast_period=12,
            slow_period=26,
            signal_period=9
        )
        
        assert len(macd) == len(data)
        assert len(signal) == len(data)
        assert len(hist) == len(data)
    
    def test_invalid_fast_period(self):
        """测试快线周期大于慢线周期"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 104]
        })
        
        with pytest.raises(ValueError, match="快线周期.*必须小于慢线周期"):
            TechnicalIndicators.calculate_macd(
                data,
                fast_period=26,
                slow_period=12,
                signal_period=9
            )


class TestCalculateATR:
    """测试计算ATR"""
    
    def test_valid_calculation(self):
        """测试有效计算"""
        data = pd.DataFrame({
            'high': [105, 106, 107, 108, 109, 110, 111, 112, 113, 114,
                     115, 116, 117, 118, 119],
            'low': [95, 96, 97, 98, 99, 100, 101, 102, 103, 104,
                    105, 106, 107, 108, 109],
            'close': [100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
                      110, 111, 112, 113, 114]
        })
        
        atr = TechnicalIndicators.calculate_atr(data, period=14)
        
        assert len(atr) == len(data)
    
    def test_missing_columns(self):
        """测试缺少必需列"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 104]
        })
        
        with pytest.raises(ValueError, match="数据缺少必需的列"):
            TechnicalIndicators.calculate_atr(data, period=14)


class TestCalculateBollingerBands:
    """测试计算布林带"""
    
    def test_valid_calculation(self):
        """测试有效计算"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
                      110, 111, 112, 113, 114, 115, 116, 117, 118, 119,
                      120, 121, 122, 123, 124]
        })
        
        upper, middle, lower = TechnicalIndicators.calculate_bollinger_bands(
            data,
            period=20,
            std_dev=2
        )
        
        assert len(upper) == len(data)
        assert len(middle) == len(data)
        assert len(lower) == len(data)
    
    def test_invalid_std_dev(self):
        """测试无效的标准差倍数"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 104]
        })
        
        with pytest.raises(ValueError, match="标准差倍数必须大于0"):
            TechnicalIndicators.calculate_bollinger_bands(
                data,
                period=20,
                std_dev=0
            )


class TestCalculateAll:
    """测试计算所有指标"""
    
    def test_valid_calculation(self):
        """测试有效计算"""
        np.random.seed(42)
        data = pd.DataFrame({
            'open': np.random.randn(100) * 10 + 100,
            'high': np.random.randn(100) * 10 + 105,
            'low': np.random.randn(100) * 10 + 95,
            'close': np.random.randn(100) * 10 + 100,
            'volume': np.random.randn(100) * 1000 + 10000
        })
        
        indicators = TechnicalIndicators.calculate_all(data)
        
        assert 'MA7' in indicators
        assert 'MA21' in indicators
        assert 'MA55' in indicators
        assert 'EMA12' in indicators
        assert 'EMA26' in indicators
        assert 'EMA55' in indicators
        assert 'RSI' in indicators
        assert 'MACD' in indicators
        assert 'MACD_Signal' in indicators
        assert 'MACD_Hist' in indicators
        assert 'ATR' in indicators
        assert 'ADX' in indicators
        assert 'BB_Upper' in indicators
        assert 'BB_Middle' in indicators
        assert 'BB_Lower' in indicators
    
    def test_missing_columns(self):
        """测试缺少必需列"""
        data = pd.DataFrame({
            'close': [100, 101, 102, 103, 104]
        })
        
        with pytest.raises(ValueError, match="数据缺少必需的列"):
            TechnicalIndicators.calculate_all(data)
