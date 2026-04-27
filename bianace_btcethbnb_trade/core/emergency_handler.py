#!/usr/bin/env python3
"""
应急处理模块

基于 traderule.txt 第七章实现特殊情况应急处理：
1. 极端行情处理（暴涨暴跌）
2. 连续亏损处理
3. 系统异常处理
4. 应急通知机制

核心规则（第七章）：
- 单日亏损 > 30U → 停止交易 24 小时
- 连续亏损 3 笔 → 停止交易 48 小时
- 总资金回撤 > 10% → 停止交易并复盘
- 极端行情（±5%） → 暂停开仓
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional
from config.strategy_params import StrategyParams, get_params

logger = logging.getLogger(__name__)


class EmergencyHandler:
    """应急处理类"""
    
    def __init__(self, params: StrategyParams = None):
        """
        初始化应急处理器
        
        Args:
            params: 策略参数
        """
        self.params = params or get_params()
        
        # 交易记录缓存（用于统计）
        self.recent_trades: List[Dict[str, Any]] = []
        
        # 停止交易状态
        self.trading_halt_until: Optional[datetime] = None
        self.trading_halt_reason: str = ""
        self.trading_halt_indefinite: bool = False  # 是否无限期停止交易
    
    def check_extreme_market(
        self,
        symbol: str,
        price_change_percent: Decimal
    ) -> bool:
        """
        检查极端行情（第七章极端行情处理）
        
        规则：24 小时涨跌幅超过 ±5% → 暂停开仓
        
        Args:
            symbol: 交易对
            price_change_percent: 24 小时涨跌幅（百分比，如 5.5 表示 +5.5%）
        
        Returns:
            是否为极端行情（True=不能开仓）
        """
        extreme_threshold = self.params.get('emergency.extreme_market_threshold', Decimal('5.0'))
        
        if abs(price_change_percent) > extreme_threshold:
            logger.warning(f"🚨 极端行情：{symbol} 24h 涨跌幅 {price_change_percent:+.2f}% > {extreme_threshold}%")
            logger.info(f"  建议：暂停开仓，等待市场稳定")
            return True
        else:
            logger.info(f"✅ {symbol} 市场正常：24h 涨跌幅 {price_change_percent:+.2f}%")
            return False
    
    def check_daily_loss(
        self,
        daily_pnl: Decimal
    ) -> bool:
        """
        检查单日亏损（第七章连续亏损）
        
        规则：单日亏损 > 30U → 停止交易 24 小时
        
        Args:
            daily_pnl: 当日盈亏（负值表示亏损）
        
        Returns:
            是否需要停止交易
        """
        max_daily_loss = self.params.get('emergency.max_daily_loss', Decimal('30'))
        
        if daily_pnl < -max_daily_loss:
            logger.error(f"🚨 单日亏损超限：{daily_pnl:.2f}U < -{max_daily_loss}U")
            logger.info(f"  措施：停止交易 24 小时")
            
            # 设置停止交易状态
            self.trading_halt_until = datetime.now() + timedelta(hours=24)
            self.trading_halt_reason = f"单日亏损超限 ({daily_pnl:.2f}U)"
            
            return True
        else:
            logger.info(f"✅ 单日亏损在可控范围内：{daily_pnl:.2f}U")
            return False
    
    def check_consecutive_losses(
        self,
        trades: List[Dict[str, Any]]
    ) -> bool:
        """
        检查连续亏损（第七章连续亏损）
        
        规则：连续亏损 3 笔 → 停止交易 48 小时
        
        Args:
            trades: 交易记录列表（按时间倒序）
        
        Returns:
            是否需要停止交易
        """
        max_consecutive_losses = self.params.get('emergency.max_consecutive_losses', 3)
        
        # 统计连续亏损次数
        consecutive_losses = 0
        for trade in trades:
            if trade.get('pnl', Decimal('0')) < 0:
                consecutive_losses += 1
                if consecutive_losses >= max_consecutive_losses:
                    logger.error(f"🚨 连续亏损超限：{consecutive_losses} 笔")
                    logger.info(f"  措施：停止交易 48 小时")
                    
                    # 设置停止交易状态
                    self.trading_halt_until = datetime.now() + timedelta(hours=48)
                    self.trading_halt_reason = f"连续亏损{consecutive_losses}笔"
                    
                    return True
            else:
                # 遇到盈利，重置计数
                consecutive_losses = 0
        
        logger.info(f"✅ 连续亏损次数：{consecutive_losses}/{max_consecutive_losses}")
        return False
    
    def check_total_drawdown(
        self,
        total_capital: Decimal,
        current_equity: Decimal
    ) -> bool:
        """
        检查总资金回撤（第七章风险控制）
        
        规则：总资金回撤 > 10% → 停止交易并复盘
        
        Args:
            total_capital: 初始总资金
            current_equity: 当前账户权益
        
        Returns:
            是否需要停止交易
        """
        max_drawdown_ratio = self.params.get('emergency.max_total_drawdown', Decimal('0.1'))
        
        drawdown = total_capital - current_equity
        drawdown_ratio = drawdown / total_capital if total_capital > 0 else Decimal('0')
        
        if drawdown_ratio > max_drawdown_ratio:
            logger.error(f"🚨 总资金回撤超限：{drawdown_ratio:.1%} > {max_drawdown_ratio:.0%}")
            logger.info(f"  措施：停止交易并复盘")
            
            # 设置停止交易状态（无限期，直到手动恢复）
            self.trading_halt_until = None  # None 表示无限期停止
            self.trading_halt_reason = f"总资金回撤{drawdown_ratio:.1%}"
            self.trading_halt_indefinite = True  # 标记为无限期停止
            
            return True
        else:
            logger.info(f"✅ 资金回撤在可控范围内：{drawdown_ratio:.1%}")
            return False
    
    def is_trading_allowed(self) -> tuple[bool, str]:
        """
        检查是否允许交易
        
        Returns:
            (是否允许交易，原因)
        """
        # 检查是否无限期停止交易
        if self.trading_halt_indefinite:
            return False, f"停止交易中：{self.trading_halt_reason}（需手动恢复）"
        
        # 检查是否有停止交易状态
        if self.trading_halt_until is None:
            # 没有停止交易，允许交易
            return True, "允许交易"
        
        if self.trading_halt_until > datetime.now():
            remaining_hours = (self.trading_halt_until - datetime.now()).seconds // 3600
            return False, f"停止交易中：{self.trading_halt_reason}，剩余{remaining_hours}小时"
        
        # 停止交易时间已过，恢复交易
        logger.info(f"✅ 停止交易时间结束，恢复交易")
        self.trading_halt_until = None
        self.trading_halt_reason = ""
        
        return True, "允许交易"
    
    def reset_trading_halt(self):
        """
        手动恢复交易（用于管理员干预）
        """
        logger.info(f"手动恢复交易：{self.trading_halt_reason}")
        self.trading_halt_until = None
        self.trading_halt_reason = ""
        self.trading_halt_indefinite = False
    
    def get_emergency_status(self) -> Dict[str, Any]:
        """
        获取应急状态报告
        
        Returns:
            应急状态字典
        """
        status = {
            'trading_allowed': True,
            'halt_reason': '',
            'halt_until': None,
            'recent_trades_count': len(self.recent_trades),
            'consecutive_losses': 0,
            'daily_pnl': Decimal('0'),
            'alerts': []
        }
        
        # 检查交易是否允许
        allowed, reason = self.is_trading_allowed()
        status['trading_allowed'] = allowed
        status['halt_reason'] = reason
        status['halt_until'] = self.trading_halt_until.isoformat() if self.trading_halt_until else None
        
        # 统计近期交易
        if self.recent_trades:
            # 计算连续亏损
            consecutive_losses = 0
            for trade in reversed(self.recent_trades):
                if trade.get('pnl', Decimal('0')) < 0:
                    consecutive_losses += 1
                else:
                    break
            status['consecutive_losses'] = consecutive_losses
            
            # 计算当日盈亏
            today = datetime.now().date()
            daily_pnl = sum(
                trade.get('pnl', Decimal('0')) 
                for trade in self.recent_trades 
                if datetime.fromisoformat(trade.get('close_time', '2000-01-01')).date() == today
            )
            status['daily_pnl'] = daily_pnl
            
            # 生成警报
            if consecutive_losses >= 2:
                status['alerts'].append(f"⚠️ 连续亏损{consecutive_losses}笔，注意风险")
            if daily_pnl < Decimal('-20'):
                status['alerts'].append(f"⚠️ 当日亏损{daily_pnl:.2f}U，接近上限")
        
        return status
    
    def add_trade_record(self, trade: Dict[str, Any]):
        """
        添加交易记录（用于统计）
        
        Args:
            trade: 交易记录
        """
        self.recent_trades.append(trade)
        
        # 保留最近 100 条记录
        if len(self.recent_trades) > 100:
            self.recent_trades = self.recent_trades[-100:]
        
        logger.info(f"交易记录已添加：{trade.get('symbol', 'UNKNOWN')} {trade.get('side', 'UNKNOWN')}")
    
    def handle_emergency_close(
        self,
        symbol: str,
        reason: str,
        current_price: Decimal
    ) -> Dict[str, Any]:
        """
        处理紧急平仓（第七章极端行情）
        
        Args:
            symbol: 交易对
            reason: 平仓原因
            current_price: 当前价格
        
        Returns:
            平仓指令字典
        """
        logger.warning(f"🚨 紧急平仓：{symbol}")
        logger.warning(f"  原因：{reason}")
        logger.warning(f"  当前价格：{current_price}")
        
        emergency_close = {
            'symbol': symbol,
            'action': 'EMERGENCY_CLOSE',
            'reason': reason,
            'current_price': current_price,
            'timestamp': datetime.now().isoformat(),
            'priority': 'HIGH'
        }
        
        return emergency_close
    
    def generate_emergency_report(self) -> Dict[str, Any]:
        """
        生成应急报告（综合第七章所有指标）
        
        Returns:
            应急报告字典
        """
        status = self.get_emergency_status()
        
        report = {
            'timestamp': datetime.now().isoformat(),
            'trading_status': 'ALLOWED' if status['trading_allowed'] else 'HALTED',
            'halt_reason': status['halt_reason'],
            'recent_performance': {
                'trades_count': status['recent_trades_count'],
                'consecutive_losses': status['consecutive_losses'],
                'daily_pnl': status['daily_pnl']
            },
            'alerts': status['alerts'],
            'recommendations': self._generate_recommendations(status)
        }
        
        logger.info("应急报告生成完成")
        return report
    
    def _generate_recommendations(self, status: Dict[str, Any]) -> List[str]:
        """
        生成建议措施
        
        Args:
            status: 当前状态
        
        Returns:
            建议列表
        """
        recommendations = []
        
        if not status['trading_allowed']:
            recommendations.append("🚫 停止交易，等待恢复")
        
        if status['consecutive_losses'] >= 2:
            recommendations.append("⚠️ 连续亏损，建议降低仓位或暂停交易")
        
        if status['daily_pnl'] < Decimal('-20'):
            recommendations.append("⚠️ 当日亏损较大，建议休息调整")
        
        if not recommendations:
            recommendations.append("✅ 系统运行正常，继续按策略交易")
        
        return recommendations


# 全局实例
_global_emergency_handler: Optional[EmergencyHandler] = None


def get_emergency_handler(params: StrategyParams = None) -> EmergencyHandler:
    """获取应急处理器实例（单例模式）"""
    global _global_emergency_handler
    if _global_emergency_handler is None:
        _global_emergency_handler = EmergencyHandler(params)
    return _global_emergency_handler


# 便捷函数
def check_extreme_market(
    symbol: str,
    price_change_percent: Decimal
) -> bool:
    """检查极端行情的便捷函数"""
    return get_emergency_handler().check_extreme_market(symbol, price_change_percent)


def check_daily_loss(daily_pnl: Decimal) -> bool:
    """检查单日亏损的便捷函数"""
    return get_emergency_handler().check_daily_loss(daily_pnl)


def check_consecutive_losses(trades: List[Dict[str, Any]]) -> bool:
    """检查连续亏损的便捷函数"""
    return get_emergency_handler().check_consecutive_losses(trades)


def is_trading_allowed() -> tuple[bool, str]:
    """检查是否允许交易的便捷函数"""
    return get_emergency_handler().is_trading_allowed()
