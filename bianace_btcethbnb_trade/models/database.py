#!/usr/bin/env python3
"""
PostgreSQL 数据库模块
提供交易数据持久化功能：交易记录、持仓记录、资金划转、监控日志

使用 PostgreSQL 的原因:
- 支持高并发访问，多容器可共享数据
- 完整的 ACID 事务支持
- 强大的查询能力和索引支持
- 适合金融场景，数据安全性高
- 支持水平扩展和读写分离
"""

import os
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)


class DecimalEncoder:
    """Decimal 类型编码器"""
    @staticmethod
    def encode(value: Decimal) -> str:
        """将 Decimal 编码为字符串"""
        return str(value) if value is not None else None
    
    @staticmethod
    def decode(value: str) -> Optional[Decimal]:
        """将字符串解码为 Decimal"""
        return Decimal(value) if value is not None else None


@contextmanager
def get_db_connection():
    """
    数据库连接上下文管理器
    
    Usage:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(...)
    """
    conn = None
    try:
        conn = get_connection_pool().getconn()
        # 每次获取连接时设置 search_path
        with conn.cursor() as cursor:
            cursor.execute("SET search_path TO schema_bianace, public")
        yield conn
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"数据库操作失败：{str(e)}")
        raise
    finally:
        if conn:
            get_connection_pool().putconn(conn)


# 全局连接池
_connection_pool = None


def get_connection_pool():
    """获取数据库连接池"""
    global _connection_pool
    if _connection_pool is None:
        db_url = os.getenv('DATABASE_URL', 'postgresql://bianace_user:Bianace%402024@postgres-db:5432/trading_platform')
        _connection_pool = pool.SimpleConnectionPool(
            1, 10,
            dsn=db_url,
            cursor_factory=RealDictCursor
        )
        logger.info("数据库连接池初始化完成")
        
        # 设置 search_path 到 schema_bianace
        try:
            with _connection_pool.getconn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SET search_path TO schema_bianace, public")
                    conn.commit()
                _connection_pool.putconn(conn)
            logger.info("数据库 search_path 设置为 schema_bianace, public")
        except Exception as e:
            logger.warning(f"设置 search_path 失败：{str(e)}")
    return _connection_pool


class DatabaseManager:
    """数据库管理类"""
    
    def __init__(self, db_url: str = None):
        """
        初始化数据库管理器
        
        Args:
            db_url: 数据库连接 URL，默认从环境变量读取
        """
        self.db_url = db_url or os.getenv('DATABASE_URL')
        if not self.db_url:
            # 移除 schema 参数，使用默认 schema
            self.db_url = 'postgresql://bianace_user:Bianace%402024@postgres-db:5432/trading_platform'
        
        # 初始化连接池
        global _connection_pool
        _connection_pool = pool.SimpleConnectionPool(
            1, 10,
            dsn=self.db_url,
            cursor_factory=RealDictCursor
        )
        
        logger.info(f"数据库初始化完成：{self.db_url}")
        
        # 设置 search_path 到 schema_bianace
        try:
            with _connection_pool.getconn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SET search_path TO schema_bianace, public")
                    conn.commit()
                _connection_pool.putconn(conn)
            logger.info("数据库 search_path 设置为 schema_bianace, public")
        except Exception as e:
            logger.warning(f"设置 search_path 失败：{str(e)}")
    
    def _execute_query(self, query: str, params: tuple = None):
        """执行查询并返回结果"""
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, params or ())
                if query.strip().upper().startswith('SELECT'):
                    return cursor.fetchall()
                conn.commit()
                return cursor.rowcount
    
    def _execute_one(self, query: str, params: tuple = None):
        """执行查询并返回单行结果"""
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, params or ())
                result = cursor.fetchone()
                if not query.strip().upper().startswith('SELECT'):
                    conn.commit()
                return result
    
    # ==================== 交易记录操作 ====================
    
    def save_trade(self, order_data: Dict[str, Any], 
                  tp_price: Decimal = None, sl_price: Decimal = None,
                  transaction_id: int = None):
        """
        保存交易记录
        
        Args:
            order_data: 订单数据
            tp_price: 止盈价
            sl_price: 止损价
            transaction_id: 关联的资金划转 ID
        """
        query = """
            INSERT INTO trades (
                order_id, symbol, side, position_side, type,
                quantity, price, avg_price, executed_qty, cum_quote,
                status, reduce_only, time_in_force, client_order_id,
                tp_trigger_price, tp_price, sl_trigger_price, sl_price,
                create_time, update_time, transaction_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (order_id) DO UPDATE SET
                status = EXCLUDED.status,
                avg_price = EXCLUDED.avg_price,
                executed_qty = EXCLUDED.executed_qty,
                update_time = EXCLUDED.update_time,
                updated_at = CURRENT_TIMESTAMP
        """
        
        params = (
            order_data.get('orderId'),
            order_data.get('symbol'),
            order_data.get('side'),
            order_data.get('positionSide'),
            order_data.get('type'),
            str(Decimal(order_data.get('origQty', '0'))),
            str(Decimal(order_data.get('price', '0'))) if order_data.get('price') else None,
            str(Decimal(order_data.get('avgPrice', '0'))) if order_data.get('avgPrice') else None,
            str(Decimal(order_data.get('executedQty', '0'))),
            str(Decimal(order_data.get('cumQuote', '0'))) if order_data.get('cumQuote') else None,
            order_data.get('status'),
            order_data.get('reduceOnly', False),
            order_data.get('timeInForce'),
            order_data.get('clientOrderId'),
            str(tp_price) if tp_price else None,
            str(tp_price) if tp_price else None,
            str(sl_price) if sl_price else None,
            str(sl_price) if sl_price else None,
            order_data.get('updateTime', int(datetime.now().timestamp() * 1000)),
            order_data.get('updateTime', int(datetime.now().timestamp() * 1000)),
            transaction_id
        )
        
        self._execute_query(query, params)
        logger.info(f"交易记录已保存：订单 ID={order_data.get('orderId')}")
    
    def update_trade_status(self, order_id: int, status: str, 
                           avg_price: Decimal = None, executed_qty: Decimal = None):
        """更新交易状态"""
        update_fields = ["status = %s", "update_time = %s"]
        params = [status, int(datetime.now().timestamp() * 1000)]
        
        if avg_price:
            update_fields.append("avg_price = %s")
            params.append(str(avg_price))
        
        if executed_qty:
            update_fields.append("executed_qty = %s")
            params.append(str(executed_qty))
        
        params.append(order_id)
        
        query = f"""
            UPDATE trades 
            SET {', '.join(update_fields)}, updated_at = CURRENT_TIMESTAMP
            WHERE order_id = %s
        """
        
        self._execute_query(query, tuple(params))
        logger.info(f"交易状态已更新：订单 ID={order_id}, 状态={status}")
    
    def get_trade_by_order_id(self, order_id: int) -> Optional[Dict[str, Any]]:
        """根据订单 ID 查询交易记录"""
        query = "SELECT * FROM trades WHERE order_id = %s"
        result = self._execute_one(query, (order_id,))
        return dict(result) if result else None
    
    def get_trades_by_symbol(self, symbol: str, limit: int = 50) -> List[Dict[str, Any]]:
        """查询指定交易对的交易记录"""
        query = """
            SELECT * FROM trades 
            WHERE symbol = %s 
            ORDER BY create_time DESC 
            LIMIT %s
        """
        results = self._execute_query(query, (symbol, limit))
        return [dict(row) for row in results]
    
    def get_recent_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        """查询最近的交易记录"""
        query = """
            SELECT * FROM trades 
            ORDER BY create_time DESC 
            LIMIT %s
        """
        results = self._execute_query(query, (limit,))
        return [dict(row) for row in results]
    
    # ==================== 持仓记录操作 ====================
    
    def save_position(self, position_data: Dict[str, Any], leverage: int = 20):
        """保存持仓记录"""
        # 检查是否已存在
        existing = self.get_position(
            position_data.get('symbol'), 
            position_data.get('positionSide')
        )
        
        if existing:
            # 更新现有持仓
            query = """
                UPDATE positions SET
                    position_amt = %s,
                    entry_price = %s,
                    mark_price = %s,
                    unrealized_profit = %s,
                    leverage = %s,
                    liquidation_price = %s,
                    margin_type = %s,
                    is_auto_add_margin = %s,
                    last_update_time = %s,
                    recorded_at = CURRENT_TIMESTAMP
                WHERE symbol = %s AND position_side = %s
            """
            
            params = (
                str(Decimal(position_data.get('positionAmt', '0'))),
                str(Decimal(position_data.get('entryPrice', '0'))),
                str(Decimal(position_data.get('markPrice', '0'))),
                str(Decimal(position_data.get('unRealizedProfit', '0'))),
                leverage,
                str(Decimal(position_data.get('liquidationPrice', '0'))),
                position_data.get('marginType'),
                position_data.get('isAutoAddMargin', False),
                int(datetime.now().timestamp() * 1000),
                position_data.get('symbol'),
                position_data.get('positionSide')
            )
            
            self._execute_query(query, params)
        else:
            # 插入新持仓
            query = """
                INSERT INTO positions (
                    symbol, position_side, position_amt, entry_price,
                    mark_price, unrealized_profit, leverage, liquidation_price,
                    margin_type, is_auto_add_margin, last_update_time
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            params = (
                position_data.get('symbol'),
                position_data.get('positionSide'),
                str(Decimal(position_data.get('positionAmt', '0'))),
                str(Decimal(position_data.get('entryPrice', '0'))),
                str(Decimal(position_data.get('markPrice', '0'))),
                str(Decimal(position_data.get('unRealizedProfit', '0'))),
                leverage,
                str(Decimal(position_data.get('liquidationPrice', '0'))),
                position_data.get('marginType'),
                position_data.get('isAutoAddMargin', False),
                int(datetime.now().timestamp() * 1000)
            )
            
            self._execute_query(query, params)
        
        logger.info(f"持仓记录已保存：{position_data.get('symbol')}")
    
    def get_all_positions(self) -> List[Dict[str, Any]]:
        """获取所有持仓记录"""
        query = "SELECT * FROM positions ORDER BY last_update_time DESC"
        results = self._execute_query(query)
        return [dict(row) for row in results]
    
    def get_position(self, symbol: str, position_side: str = 'BOTH') -> Optional[Dict[str, Any]]:
        """获取指定持仓"""
        query = """
            SELECT * FROM positions 
            WHERE symbol = %s AND position_side = %s
        """
        result = self._execute_one(query, (symbol, position_side))
        return dict(result) if result else None
    
    def delete_position(self, symbol: str, position_side: str):
        """删除持仓记录 (平仓后调用)"""
        query = """
            DELETE FROM positions 
            WHERE symbol = %s AND position_side = %s
        """
        self._execute_query(query, (symbol, position_side))
        logger.info(f"持仓记录已删除：{symbol} {position_side}")
    
    # ==================== 资金划转记录操作 ====================
    
    def save_transfer(self, transfer_data: Dict[str, Any], status: str = 'SUCCESS',
                     remark: str = None, related_order_id: int = None):
        """保存资金划转记录"""
        query = """
            INSERT INTO account_transfers (
                tran_id, asset, amount, type, from_account, to_account,
                status, create_time, remark, related_order_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        params = (
            transfer_data.get('tranId'),
            transfer_data.get('asset'),
            Decimal(transfer_data.get('amount', '0')),
            transfer_data.get('type'),
            transfer_data.get('fromAccount', ''),
            transfer_data.get('toAccount', ''),
            status,
            int(datetime.now().timestamp() * 1000),
            remark,
            related_order_id
        )
        
        self._execute_query(query, params)
        logger.info(f"资金划转记录已保存：tranId={transfer_data.get('tranId')}")
    
    def get_transfers_by_order_id(self, order_id: int) -> List[Dict[str, Any]]:
        """根据关联订单 ID 查询划转记录"""
        query = """
            SELECT * FROM account_transfers 
            WHERE related_order_id = %s
        """
        results = self._execute_query(query, (order_id,))
        return [dict(row) for row in results]
    
    # ==================== 理财赎回记录操作 ====================
    
    def save_redemption(self, redeem_data: Dict[str, Any], success: bool,
                       remark: str = None):
        """保存理财赎回记录"""
        query = """
            INSERT INTO simple_earn_redemptions (
                redeem_id, product_id, asset, amount, redeem_all,
                dest_account, success, create_time, remark
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        params = (
            redeem_data.get('redeemId'),
            redeem_data.get('productId'),
            redeem_data.get('asset'),
            Decimal(redeem_data.get('amount', '0')),
            redeem_data.get('redeemAll', False),
            redeem_data.get('destAccount'),
            success,
            int(datetime.now().timestamp() * 1000),
            remark
        )
        
        self._execute_query(query, params)
        logger.info(f"理财赎回记录已保存：redeemId={redeem_data.get('redeemId')}")
    
    # ==================== 监控日志操作 ====================
    
    def save_monitoring_log(self, check_time: datetime, symbol: str,
                           position_data: Dict[str, Any] = None,
                           current_price: Decimal = None,
                           pnl_rate: Decimal = None,
                           tp_reached: bool = False,
                           sl_reached: bool = False,
                           liquidation_risk: str = 'NONE',
                           action_taken: str = 'NONE',
                           remark: str = None):
        """保存监控日志"""
        query = """
            INSERT INTO monitoring_logs (
                check_time, symbol, position_side, position_amt,
                entry_price, current_price, unrealized_profit,
                unrealized_profit_rate, tp_reached, sl_reached,
                liquidation_risk, action_taken, remark
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        params = (
            check_time,
            symbol,
            position_data.get('positionSide') if position_data else None,
            str(Decimal(position_data.get('positionAmt', '0'))) if position_data else None,
            str(Decimal(position_data.get('entryPrice', '0'))) if position_data else None,
            str(current_price) if current_price else None,
            str(Decimal(position_data.get('unRealizedProfit', '0'))) if position_data else None,
            str(pnl_rate) if pnl_rate else None,
            tp_reached,
            sl_reached,
            liquidation_risk,
            action_taken,
            remark
        )
        
        self._execute_query(query, params)
        logger.debug(f"监控日志已保存：{symbol}")
    
    def save_time_close_log(self, symbol: str, position_side: str,
                           reason: str, order_id: int, close_time: datetime):
        """v6.13.3: 保存时间平仓日志"""
        query = """
            INSERT INTO time_close_logs 
            (symbol, position_side, reason, order_id, close_time, created_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        """
        
        params = (symbol, position_side, reason, order_id, close_time)
        self._execute_query(query, params)
        logger.info(f"时间平仓日志已保存：{symbol}")
    
    def get_monitoring_logs(self, symbol: str = None, 
                           start_time: datetime = None,
                           limit: int = 100) -> List[Dict[str, Any]]:
        """查询监控日志"""
        query = """
            SELECT * FROM monitoring_logs 
            WHERE 1=1
        """
        params = []
        
        if symbol:
            query += " AND symbol = %s"
            params.append(symbol)
        
        if start_time:
            query += " AND check_time >= %s"
            params.append(start_time)
        
        query += " ORDER BY check_time DESC LIMIT %s"
        params.append(limit)
        
        results = self._execute_query(query, tuple(params))
        return [dict(row) for row in results]
    
    # ==================== 账户余额快照操作 ====================
    
    def save_balance_snapshot(self, account_type: str, asset: str,
                             wallet_balance: Decimal, available_balance: Decimal,
                             unrealized_profit: Decimal = None,
                             total_margin_balance: Decimal = None):
        """保存账户余额快照"""
        query = """
            INSERT INTO account_balance_snapshot (
                snapshot_time, account_type, asset, wallet_balance,
                available_balance, unrealized_profit, total_margin_balance
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        
        params = (
            datetime.now(),
            account_type,
            asset,
            str(wallet_balance),
            str(available_balance),
            str(unrealized_profit) if unrealized_profit else None,
            str(total_margin_balance) if total_margin_balance else None
        )
        
        self._execute_query(query, params)
        logger.debug(f"余额快照已保存：{account_type} {asset}")
    
    # ==================== 平仓记录操作 ====================
    
    def save_closed_position(self, close_data: Dict[str, Any]):
        """保存平仓记录"""
        query = """
            INSERT INTO closed_positions (
                order_id, symbol, side, position_side,
                open_price, close_price, quantity,
                open_time, close_time, leverage,
                gross_pnl, commission, net_pnl, pnl_rate,
                close_reason, max_unrealized_profit, min_unrealized_profit,
                duration_seconds, remark
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        params = (
            close_data.get('order_id'),
            close_data.get('symbol'),
            close_data.get('side'),
            close_data.get('position_side'),
            str(close_data.get('open_price', Decimal('0'))),
            str(close_data.get('close_price', Decimal('0'))),
            str(close_data.get('quantity', Decimal('0'))),
            close_data.get('open_time'),
            close_data.get('close_time'),
            close_data.get('leverage', 20),
            str(close_data.get('gross_pnl', Decimal('0'))),
            str(close_data.get('commission', Decimal('0'))),
            str(close_data.get('net_pnl', Decimal('0'))),
            str(close_data.get('pnl_rate', Decimal('0'))),
            close_data.get('close_reason'),
            str(close_data.get('max_unrealized_profit')) if close_data.get('max_unrealized_profit') else None,
            str(close_data.get('min_unrealized_profit')) if close_data.get('min_unrealized_profit') else None,
            close_data.get('duration_seconds'),
            close_data.get('remark')
        )
        
        self._execute_query(query, params)
        logger.info(f"平仓记录已保存：订单 ID={close_data.get('order_id')}, 净盈亏={close_data.get('net_pnl')}")
    
    def get_closed_positions(self, 
                            symbol: str = None,
                            start_time: datetime = None,
                            end_time: datetime = None,
                            limit: int = 100) -> List[Dict[str, Any]]:
        """查询平仓记录"""
        query = """
            SELECT * FROM closed_positions 
            WHERE 1=1
        """
        params = []
        
        if symbol:
            query += " AND symbol = %s"
            params.append(symbol)
        
        if start_time:
            query += " AND close_time >= %s"
            params.append(int(start_time.timestamp() * 1000))
        
        if end_time:
            query += " AND close_time <= %s"
            params.append(int(end_time.timestamp() * 1000))
        
        query += " ORDER BY close_time DESC LIMIT %s"
        params.append(limit)
        
        results = self._execute_query(query, tuple(params))
        return [dict(row) for row in results]
    
    # ==================== 止盈止损触发记录操作 ====================
    
    def save_tp_sl_trigger(self, trigger_data: Dict[str, Any]):
        """保存止盈止损触发记录"""
        query = """
            INSERT INTO tp_sl_triggers (
                order_id, symbol, position_side, trigger_type,
                trigger_time, trigger_price, target_price,
                position_qty, entry_price, unrealized_profit, pnl_rate,
                remark
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        params = (
            trigger_data.get('order_id'),
            trigger_data.get('symbol'),
            trigger_data.get('position_side'),
            trigger_data.get('trigger_type'),
            trigger_data.get('trigger_time'),
            str(trigger_data.get('trigger_price', Decimal('0'))),
            str(trigger_data.get('target_price')) if trigger_data.get('target_price') else None,
            str(trigger_data.get('position_qty')) if trigger_data.get('position_qty') else None,
            str(trigger_data.get('entry_price')) if trigger_data.get('entry_price') else None,
            str(trigger_data.get('unrealized_profit')) if trigger_data.get('unrealized_profit') else None,
            str(trigger_data.get('pnl_rate')) if trigger_data.get('pnl_rate') else None,
            trigger_data.get('remark')
        )
        
        self._execute_query(query, params)
        logger.info(f"止盈止损触发记录已保存：订单 ID={trigger_data.get('order_id')}, 类型={trigger_data.get('trigger_type')}")
    
    def get_tp_sl_triggers(self,
                          order_id: int = None,
                          symbol: str = None,
                          trigger_type: str = None,
                          limit: int = 100) -> List[Dict[str, Any]]:
        """查询止盈止损触发记录"""
        query = """
            SELECT * FROM tp_sl_triggers 
            WHERE 1=1
        """
        params = []
        
        if order_id:
            query += " AND order_id = %s"
            params.append(order_id)
        
        if symbol:
            query += " AND symbol = %s"
            params.append(symbol)
        
        if trigger_type:
            query += " AND trigger_type = %s"
            params.append(trigger_type)
        
        query += " ORDER BY trigger_time DESC LIMIT %s"
        params.append(limit)
        
        results = self._execute_query(query, tuple(params))
        return [dict(row) for row in results]
    
    # ==================== 交易统计操作 ====================
    
    def save_trade_statistics(self, stats_data: Dict[str, Any]):
        """保存交易统计数据"""
        query = """
            INSERT INTO trade_statistics (
                period_type, period_start, period_end, symbol,
                total_trades, winning_trades, losing_trades,
                total_net_pnl, total_commission,
                avg_pnl_rate, max_pnl_rate, min_pnl_rate,
                win_rate, profit_loss_ratio,
                max_consecutive_wins, max_consecutive_losses,
                updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (period_type, period_start, period_end, symbol) 
            DO UPDATE SET
                total_trades = EXCLUDED.total_trades,
                winning_trades = EXCLUDED.winning_trades,
                losing_trades = EXCLUDED.losing_trades,
                total_net_pnl = EXCLUDED.total_net_pnl,
                total_commission = EXCLUDED.total_commission,
                avg_pnl_rate = EXCLUDED.avg_pnl_rate,
                max_pnl_rate = EXCLUDED.max_pnl_rate,
                min_pnl_rate = EXCLUDED.min_pnl_rate,
                win_rate = EXCLUDED.win_rate,
                profit_loss_ratio = EXCLUDED.profit_loss_ratio,
                max_consecutive_wins = EXCLUDED.max_consecutive_wins,
                max_consecutive_losses = EXCLUDED.max_consecutive_losses,
                updated_at = CURRENT_TIMESTAMP
        """
        
        params = (
            stats_data.get('period_type'),
            stats_data.get('period_start'),
            stats_data.get('period_end'),
            stats_data.get('symbol', 'ALL'),
            stats_data.get('total_trades', 0),
            stats_data.get('winning_trades', 0),
            stats_data.get('losing_trades', 0),
            str(stats_data.get('total_net_pnl', Decimal('0'))),
            str(stats_data.get('total_commission', Decimal('0'))),
            str(stats_data.get('avg_pnl_rate', Decimal('0'))),
            str(stats_data.get('max_pnl_rate', Decimal('0'))),
            str(stats_data.get('min_pnl_rate', Decimal('0'))),
            str(stats_data.get('win_rate', Decimal('0'))),
            str(stats_data.get('profit_loss_ratio', Decimal('0'))),
            stats_data.get('max_consecutive_wins', 0),
            stats_data.get('max_consecutive_losses', 0),
            datetime.now()
        )
        
        self._execute_query(query, params)
        logger.info(f"交易统计已保存：周期={stats_data.get('period_type')}, 交易数={stats_data.get('total_trades')}")
    
    def get_weekly_statistics(self, weeks: int = 4) -> List[Dict[str, Any]]:
        """获取最近 N 周的统计数据"""
        query = """
            SELECT * FROM trade_statistics 
            WHERE period_type = 'WEEKLY'
            ORDER BY period_end DESC 
            LIMIT %s
        """
        results = self._execute_query(query, (weeks,))
        return [dict(row) for row in results]
    
    def get_monthly_statistics(self, months: int = 6) -> List[Dict[str, Any]]:
        """获取最近 N 月的统计数据"""
        query = """
            SELECT * FROM trade_statistics 
            WHERE period_type = 'MONTHLY'
            ORDER BY period_end DESC 
            LIMIT %s
        """
        results = self._execute_query(query, (months,))
        return [dict(row) for row in results]
    
    # ==================== 统计查询 ====================
    
    def get_trade_statistics(self, days: int = 30) -> Dict[str, Any]:
        """获取交易统计数据"""
        query = """
            SELECT COUNT(*) as total_trades,
                   SUM(CASE WHEN status = 'FILLED' THEN 1 ELSE 0 END) as filled_trades
            FROM trades
            WHERE created_at >= NOW() - INTERVAL '%s days'
        """ % days
        
        result = self._execute_one(query)
        return {
            'total_trades': result['total_trades'] or 0,
            'filled_trades': result['filled_trades'] or 0,
            'period_days': days
        }
    
    def cleanup_old_data(self, days: int = 90):
        """清理旧数据"""
        # 清理监控日志
        query1 = """
            DELETE FROM monitoring_logs 
            WHERE check_time < NOW() - INTERVAL '%s days'
        """ % days
        self._execute_query(query1)
        
        # 清理余额快照
        query2 = """
            DELETE FROM account_balance_snapshot 
            WHERE snapshot_time < NOW() - INTERVAL '%s days'
        """ % days
        self._execute_query(query2)
        
        logger.info(f"清理了 {days} 天前的旧数据")
    
    def backup_database(self, backup_path: str = None):
        """备份数据库（PostgreSQL 使用 pg_dump）"""
        logger.warning("PostgreSQL 备份请使用 pg_dump 工具")
        return None


# 全局数据库实例
_db_manager: Optional[DatabaseManager] = None


def get_db_manager() -> DatabaseManager:
    """获取全局数据库管理器实例"""
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager


if __name__ == '__main__':
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 60)
    print("PostgreSQL 数据库模块测试")
    print("=" * 60)
    
    db = get_db_manager()
    
    # 测试保存交易记录
    print("\n1. 测试保存交易记录...")
    test_order = {
        'orderId': 123456,
        'symbol': 'BTCUSDT',
        'side': 'BUY',
        'positionSide': 'LONG',
        'type': 'LIMIT',
        'origQty': '0.001',
        'price': '50000',
        'status': 'NEW',
        'updateTime': int(datetime.now().timestamp() * 1000)
    }
    db.save_trade(test_order)
    print("✅ 交易记录保存成功")
    
    # 测试查询交易记录
    print("\n2. 测试查询交易记录...")
    trade = db.get_trade_by_order_id(123456)
    print(f"查询结果：{trade}")
    
    # 测试保存持仓记录
    print("\n3. 测试保存持仓记录...")
    test_position = {
        'symbol': 'BTCUSDT',
        'positionSide': 'LONG',
        'positionAmt': '0.001',
        'entryPrice': '50000',
        'markPrice': '50100',
        'unRealizedProfit': '0.1',
        'liquidationPrice': '45000',
        'marginType': 'CROSSED'
    }
    db.save_position(test_position)
    print("✅ 持仓记录保存成功")
    
    # 测试查询持仓
    print("\n4. 测试查询持仓...")
    positions = db.get_all_positions()
    print(f"持仓数量：{len(positions)}")
    
    # 测试保存监控日志
    print("\n5. 测试保存监控日志...")
    db.save_monitoring_log(
        check_time=datetime.now(),
        symbol='BTCUSDT',
        position_data=test_position,
        current_price=Decimal('50100'),
        pnl_rate=Decimal('0.2'),
        liquidation_risk='LOW'
    )
    print("✅ 监控日志保存成功")
    
    # 测试统计数据
    print("\n6. 测试获取交易统计...")
    stats = db.get_trade_statistics()
    print(f"交易统计：{stats}")
    
    print("\n" + "=" * 60)
    print("PostgreSQL 数据库模块测试完成!")
    print("=" * 60)
