#!/usr/bin/env python3
"""
动态仓位调整器 - v6.13 新增

根据账户可用保证金动态调整仓位大小，确保交易成功执行。

核心特性：
1. 方案 A + 最小阈值：按比例缩放仓位，同时设置最小仓位门槛
2. 保留安全垫：只使用可用余额的 80%，保留 20% 应对波动
3. 智能降仓：资金不足时自动降低仓位，而不是直接放弃交易
4. 详细日志：记录调整前后对比，便于追踪

使用方式：
    from services.position_adjuster import get_position_adjuster
    
    adjuster = get_position_adjuster()
    adjusted_position = adjuster.adjust_position(position_params, available_balance)
"""

import logging
from decimal import Decimal
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class PositionAdjuster:
    """动态仓位调整器"""
    
    def __init__(self):
        """初始化仓位调整器"""
        # v6.13 配置参数
        self.safety_ratio = Decimal('0.8')  # 保留 20% 安全垫
        self.min_position_margin = Decimal('5')  # 最小保证金 5U
        self.max_position_margin = Decimal('100')  # 单仓最大保证金 100U（防止过度杠杆）
        
        logger.info("=" * 60)
        logger.info("动态仓位调整器 v6.13 初始化完成")
        logger.info("=" * 60)
        logger.info(f"✅ 安全垫比例：{self.safety_ratio} (使用{self.safety_ratio*100}%)")
        logger.info(f"✅ 最小保证金：{self.min_position_margin}U")
        logger.info(f"✅ 最大保证金：{self.max_position_margin}U")
        logger.info("=" * 60)
    
    def adjust_position(self, position_params: Dict[str, Any], 
                       available_balance: Decimal) -> Optional[Dict[str, Any]]:
        """
        根据可用保证金动态调整仓位
        
        Args:
            position_params: 预设的仓位参数（包含 margin, quantity, notional_value 等）
            available_balance: 账户可用余额（USDT）
        
        Returns:
            调整后的仓位参数，如果资金不足则返回 None
        
        调整逻辑：
        1. 计算可用保证金 = available_balance × safety_ratio (80%)
        2. 如果 可用保证金 >= 预设保证金：不调整，全额执行
        3. 如果 可用保证金 < 预设保证金：
           - 计算调整系数 = 可用保证金 / 预设保证金
           - 调整后保证金 = 预设保证金 × 调整系数
           - 如果 调整后保证金 < 最小保证金 (5U)：返回 None，跳过交易
           - 否则：等比缩放 quantity 和 notional_value
        """
        required_margin = position_params.get('margin', Decimal('0'))
        symbol = position_params.get('symbol', 'UNKNOWN')
        
        # 参数验证
        if required_margin <= 0:
            logger.error(f"{symbol} 保证金参数错误：{required_margin}U")
            return None
        
        # 1. 计算可用保证金（保留安全垫）
        usable_balance = available_balance * self.safety_ratio
        
        logger.info(f"💰 {symbol} 资金检查：可用{available_balance}U → 可用{usable_balance}U (保留{20}%安全垫)")
        
        # 2. 资金充足，不调整
        if usable_balance >= required_margin:
            logger.info(f"✅ {symbol} 资金充足 ({usable_balance}U >= {required_margin}U)，不调整仓位")
            position_params['adjustment_info'] = {
                'adjusted': False,
                'original_margin': required_margin,
                'adjusted_margin': required_margin,
                'adjustment_ratio': Decimal('1.0'),
                'available_balance': available_balance,
                'usable_balance': usable_balance,
                'reason': '资金充足'
            }
            return position_params
        
        # 3. 资金不足，计算调整系数
        adjustment_ratio = usable_balance / required_margin
        adjusted_margin = required_margin * adjustment_ratio
        
        logger.info(f"⚠️ {symbol} 资金不足 ({usable_balance}U < {required_margin}U)，调整系数：{adjustment_ratio:.2%}")
        
        # 4. 检查是否低于最小保证金阈值
        if adjusted_margin < self.min_position_margin:
            logger.warning(f"❌ {symbol} 调整后仓位过小 ({adjusted_margin}U < {self.min_position_margin}U)，跳过交易")
            return None
        
        # 5. 检查是否超过最大保证金限制
        if adjusted_margin > self.max_position_margin:
            logger.warning(f"⚠️ {symbol} 调整后仓位过大 ({adjusted_margin}U > {self.max_position_margin}U)，限制为{self.max_position_margin}U")
            adjusted_margin = self.max_position_margin
            adjustment_ratio = adjusted_margin / required_margin
        
        # 6. 应用调整（等比缩放）
        adjusted_position = position_params.copy()
        
        # 调整数量（quantity）
        if 'quantity' in position_params:
            adjusted_position['quantity'] = position_params['quantity'] * adjustment_ratio
        
        # 调整名义价值（notional_value）
        if 'notional_value' in position_params:
            adjusted_position['notional_value'] = position_params['notional_value'] * adjustment_ratio
        
        # 更新保证金
        adjusted_position['margin'] = adjusted_margin
        
        # 记录调整信息
        adjusted_position['adjustment_info'] = {
            'adjusted': True,
            'original_margin': required_margin,
            'adjusted_margin': adjusted_margin,
            'adjustment_ratio': float(adjustment_ratio),
            'available_balance': float(available_balance),
            'usable_balance': float(usable_balance),
            'reason': '资金不足时自动降仓',
            'min_threshold': float(self.min_position_margin),
            'safety_ratio': float(self.safety_ratio)
        }
        
        # 7. 输出调整结果
        margin_reduction = required_margin - adjusted_margin
        logger.info(f"✅ {symbol} 仓位调整完成：")
        logger.info(f"   原始保证金：{required_margin}U")
        logger.info(f"   调整后保证金：{adjusted_margin}U ↓{margin_reduction}U ({adjustment_ratio:.0%})")
        logger.info(f"   调整后数量：{adjusted_position['quantity']}")
        logger.info(f"   调整后名义价值：{adjusted_position['notional_value']}U")
        
        return adjusted_position
    
    def get_adjustment_stats(self, positions: list) -> Dict[str, Any]:
        """
        获取仓位调整统计
        
        Args:
            positions: 调整后的仓位列表
        
        Returns:
            统计信息字典
        """
        total_original = Decimal('0')
        total_adjusted = Decimal('0')
        adjusted_count = 0
        
        for pos in positions:
            adj_info = pos.get('adjustment_info', {})
            if adj_info.get('adjusted'):
                total_original += Decimal(str(adj_info.get('original_margin', 0)))
                total_adjusted += Decimal(str(adj_info.get('adjusted_margin', 0)))
                adjusted_count += 1
        
        reduction_rate = (total_original - total_adjusted) / total_original if total_original > 0 else Decimal('0')
        
        return {
            'total_positions': len(positions),
            'adjusted_count': adjusted_count,
            'total_original_margin': float(total_original),
            'total_adjusted_margin': float(total_adjusted),
            'total_reduction': float(total_original - total_adjusted),
            'reduction_rate': float(reduction_rate),
            'average_adjustment_ratio': float(total_adjusted / total_original) if total_original > 0 else 0
        }


# 全局单例
_adjuster_instance = None


def get_position_adjuster() -> PositionAdjuster:
    """获取仓位调整器单例"""
    global _adjuster_instance
    if _adjuster_instance is None:
        _adjuster_instance = PositionAdjuster()
    return _adjuster_instance


if __name__ == '__main__':
    # 测试示例
    from decimal import Decimal
    
    adjuster = get_position_adjuster()
    
    # 测试用例 1：资金充足
    print("\n=== 测试 1：资金充足 ===")
    position1 = {
        'symbol': 'BTCUSDT',
        'margin': Decimal('14'),
        'quantity': Decimal('0.001'),
        'notional_value': Decimal('71')
    }
    result1 = adjuster.adjust_position(position1, Decimal('100'))
    print(f"结果：{'成功' if result1 else '失败'}")
    
    # 测试用例 2：资金略不足
    print("\n=== 测试 2：资金略不足 ===")
    position2 = {
        'symbol': 'ETHUSDT',
        'margin': Decimal('14'),
        'quantity': Decimal('0.01'),
        'notional_value': Decimal('71')
    }
    result2 = adjuster.adjust_position(position2, Decimal('12'))
    print(f"结果：{'成功' if result2 else '失败'}")
    if result2:
        print(f"调整后保证金：{result2['margin']}U")
        print(f"调整系数：{result2['adjustment_info']['adjustment_ratio']:.2%}")
    
    # 测试用例 3：资金严重不足
    print("\n=== 测试 3：资金严重不足 ===")
    position3 = {
        'symbol': 'BNBUSDT',
        'margin': Decimal('14'),
        'quantity': Decimal('0.1'),
        'notional_value': Decimal('71')
    }
    result3 = adjuster.adjust_position(position3, Decimal('6'))
    print(f"结果：{'成功' if result3 else '失败'}")
    print(f"原因：可用余额过低，调整后仓位小于最小阈值")
