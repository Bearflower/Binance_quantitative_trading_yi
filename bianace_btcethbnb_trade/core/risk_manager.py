#!/usr/bin/env python3
"""
风险管理模块

基于《虚拟货币合约交易系统化操作规范 v5.3》第五章实现风险管理功能：
1. 止损设置（固定 ATR 倍数）：止损距离 = max(关键位距离，2.0×ATR14)
2. 止盈策略（分批止盈 + 移动止损）：
   - V6.13: TP1 = 开仓价 ± 4.0×ATR14，平仓 20%（S 级）/ 30%（A/B 级）
   - V6.13: TP2 = 开仓价 ± 6.0×ATR14，平仓 30%（S 级）/ 30%（A/B 级）
   - V6.13.1: TP1 = 开仓价 ± 2.5×ATR14，平仓 25%
   - V6.13.1: TP2 = 开仓价 ± 4.0×ATR14，平仓 25%
   - 剩余 50% 使用移动止损跟踪（V6.13.1: 吊灯 1.8×ATR 启动，1.2×ATR 回撤）
3. 移动止损规则：
   - 启动条件：价格达到 TP1 后，止损移至开仓价（保本）
   - 主要跟踪：价格达到 TP2 后，使用 EMA21 作为移动止损线
   - 辅助保护：从最高点回撤 2.5×ATR（V6.13）/ 1.8×ATR（V6.13.1）立即平仓
4. 强平预防（保证金率监控）
5. 时间止损（V6.13.1 新增）：72 小时未达 TP1 平仓 50%

核心公式：
- V6.13: TP1 = 开仓价 ± 4.0×ATR14, TP2 = 开仓价 ± 6.0×ATR14
- V6.13.1: TP1 = 开仓价 ± 2.5×ATR14, TP2 = 开仓价 ± 4.0×ATR14
- 移动止损：EMA21 跟踪 + 2.5×ATR（V6.13）/ 1.8×ATR（V6.13.1）回撤保护
"""

import logging
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from config.strategy_params import StrategyParams, get_params

logger = logging.getLogger(__name__)


class RiskManager:
    """风险管理类"""
    
    def __init__(self, params: StrategyParams = None):
        """
        初始化风险管理器
        
        Args:
            params: 策略参数
        """
        self.params = params or get_params()
    
    def calculate_stop_loss(
        self,
        entry_price: Decimal,
        direction: int,
        stop_loss_pct: Decimal
    ) -> Decimal:
        """
        计算止损价（第五章止损设置）
        
        Args:
            entry_price: 开仓价
            direction: 方向（1=多，-1=空）
            stop_loss_pct: 止损幅度（百分比）
        
        Returns:
            止损价
        """
        if direction == 1:  # 多头
            # 硬止损价（多头）= 开仓价 × (1 – 止损幅度)
            stop_loss_price = entry_price * (1 - stop_loss_pct)
        else:  # 空头
            # 硬止损价（空头）= 开仓价 × (1 + 止损幅度)
            stop_loss_price = entry_price * (1 + stop_loss_pct)
        
        logger.info(f"止损价计算：{entry_price} × (1 {'-' if direction == 1 else '+'} {stop_loss_pct:.2%}) = {stop_loss_price:.2f}")
        
        return stop_loss_price
    
    def calculate_atr_based_stop_loss(
        self,
        entry_price: Decimal,
        direction: int,
        atr14: Decimal,
        key_level_distance: Decimal = None,
        max_stop_pct: Decimal = Decimal('0.07')
    ) -> Tuple[Decimal, Decimal]:
        """
        计算基于 ATR 的动态止损价（第五章 v5.3 规范）
        
        止损距离 = max(关键位距离，2.0×ATR14)
        最大止损幅度 ≤ 开仓价的 7%
        
        Args:
            entry_price: 开仓价
            direction: 方向（1=多，-1=空）
            atr14: 1 小时级别 ATR14 值
            key_level_distance: 关键支撑/阻力距离（可选）
            max_stop_pct: 最大止损幅度（默认 7%）
        
        Returns:
            (止损价，止损距离)
        """
        # v5.3 规范：2.0×ATR14
        atr_multiplier = Decimal('2.0')
        atr_distance = atr14 * atr_multiplier
        
        # 如果有技术位距离，取较大者
        if key_level_distance is not None:
            stop_distance = max(atr_distance, key_level_distance)
        else:
            stop_distance = atr_distance
        
        # 限制最大止损幅度
        max_stop_distance = entry_price * max_stop_pct
        if stop_distance > max_stop_distance:
            logger.warning(f"ATR 止损距离 {stop_distance:.2f} 超过最大限制 {max_stop_distance:.2f}，已调整")
            stop_distance = max_stop_distance
        
        # 计算止损价
        if direction == 1:  # 多头
            stop_loss_price = entry_price - stop_distance
        else:  # 空头
            stop_loss_price = entry_price + stop_distance
        
        logger.info(f"ATR 动态止损计算（v5.3）:")
        logger.info(f"  ATR14: {atr14:.2f}")
        logger.info(f"  ATR 倍数：{atr_multiplier}×")
        logger.info(f"  ATR 止损距离：{atr_distance:.2f}")
        if key_level_distance:
            logger.info(f"  技术位距离：{key_level_distance:.2f}")
        logger.info(f"  最终止损距离：{stop_distance:.2f}")
        logger.info(f"  止损价：{stop_loss_price:.2f}")
        
        return stop_loss_price, stop_distance
    
    def calculate_take_profit_levels(
        self,
        entry_price: Decimal,
        direction: int,
        atr14: Decimal,
        signal_grade: str = 'A'
    ) -> List[Dict[str, Any]]:
        """
        计算止盈水平（第五章 v5.3 止盈策略）
        
        基于 ATR14 计算分批止盈价格（v5.3 规范）：
        - TP1 = 开仓价 ± 4.0×ATR14，平仓 20%（S 级）/ 30%（A/B 级）
        - TP2 = 开仓价 ± 6.0×ATR14，平仓 30%（S 级）/ 30%（A/B 级）
        - 剩余 50%（S 级）/ 40%（A/B 级）使用移动止损跟踪
        
        Args:
            entry_price: 开仓价
            direction: 方向（1=多，-1=空）
            atr14: 1 小时级别 ATR14 值
            signal_grade: 信号等级（S/A/B），用于确定平仓比例
        
        Returns:
            止盈水平列表
        """
        # V6.13.1 止盈倍数配置（优化版）
        tp1_mult = Decimal('2.5')  # V6.13.1: TP1 = 2.5×ATR
        tp2_mult = Decimal('4.0')  # V6.13.1: TP2 = 4.0×ATR
        
        # V6.13.1 统一平仓比例（不再区分 S/A/B 级）
        tp1_ratio = Decimal('0.25')  # TP1 平仓 25%
        tp2_ratio = Decimal('0.25')  # TP2 平仓 25%
        tp3_ratio = Decimal('0.50')  # 剩余 50%
        
        take_profits = []
        
        if direction == 1:  # 多头
            # TP1 = 开仓价 + 4.0×ATR14
            tp1_price = entry_price + atr14 * tp1_mult
            # TP2 = 开仓价 + 6.0×ATR14
            tp2_price = entry_price + atr14 * tp2_mult
        else:  # 空头
            # TP1 = 开仓价 - 4.0×ATR14
            tp1_price = entry_price - atr14 * tp1_mult
            # TP2 = 开仓价 - 6.0×ATR14
            tp2_price = entry_price - atr14 * tp2_mult
        
        # TP1
        take_profits.append({
            'level': 'TP1',
            'price': tp1_price,
            'ratio': tp1_ratio,
            'description': f'{signal_grade}级：盈利{tp1_mult}×ATR，平{tp1_ratio * 100:.0f}%仓位',
            'multiplier': tp1_mult
        })
        
        # TP2
        take_profits.append({
            'level': 'TP2',
            'price': tp2_price,
            'ratio': tp2_ratio,
            'description': f'{signal_grade}级：盈利{tp2_mult}×ATR，平{tp2_ratio * 100:.0f}%仓位',
            'multiplier': tp2_mult
        })
        
        # TP3（移动止损）
        take_profits.append({
            'level': 'TP3',
            'price': None,  # 移动止损，无固定价格
            'ratio': tp3_ratio,
            'description': f'{signal_grade}级：剩余{tp3_ratio * 100:.0f}%仓位，EMA21 移动止损跟踪',
            'multiplier': None
        })
        
        logger.info(f"止盈水平计算完成（v5.3 {signal_grade}级）:")
        logger.info(f"  ATR14: {atr14:.2f}")
        logger.info(f"  TP1: {tp1_price:.2f} (盈利{tp1_mult}×ATR, 平{tp1_ratio * 100:.0f}%)")
        logger.info(f"  TP2: {tp2_price:.2f} (盈利{tp2_mult}×ATR, 平{tp2_ratio * 100:.0f}%)")
        logger.info(f"  TP3: 移动止损 (剩余{tp3_ratio * 100:.0f}%, EMA21 跟踪)")
        
        return take_profits
    
    def calculate_r_value(
        self,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        direction: int
    ) -> Decimal:
        """
        计算 R 值（止损距离）
        
        Args:
            entry_price: 开仓价
            stop_loss_price: 止损价
            direction: 方向
        
        Returns:
            R 值（绝对值）
        """
        r_value = abs(entry_price - stop_loss_price)
        logger.info(f"R 值计算：|{entry_price} - {stop_loss_price}| = {r_value:.2f}")
        return r_value
    
    def check_margin_ratio(
        self,
        account_equity: Decimal,
        used_margin: Decimal
    ) -> Tuple[Decimal, str, bool]:
        """
        检查保证金率（第五章强平预防）
        
        保证金率 = 账户权益 / 占用保证金
        
        预警线：≤ 1.5 → 减仓 50%
        紧急线：≤ 1.2 → 全部平仓
        
        Args:
            account_equity: 账户权益
            used_margin: 占用保证金
        
        Returns:
            (保证金率，风险等级，是否需要干预)
        """
        if used_margin == 0:
            return Decimal('999'), 'NONE', False
        
        margin_ratio = account_equity / used_margin
        
        # 判断风险等级
        warning_line = self.params.get('risk_management.margin_ratio_warning', Decimal('1.5'))
        emergency_line = self.params.get('risk_management.margin_ratio_emergency', Decimal('1.2'))
        
        if margin_ratio <= emergency_line:
            risk_level = 'EMERGENCY'
            need_intervention = True
            logger.warning(f"🚨 紧急线：保证金率 {margin_ratio:.2f} ≤ {emergency_line}，需要全部平仓！")
        elif margin_ratio <= warning_line:
            risk_level = 'WARNING'
            need_intervention = True
            logger.warning(f"⚠️ 预警线：保证金率 {margin_ratio:.2f} ≤ {warning_line}，需要减仓 50%！")
        else:
            risk_level = 'SAFE'
            need_intervention = False
            logger.info(f"✅ 安全：保证金率 {margin_ratio:.2f} > {warning_line}")
        
        return margin_ratio, risk_level, need_intervention
    
    def check_margin_usage(
        self,
        total_capital: Decimal,
        used_margin: Decimal
    ) -> Tuple[Decimal, bool]:
        """
        检查保证金使用率（第五章手动干预）
        
        保证金使用率 > 60% → 降杠杆或减仓
        
        Args:
            total_capital: 总资金
            used_margin: 占用保证金
        
        Returns:
            (保证金使用率，是否超限)
        """
        margin_usage = used_margin / total_capital
        max_usage = self.params.get('risk_management.max_margin_usage', Decimal('0.6'))
        
        if margin_usage > max_usage:
            logger.warning(f"⚠️ 保证金使用率 {margin_usage:.0%} > {max_usage:.0%}，需要降杠杆或减仓")
            return margin_usage, True
        else:
            logger.info(f"✅ 保证金使用率 {margin_usage:.0%} 在安全范围内")
            return margin_usage, False
    
    def check_float_loss(
        self,
        float_loss: Decimal,
        risk_amount: Decimal
    ) -> bool:
        """
        检查浮动亏损（第五章手动干预）
        
        单笔浮亏 > 风险金额 2 倍（20U） → 强制止损
        
        Args:
            float_loss: 浮动亏损（负值）
            risk_amount: 风险金额（10U）
        
        Returns:
            是否需要强制止损
        """
        max_float_loss = self.params.get('risk_management.max_float_loss', Decimal('20'))
        
        # float_loss 是负值，所以用小于号
        if float_loss < -max_float_loss:
            logger.warning(f"🚨 强制止损：单笔浮亏 {float_loss:.2f}U > {max_float_loss:.2f}U")
            return True
        else:
            logger.info(f"✅ 浮亏在可控范围内：{float_loss:.2f}U")
            return False
    
    def calculate_trailing_stop_adjustment(
        self,
        current_price: Decimal,
        original_stop_loss: Decimal,
        direction: int,
        tp_reached: str = None,
        entry_price: Decimal = None,
        tp1_price: Decimal = None
    ) -> Optional[Decimal]:
        """
        计算移动止损调整（第五章移动止损规则）
        
        移动止损规则：
        - TP1 后止损移至开仓价（保本）
        - TP2 后止损移至 TP1 价
        - TP3 后用 EMA21 跟踪 + 2.5×ATR 回撤保护
        
        Args:
            current_price: 当前价格
            original_stop_loss: 原始止损价
            direction: 方向
            tp_reached: 已到达的止盈水平（'TP1', 'TP2', 'TP3'）
            entry_price: 开仓价（用于保本止损）
            tp1_price: TP1 价格（用于 TP2 后移动）
        
        Returns:
            新的止损价，如果不需要调整则返回 None
        """
        trailing_config = self.params.get('trailing_stop', {})
        
        if tp_reached == 'TP1':
            # TP1 后止损移至开仓价（保本）
            if trailing_config.get('enable_after_tp1', True):
                logger.info("TP1 已到达，止损移至开仓价（保本）")
                return entry_price
            return None
        
        elif tp_reached == 'TP2':
            # TP2 后止损移至 TP1 价
            if trailing_config.get('enable_after_tp2', True):
                logger.info(f"TP2 已到达，止损移至 TP1 价 {tp1_price:.2f}")
                return tp1_price
            return None
        
        elif tp_reached == 'TP3':
            # TP3 后用 EMA21 跟踪（v5.3 规范）
            tracking_method = trailing_config.get('use_sar_or_ema', 'EMA21')
            logger.info(f"TP3 已到达，使用 {tracking_method} 跟踪止损")
            # 实际实现需要 EMA21 数据，由外部提供
            return None
        
        return None
    
    def check_trailing_stop_ema21(
        self,
        current_price: Decimal,
        ema21_value: Decimal,
        direction: int
    ) -> bool:
        """
        检查是否触发 EMA21 移动止损（v5.3 规范）
        
        Args:
            current_price: 当前价格
            ema21_value: 1 小时 EMA21 值
            direction: 方向（1=多，-1=空）
        
        Returns:
            True 表示触发止损，需要平仓
        """
        if direction == 1:  # 多头
            # 多头：1 小时收盘价跌破 EMA21
            if current_price < ema21_value:
                logger.info(f"多头移动止损触发：当前价 {current_price:.2f} < EMA21 {ema21_value:.2f}")
                return True
        else:  # 空头
            # 空头：1 小时收盘价突破 EMA21
            if current_price > ema21_value:
                logger.info(f"空头移动止损触发：当前价 {current_price:.2f} > EMA21 {ema21_value:.2f}")
                return True
        
        return False
    
    def check_trailing_stop_drawdown(
        self,
        current_price: Decimal,
        highest_price: Decimal,
        lowest_price: Decimal,
        atr14: Decimal,
        direction: int
    ) -> bool:
        """
        检查是否触发最大回撤止损（V6.13.1 辅助保护）
        
        V6.13: 从最高点（多头）或最低点（空头）回撤 2.5×ATR 立即平仓
        V6.13.1: 从最高点（多头）或最低点（空头）回撤 1.8×ATR 立即平仓
        
        Args:
            current_price: 当前价格
            highest_price: 持仓期间最高价（多头）或最低价（空头）
            atr14: 1 小时级别 ATR14 值
            direction: 方向（1=多，-1=空）
        
        Returns:
            True 表示触发止损，需要平仓
        """
        # V6.13.1 规范：1.8×ATR 回撤保护（优化版）
        drawdown_threshold = atr14 * Decimal('1.8')
        
        if direction == 1:  # 多头
            # 从最高点回撤
            drawdown = highest_price - current_price
            if drawdown >= drawdown_threshold:
                logger.info(f"多头回撤止损触发：从最高价 {highest_price:.2f} 回撤 {drawdown:.2f} >= {drawdown_threshold:.2f} (2.5×ATR)")
                return True
        else:  # 空头
            # 从最低点反弹
            drawdown = current_price - lowest_price
            if drawdown >= drawdown_threshold:
                logger.info(f"空头回撤止损触发：从最低价 {lowest_price:.2f} 反弹 {drawdown:.2f} >= {drawdown_threshold:.2f} (2.5×ATR)")
                return True
        
        return False
    
    def generate_risk_report(
        self,
        positions: List[Dict[str, Any]],
        account_equity: Decimal,
        total_capital: Decimal
    ) -> Dict[str, Any]:
        """
        生成风险报告（综合第五章所有指标）
        
        Args:
            positions: 持仓列表
            account_equity: 账户权益
            total_capital: 总资金
        
        Returns:
            风险报告字典
        """
        # 计算总占用保证金
        total_margin = sum(
            Decimal(pos.get('margin', '0')) for pos in positions
        )
        
        # 计算总名义价值
        total_notional = sum(
            Decimal(pos.get('notional_value', '0')) for pos in positions
        )
        
        # 计算保证金率
        margin_ratio, risk_level, need_intervention = self.check_margin_ratio(
            account_equity, total_margin
        )
        
        # 计算保证金使用率
        margin_usage, usage_exceeded = self.check_margin_usage(total_capital, total_margin)
        
        # 计算总杠杆
        total_leverage = total_notional / total_capital if total_capital > 0 else Decimal('0')
        
        # 计算最大允许名义价值
        max_total_notional = self.params.get('position_sizing.max_total_notional', Decimal('4000'))
        
        report = {
            'account_equity': account_equity,
            'total_capital': total_capital,
            'total_margin': total_margin,
            'margin_usage': margin_usage,
            'usage_exceeded': usage_exceeded,
            'margin_ratio': margin_ratio,
            'risk_level': risk_level,
            'need_intervention': need_intervention,
            'total_notional': total_notional,
            'total_leverage': total_leverage,
            'max_allowed_notional': max_total_notional,
            'positions_count': len(positions),
        }
        
        logger.info("风险报告生成完成:")
        logger.info(f"  账户权益：{account_equity:.2f}U")
        logger.info(f"  总保证金：{total_margin:.2f}U")
        logger.info(f"  保证金使用率：{margin_usage:.0%}")
        logger.info(f"  保证金率：{margin_ratio:.2f} ({risk_level})")
        logger.info(f"  总杠杆：{total_leverage:.2f}x")
        logger.info(f"  持仓数：{len(positions)}")
        
        return report
    
    def get_emergency_actions(self, risk_level: str) -> List[str]:
        """
        获取应急处理措施（第五章强平预防）
        
        Args:
            risk_level: 风险等级（'EMERGENCY', 'WARNING', 'SAFE'）
        
        Returns:
            应急措施列表
        """
        if risk_level == 'EMERGENCY':
            return [
                "🚨 立即全部平仓",
                "取消所有未成交挂单",
                "检查账户状态",
                "发送紧急通知"
            ]
        elif risk_level == 'WARNING':
            return [
                "⚠️ 减仓 50%（优先平仓亏损最大的仓位）",
                "准备追加保证金",
                "密切关注市场走势",
                "发送预警通知"
            ]
        else:
            return [
                "✅ 继续正常监控",
                "无需特别干预"
            ]


# 全局实例
_global_risk_manager: Optional[RiskManager] = None


def get_risk_manager(params: StrategyParams = None) -> RiskManager:
    """获取风险管理器实例（单例模式）"""
    global _global_risk_manager
    if _global_risk_manager is None:
        _global_risk_manager = RiskManager(params)
    return _global_risk_manager


# 便捷函数
def calculate_stop_loss(
    entry_price: Decimal,
    direction: int,
    stop_loss_pct: Decimal
) -> Decimal:
    """计算止损价的便捷函数"""
    return get_risk_manager().calculate_stop_loss(entry_price, direction, stop_loss_pct)


def calculate_take_profit_levels(
    entry_price: Decimal,
    direction: int,
    r_value: Decimal
) -> List[Dict[str, Any]]:
    """计算止盈水平的便捷函数"""
    return get_risk_manager().calculate_take_profit_levels(entry_price, direction, r_value)


def check_margin_ratio(
    account_equity: Decimal,
    used_margin: Decimal
) -> Tuple[Decimal, str, bool]:
    """检查保证金率的便捷函数"""
    return get_risk_manager().check_margin_ratio(account_equity, used_margin)
