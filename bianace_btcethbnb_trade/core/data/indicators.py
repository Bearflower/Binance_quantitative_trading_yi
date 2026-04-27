#!/usr/bin/env python3
"""
技术指标计算模块

功能：
1. 计算各种技术指标（EMA、ATR、RSI、MACD等）
2. 支持多时间框架
3. 处理数据格式转换

Author: Trading System
Version: 1.0.0
"""

import logging
from typing import Dict, Any, List
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class IndicatorCalculator:
    """
    技术指标计算类

    负责计算各种技术指标，包括趋势指标、动量指标、波动率指标等
    """

    @staticmethod
    def calculate_timeframe_indicators(kline_data: Dict[str, List], timeframe: str) -> Dict[str, Any]:
        """
        计算单个时间框架的技术指标

        Args:
            kline_data: K线数据字典 {'close': [...], 'high': [...], 'low': [...], 'volume': [...]}
            timeframe: 时间框架

        Returns:
            指标字典
        """
        try:
            # 转换为pandas Series以便计算指标
            close_list = kline_data.get('close', [])
            high_list = kline_data.get('high', [])
            low_list = kline_data.get('low', [])

            # 检查是否有数据
            if not close_list or len(close_list) == 0:
                logger.warning(f"{timeframe} 没有K线数据")
                return {
                    'close': None,
                    'high': None,
                    'low': None,
                }

            closes = pd.Series([float(price) for price in close_list])
            highs = pd.Series([float(price) for price in high_list])
            lows = pd.Series([float(price) for price in low_list])

            # 重置索引，确保可以用整数索引访问
            closes = closes.reset_index(drop=True)
            highs = highs.reset_index(drop=True)
            lows = lows.reset_index(drop=True)

            # 基础数据（总是返回）
            result = {
                'close': float(closes.iloc[-1]) if len(closes) > 0 else None,
                'close_list': [float(c) for c in closes],
                'high': float(highs.iloc[-1]) if len(highs) > 0 else None,
                'low': float(lows.iloc[-1]) if len(lows) > 0 else None,
            }

            # 检查数据是否足够计算指标
            if len(closes) < 21:
                logger.warning(f"{timeframe} K线数据不足（只有{len(closes)}条），无法计算EMA21等指标")
                return result

            # 计算EMA21
            ema21 = IndicatorCalculator.calculate_ema(closes, period=21)
            ema21 = ema21.reset_index(drop=True)

            # 计算ATR14
            atr14 = IndicatorCalculator.calculate_atr(highs, lows, closes, period=14)
            atr14 = atr14.reset_index(drop=True)

            # 计算RSI14
            rsi14 = IndicatorCalculator.calculate_rsi(closes, period=14)
            rsi14 = rsi14.reset_index(drop=True)

            # 调试日志：查看RSI计算结果
            logger.info(f"{timeframe} RSI计算结果：长度={len(rsi14)}, 最后5个值={rsi14.tail().tolist() if len(rsi14) > 0 else '空'}")
            logger.info(f"{timeframe} RSI是否有NaN: {rsi14.isna().sum()}")

            # 修复NaN值处理：使用ffill + bfill填充初始NaN
            if rsi14.isna().sum() > 0:
                logger.info(f"{timeframe} 填充RSI NaN值，数量={rsi14.isna().sum()}")
                rsi14 = rsi14.ffill().bfill()
                logger.info(f"{timeframe} 填充后RSI是否有NaN: {rsi14.isna().sum()}")

            # 检查指标计算结果是否有效
            if len(ema21) > 0 and not pd.isna(ema21.iloc[-1]):
                result['ema21'] = float(ema21.iloc[-1])
                result['ema21_list'] = [float(e) for e in ema21]
            else:
                result['ema21'] = None
                result['ema21_list'] = []

            if len(atr14) > 0 and not pd.isna(atr14.iloc[-1]):
                result['atr14'] = float(atr14.iloc[-1])
                result['atr14_list'] = [float(a) for a in atr14]
            else:
                result['atr14'] = None
                result['atr14_list'] = []

            if len(rsi14) > 0 and not pd.isna(rsi14.iloc[-1]):
                result['rsi'] = float(rsi14.iloc[-1])
                result['rsi_list'] = [float(r) for r in rsi14]
                logger.info(f"{timeframe} RSI设置成功：{result['rsi']}")
            else:
                result['rsi'] = None
                result['rsi_list'] = []
                logger.warning(f"{timeframe} RSI计算结果为空或NaN")

            # 计算布林带（20周期，2倍标准差）
            if len(closes) >= 20:
                bb_middle = closes.rolling(window=20).mean()
                bb_std = closes.rolling(window=20).std()
                bb_upper = bb_middle + 2 * bb_std
                bb_lower = bb_middle - 2 * bb_std

                # 填充NaN值
                bb_middle = bb_middle.ffill().bfill()
                bb_upper = bb_upper.ffill().bfill()
                bb_lower = bb_lower.ffill().bfill()

                result['bollinger'] = {
                    'upper': [float(u) for u in bb_upper],
                    'middle': [float(m) for m in bb_middle],
                    'lower': [float(l) for l in bb_lower]
                }
                logger.info(f"{timeframe} 布林带计算成功，upper最后值={bb_upper.iloc[-1]:.2f}")
            else:
                result['bollinger'] = {'upper': [], 'middle': [], 'lower': []}
                logger.warning(f"{timeframe} 数据不足，无法计算布林带")

            # 添加Volume列表
            volume_list = kline_data.get('volume', [])
            if volume_list and len(volume_list) > 0:
                result['volume'] = [float(v) for v in volume_list]
                logger.info(f"{timeframe} Volume数据添加成功，长度={len(result['volume'])}")
            else:
                result['volume'] = []
                logger.warning(f"{timeframe} Volume数据为空")

            return result

        except Exception as e:
            import traceback
            logger.error(f"计算{timeframe}指标失败：{str(e)}")
            logger.error(f"堆栈跟踪：{traceback.format_exc()}")
            return {}

    @staticmethod
    def calculate_ema(data: pd.Series, period: int = 21) -> pd.Series:
        """
        计算指数移动平均线（EMA）

        Args:
            data: 价格数据序列
            period: 周期

        Returns:
            EMA序列
        """
        return data.ewm(span=period, adjust=False).mean()

    @staticmethod
    def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        """
        计算平均真实波幅（ATR）

        Args:
            high: 最高价序列
            low: 最低价序列
            close: 收盘价序列
            period: 周期

        Returns:
            ATR序列
        """
        # 计算真实波幅（TR）
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # 计算ATR
        atr = tr.rolling(window=period).mean()
        return atr

    @staticmethod
    def calculate_rsi(data: pd.Series, period: int = 14) -> pd.Series:
        """
        计算相对强弱指数（RSI）

        Args:
            data: 价格数据序列
            period: 周期

        Returns:
            RSI序列
        """
        # 计算价格变化
        delta = data.diff()

        # 分离上涨和下跌
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)

        # 计算平均上涨和平均下跌
        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()

        # 计算RS
        rs = avg_gain / avg_loss

        # 计算RSI
        rsi = 100 - (100 / (1 + rs))

        return rsi

    @staticmethod
    def calculate_macd(data: pd.Series, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> Dict[str, pd.Series]:
        """
        计算MACD指标

        Args:
            data: 价格数据序列
            fast_period: 快线周期
            slow_period: 慢线周期
            signal_period: 信号线周期

        Returns:
            MACD指标字典 {'macd': ..., 'signal': ..., 'histogram': ...}
        """
        # 计算快慢EMA
        ema_fast = data.ewm(span=fast_period, adjust=False).mean()
        ema_slow = data.ewm(span=slow_period, adjust=False).mean()

        # 计算MACD线
        macd_line = ema_fast - ema_slow

        # 计算信号线
        signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()

        # 计算柱状图
        histogram = macd_line - signal_line

        return {
            'macd': macd_line,
            'signal': signal_line,
            'histogram': histogram
        }
