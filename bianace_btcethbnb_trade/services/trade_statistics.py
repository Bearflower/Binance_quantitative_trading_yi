#!/usr/bin/env python3
"""
交易统计模块
负责交易盈亏统计、胜率计算、绩效分析
"""

import logging
from decimal import Decimal
from datetime import datetime, date
from typing import Dict, Any, Optional
from models.database import DatabaseManager

logger = logging.getLogger(__name__)


class TradeStatisticsManager:
    """交易统计管理器"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
    
    def record_close_and_update_stats(self, trade_data: Dict[str, Any]):
        """
        记录平仓并更新统计
        
        Args:
            trade_data: 平仓数据
                - symbol: 交易对
                - direction: 方向 (BUY/SELL)
                - entry_price: 开仓价
                - close_price: 平仓价
                - quantity: 数量
                - close_time: 平仓时间
                - close_reason: 平仓原因 (止盈/止损/限期/强平)
        """
        # 1. 计算盈亏
        pnl = self._calculate_pnl(
            direction=trade_data['direction'],
            entry_price=trade_data['entry_price'],
            close_price=trade_data['close_price'],
            quantity=trade_data['quantity']
        )
        
        # 2. 判断盈亏
        is_win = pnl > 0
        
        # 3. 更新 trade_records 状态
        self._update_trade_record(trade_data, pnl)
        
        # 4. 更新 daily_execution_stats
        self._update_daily_stats(trade_data['close_time'].date(), is_win)
        
        # 5. 记录日志
        logger.info(f"📊 交易统计：{trade_data['symbol']} "
                   f"{'✅盈利' if is_win else '❌亏损'} {abs(pnl):.2f} USDT "
                   f"({trade_data['close_reason']})")
    
    def _calculate_pnl(self, direction: str, entry_price: Decimal,
                       close_price: Decimal, quantity: Decimal) -> Decimal:
        """计算盈亏"""
        if direction == 'BUY':
            # 做多：平仓价 - 开仓价
            pnl = (close_price - entry_price) * quantity
        else:
            # 做空：开仓价 - 平仓价
            pnl = (entry_price - close_price) * quantity
        
        return pnl
    
    def _update_trade_record(self, trade_data: Dict[str, Any], pnl: Decimal):
        """更新交易记录"""
        query = """
            UPDATE trade_records
            SET status = 'CLOSED',
                close_time = %s,
                pnl = %s,
                pnl_percent = %s,
                close_price = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE symbol = %s AND status = 'OPEN'
        """
        
        # 计算盈亏比例
        pnl_percent = (pnl / (trade_data['entry_price'] * trade_data['quantity'])) * 100
        
        self.db._execute_query(query, (
            trade_data['close_time'],
            pnl,
            pnl_percent,
            trade_data['close_price'],
            trade_data['symbol']
        ))
    
    def _update_daily_stats(self, trade_date: date, is_win: bool):
        """更新每日统计"""
        if is_win:
            query = """
                UPDATE daily_execution_stats
                SET win_count = win_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE stat_date = %s
            """
        else:
            query = """
                UPDATE daily_execution_stats
                SET loss_count = loss_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE stat_date = %s
            """
        
        self.db._execute_query(query, (trade_date,))
    
    def get_statistics(self, start_date: date, end_date: date) -> Dict[str, Any]:
        """获取统计数据"""
        query = """
            SELECT 
                SUM(executed_count) as total_trades,
                SUM(win_count) as total_wins,
                SUM(loss_count) as total_losses,
                AVG(win_count::float / NULLIF(executed_count, 0)) as avg_win_rate
            FROM daily_execution_stats
            WHERE stat_date BETWEEN %s AND %s
        """
        
        result = self.db._execute_one(query, (start_date, end_date))
        
        return {
            'total_trades': result['total_trades'] or 0,
            'total_wins': result['total_wins'] or 0,
            'total_losses': result['total_losses'] or 0,
            'win_rate': (result['avg_win_rate'] or 0) * 100
        }
    
    def update_statistics(self):
        """
        更新交易统计（供 position_monitor.py 调用）
        检测平仓订单并更新统计
        """
        logger.info("开始更新交易统计...")
        
        # 使用 close_detector 检测平仓
        from services.close_detector import get_close_detector
        close_detector = get_close_detector()
        closed_positions = close_detector.detect_closed_positions()
        
        # 逐个更新统计
        for close_data in closed_positions:
            try:
                # 计算盈亏
                pnl = close_data.get('net_pnl', Decimal('0'))
                symbol = close_data.get('symbol', 'UNKNOWN')
                close_time = close_data.get('close_time', datetime.now())
                
                # 判断盈亏
                is_win = pnl > 0
                
                # 更新每日统计
                self._update_daily_stats(close_time.date(), is_win)
                
                result = '盈利' if is_win else '亏损'
                logger.info(f"📊 交易统计已更新：{symbol} {result} {abs(pnl):.2f} USDT")
                
            except Exception as e:
                logger.error(f"更新单笔交易统计失败：{str(e)}")
        
        logger.info(f"交易统计更新完成，共处理 {len(closed_positions)} 笔平仓")


def get_trade_statistics_manager():
    """获取交易统计管理器实例"""
    from models.database import get_db_manager
    db = get_db_manager()
    return TradeStatisticsManager(db)


# 别名，兼容旧代码
get_stats_calculator = get_trade_statistics_manager
