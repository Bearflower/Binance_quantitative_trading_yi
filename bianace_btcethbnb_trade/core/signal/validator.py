#!/usr/bin/env python3
"""
信号验证器模块

功能：
1. 检查禁止交易情形
2. 验证数据完整性
3. 执行一票否决项检查
"""

import logging
from decimal import Decimal
from typing import Dict, Any
from config.strategy_params import StrategyParams, get_params

logger = logging.getLogger(__name__)


class SignalValidator:
    """信号验证器类"""

    def __init__(self, params: StrategyParams = None):
        """
        初始化信号验证器

        Args:
            params: 策略参数
        """
        self.params = params or get_params()

    def check_prohibited_conditions(self, data: Dict[str, Any]) -> bool:
        """
        检查禁止交易情形（第二章）

        Args:
            data: 行情数据

        Returns:
            True 表示可以交易，False 表示禁止交易
        """
        price_change_24h = data.get('price_change_24h', Decimal('0'))
        funding_rate = data.get('funding_rate', Decimal('0'))

        # 24 小时涨幅 > 25% 或 跌幅 > 20%
        max_rise = self.params.get('prohibited_conditions.max_24h_price_change', Decimal('0.25'))
        max_drop = self.params.get('prohibited_conditions.max_24h_price_drop', Decimal('0.20'))

        if price_change_24h > max_rise:
            logger.info(f"24 小时涨幅 {price_change_24h:.2%} > {max_rise:.2%}，禁止交易")
            return False

        if price_change_24h < -max_drop:
            logger.info(f"24 小时跌幅 {abs(price_change_24h):.2%} > {abs(max_drop):.2%}，禁止交易")
            return False

        # |资金费率| > 0.08%
        max_funding = self.params.get('prohibited_conditions.max_funding_rate', Decimal('0.0008'))

        if abs(funding_rate) > max_funding:
            logger.info(f"|资金费率| {abs(funding_rate):.4%} > {max_funding:.4%}，禁止交易")
            return False

        # TODO: 买卖价差检查（需要深度数据）
        # TODO: 重大消息检查（需要外部日历）

        return True

    def validate_data_integrity(self, data: Dict[str, Any]) -> bool:
        """
        验证数据完整性

        Args:
            data: 行情数据

        Returns:
            True 表示数据完整，False 表示数据不完整
        """
        # 检查必需字段
        required_fields = ['last_price', 'indicators']
        for field in required_fields:
            if field not in data:
                logger.warning(f"缺少必需字段：{field}")
                return False

        # 检查指标数据
        indicators = data.get('indicators', {})
        if not indicators:
            logger.warning("指标数据为空")
            return False

        # 检查至少有一个时间框架的数据
        if not any(tf in indicators for tf in ['1d', '4h', '1h']):
            logger.warning("缺少时间框架数据")
            return False

        return True

    def validate_signal(self, data: Dict[str, Any]) -> tuple:
        """
        验证信号（综合检查）

        Args:
            data: 行情数据

        Returns:
            (是否通过验证, 失败原因)
        """
        # 检查数据完整性
        if not self.validate_data_integrity(data):
            return False, "数据不完整"

        # 检查禁止交易情形
        if not self.check_prohibited_conditions(data):
            return False, "触发禁止交易情形"

        return True, None
