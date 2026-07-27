"""
技术指标计算
提供常用的技术指标计算方法
"""
from typing import List
import pandas as pd
import numpy as np
import structlog


logger = structlog.get_logger()


class TechnicalIndicators:
    """技术指标计算器"""
    
    @staticmethod
    def _validate_dataframe(data: pd.DataFrame, required_columns: List[str]) -> None:
        """
        验证DataFrame数据
        
        Args:
            data: DataFrame数据
            required_columns: 必需的列名列表
        
        Raises:
            ValueError: 数据验证失败
        """
        if not isinstance(data, pd.DataFrame):
            raise ValueError(f"数据必须是DataFrame类型，实际为 {type(data).__name__}")
        
        if data.empty:
            raise ValueError("数据不能为空")
        
        missing_columns = [col for col in required_columns if col not in data.columns]
        if missing_columns:
            raise ValueError(f"数据缺少必需的列: {', '.join(missing_columns)}")
    
    @staticmethod
    def _validate_period(period: int) -> None:
        """
        验证周期参数
        
        Args:
            period: 周期参数
        
        Raises:
            ValueError: 周期验证失败
        """
        if not isinstance(period, (int, float)):
            raise ValueError(f"周期必须是数字，实际为 {type(period).__name__}")
        
        if period <= 0:
            raise ValueError(f"周期必须大于0: {period}")
    
    @staticmethod
    def calculate_ma(data: pd.DataFrame, period: int = 20) -> pd.Series:
        """
        计算移动平均线
        
        Args:
            data: 包含 'close' 列的DataFrame
            period: 周期
        
        Returns:
            MA序列
        
        Raises:
            ValueError: 参数验证失败
        """
        # 参数验证
        TechnicalIndicators._validate_dataframe(data, ['close'])
        TechnicalIndicators._validate_period(period)
        
        return data['close'].rolling(window=int(period)).mean()
    
    @staticmethod
    def calculate_ema(data: pd.DataFrame, period: int = 20) -> pd.Series:
        """
        计算指数移动平均线
        
        Args:
            data: 包含 'close' 列的DataFrame
            period: 周期
        
        Returns:
            EMA序列
        
        Raises:
            ValueError: 参数验证失败
        """
        # 参数验证
        TechnicalIndicators._validate_dataframe(data, ['close'])
        TechnicalIndicators._validate_period(period)
        
        return data['close'].ewm(span=int(period), adjust=False).mean()
    
    @staticmethod
    def calculate_rsi(data: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        计算RSI
        
        Args:
            data: 包含 'close' 列的DataFrame
            period: 周期
        
        Returns:
            RSI序列
        
        Raises:
            ValueError: 参数验证失败
        """
        # 参数验证
        TechnicalIndicators._validate_dataframe(data, ['close'])
        TechnicalIndicators._validate_period(period)
        
        delta = data['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=int(period)).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=int(period)).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    @staticmethod
    def calculate_macd(
        data: pd.DataFrame,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9
    ) -> tuple:
        """
        计算MACD
        
        Args:
            data: 包含 'close' 列的DataFrame
            fast_period: 快线周期
            slow_period: 慢线周期
            signal_period: 信号线周期
        
        Returns:
            (MACD, Signal, Histogram)
        
        Raises:
            ValueError: 参数验证失败
        """
        # 参数验证
        TechnicalIndicators._validate_dataframe(data, ['close'])
        TechnicalIndicators._validate_period(fast_period)
        TechnicalIndicators._validate_period(slow_period)
        TechnicalIndicators._validate_period(signal_period)
        
        if fast_period >= slow_period:
            raise ValueError(f"快线周期({fast_period})必须小于慢线周期({slow_period})")
        
        ema_fast = data['close'].ewm(span=int(fast_period), adjust=False).mean()
        ema_slow = data['close'].ewm(span=int(slow_period), adjust=False).mean()
        
        macd = ema_fast - ema_slow
        signal = macd.ewm(span=int(signal_period), adjust=False).mean()
        hist = macd - signal
        
        return macd, signal, hist
    
    @staticmethod
    def calculate_atr(data: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        计算ATR
        
        Args:
            data: 包含 'high', 'low', 'close' 列的DataFrame
            period: 周期
        
        Returns:
            ATR序列
        
        Raises:
            ValueError: 参数验证失败
        """
        # 参数验证
        TechnicalIndicators._validate_dataframe(data, ['high', 'low', 'close'])
        TechnicalIndicators._validate_period(period)
        
        high = data['high']
        low = data['low']
        close = data['close']
        
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=int(period)).mean()
        
        return atr
    
    @staticmethod
    def calculate_adx(data: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        计算ADX
        
        Args:
            data: 包含 'high', 'low', 'close' 列的DataFrame
            period: 周期
        
        Returns:
            ADX序列
        
        Raises:
            ValueError: 参数验证失败
        """
        # 参数验证
        TechnicalIndicators._validate_dataframe(data, ['high', 'low', 'close'])
        TechnicalIndicators._validate_period(period)
        
        high = data['high']
        low = data['low']
        close = data['close']
        
        plus_dm = high.diff()
        minus_dm = low.diff()
        
        plus_dm[plus_dm < 0] = 0
        minus_dm[minus_dm > 0] = 0
        
        tr = TechnicalIndicators.calculate_atr(data, period=1) * int(period)
        
        plus_di = 100 * (plus_dm.rolling(window=int(period)).mean() / tr)
        minus_di = 100 * (abs(minus_dm).rolling(window=int(period)).mean() / tr)
        
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        adx = dx.rolling(window=int(period)).mean()
        
        return adx
    
    @staticmethod
    def calculate_bollinger_bands(
        data: pd.DataFrame,
        period: int = 20,
        std_dev: int = 2
    ) -> tuple:
        """
        计算布林带
        
        Args:
            data: 包含 'close' 列的DataFrame
            period: 周期
            std_dev: 标准差倍数
        
        Returns:
            (Upper, Middle, Lower)
        
        Raises:
            ValueError: 参数验证失败
        """
        # 参数验证
        TechnicalIndicators._validate_dataframe(data, ['close'])
        TechnicalIndicators._validate_period(period)
        
        if not isinstance(std_dev, (int, float)):
            raise ValueError(f"标准差倍数必须是数字，实际为 {type(std_dev).__name__}")
        
        if std_dev <= 0:
            raise ValueError(f"标准差倍数必须大于0: {std_dev}")
        
        middle = data['close'].rolling(window=int(period)).mean()
        std = data['close'].rolling(window=int(period)).std()
        
        upper = middle + (std * std_dev)
        lower = middle - (std * std_dev)
        
        return upper, middle, lower
    
    @staticmethod
    def calculate_volume_ma(data: pd.DataFrame, period: int = 20) -> pd.Series:
        """
        计算成交量移动平均线
        
        Args:
            data: 包含 'volume' 列的DataFrame
            period: 周期
        
        Returns:
            成交量MA序列
        
        Raises:
            ValueError: 参数验证失败
        """
        # 参数验证
        TechnicalIndicators._validate_dataframe(data, ['volume'])
        TechnicalIndicators._validate_period(period)
        
        return data['volume'].rolling(window=int(period)).mean()
    
    @staticmethod
    def calculate_all(data: pd.DataFrame) -> dict:
        """
        计算所有常用指标
        
        Args:
            data: K线数据
        
        Returns:
            指标字典
        
        Raises:
            ValueError: 参数验证失败
        """
        # 参数验证
        TechnicalIndicators._validate_dataframe(data, ['high', 'low', 'close'])
        
        indicators = {}
        
        for period in [7, 21, 55]:
            indicators[f'MA{period}'] = TechnicalIndicators.calculate_ma(data, period)
        
        for period in [12, 26, 55]:  # 添加55周期EMA，用于日线趋势过滤
            indicators[f'EMA{period}'] = TechnicalIndicators.calculate_ema(data, period)
        
        indicators['RSI'] = TechnicalIndicators.calculate_rsi(data)
        
        macd, signal, hist = TechnicalIndicators.calculate_macd(data)
        indicators['MACD'] = macd
        indicators['MACD_Signal'] = signal
        indicators['MACD_Hist'] = hist
        
        indicators['ATR'] = TechnicalIndicators.calculate_atr(data)
        
        indicators['ADX'] = TechnicalIndicators.calculate_adx(data)
        
        # 成交量指标
        indicators['Volume_MA'] = TechnicalIndicators.calculate_volume_ma(data, 20)
        
        upper, middle, lower = TechnicalIndicators.calculate_bollinger_bands(data)
        indicators['BB_Upper'] = upper
        indicators['BB_Middle'] = middle
        indicators['BB_Lower'] = lower
        
        logger.info(
            "技术指标计算完成",
            indicators_count=len(indicators)
        )
        
        return indicators
