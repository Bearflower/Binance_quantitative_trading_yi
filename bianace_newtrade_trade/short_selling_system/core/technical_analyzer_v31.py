"""
技术面分析模块 v3.1

基于 1 小时 K 线的技术面分析
- 三次冲顶形态识别
- 成交量分析
- 技术面评分

修改内容：
1. 仅使用 1 小时 K 线（收益最高）
2. 三次冲顶跨 K 线判断
3. 成交量比较基准：前 5 根 K 线平均成交量的 1.5 倍
4. 信号冷却机制
"""

from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
import numpy as np

from utils.logger import logger
from core.binance_client import binance_client


try:
    import talib
    TALIB_AVAILABLE = True
    logger.info("✅ TA-Lib 已安装，使用 TA-Lib 计算技术指标")
except ImportError:
    TALIB_AVAILABLE = False
    logger.warning("⚠️  TA-Lib 未安装，使用 NumPy 备用方案计算技术指标")


class TechnicalAnalyzerV31:
    """技术面分析器 v3.1（仅使用 1 小时 K 线）"""
    
    def __init__(self):
        """初始化技术面分析器"""
        self.ema_periods = [21, 50, 200]
        self.atr_period = 14
        self.rsi_period = 14
        
        # 三次冲顶判断参数
        self.top_count_threshold = 3  # 至少 3 次冲顶
        self.top_price_tolerance = 0.02  # 价格容忍度 2%
        
        # 成交量分析参数
        self.volume_ma_period = 5  # 前 5 根 K 线
        self.volume_multiplier = 1.5  # 1.5 倍
        
        # 信号冷却
        self.cooldown_hours = 2  # 冷却时间 2 小时
        
        logger.info("✅ 技术面分析器 v3.1 初始化完成")
    
    @staticmethod
    def _numpy_ema(data: np.ndarray, period: int) -> np.ndarray:
        """使用 NumPy 计算 EMA"""
        ema = np.zeros_like(data, dtype=float)
        ema[0] = data[0]
        multiplier = 2 / (period + 1)
        
        for i in range(1, len(data)):
            ema[i] = (data[i] - ema[i-1]) * multiplier + ema[i-1]
        
        return ema
    
    @staticmethod
    def _numpy_rsi(data: np.ndarray, period: int = 14) -> np.ndarray:
        """使用 NumPy 计算 RSI"""
        delta = np.diff(data)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        
        avg_gain = np.zeros_like(data, dtype=float)
        avg_loss = np.zeros_like(data, dtype=float)
        
        if len(data) > period:
            avg_gain[period] = np.mean(gain[:period])
            avg_loss[period] = np.mean(loss[:period])
            
            for i in range(period + 1, len(data)):
                avg_gain[i] = (avg_gain[i-1] * (period - 1) + gain[i-1]) / period
                avg_loss[i] = (avg_loss[i-1] * (period - 1) + loss[i-1]) / period
            
            rs = np.zeros_like(data, dtype=float)
            mask = avg_loss != 0
            rs[mask] = avg_gain[mask] / avg_loss[mask]
            rsi = 100 - (100 / (1 + rs))
            rsi[0:period] = 50
            return rsi
        else:
            return np.full_like(data, 50.0)
    
    @staticmethod
    def _numpy_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
        """使用 NumPy 计算 ATR"""
        atr = np.zeros_like(closes, dtype=float)
        
        tr = np.zeros_like(closes, dtype=float)
        tr[0] = highs[0] - lows[0]
        
        for i in range(1, len(closes)):
            tr1 = highs[i] - lows[i]
            tr2 = abs(highs[i] - closes[i-1])
            tr3 = abs(lows[i] - closes[i-1])
            tr[i] = max(tr1, tr2, tr3)
        
        if len(closes) >= period:
            atr[period-1] = np.mean(tr[:period])
            
            for i in range(period, len(closes)):
                atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
            
            atr[0:period-1] = atr[period-1]
            return atr
        else:
            return np.full_like(closes, np.mean(tr))
    
    def calculate_ema(self, klines: List[Dict[str, Any]], period: int = 21) -> Optional[float]:
        """计算 EMA"""
        try:
            closes = np.array([k['close'] for k in klines], dtype=float)
            
            if len(closes) < period:
                return None
            
            if TALIB_AVAILABLE:
                ema = talib.EMA(closes, timeperiod=period)
            else:
                ema = self._numpy_ema(closes, period)
            
            return ema[-1]
            
        except Exception as e:
            logger.error(f"❌ 计算 EMA{period}失败：{e}")
            return None
    
    def calculate_atr(self, klines: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
        """计算 ATR"""
        try:
            highs = np.array([k['high'] for k in klines], dtype=float)
            lows = np.array([k['low'] for k in klines], dtype=float)
            closes = np.array([k['close'] for k in klines], dtype=float)
            
            if len(closes) < period:
                return None
            
            if TALIB_AVAILABLE:
                atr = talib.ATR(highs, lows, closes, timeperiod=period)
            else:
                atr = self._numpy_atr(highs, lows, closes, period)
            
            return atr[-1]
            
        except Exception as e:
            logger.error(f"❌ 计算 ATR 失败：{e}")
            return None
    
    def calculate_rsi(self, klines: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
        """计算 RSI"""
        try:
            closes = np.array([k['close'] for k in klines], dtype=float)
            
            if len(closes) < period:
                return None
            
            if TALIB_AVAILABLE:
                rsi = talib.RSI(closes, timeperiod=period)
            else:
                rsi = self._numpy_rsi(closes, period)
            
            return rsi[-1]
            
        except Exception as e:
            logger.error(f"❌ 计算 RSI 失败：{e}")
            return None
    
    def analyze_trend(self, klines: List[Dict[str, Any]]) -> str:
        """分析趋势"""
        try:
            data_points = len(klines)
            current_price = klines[-1]['close']
            
            if data_points >= 200:
                ema_short = self.calculate_ema(klines, 21)
                ema_mid = self.calculate_ema(klines, 50)
                ema_long = self.calculate_ema(klines, 200)
            elif data_points >= 50:
                ema_short = self.calculate_ema(klines, 10)
                ema_mid = self.calculate_ema(klines, 21)
                ema_long = self.calculate_ema(klines, 50)
            else:
                ema_short = self.calculate_ema(klines, 5)
                ema_mid = self.calculate_ema(klines, 10)
                ema_long = self.calculate_ema(klines, 21)
            
            if not all([ema_short, ema_mid, ema_long]):
                return 'unknown'
            
            if ema_short > ema_mid > ema_long and current_price > ema_short:
                return 'uptrend'
            elif ema_short < ema_mid < ema_long and current_price < ema_short:
                return 'downtrend'
            else:
                return 'sideways'
                
        except Exception as e:
            logger.error(f"❌ 分析趋势失败：{e}")
            return 'unknown'
    
    def detect_three_tops(self, klines: List[Dict[str, Any]], lookback: int = 5) -> Tuple[bool, Optional[float]]:
        """
        检测三次冲顶形态（支持最少 5 根 K 线）
        
        Args:
            klines: K 线数据列表（按时间正序排列）
            lookback: 回看 K 线数量（默认 5 根，最少 5 根）
            
        Returns:
            (是否形成三次冲顶，阻力位价格)
        """
        # 支持 5-10 根 K 线就开始判断
        min_klines = 5
        if len(klines) < min_klines:
            return False, None
        
        # 使用实际可用的 K 线数量（最多 lookback 根）
        actual_lookback = min(lookback, len(klines))
        recent_klines = klines[-actual_lookback:]
        
        # 获取最近 actual_lookback 根 K 线的最高点
        highs = [k['high'] for k in recent_klines]
        
        # 找出前 3 个高点
        sorted_highs = sorted(highs, reverse=True)[:3]
        
        if len(sorted_highs) < 3:
            return False, None
        
        # 检查是否在同一水平（容忍 2% 误差）
        resistance_level = sorted_highs[0]
        tolerance = resistance_level * self.top_price_tolerance
        
        # 统计有多少高点在阻力位附近
        tops_at_resistance = 0
        for high in sorted_highs:
            if abs(high - resistance_level) <= tolerance:
                tops_at_resistance += 1
        
        # 如果有 3 个高点在同一水平，形成三次冲顶
        if tops_at_resistance >= 3:
            logger.debug(f"🔝 检测到三次冲顶形态，阻力位：{resistance_level:.4f}")
            return True, resistance_level
        
        # 检查高点是否逐次降低
        if sorted_highs[0] > sorted_highs[1] > sorted_highs[2]:
            # 检查降低幅度是否显著（每次至少降低 0.5%）
            decrease_threshold = 0.005
            if (sorted_highs[0] - sorted_highs[1]) / sorted_highs[0] > decrease_threshold and \
               (sorted_highs[1] - sorted_highs[2]) / sorted_highs[1] > decrease_threshold:
                logger.debug(f"📉 检测到高点逐次降低：{sorted_highs}")
                return True, sorted_highs[0]
        
        return False, None
    
    def analyze_volume(self, klines: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        分析成交量（支持最少 6 根 K 线）
        
        Args:
            klines: K 线数据列表
            
        Returns:
            成交量分析结果
        """
        # 支持最少 6 根 K 线（5 根计算平均 + 1 根当前）
        min_klines = 6
        if len(klines) < min_klines:
            return {
                'is_high_volume': False,
                'current_volume': 0,
                'avg_volume': 0,
                'volume_ratio': 0
            }
        
        # 前 5 根 K 线的平均成交量
        recent_volumes = [k['volume'] for k in klines[-self.volume_ma_period-1:-1]]
        avg_volume = sum(recent_volumes) / len(recent_volumes)
        
        # 当前 K 线的成交量
        current_volume = klines[-1]['volume']
        
        # 成交量比率
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        # 是否放量（大于 1.5 倍）
        is_high_volume = volume_ratio >= self.volume_multiplier
        
        return {
            'is_high_volume': is_high_volume,
            'current_volume': current_volume,
            'avg_volume': avg_volume,
            'volume_ratio': round(volume_ratio, 2)
        }
    
    def check_volume_price_divergence(self, klines: List[Dict[str, Any]]) -> Tuple[bool, str]:
        """
        检查量价背离（放量滞涨，支持最少 6 根 K 线）
        
        Args:
            klines: K 线数据列表
            
        Returns:
            (是否放量滞涨，描述)
        """
        # 支持最少 6 根 K 线
        min_klines = 6
        if len(klines) < min_klines:
            return False, "数据不足"
        
        # 成交量分析
        volume_analysis = self.analyze_volume(klines)
        
        if not volume_analysis['is_high_volume']:
            return False, "成交量未放大"
        
        # 检查价格是否创新高
        current_high = klines[-1]['high']
        previous_high = max(k['high'] for k in klines[-self.volume_ma_period-1:-1])
        
        if current_high > previous_high:
            return False, "价格创新高，正常上涨"
        
        # 放量但价格未创新高 → 放量滞涨
        logger.debug(
            f"⚠️  检测到放量滞涨："
            f"成交量={volume_analysis['volume_ratio']}倍，"
            f"当前高价={current_high}, 前期高价={previous_high}"
        )
        return True, f"放量{volume_analysis['volume_ratio']}倍但价格未创新高"
    
    def calculate_technical_score(
        self,
        symbol: str,
        klines: Optional[List[Dict[str, Any]]] = None,
        listing_hours: float = 0
    ) -> Tuple[float, Dict[str, Any]]:
        """
        计算技术面评分 v3.1
        
        Args:
            symbol: 币种符号
            klines: K 线数据（可选，如果为 None 则自动获取）
            listing_hours: 上线时间（小时）
            
        Returns:
            (技术面评分，详细分析结果)
        """
        details = {
            'trend': 'unknown',
            'rsi': None,
            'atr_ratio': None,
            'data_points': 0,
            'three_tops': False,
            'resistance_level': None,
            'volume_analysis': {},
            'volume_price_divergence': False
        }
        
        try:
            # 获取 1 小时 K 线数据
            if not klines:
                klines = self.get_klines(symbol, interval='1h', limit=500)
            
            # 支持最少 10 根 K 线就开始评分（5 根看形态 +5 根看趋势）
            if not klines or len(klines) < 10:
                logger.warning(f"⚠️  {symbol} K 线数据不足（只有 {len(klines) if klines else 0} 条），使用默认评分 5.0")
                details['reason'] = '数据不足（最少 10 根）'
                return 5.0, details
            
            details['data_points'] = len(klines)
            current_price = klines[-1]['close']
            
            # 1. 趋势评分（4 分）
            trend = self.analyze_trend(klines)
            details['trend'] = trend
            
            if trend == 'downtrend':
                trend_score = 4.0
            elif trend == 'sideways':
                trend_score = 2.0
            else:
                trend_score = 0.0
            
            logger.debug(f"📊  {symbol} 趋势：{trend}, 评分：{trend_score}/4.0")
            
            # 2. RSI 评分（3 分）
            rsi = self.calculate_rsi(klines, 14)
            details['rsi'] = rsi
            
            if rsi:
                if rsi < 30:
                    rsi_score = 3.0
                elif rsi < 50:
                    rsi_score = 2.0
                elif rsi < 70:
                    rsi_score = 1.0
                else:
                    rsi_score = 0.0
                logger.debug(f"📊  {symbol} RSI: {rsi:.2f}, 评分：{rsi_score}/3.0")
            else:
                rsi_score = 1.5
            
            # 3. 波动率评分（3 分）
            atr = self.calculate_atr(klines, 14)
            details['atr_ratio'] = atr / current_price if atr and current_price > 0 else None
            
            if atr and current_price > 0:
                atr_ratio = atr / current_price
                if atr_ratio > 0.05:
                    volatility_score = 3.0
                elif atr_ratio > 0.03:
                    volatility_score = 2.0
                elif atr_ratio > 0.01:
                    volatility_score = 1.0
                else:
                    volatility_score = 0.5
                logger.debug(f"📊  {symbol} ATR 比率：{atr_ratio:.4f}, 评分：{volatility_score}/3.0")
            else:
                volatility_score = 1.5
            
            # 4. 三次冲顶形态（额外加分项，最多加 1 分）
            three_tops, resistance_level = self.detect_three_tops(klines, lookback=5)
            details['three_tops'] = three_tops
            details['resistance_level'] = resistance_level
            
            pattern_bonus = 0.0
            if three_tops:
                pattern_bonus = 1.0
                logger.info(f"🔝  {symbol} 检测到三次冲顶形态，阻力位：{resistance_level:.4f}")
            
            # 5. 量价背离分析（额外加分项，最多加 1 分）
            divergence, divergence_reason = self.check_volume_price_divergence(klines)
            details['volume_price_divergence'] = divergence
            
            divergence_bonus = 0.0
            if divergence:
                divergence_bonus = 1.0
                logger.info(f"⚠️  {symbol} 检测到放量滞涨：{divergence_reason}")
            
            # 计算总分（基础分 10 分 + 额外加分 2 分）
            total_score = trend_score + rsi_score + volatility_score + pattern_bonus + divergence_bonus
            total_score = min(total_score, 10.0)  # 最高 10 分
            
            details['volume_analysis'] = self.analyze_volume(klines)
            details['reason'] = f"趋势{trend}({trend_score}) + RSI({rsi_score}) + 波动 ({volatility_score}) + 形态 ({pattern_bonus}) + 量价 ({divergence_bonus})"
            
            logger.info(f"📊  {symbol} 技术面评分 v3.1: {total_score:.2f}/10.0")
            
            return total_score, details
            
        except Exception as e:
            logger.error(f"❌ 计算技术面评分失败：{e}")
            details['reason'] = '计算失败'
            return 5.0, details
    
    def get_klines(
        self,
        symbol: str,
        interval: str = '1h',
        limit: int = 500
    ) -> Optional[List[Dict[str, Any]]]:
        """获取 K 线数据"""
        try:
            klines = binance_client.get_kline_data(symbol, interval, limit)
            
            if not klines:
                logger.warning(f"⚠️  无法获取 {symbol} 的 K 线数据")
                return None
            
            logger.debug(f"📊 获取 {symbol} K 线数据成功，共 {len(klines)} 条")
            return klines
            
        except Exception as e:
            logger.error(f"❌ 获取 K 线数据失败：{e}")
            return None


# 全局分析器实例 v3.1
technical_analyzer_v31 = TechnicalAnalyzerV31()
