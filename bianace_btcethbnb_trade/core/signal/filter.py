#!/usr/bin/env python3
"""
信号过滤器模块

功能：
1. 趋势方向判断
2. ADX 趋势强度过滤
3. 成交量过滤
4. ATR 波动率过滤
"""

import logging
from decimal import Decimal
from typing import Dict, Any
from config.strategy_params import StrategyParams, get_params

logger = logging.getLogger(__name__)


class SignalFilter:
    """信号过滤器类"""

    def __init__(self, params: StrategyParams = None):
        """
        初始化信号过滤器

        Args:
            params: 策略参数
        """
        self.params = params or get_params()

    def determine_trend_direction(self, data: Dict[str, Any]) -> int:
        """
        判断趋势方向（第三章趋势过滤器）

        Args:
            data: 行情数据

        Returns:
            1: 多头方向
            -1: 空头方向
            0: 趋势不明
        """
        indicators = data.get('indicators', {})

        # 获取日线数据
        daily = indicators.get('1d', {})
        daily_ema21 = daily.get('ema21')
        daily_close = daily.get('close')

        if daily_ema21 is None or daily_close is None:
            logger.warning("日线数据不足，无法判断趋势")
            return 0

        # 日线 EMA21 方向判断
        if daily_close > daily_ema21:
            # 价格在 EMA21 之上，可能是多头
            return 1
        elif daily_close < daily_ema21:
            # 价格在 EMA21 之下，可能是空头
            return -1
        else:
            # EMA21 走平
            logger.info("日线 EMA21 走平，趋势不明")
            return 0

    def check_adx_filter(self, data: Dict[str, Any], min_adx: Decimal = Decimal('20')) -> bool:
        """
        检查 ADX 趋势强度过滤

        Args:
            data: 行情数据
            min_adx: 最小 ADX 值（默认 20）

        Returns:
            True 表示通过过滤，False 表示未通过
        """
        indicators = data.get('indicators', {})
        daily = indicators.get('1d', {})

        # TODO: 实现 ADX 计算
        # 目前简化处理：总是返回 True
        return True

    def check_volume_filter(self, data: Dict[str, Any], signal_grade: str) -> bool:
        """
        检查成交量过滤

        Args:
            data: 行情数据
            signal_grade: 信号等级

        Returns:
            True 表示通过过滤，False 表示未通过
        """
        # TODO: 实现成交量过滤
        # 目前简化处理：总是返回 True
        return True

    def check_atr_filter(self, data: Dict[str, Any]) -> bool:
        """
        检查 ATR 波动率过滤

        Args:
            data: 行情数据

        Returns:
            True 表示通过过滤，False 表示未通过
        """
        indicators = data.get('indicators', {})
        hourly = indicators.get('1h', {})

        atr14 = hourly.get('atr14')
        current_price = data.get('last_price')

        if atr14 is None or current_price is None:
            logger.warning("ATR 或价格数据不足，跳过 ATR 过滤")
            return True

        # 计算 ATR 百分比
        atr_pct = atr14 / current_price

        # ATR% 区间：2.0% ~ 4.5%
        min_atr_pct = self.params.get('signal_filters.min_atr_pct', Decimal('0.02'))
        max_atr_pct = self.params.get('signal_filters.max_atr_pct', Decimal('0.045'))

        if atr_pct < min_atr_pct:
            logger.info(f"ATR% {atr_pct:.2%} < {min_atr_pct:.2%}，波动率过低")
            return False

        if atr_pct > max_atr_pct:
            logger.info(f"ATR% {atr_pct:.2%} > {max_atr_pct:.2%}，波动率过高")
            return False

        return True

    def apply_all_filters(self, data: Dict[str, Any], direction: int, grade: str) -> tuple:
        """
        应用所有过滤器

        Args:
            data: 行情数据
            direction: 趋势方向
            grade: 信号等级

        Returns:
            (是否通过所有过滤器, 失败原因)
        """
        # 检查 ADX 过滤
        if not self.check_adx_filter(data):
            return False, "ADX 过滤未通过"

        # 检查成交量过滤
        if not self.check_volume_filter(data, grade):
            return False, "成交量过滤未通过"

        # 检查 ATR 过滤
        if not self.check_atr_filter(data):
            return False, "ATR 过滤未通过"

        return True, None
