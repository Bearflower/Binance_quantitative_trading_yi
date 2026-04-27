#!/usr/bin/env python3
"""
仓位计算模块

基于《虚拟货币合约交易系统化操作规范 v5.3》第四章实现仓位管理功能：
1. 基础名义价值计算：基础名义价值 = 风险金额 / 止损百分比
2. 实际名义价值计算：实际名义价值 = 基础名义价值 × 仓位系数
3. 合约数量计算：合约数量 = 实际名义价值 / 开仓价格
4. 保证金计算：保证金 = 实际名义价值 / 杠杆
5. 仓位限制检查：单品种最大名义价值、总名义价值限制

核心公式（第四章 v5.3）：
```
基础名义价值（U） = 风险金额 / 止损百分比
实际名义价值 = 基础名义价值 × 仓位系数
合约数量 = 实际名义价值 / 开仓价格
所需保证金 = 实际名义价值 / 杠杆
```

仓位系数（v5.3 差异化版）：
- S 级：50%（高确信度，半仓）
- A 级：30%（中等确信度，轻仓）
- B 级：20%（试仓，极轻仓）
"""

import logging
from decimal import Decimal
from typing import Dict, Any, Optional, Tuple
from config.strategy_params import StrategyParams, get_params

logger = logging.getLogger(__name__)


class PositionCalculator:
    """仓位计算类"""
    
    def __init__(self, params: StrategyParams = None):
        """
        初始化仓位计算器
        
        Args:
            params: 策略参数
        """
        self.params = params or get_params()
    
    def calculate_position(
        self,
        symbol: str,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        direction: int,
        signal_grade: str = 'A'
    ) -> Dict[str, Any]:
        """
        计算仓位参数（第四章核心功能）
        
        Args:
            symbol: 交易对
            entry_price: 开仓价
            stop_loss_price: 止损价
            direction: 方向（1=多，-1=空）
            signal_grade: 信号等级（S/A/B）
        
        Returns:
            仓位参数字典
        """
        logger.info(f"计算 {symbol} 仓位参数...")
        
        # 1. 计算止损百分比
        stop_loss_pct = self._calculate_stop_loss_percentage(entry_price, stop_loss_price, direction)
        
        # 2. 验证止损幅度是否在合理范围内
        if not self._validate_stop_loss_range(stop_loss_pct):
            logger.warning(f"{symbol}: 止损幅度 {stop_loss_pct:.2%} 超出合理范围")
            # 调整到合理范围
            min_stop = self.params.get('position_sizing.min_stop_loss_pct', Decimal('0.03'))
            max_stop = self.params.get('position_sizing.max_stop_loss_pct', Decimal('0.07'))
            stop_loss_pct = max(min_stop, min(stop_loss_pct, max_stop))
            logger.info(f"调整为 {stop_loss_pct:.2%}")
        
        # 3. 计算风险金额（固定为总资金的 2% = 10U）
        risk_amount = self.params.get('position_sizing.risk_amount', Decimal('10'))
        
        # 4. 计算基础名义价值：基础名义价值 = 风险金额 / 止损百分比
        base_notional_value = risk_amount / stop_loss_pct
        
        # 5. 获取信号等级的仓位系数（v5.3 核心机制）
        position_coefficient = self._get_position_coefficient(signal_grade)
        
        # 6. 计算实际名义价值：实际名义价值 = 基础名义价值 × 仓位系数
        actual_notional_value = base_notional_value * position_coefficient
        
        # 7. 获取信号等级的杠杆上限
        grade_config = self.params.get(f'signal_grades.{signal_grade}', {})
        max_leverage = grade_config.get('max_leverage', 5)
        
        # 8. 计算保证金：保证金 = 实际名义价值 / 杠杆
        margin = actual_notional_value / max_leverage
        
        # 9. 检查单仓保证金限制
        max_single_margin = self.params.get('account.single_position_margin', Decimal('30'))
        if margin > max_single_margin:
            logger.warning(f"{symbol}: 计算保证金 {margin:.2f}U 超过单仓上限 {max_single_margin:.2f}U")
            # 调整实际名义价值以符合保证金限制
            margin = max_single_margin
            actual_notional_value = margin * max_leverage
        
        # 10. 计算合约数量：合约数量 = 实际名义价值 / 开仓价格
        quantity = actual_notional_value / entry_price
        
        # 11. 检查名义价值限制
        max_position_notional = self.params.get('position_sizing.max_position_notional', Decimal('1500'))
        if actual_notional_value > max_position_notional:
            logger.warning(f"{symbol}: 名义价值 {actual_notional_value:.2f}U 超过单品种上限 {max_position_notional:.2f}U")
            actual_notional_value = max_position_notional
            quantity = actual_notional_value / entry_price
            margin = actual_notional_value / max_leverage
        
        # 12. 计算风险占比
        total_capital = self.params.get('account.total_capital', Decimal('500'))
        risk_ratio = risk_amount / total_capital
        
        position_params = {
            'symbol': symbol,
            'entry_price': entry_price,
            'stop_loss_price': stop_loss_price,
            'stop_loss_pct': stop_loss_pct,
            'direction': direction,
            'signal_grade': signal_grade,
            'base_notional_value': base_notional_value,  # 基础名义价值（U）
            'actual_notional_value': actual_notional_value,  # 实际名义价值（U）
            'position_coefficient': position_coefficient,  # 仓位系数
            'quantity': quantity,  # 合约数量
            'margin': margin,  # 保证金（U）
            'leverage': max_leverage,  # 实际使用杠杆
            'risk_amount': risk_amount,  # 风险金额（U）
            'risk_ratio': risk_ratio,  # 风险占比（%）
        }
        
        logger.info(f"{symbol} 仓位计算完成（v5.3 仓位系数机制）:")
        logger.info(f"  基础名义价值：{base_notional_value:.2f}U")
        logger.info(f"  仓位系数：{position_coefficient:.0%}")
        logger.info(f"  实际名义价值：{actual_notional_value:.2f}U")
        logger.info(f"  合约数量：{quantity:.6f}")
        logger.info(f"  保证金：{margin:.2f}U")
        logger.info(f"  杠杆：{max_leverage}x")
        logger.info(f"  风险占比：{risk_ratio:.2%}")
        
        return position_params
    
    def _calculate_stop_loss_percentage(
        self,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        direction: int
    ) -> Decimal:
        """
        计算止损百分比
        
        Args:
            entry_price: 开仓价
            stop_loss_price: 止损价
            direction: 方向（1=多，-1=空）
        
        Returns:
            止损百分比
        """
        if direction == 1:  # 多头
            # 止损百分比 = (开仓价 - 止损价) / 开仓价
            stop_loss_pct = (entry_price - stop_loss_price) / entry_price
        else:  # 空头
            # 止损百分比 = (止损价 - 开仓价) / 开仓价
            stop_loss_pct = (stop_loss_price - entry_price) / entry_price
        
        return abs(stop_loss_pct)
    
    def _validate_stop_loss_range(self, stop_loss_pct: Decimal) -> bool:
        """
        验证止损幅度是否在合理范围内
        
        Args:
            stop_loss_pct: 止损百分比
        
        Returns:
            True 表示在合理范围内
        """
        min_stop = self.params.get('position_sizing.min_stop_loss_pct', Decimal('0.03'))
        max_stop = self.params.get('position_sizing.max_stop_loss_pct', Decimal('0.07'))
        
        return min_stop <= stop_loss_pct <= max_stop
    
    def _get_position_coefficient(self, signal_grade: str) -> Decimal:
        """
        获取信号等级对应的仓位系数（v5.3 核心机制）
        
        根据《虚拟货币合约交易系统化操作规范 v5.3》第四章 4.2 节：
        - S 级：50%（高确信度，半仓）
        - A 级：30%（中等确信度，轻仓）
        - B 级：20%（试仓，极轻仓）
        
        Args:
            signal_grade: 信号等级（S/A/B）
        
        Returns:
            仓位系数（Decimal）
        """
        # 从配置中获取仓位系数，如果没有则使用默认值
        coefficient_map = {
            'S': Decimal('0.5'),  # S 级：50%
            'A': Decimal('0.3'),  # A 级：30%
            'B': Decimal('0.2'),  # B 级：20%
        }
        
        # 优先从配置读取，如果没有配置则使用默认值
        coefficient = self.params.get(
            f'position_sizing.position_coefficient.{signal_grade}',
            coefficient_map.get(signal_grade, Decimal('0.3'))  # 默认 30%（A 级）
        )
        
        logger.debug(f"{signal_grade}级信号使用仓位系数：{coefficient:.0%}")
        
        return coefficient
    
    def check_total_margin_usage(self, current_positions: list, new_position: dict) -> Tuple[bool, str]:
        """
        检查总保证金使用率（第四章仓位配置表）
        
        Args:
            current_positions: 当前持仓列表
            new_position: 新仓位参数
        
        Returns:
            (是否允许，原因)
        """
        # 计算当前已用保证金
        current_margin_used = sum(
            Decimal(pos.get('margin', '0')) for pos in current_positions
        )
        
        # 加上新仓位保证金
        new_margin = new_position.get('margin', Decimal('0'))
        total_margin = current_margin_used + new_margin
        
        # 总资金
        total_capital = self.params.get('account.total_capital', Decimal('500'))
        
        # 检查保证金使用率预警线（60%）
        margin_usage_ratio = total_margin / total_capital
        max_usage = self.params.get('risk_management.max_margin_usage', Decimal('0.6'))
        
        if margin_usage_ratio > max_usage:
            reason = f"保证金使用率 {margin_usage_ratio:.0%} 超过预警线 {max_usage:.0%}"
            return False, reason
        
        # 最大总保证金占用比例（30%）
        max_margin_ratio = self.params.get('account.max_total_margin_ratio', Decimal('0.3'))
        max_allowed_margin = total_capital * max_margin_ratio
        
        if total_margin > max_allowed_margin:
            reason = f"总保证金 {total_margin:.2f}U 超过上限 {max_allowed_margin:.2f}U（{max_margin_ratio:.0%}总资金）"
            return False, reason
        
        return True, "保证金使用率在安全范围内"
    
    def check_total_notional_value(self, current_positions: list, new_position: dict) -> Tuple[bool, str]:
        """
        检查总名义价值限制（第四章注意事项）
        
        Args:
            current_positions: 当前持仓列表
            new_position: 新仓位参数
        
        Returns:
            (是否允许，原因)
        """
        # 计算当前总名义价值
        current_notional = sum(
            Decimal(pos.get('notional_value', '0')) for pos in current_positions
        )
        
        # 加上新仓位名义价值
        new_notional = new_position.get('notional_value', Decimal('0'))
        total_notional = current_notional + new_notional
        
        # 最大总名义价值（8 倍总资金 = 4000U）
        max_total_notional = self.params.get('position_sizing.max_total_notional', Decimal('4000'))
        
        if total_notional > max_total_notional:
            reason = f"总名义价值 {total_notional:.2f}U 超过上限 {max_total_notional:.2f}U（8 倍总资金）"
            return False, reason
        
        return True, "总名义价值在安全范围内"
    
    def calculate_position_adjustment(
        self,
        original_position: dict,
        adjustment_ratio: Decimal
    ) -> dict:
        """
        计算仓位调整（加仓/减仓）
        
        Args:
            original_position: 原始仓位参数
            adjustment_ratio: 调整比例（0.5=加仓 50%，-0.3=减仓 30%）
        
        Returns:
            调整后的仓位参数
        """
        adjusted = original_position.copy()
        
        # 调整合约数量
        original_quantity = original_position['quantity']
        adjusted_quantity = original_quantity * (1 + adjustment_ratio)
        
        # 调整名义价值
        original_notional = original_position['notional_value']
        adjusted_notional = original_notional * (1 + adjustment_ratio)
        
        # 调整保证金
        original_margin = original_position['margin']
        adjusted_margin = original_margin * (1 + adjustment_ratio)
        
        adjusted.update({
            'quantity': adjusted_quantity,
            'notional_value': adjusted_notional,
            'margin': adjusted_margin,
        })
        
        logger.info(f"仓位调整：{adjustment_ratio:.0%}")
        logger.info(f"  原合约数量：{original_quantity:.6f} → 新合约数量：{adjusted_quantity:.6f}")
        logger.info(f"  原名义价值：{original_notional:.2f}U → 新名义价值：{adjusted_notional:.2f}U")
        logger.info(f"  原保证金：{original_margin:.2f}U → 新保证金：{adjusted_margin:.2f}U")
        
        return adjusted
    
    def get_position_summary(self, position: dict) -> str:
        """
        生成仓位摘要（用于日志和通知）
        
        Args:
            position: 仓位参数
        
        Returns:
            摘要字符串
        """
        # 支持新旧两种格式
        notional_value = position.get('actual_notional_value', position.get('notional_value', Decimal('0')))
        position_coefficient = position.get('position_coefficient', Decimal('1'))
        
        summary = (
            f"{position['symbol']} {position['direction']} "
            f"×{position['leverage']}杠杆 | "
            f"仓位系数：{position_coefficient:.0%} | "
            f"保证金：{position['margin']:.2f}U | "
            f"名义价值：{notional_value:.2f}U | "
            f"合约数量：{position['quantity']:.6f} | "
            f"风险：{position['risk_amount']:.2f}U ({position['risk_ratio']:.1%})"
        )
        
        return summary


# 全局实例
_global_calculator: Optional[PositionCalculator] = None


def get_position_calculator(params: StrategyParams = None) -> PositionCalculator:
    """获取仓位计算器实例（单例模式）"""
    global _global_calculator
    if _global_calculator is None:
        _global_calculator = PositionCalculator(params)
    return _global_calculator


# 便捷函数
def calculate_position(
    symbol: str,
    entry_price: Decimal,
    stop_loss_price: Decimal,
    direction: int,
    signal_grade: str = 'A'
) -> Dict[str, Any]:
    """计算仓位的便捷函数"""
    return get_position_calculator().calculate_position(
        symbol, entry_price, stop_loss_price, direction, signal_grade
    )
