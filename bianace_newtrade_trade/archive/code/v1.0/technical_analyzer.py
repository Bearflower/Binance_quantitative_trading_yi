"""
技术面分析模块

负责：
- 获取 K 线数据
- 计算技术指标（EMA、ATR、RSI 等）
- 识别趋势
- 计算技术面评分（0-10 分）

注意：TA-Lib 是可选依赖，如果未安装会自动使用 NumPy 实现
"""

from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
import numpy as np

from utils.logger import logger
from core.binance_client import binance_client

# 尝试导入 TA-Lib，如果失败则使用备用方案
try:
    import talib
    TALIB_AVAILABLE = True
    logger.info("✅ TA-Lib 已安装，使用 TA-Lib 计算技术指标")
except ImportError:
    TALIB_AVAILABLE = False
    logger.warning("⚠️  TA-Lib 未安装，使用 NumPy 备用方案计算技术指标")


class TechnicalAnalyzer:
    """技术面分析器（支持 TA-Lib 和 NumPy 两种实现）"""
    
    def __init__(self):
        """初始化技术面分析器"""
        # 技术指标配置
        self.ema_periods = [21, 50, 200]  # EMA 周期
        self.atr_period = 14  # ATR 周期
        self.rsi_period = 14  # RSI 周期
        
        logger.info("✅ 技术面分析器初始化完成")
    
    @staticmethod
    def _numpy_ema(data: np.ndarray, period: int) -> np.ndarray:
        """
        使用 NumPy 计算 EMA（TA-Lib 备用方案）
        
        Args:
            data: 数据数组
            period: EMA 周期
            
        Returns:
            EMA 数组
        """
        ema = np.zeros_like(data, dtype=float)
        ema[0] = data[0]
        multiplier = 2 / (period + 1)
        
        for i in range(1, len(data)):
            ema[i] = (data[i] - ema[i-1]) * multiplier + ema[i-1]
        
        return ema
    
    @staticmethod
    def _numpy_rsi(data: np.ndarray, period: int = 14) -> np.ndarray:
        """
        使用 NumPy 计算 RSI（TA-Lib 备用方案）
        
        Args:
            data: 数据数组
            period: RSI 周期
            
        Returns:
            RSI 数组
        """
        delta = np.diff(data)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        
        # 初始化
        avg_gain = np.zeros_like(data, dtype=float)
        avg_loss = np.zeros_like(data, dtype=float)
        
        # 第一个周期使用简单平均
        if len(data) > period:
            avg_gain[period] = np.mean(gain[:period])
            avg_loss[period] = np.mean(loss[:period])
            
            # 后续使用平滑移动平均
            for i in range(period + 1, len(data)):
                avg_gain[i] = (avg_gain[i-1] * (period - 1) + gain[i-1]) / period
                avg_loss[i] = (avg_loss[i-1] * (period - 1) + loss[i-1]) / period
            
            # 计算 RS 和 RSI
            rs = np.zeros_like(data, dtype=float)
            mask = avg_loss != 0
            rs[mask] = avg_gain[mask] / avg_loss[mask]
            rsi = 100 - (100 / (1 + rs))
            rsi[0:period] = 50  # 前面的数据填充 50
            return rsi
        else:
            return np.full_like(data, 50.0)
    
    @staticmethod
    def _numpy_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
        """
        使用 NumPy 计算 ATR（TA-Lib 备用方案）
        
        Args:
            highs: 最高价数组
            lows: 最低价数组
            closes: 收盘价数组
            period: ATR 周期
            
        Returns:
            ATR 数组
        """
        atr = np.zeros_like(closes, dtype=float)
        
        # 计算 TR（真实波幅）
        tr = np.zeros_like(closes, dtype=float)
        tr[0] = highs[0] - lows[0]
        
        for i in range(1, len(closes)):
            tr1 = highs[i] - lows[i]
            tr2 = abs(highs[i] - closes[i-1])
            tr3 = abs(lows[i] - closes[i-1])
            tr[i] = max(tr1, tr2, tr3)
        
        # 第一个 ATR 使用简单平均
        if len(closes) >= period:
            atr[period-1] = np.mean(tr[:period])
            
            # 后续使用平滑移动平均
            for i in range(period, len(closes)):
                atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
            
            atr[0:period-1] = atr[period-1]  # 前面的数据填充
            return atr
        else:
            return np.full_like(closes, np.mean(tr))
    
    def calculate_ema(
        self,
        klines: List[Dict[str, Any]],
        period: int = 21
    ) -> Optional[float]:
        """
        计算 EMA（自动选择 TA-Lib 或 NumPy 实现）
        
        Args:
            klines: K 线数据
            period: EMA 周期
            
        Returns:
            当前 EMA 值
        """
        try:
            closes = np.array([k['close'] for k in klines], dtype=float)
            
            if len(closes) < period:
                logger.warning(f"⚠️ K 线数据不足，无法计算 EMA{period}")
                return None
            
            if TALIB_AVAILABLE:
                ema = talib.EMA(closes, timeperiod=period)
            else:
                ema = self._numpy_ema(closes, period)
            
            return ema[-1]
            
        except Exception as e:
            logger.error(f"❌ 计算 EMA{period}失败：{e}")
            return None
    
    def calculate_atr(
        self,
        klines: List[Dict[str, Any]],
        period: int = 14
    ) -> Optional[float]:
        """
        计算 ATR（自动选择 TA-Lib 或 NumPy 实现）
        
        Args:
            klines: K 线数据
            period: ATR 周期
            
        Returns:
            当前 ATR 值
        """
        try:
            highs = np.array([k['high'] for k in klines], dtype=float)
            lows = np.array([k['low'] for k in klines], dtype=float)
            closes = np.array([k['close'] for k in klines], dtype=float)
            
            if len(closes) < period:
                logger.warning(f"⚠️ K 线数据不足，无法计算 ATR{period}")
                return None
            
            if TALIB_AVAILABLE:
                atr = talib.ATR(highs, lows, closes, timeperiod=period)
            else:
                atr = self._numpy_atr(highs, lows, closes, period)
            
            return atr[-1]
            
        except Exception as e:
            logger.error(f"❌ 计算 ATR 失败：{e}")
            return None
    
    def calculate_rsi(
        self,
        klines: List[Dict[str, Any]],
        period: int = 14
    ) -> Optional[float]:
        """
        计算 RSI（自动选择 TA-Lib 或 NumPy 实现）
        
        Args:
            klines: K 线数据
            period: RSI 周期
            
        Returns:
            当前 RSI 值（0-100）
        """
        try:
            closes = np.array([k['close'] for k in klines], dtype=float)
            
            if len(closes) < period:
                logger.warning(f"⚠️ K 线数据不足，无法计算 RSI{period}")
                return None
            
            if TALIB_AVAILABLE:
                rsi = talib.RSI(closes, timeperiod=period)
            else:
                rsi = self._numpy_rsi(closes, period)
            
            return rsi[-1]
            
        except Exception as e:
            logger.error(f"❌ 计算 RSI 失败：{e}")
            return None
    
    def calculate_macd(
        self,
        klines: List[Dict[str, Any]]
    ) -> Optional[Tuple[float, float, float]]:
        """
        计算 MACD（自动选择 TA-Lib 或 NumPy 实现）
        
        Args:
            klines: K 线数据
            
        Returns:
            (MACD, Signal, Histogram) 元组
        """
        try:
            closes = np.array([k['close'] for k in klines], dtype=float)
            
            if len(closes) < 26:
                logger.warning(f"⚠️ K 线数据不足，无法计算 MACD")
                return None
            
            if TALIB_AVAILABLE:
                macd, signal, hist = talib.MACD(closes)
            else:
                # NumPy 实现 MACD
                ema12 = self._numpy_ema(closes, 12)
                ema26 = self._numpy_ema(closes, 26)
                macd = ema12 - ema26
                signal = self._numpy_ema(macd, 9)
                hist = macd - signal
            
            return macd[-1], signal[-1], hist[-1]
            
        except Exception as e:
            logger.error(f"❌ 计算 MACD 失败：{e}")
            return None
    
    def analyze_trend(
        self,
        klines: List[Dict[str, Any]]
    ) -> str:
        """
        分析趋势
        
        Args:
            klines: K 线数据
            
        Returns:
            'uptrend' / 'downtrend' / 'sideways'
        """
        try:
            data_points = len(klines)
            current_price = klines[-1]['close']
            
            # 根据数据量选择合适的 EMA 周期
            if data_points >= 200:
                ema_short = self.calculate_ema(klines, 21)
                ema_mid = self.calculate_ema(klines, 50)
                ema_long = self.calculate_ema(klines, 200)
            elif data_points >= 50:
                ema_short = self.calculate_ema(klines, 10)
                ema_mid = self.calculate_ema(klines, 21)
                ema_long = self.calculate_ema(klines, 50)
            else:  # 21-49 条数据
                ema_short = self.calculate_ema(klines, 5)
                ema_mid = self.calculate_ema(klines, 10)
                ema_long = self.calculate_ema(klines, 21)
            
            if not all([ema_short, ema_mid, ema_long]):
                return 'unknown'
            
            # 判断趋势
            if ema_short > ema_mid > ema_long and current_price > ema_short:
                return 'uptrend'
            elif ema_short < ema_mid < ema_long and current_price < ema_short:
                return 'downtrend'
            else:
                return 'sideways'
                
        except Exception as e:
            logger.error(f"❌ 分析趋势失败：{e}")
            return 'unknown'
    
    def calculate_technical_score(
        self,
        symbol: str,
        klines: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """
        计算技术面评分
        
        Args:
            symbol: 币种符号
            klines: K 线数据（可选，如果为 None 则自动获取）
            
        Returns:
            技术面评分（0-10 分）
            
        评分规则：
            - 趋势评分（4 分）：downtrend=10, sideways=5, uptrend=0
            - RSI 评分（3 分）：RSI<30=10, 30-50=7, 50-70=3, >70=0
            - 波动率评分（3 分）：基于 ATR/价格比率
        """
        try:
            # 获取 K 线数据（1h 周期，尽可能获取最多数据）
            if not klines:
                klines = self.get_klines(symbol, interval='1h', limit=500)
            
            if not klines or len(klines) < 21:
                logger.warning(f"⚠️  {symbol} K 线数据不足（只有 {len(klines) if klines else 0} 条），使用默认评分 5.0")
                return 5.0
            
            # 根据可用数据量调整分析策略
            data_points = len(klines)
            logger.debug(f"📊  {symbol} 可用 K 线数据：{data_points} 条")
            
            current_price = klines[-1]['close']
            
            # 1. 趋势评分（4 分）
            trend = self.analyze_trend(klines)
            if trend == 'downtrend':
                trend_score = 4.0  # 下跌趋势，适合做空
            elif trend == 'sideways':
                trend_score = 2.0  # 震荡
            else:
                trend_score = 0.0  # 上涨趋势，不适合做空
            
            logger.debug(f"📊  {symbol} 趋势：{trend}, 评分：{trend_score}/4.0")
            
            # 2. RSI 评分（3 分）
            rsi = self.calculate_rsi(klines, 14)
            if rsi:
                if rsi < 30:
                    rsi_score = 3.0  # 超卖，可能反弹
                elif rsi < 50:
                    rsi_score = 2.0  # 偏弱
                elif rsi < 70:
                    rsi_score = 1.0  # 偏强
                else:
                    rsi_score = 0.0  # 超买，不适合做空
                logger.debug(f"📊  {symbol} RSI: {rsi:.2f}, 评分：{rsi_score}/3.0")
            else:
                rsi_score = 1.5  # 默认中间值
            
            # 3. 波动率评分（3 分）
            atr = self.calculate_atr(klines, 14)
            if atr:
                atr_ratio = atr / current_price  # ATR/价格比率
                if atr_ratio > 0.05:
                    volatility_score = 3.0  # 高波动，机会大
                elif atr_ratio > 0.03:
                    volatility_score = 2.0  # 中等波动
                elif atr_ratio > 0.01:
                    volatility_score = 1.0  # 低波动
                else:
                    volatility_score = 0.5  # 极低波动
                logger.debug(f"📊  {symbol} ATR 比率：{atr_ratio:.4f}, 评分：{volatility_score}/3.0")
            else:
                volatility_score = 1.5  # 默认中间值
            
            # 计算总分
            total_score = trend_score + rsi_score + volatility_score
            
            logger.info(f"📊  {symbol} 技术面评分：{total_score:.2f}/10.0")
            
            return total_score
            
        except Exception as e:
            logger.error(f"❌ 计算技术面评分失败：{e}")
            return 5.0  # 失败时返回默认值
    
    def get_klines(
        self,
        symbol: str,
        interval: str = '1h',
        limit: int = 200
    ) -> Optional[List[Dict[str, Any]]]:
        """
        获取 K 线数据
        
        Args:
            symbol: 币种符号
            interval: K 线周期（15m/1h/4h/1d）
            limit: 获取数量
            
        Returns:
            K 线数据列表，每项包含：open/high/low/close/volume
        """
        try:
            klines = binance_client.get_kline_data(symbol, interval, limit)
            
            if not klines:
                logger.warning(f"⚠️  无法获取 {symbol} 的 K 线数据")
                return None
            
            # get_kline_data 已经返回字典列表，直接使用
            logger.debug(f"📊 获取 {symbol} K 线数据成功，共 {len(klines)} 条")
            return klines
            
        except Exception as e:
            logger.error(f"❌ 获取 K 线数据失败：{e}")
            return None


# 全局分析器实例
technical_analyzer = TechnicalAnalyzer()
