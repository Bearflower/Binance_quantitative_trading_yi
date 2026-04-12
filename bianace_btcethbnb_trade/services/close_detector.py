#!/usr/bin/env python3
"""
平仓检测模块
检测订单是否已完成平仓，并记录平仓信息
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, Optional, List

from utils.binance_trade_api import BinanceTradeAPI, get_trade_api
from models.database import DatabaseManager, get_db_manager

logger = logging.getLogger(__name__)


class PositionCloseDetector:
    """平仓检测器"""
    
    def __init__(self):
        self.api: BinanceTradeAPI = get_trade_api()
        self.db: DatabaseManager = get_db_manager()
    
    def detect_closed_positions(self) -> List[Dict[str, Any]]:
        """
        检测所有已平仓的订单
        
        Returns:
            新检测到的平仓记录列表
        """
        logger.info("开始检测平仓订单...")
        
        # 1. 获取所有已完成的交易记录（FILLED 状态）
        filled_trades = self._get_filled_trades()
        logger.info(f"找到 {len(filled_trades)} 笔已完成交易")
        
        # 2. 获取所有持仓（用于判断是否已平仓）
        current_positions = self._get_current_positions()
        
        # 3. 检测哪些订单已平仓
        closed_positions = []
        
        for trade in filled_trades:
            # 检查该订单是否已记录平仓
            if self._is_already_recorded(trade['order_id']):
                logger.debug(f"订单 {trade['order_id']} 已记录平仓，跳过")
                continue
            
            # 检查该订单对应持仓是否已平
            is_closed = self._check_if_closed(trade, current_positions)
            
            if is_closed:
                logger.info(f"检测到订单 {trade['order_id']} 已平仓")
                close_data = self._prepare_close_data(trade)
                
                if close_data:
                    closed_positions.append(close_data)
                    self.db.save_closed_position(close_data)
                    logger.info(f"✅ 平仓记录已保存：订单 {trade['order_id']}, 盈亏={close_data['net_pnl']} USDT")
        
        logger.info(f"检测到 {len(closed_positions)} 笔新的平仓记录")
        return closed_positions
    
    def _get_filled_trades(self) -> List[Dict[str, Any]]:
        """获取所有已完成的交易记录"""
        try:
            # 从币安 API 获取最近 7 天的历史订单
            from datetime import datetime, timedelta
            
            end_time = int(datetime.now().timestamp() * 1000)
            start_time = int((datetime.now() - timedelta(days=7)).timestamp() * 1000)
            
            # 获取所有交易对的历史订单
            symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']  # 可以根据需要扩展
            all_orders = []
            
            for symbol in symbols:
                try:
                    orders = self.api.get_um_order_history(
                        symbol=symbol,
                        limit=500,
                        start_time=start_time,
                        end_time=end_time
                    )
                    
                    # 过滤已完成的订单（FILLED 状态）
                    filled_orders = [o for o in orders if o['status'] == 'FILLED']
                    all_orders.extend(filled_orders)
                    
                    logger.info(f"{symbol}: 查询到 {len(filled_orders)} 笔已完成订单")
                except Exception as e:
                    logger.warning(f"查询 {symbol} 历史订单失败：{e}")
            
            return all_orders
            
        except Exception as e:
            logger.error(f"获取已完成交易失败：{e}")
            
            # 降级方案：从数据库查询
            with self.db._get_db_connection(self.db.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM trades 
                    WHERE status = 'FILLED' 
                    ORDER BY create_time DESC 
                    LIMIT 500
                """)
                return [dict(row) for row in cursor.fetchall()]
    
    def _get_current_positions(self) -> Dict[str, Dict[str, Any]]:
        """获取当前所有持仓"""
        positions = {}
        
        try:
            # 从币安 API 获取当前持仓
            api_positions = self.api.get_all_positions()
            
            for pos in api_positions:
                key = f"{pos['symbol']}_{pos['positionSide']}"
                positions[key] = {
                    'symbol': pos['symbol'],
                    'position_side': pos['positionSide'],
                    'position_amt': Decimal(pos['positionAmt']),
                    'entry_price': Decimal(pos['entryPrice'])
                }
        except Exception as e:
            logger.error(f"获取当前持仓失败：{e}")
        
        return positions
    
    def _is_already_recorded(self, order_id: int) -> bool:
        """检查订单是否已记录平仓"""
        with self.db._get_db_connection(self.db.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM closed_positions WHERE order_id = ?",
                (order_id,)
            )
            return cursor.fetchone() is not None
    
    def _check_if_closed(self, trade: Dict[str, Any], 
                        current_positions: Dict[str, Dict[str, Any]]) -> bool:
        """
        检查订单对应的持仓是否已平仓
        
        Args:
            trade: 交易记录
            current_positions: 当前持仓
        
        Returns:
            True 表示已平仓，False 表示仍有持仓
        """
        symbol = trade['symbol']
        position_side = trade['position_side']
        
        # 如果是开仓单（reduce_only=False），需要检查持仓是否已平
        if not trade.get('reduce_only', False):
            key = f"{symbol}_{position_side}"
            
            # 如果当前没有该持仓，说明已平仓
            if key not in current_positions:
                return True
            
            # 如果当前持仓为 0，说明已平仓
            if current_positions[key]['position_amt'] == 0:
                return True
        
        # 如果是平仓单（reduce_only=True），直接认为已平仓
        if trade.get('reduce_only', False):
            return True
        
        return False
    
    def _prepare_close_data(self, trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        准备平仓数据
        
        Args:
            trade: 交易记录
        
        Returns:
            平仓数据字典
        """
        try:
            symbol = trade['symbol']
            order_id = trade['order_id']
            
            # 1. 获取开仓信息（如果是平仓单）
            open_trade = None
            if trade.get('reduce_only', False):
                # 查找对应的开仓记录
                open_trade = self._find_opening_trade(trade)
            
            # 2. 获取监控日志中的最大浮盈/浮亏
            max_pnl_data = self._get_max_pnl_from_logs(symbol, order_id)
            
            # 3. 计算盈亏
            quantity = Decimal(trade['executed_qty'])
            open_price = Decimal(trade['avg_price'] or '0')
            close_price = Decimal(trade['avg_price'] or '0')
            
            # 如果是平仓单，使用开仓价
            if open_trade:
                open_price = Decimal(open_trade['avg_price'] or '0')
            
            # 计算毛盈亏
            if trade['side'] == 'SELL':  # 卖出平仓
                if trade.get('position_side') == 'LONG':  # 多单平仓
                    gross_pnl = (close_price - open_price) * quantity
                else:  # 空单平仓
                    gross_pnl = (open_price - close_price) * quantity
            else:  # BUY 平仓（空单平仓）
                gross_pnl = (open_price - close_price) * quantity
            
            # 估算手续费（假设 taker 费率 0.04%）
            commission_rate = Decimal('0.0004')
            commission = close_price * quantity * commission_rate
            
            # 净盈亏
            net_pnl = gross_pnl - commission
            
            # 收益率
            margin = open_price * quantity / Decimal(trade.get('leverage', '20'))
            pnl_rate = (net_pnl / margin * 100) if margin > 0 else Decimal('0')
            
            # 持仓时长
            open_time = int(trade['create_time']) if open_trade else int(trade['create_time'])
            close_time = int(trade['update_time'])
            duration_seconds = (close_time - open_time) // 1000
            
            # 判断平仓原因
            close_reason = self._determine_close_reason(trade, net_pnl)
            
            return {
                'order_id': order_id,
                'symbol': symbol,
                'side': trade['side'],
                'position_side': trade['position_side'],
                'open_price': open_price,
                'close_price': close_price,
                'quantity': quantity,
                'open_time': open_time,
                'close_time': close_time,
                'leverage': int(trade.get('leverage', 20)),
                'gross_pnl': gross_pnl.quantize(Decimal('0.00000001')),
                'commission': commission.quantize(Decimal('0.00000001')),
                'net_pnl': net_pnl.quantize(Decimal('0.00000001')),
                'pnl_rate': pnl_rate.quantize(Decimal('0.0001')),
                'close_reason': close_reason,
                'max_unrealized_profit': max_pnl_data.get('max_profit'),
                'min_unrealized_profit': max_pnl_data.get('min_profit'),
                'duration_seconds': duration_seconds,
                'remark': f"平仓检测自动记录"
            }
        
        except Exception as e:
            logger.error(f"准备平仓数据失败：{e}", exc_info=True)
            return None
    
    def _find_opening_trade(self, close_trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """查找对应的开仓记录"""
        with self.db._get_db_connection(self.db.db_path) as conn:
            cursor = conn.cursor()
            
            # 查找同一交易对、同方向、未平仓的记录
            cursor.execute("""
                SELECT * FROM trades 
                WHERE symbol = ? 
                AND position_side = ?
                AND reduce_only = 0
                AND status = 'FILLED'
                ORDER BY create_time DESC 
                LIMIT 1
            """, (close_trade['symbol'], close_trade['position_side']))
            
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def _get_max_pnl_from_logs(self, symbol: str, order_id: int) -> Dict[str, Optional[Decimal]]:
        """从监控日志中获取最大浮盈和最小浮亏"""
        with self.db._get_db_connection(self.db.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT MAX(unrealized_profit) as max_profit,
                       MIN(unrealized_profit) as min_profit
                FROM monitoring_logs
                WHERE symbol = ?
            """, (symbol,))
            
            row = cursor.fetchone()
            
            return {
                'max_profit': Decimal(row['max_profit']) if row['max_profit'] else None,
                'min_profit': Decimal(row['min_profit']) if row['min_profit'] else None
            }
    
    def _determine_close_reason(self, trade: Dict[str, Any], net_pnl: Decimal) -> str:
        """判断平仓原因"""
        # 如果是止盈止损单
        if trade.get('type') in ['TAKE_PROFIT_MARKET', 'TAKE_PROFIT']:
            return 'TAKE_PROFIT'
        
        if trade.get('type') in ['STOP_LOSS_MARKET', 'STOP_LOSS']:
            return 'STOP_LOSS'
        
        # 如果是市价单或限价单，根据盈亏判断
        if net_pnl > Decimal('0'):
            # 盈利平仓，可能是止盈
            return 'MANUAL_CLOSE'  # 暂时归为手动平仓
        else:
            return 'MANUAL_CLOSE'


# 全局实例
_close_detector: Optional[PositionCloseDetector] = None


def get_close_detector() -> PositionCloseDetector:
    """获取平仓检测器实例"""
    global _close_detector
    if _close_detector is None:
        _close_detector = PositionCloseDetector()
    return _close_detector


if __name__ == '__main__':
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 60)
    print("平仓检测模块测试")
    print("=" * 60)
    
    detector = get_close_detector()
    
    print("\n开始检测平仓订单...")
    closed_positions = detector.detect_closed_positions()
    
    print(f"\n检测到 {len(closed_positions)} 笔新的平仓记录:")
    for close in closed_positions:
        print(f"\n订单 {close['order_id']}:")
        print(f"  交易对：{close['symbol']}")
        print(f"  方向：{close['side']} {close['position_side']}")
        print(f"  开仓价：{close['open_price']}")
        print(f"  平仓价：{close['close_price']}")
        print(f"  数量：{close['quantity']}")
        print(f"  净盈亏：{close['net_pnl']} USDT")
        print(f"  收益率：{close['pnl_rate']}%")
        print(f"  平仓原因：{close['close_reason']}")
        print(f"  持仓时长：{close['duration_seconds']} 秒")
    
    print("\n" + "=" * 60)
    print("测试完成")
