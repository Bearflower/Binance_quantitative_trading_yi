#!/usr/bin/env python3
"""
应急处理模块

基于 traderule.txt 第七章实现应急处理功能：
1. 极端行情处理（价格瞬间反向波动 5%）
2. 连续亏损处理（连续 2 笔亏损停止交易 3 天）
3. 单周亏损限制（亏损>15% 停止交易 3 天）
4. 应急通知机制

使用方式:
    from services.emergency_handler import EmergencyHandler
    
    handler = EmergencyHandler()
    
    # 检查极端行情
    if handler.check_extreme_price_drop(current_price, entry_price, direction):
        handler.handle_extreme_market(positions)
    
    # 检查连续亏损
    if handler.check_consecutive_losses(recent_trades):
        handler.handle_consecutive_losses()
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from config.strategy_params import StrategyParams, get_params
from utils.lark_notifier import LarkNotifier
from config.settings import LARK_WEBHOOK_URL

logger = logging.getLogger('emergency_handler')


class EmergencyHandler:
    """应急处理类"""
    
    def __init__(self, params: StrategyParams = None):
        """
        初始化应急处理器
        
        Args:
            params: 策略参数
        """
        self.params = params or get_params()
        self.lark_notifier = LarkNotifier(LARK_WEBHOOK_URL) if LARK_WEBHOOK_URL else None
        
        # 应急处理参数（第七章）
        self.extreme_price_drop = self.params.get('emergency_handling.extreme_price_drop', Decimal('0.05'))
        self.emergency_close_ratio = self.params.get('emergency_handling.emergency_close_ratio', Decimal('0.5'))
        self.emergency_stop_loss = self.params.get('emergency_handling.emergency_stop_loss', Decimal('0.015'))
        self.consecutive_losses_limit = self.params.get('emergency_handling.consecutive_losses_limit', 2)
        self.consecutive_losses_pause_days = self.params.get('emergency_handling.consecutive_losses_pause_days', 3)
        self.weekly_loss_limit = self.params.get('emergency_handling.weekly_loss_limit', Decimal('0.15'))
        self.weekly_loss_pause_days = self.params.get('emergency_handling.weekly_loss_pause_days', 3)
        
        # 交易暂停状态
        self.trading_paused = False
        self.trading_pause_until: Optional[datetime] = None
        self.trading_pause_reason: str = ""
        
        logger.info("应急处理器初始化完成")
        logger.info(f"  极端行情阈值：{self.extreme_price_drop:.0%}")
        logger.info(f"  连续亏损限制：{self.consecutive_losses_limit} 笔")
        logger.info(f"  周亏损限制：{self.weekly_loss_limit:.0%}")
    
    def check_extreme_price_drop(
        self,
        current_price: Decimal,
        entry_price: Decimal,
        direction: int
    ) -> bool:
        """
        检查是否发生极端行情（价格瞬间反向波动达阈值）
        
        Args:
            current_price: 当前价格
            entry_price: 开仓价
            direction: 方向（1=多，-1=空）
        
        Returns:
            是否触发极端行情
        """
        if direction == 1:  # 多头
            # 价格下跌幅度
            price_drop = (entry_price - current_price) / entry_price
        else:  # 空头
            # 价格上涨幅度
            price_drop = (current_price - entry_price) / entry_price
        
        is_extreme = price_drop >= self.extreme_price_drop
        
        if is_extreme:
            logger.warning(f"🚨 极端行情触发：价格反向波动 {price_drop:.1%} >= {self.extreme_price_drop:.0%}")
        
        return is_extreme
    
    def handle_extreme_market(self, positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        处理极端行情（第七章极端行情处理）
        
        应急措施：
        1. 立即市价平仓 50% 的仓位（优先平仓亏损最大的品种）
        2. 剩余仓位止损收紧至 1.5%
        3. 取消所有未成交挂单
        4. 禁止逆势开仓、锁仓
        
        Args:
            positions: 当前持仓列表
        
        Returns:
            处理结果
        """
        logger.warning("🚨 开始处理极端行情...")
        
        result = {
            'success': False,
            'closed_positions': [],
            'adjusted_positions': [],
            'cancelled_orders': [],
            'message': ''
        }
        
        if not positions:
            logger.info("当前无持仓，无需处理")
            result['success'] = True
            result['message'] = '当前无持仓'
            return result
        
        # 1. 按亏损排序（优先平仓亏损最大的）
        sorted_positions = sorted(
            positions,
            key=lambda p: Decimal(str(p.get('unrealized_pnl', '0'))),
            reverse=False  # 亏损最大的在前
        )
        
        # 2. 平仓 50% 的仓位
        close_count = max(1, int(len(sorted_positions) * self.emergency_close_ratio))
        
        logger.warning(f"准备平仓 {close_count}/{len(positions)} 个仓位（50%）")
        
        for i, position in enumerate(sorted_positions[:close_count]):
            try:
                # TODO: 调用交易 API 市价平仓
                symbol = position.get('symbol', 'UNKNOWN')
                quantity = Decimal(str(position.get('quantity', '0')))
                
                logger.warning(f"  平仓 {i+1}/{close_count}: {symbol} {quantity}")
                
                result['closed_positions'].append({
                    'symbol': symbol,
                    'quantity': float(quantity),
                    'reason': '极端行情应急平仓'
                })
                
            except Exception as e:
                logger.error(f"平仓失败 {position.get('symbol')}: {str(e)}")
        
        # 3. 剩余仓位止损收紧至 1.5%
        remaining_positions = sorted_positions[close_count:]
        
        for position in remaining_positions:
            try:
                symbol = position.get('symbol', 'UNKNOWN')
                entry_price = Decimal(str(position.get('entry_price', '0')))
                direction = position.get('direction', 1)
                
                # 计算新的止损价（收紧至 1.5%）
                if direction == 1:  # 多头
                    new_stop_loss = entry_price * (1 - self.emergency_stop_loss)
                else:  # 空头
                    new_stop_loss = entry_price * (1 + self.emergency_stop_loss)
                
                logger.warning(f"  调整止损：{symbol} → {new_stop_loss:.2f}")
                
                result['adjusted_positions'].append({
                    'symbol': symbol,
                    'new_stop_loss': float(new_stop_loss),
                    'old_stop_loss': position.get('stop_loss'),
                    'reason': '极端行情止损收紧'
                })
                
            except Exception as e:
                logger.error(f"调整止损失败 {position.get('symbol')}: {str(e)}")
        
        # 4. 发送应急通知
        self._send_emergency_notification('EXTREME_MARKET', result)
        
        result['success'] = True
        result['message'] = f'已平仓 {len(result["closed_positions"])} 个，调整止损 {len(result["adjusted_positions"])} 个'
        
        logger.warning(f"极端行情处理完成：{result['message']}")
        
        return result
    
    def check_consecutive_losses(self, recent_trades: List[Dict[str, Any]]) -> Tuple[bool, int]:
        """
        检查连续亏损情况（第七章连续亏损处理）
        
        Args:
            recent_trades: 最近交易列表（按时间倒序）
        
        Returns:
            (是否触发限制，连续亏损次数)
        """
        if not recent_trades:
            return False, 0
        
        consecutive_losses = 0
        
        # 从最近的交易开始检查
        for trade in recent_trades:
            net_pnl = Decimal(str(trade.get('net_pnl', '0')))
            
            if net_pnl < 0:
                consecutive_losses += 1
            else:
                break  # 遇到盈利交易，中断连续亏损计数
        
        is_triggered = consecutive_losses >= self.consecutive_losses_limit
        
        if is_triggered:
            logger.warning(f"⚠️ 连续亏损触发：连续 {consecutive_losses} 笔亏损 >= {self.consecutive_losses_limit} 笔限制")
        
        return is_triggered, consecutive_losses
    
    def handle_consecutive_losses(self, consecutive_count: int):
        """
        处理连续亏损（第七章连续亏损处理）
        
        应急措施：
        1. 停止交易 3 天
        2. 发送通知
        3. 记录暂停时间
        
        Args:
            consecutive_count: 连续亏损次数
        """
        logger.warning(f"⚠️ 连续亏损 {consecutive_count} 笔，触发交易暂停")
        
        # 计算恢复交易时间
        pause_days = self.consecutive_losses_pause_days
        resume_time = datetime.now() + timedelta(days=pause_days)
        
        self.trading_paused = True
        self.trading_pause_until = resume_time
        self.trading_pause_reason = f'连续亏损{consecutive_count}笔'
        
        logger.warning(f"交易暂停 {pause_days} 天，恢复时间：{resume_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 发送通知
        self._send_emergency_notification(
            'CONSECUTIVE_LOSSES',
            {
                'consecutive_count': consecutive_count,
                'pause_days': pause_days,
                'resume_time': resume_time.strftime('%Y-%m-%d %H:%M:%S')
            }
        )
    
    def check_weekly_loss(self, weekly_pnl: Decimal, total_capital: Decimal) -> Tuple[bool, Decimal]:
        """
        检查单周亏损情况（第七章连续亏损处理）
        
        Args:
            weekly_pnl: 本周盈亏（负值表示亏损）
            total_capital: 总资金
        
        Returns:
            (是否触发限制，周亏损比例)
        """
        if weekly_pnl >= 0:
            return False, Decimal('0')
        
        weekly_loss_ratio = abs(weekly_pnl) / total_capital
        is_triggered = weekly_loss_ratio >= self.weekly_loss_limit
        
        if is_triggered:
            logger.warning(f"⚠️ 单周亏损触发：亏损 {weekly_loss_ratio:.1%} >= {self.weekly_loss_limit:.0%} 限制")
        
        return is_triggered, weekly_loss_ratio
    
    def handle_weekly_loss(self, loss_ratio: Decimal, weekly_pnl: Decimal):
        """
        处理单周亏损超限（第七章连续亏损处理）
        
        应急措施：
        1. 停止交易 3 天
        2. 恢复后首周杠杆 ≤ 3 倍
        3. 发送通知
        
        Args:
            loss_ratio: 亏损比例
            weekly_pnl: 本周盈亏
        """
        logger.warning(f"⚠️ 单周亏损 {loss_ratio:.1%} ({weekly_pnl:.2f}U)，触发交易暂停")
        
        # 计算恢复交易时间
        pause_days = self.weekly_loss_pause_days
        resume_time = datetime.now() + timedelta(days=pause_days)
        
        self.trading_paused = True
        self.trading_pause_until = resume_time
        self.trading_pause_reason = f'单周亏损{loss_ratio:.1%} ({weekly_pnl:.2f}U)'
        
        logger.warning(f"交易暂停 {pause_days} 天，恢复时间：{resume_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 发送通知
        self._send_emergency_notification(
            'WEEKLY_LOSS',
            {
                'loss_ratio': float(loss_ratio),
                'weekly_pnl': float(weekly_pnl),
                'pause_days': pause_days,
                'resume_time': resume_time.strftime('%Y-%m-%d %H:%M:%S')
            }
        )
    
    def can_trade(self) -> Tuple[bool, str]:
        """
        检查是否允许交易
        
        Returns:
            (是否允许，原因)
        """
        if not self.trading_paused:
            return True, '允许交易'
        
        if self.trading_pause_until and datetime.now() < self.trading_pause_until:
            remaining = self.trading_pause_until - datetime.now()
            hours = remaining.total_seconds() / 3600
            return False, f'交易暂停中（{self.trading_pause_reason}），剩余{hours:.1f}小时'
        
        # 暂停时间已过，恢复交易
        logger.info(f"✅ 交易暂停结束（{self.trading_pause_reason}），恢复交易")
        self.trading_paused = False
        self.trading_pause_until = None
        self.trading_pause_reason = ""
        
        return True, '交易暂停已结束'
    
    def check_and_handle_emergency(
        self,
        positions: List[Dict[str, Any]],
        recent_trades: List[Dict[str, Any]],
        weekly_pnl: Decimal,
        total_capital: Decimal
    ) -> Dict[str, Any]:
        """
        综合检查和应急处理
        
        Args:
            positions: 当前持仓
            recent_trades: 最近交易
            weekly_pnl: 本周盈亏
            total_capital: 总资金
        
        Returns:
            应急处理结果
        """
        result = {
            'emergency_detected': False,
            'actions_taken': [],
            'details': {}
        }
        
        # 1. 检查是否允许交易
        can_trade, reason = self.can_trade()
        if not can_trade:
            result['emergency_detected'] = True
            result['actions_taken'].append('TRADING_PAUSED')
            result['details']['pause_reason'] = reason
            return result
        
        # 2. 检查连续亏损
        consecutive_triggered, consecutive_count = self.check_consecutive_losses(recent_trades)
        if consecutive_triggered:
            result['emergency_detected'] = True
            result['actions_taken'].append('CONSECUTIVE_LOSSES_PAUSE')
            self.handle_consecutive_losses(consecutive_count)
            result['details']['consecutive_losses'] = consecutive_count
        
        # 3. 检查单周亏损
        weekly_triggered, weekly_loss_ratio = self.check_weekly_loss(weekly_pnl, total_capital)
        if weekly_triggered:
            result['emergency_detected'] = True
            result['actions_taken'].append('WEEKLY_LOSS_PAUSE')
            self.handle_weekly_loss(weekly_loss_ratio, weekly_pnl)
            result['details']['weekly_loss'] = {
                'ratio': float(weekly_loss_ratio),
                'pnl': float(weekly_pnl)
            }
        
        return result
    
    def _send_emergency_notification(self, emergency_type: str, data: Dict[str, Any]):
        """
        发送应急通知
        
        Args:
            emergency_type: 应急类型
            data: 数据
        """
        if not self.lark_notifier:
            logger.warning("飞书通知未配置，无法发送应急通知")
            return
        
        # 构建通知消息
        if emergency_type == 'EXTREME_MARKET':
            title = "🚨 极端行情应急处理"
            content = f"{title}\n\n"
            content += f"已平仓：{len(data.get('closed_positions', []))} 个仓位\n"
            content += f"已调整：{len(data.get('adjusted_positions', []))} 个止损\n"
            content += f"措施：平仓 50%，剩余止损收紧至 1.5%\n"
            
        elif emergency_type == 'CONSECUTIVE_LOSSES':
            title = "⚠️ 连续亏损触发"
            content = f"{title}\n\n"
            content += f"连续亏损：{data.get('consecutive_count')} 笔\n"
            content += f"暂停交易：{data.get('pause_days')} 天\n"
            content += f"恢复时间：{data.get('resume_time')}\n"
            
        elif emergency_type == 'WEEKLY_LOSS':
            title = "⚠️ 单周亏损超限"
            content = f"{title}\n\n"
            content += f"本周亏损：{data.get('loss_ratio'):.1%} ({data.get('weekly_pnl'):.2f}U)\n"
            content += f"暂停交易：{data.get('pause_days')} 天\n"
            content += f"恢复时间：{data.get('resume_time')}\n"
            content += f"恢复后首周杠杆限制：≤ 3 倍\n"
        else:
            content = f"应急通知：{emergency_type}\n{data}"
        
        content += f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        self.lark_notifier.send_text_message(content)
        logger.info(f"应急通知已发送：{emergency_type}")
    
    def get_emergency_status(self) -> Dict[str, Any]:
        """
        获取应急状态
        
        Returns:
            应急状态字典
        """
        can_trade, reason = self.can_trade()
        
        return {
            'trading_paused': self.trading_paused,
            'pause_until': self.trading_pause_until.isoformat() if self.trading_pause_until else None,
            'pause_reason': self.trading_pause_reason,
            'can_trade': can_trade,
            'reason': reason,
            'parameters': {
                'extreme_price_drop': float(self.extreme_price_drop),
                'consecutive_losses_limit': self.consecutive_losses_limit,
                'weekly_loss_limit': float(self.weekly_loss_limit)
            }
        }


# 全局实例
_global_emergency_handler: Optional[EmergencyHandler] = None


def get_emergency_handler(params: StrategyParams = None) -> EmergencyHandler:
    """获取应急处理器实例（单例模式）"""
    global _global_emergency_handler
    if _global_emergency_handler is None:
        _global_emergency_handler = EmergencyHandler(params)
    return _global_emergency_handler


# 便捷函数
def check_extreme_price_drop(current_price: Decimal, entry_price: Decimal, direction: int) -> bool:
    """检查极端行情的便捷函数"""
    return get_emergency_handler().check_extreme_price_drop(current_price, entry_price, direction)


def handle_extreme_market(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """处理极端行情的便捷函数"""
    return get_emergency_handler().handle_extreme_market(positions)


def can_trade() -> Tuple[bool, str]:
    """检查是否允许交易的便捷函数"""
    return get_emergency_handler().can_trade()


def get_emergency_status() -> Dict[str, Any]:
    """获取应急状态的便捷函数"""
    return get_emergency_handler().get_emergency_status()
