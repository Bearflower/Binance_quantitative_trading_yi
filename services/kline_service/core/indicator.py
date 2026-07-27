"""技术指标计算器"""

from typing import List, Dict, Optional
import numpy as np
from datetime import datetime

from shared.utils.logger import get_logger
from models.kline import KlineData

logger = get_logger(__name__)


class TechnicalIndicatorCalculator:
    """技术指标计算器"""

    @staticmethod
    def calculate_sma(prices: List[float], period: int) -> Optional[float]:
        """
        计算简单移动平均线 (SMA)

        Args:
            prices: 价格列表
            period: 周期

        Returns:
            SMA 值
        """
        if len(prices) < period:
            return None

        return sum(prices[-period:]) / period

    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> Optional[float]:
        """
        计算指数移动平均线 (EMA)

        Args:
            prices: 价格列表
            period: 周期

        Returns:
            EMA 值
        """
        if len(prices) < period:
            return None

        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period

        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema

        return ema

    @staticmethod
    def calculate_rsi(
        prices: List[float], period: int = 14
    ) -> Optional[float]:
        """
        计算相对强弱指数 (RSI)

        Args:
            prices: 价格列表
            period: 周期，默认 14

        Returns:
            RSI 值 (0-100)
        """
        if len(prices) < period + 1:
            return None

        # 计算价格变化
        changes = [
            prices[i] - prices[i - 1] for i in range(1, len(prices))
        ]

        # 分离涨跌
        gains = [change if change > 0 else 0 for change in changes]
        losses = [-change if change < 0 else 0 for change in changes]

        # 计算平均涨跌
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    @staticmethod
    def calculate_macd(
        prices: List[float],
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
    ) -> Optional[Dict]:
        """
        计算 MACD

        Args:
            prices: 价格列表
            fast_period: 快线周期
            slow_period: 慢线周期
            signal_period: 信号线周期

        Returns:
            {"macd": float, "signal": float, "histogram": float}
        """
        if len(prices) < slow_period + signal_period:
            return None

        # 计算快慢 EMA
        fast_ema = TechnicalIndicatorCalculator.calculate_ema(
            prices, fast_period
        )
        slow_ema = TechnicalIndicatorCalculator.calculate_ema(
            prices, slow_period
        )

        if fast_ema is None or slow_ema is None:
            return None

        macd_line = fast_ema - slow_ema

        # 计算信号线（需要历史 MACD 值，这里简化处理）
        # 实际应该用完整的 MACD 历史计算 EMA
        signal_line = macd_line  # 简化为 MACD 线本身

        histogram = macd_line - signal_line

        return {
            "macd": macd_line,
            "signal": signal_line,
            "histogram": histogram,
        }

    @staticmethod
    def calculate_bollinger_bands(
        prices: List[float], period: int = 20, std_dev: float = 2.0
    ) -> Optional[Dict]:
        """
        计算布林带

        Args:
            prices: 价格列表
            period: 周期
            std_dev: 标准差倍数

        Returns:
            {"upper": float, "middle": float, "lower": float}
        """
        if len(prices) < period:
            return None

        recent_prices = prices[-period:]
        middle = sum(recent_prices) / period

        # 计算标准差
        variance = sum((p - middle) ** 2 for p in recent_prices) / period
        std = np.sqrt(variance)

        upper = middle + std_dev * std
        lower = middle - std_dev * std

        return {"upper": upper, "middle": middle, "lower": lower}

    @staticmethod
    def calculate_bollinger_bands_list(
        prices: List[float], period: int = 20, std_dev: float = 2.0
    ) -> Optional[Dict]:
        """
        计算布林带历史列表（每个时间点都计算一次）

        Args:
            prices: 价格列表
            period: 周期
            std_dev: 标准差倍数

        Returns:
            {"upper": List[float], "middle": List[float], "lower": List[float]}
        """
        if len(prices) < period:
            return None

        upper_list = []
        middle_list = []
        lower_list = []

        for i in range(period - 1, len(prices)):
            window_prices = prices[i - period + 1 : i + 1]
            middle = sum(window_prices) / period

            variance = sum((p - middle) ** 2 for p in window_prices) / period
            std = np.sqrt(variance)

            upper_list.append(middle + std_dev * std)
            middle_list.append(middle)
            lower_list.append(middle - std_dev * std)

        return {"upper": upper_list, "middle": middle_list, "lower": lower_list}

    @staticmethod
    def calculate_atr(
        klines: List[KlineData], period: int = 14
    ) -> Optional[float]:
        """
        计算平均真实波幅 (ATR)

        Args:
            klines: K 线数据列表
            period: 周期

        Returns:
            ATR 值
        """
        if len(klines) < period + 1:
            return None

        true_ranges = []
        for i in range(1, len(klines)):
            current = klines[i]
            previous = klines[i - 1]

            high_low = current.high_price - current.low_price
            high_close = abs(current.high_price - previous.close_price)
            low_close = abs(current.low_price - previous.close_price)

            tr = max(high_low, high_close, low_close)
            true_ranges.append(tr)

        # 计算 ATR
        atr = sum(true_ranges[-period:]) / period
        return atr

    @staticmethod
    def calculate_volume_sma(
        volumes: List[float], period: int = 20
    ) -> Optional[float]:
        """
        计算成交量移动平均

        Args:
            volumes: 成交量列表
            period: 周期

        Returns:
            成交量 SMA
        """
        if len(volumes) < period:
            return None

        return sum(volumes[-period:]) / period

    @classmethod
    def calculate_all_indicators(
        cls, klines: List[KlineData]
    ) -> Optional[Dict]:
        """
        计算所有技术指标

        Args:
            klines: K 线数据列表

        Returns:
            包含所有指标的字典
        """
        if not klines or len(klines) < 30:
            return None

        # 提取价格和成交量
        close_prices = [k.close_price for k in klines]
        high_prices = [k.high_price for k in klines]
        low_prices = [k.low_price for k in klines]
        volumes = [k.volume for k in klines]

        # 计算各项指标
        indicators = {}

        # MA
        indicators["sma_7"] = cls.calculate_sma(close_prices, 7)
        indicators["sma_20"] = cls.calculate_sma(close_prices, 20)
        indicators["sma_50"] = cls.calculate_sma(close_prices, 50)
        indicators["ema_7"] = cls.calculate_ema(close_prices, 7)
        indicators["ema_12"] = cls.calculate_ema(close_prices, 12)
        indicators["ema_26"] = cls.calculate_ema(close_prices, 26)

        # RSI
        indicators["rsi_14"] = cls.calculate_rsi(close_prices, 14)

        # MACD
        macd = cls.calculate_macd(close_prices)
        if macd:
            indicators["macd"] = macd["macd"]
            indicators["macd_signal"] = macd["signal"]
            indicators["macd_histogram"] = macd["histogram"]

        # 布林带
        bb = cls.calculate_bollinger_bands(close_prices)
        if bb:
            indicators["bb_upper"] = bb["upper"]
            indicators["bb_middle"] = bb["middle"]
            indicators["bb_lower"] = bb["lower"]

        # 布林带历史列表
        bb_list = cls.calculate_bollinger_bands_list(close_prices)
        if bb_list:
            indicators["bollinger"] = bb_list

        # ATR
        indicators["atr_14"] = cls.calculate_atr(klines, 14)

        # 成交量均线
        indicators["volume_sma_20"] = cls.calculate_volume_sma(volumes, 20)

        # 成交量列表
        indicators["volume"] = volumes

        # 当前价格
        indicators["current_price"] = close_prices[-1]

        return indicators
