#!/usr/bin/env python3
"""
SQLite 到 PostgreSQL 数据迁移脚本
迁移所有交易数据到 PostgreSQL 数据库
"""

import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from decimal import Decimal
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DecimalEncoder:
    """Decimal 编码器"""
    @staticmethod
    def encode(value):
        if value is None:
            return None
        if isinstance(value, Decimal):
            return str(value)
        return value


def get_sqlite_connection():
    """获取 SQLite 连接"""
    db_path = './database/trading.db'
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_postgres_connection():
    """获取 PostgreSQL 连接"""
    # 本地连接（在服务器上执行时使用）
    conn = psycopg2.connect(
        host='localhost',
        port=5432,
        database='trading_platform',
        user='bianace_user',
        password='Bianace@2024',
        cursor_factory=RealDictCursor
    )
    return conn


def migrate_trades(sqlite_conn, pg_conn):
    """迁移交易记录"""
    logger.info("开始迁移交易记录...")
    
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT * FROM trades")
    trades = sqlite_cursor.fetchall()
    
    pg_cursor = pg_conn.cursor()
    
    migrated_count = 0
    for trade in trades:
        try:
            query = """
                INSERT INTO schema_bianace.trades (
                    order_id, symbol, side, position_side, type,
                    quantity, price, avg_price, executed_qty, cum_quote,
                    status, reduce_only, time_in_force, client_order_id,
                    tp_trigger_price, tp_price, sl_trigger_price, sl_price,
                    create_time, update_time, transaction_id,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (order_id) DO NOTHING
            """
            
            params = (
                trade['order_id'],
                trade['symbol'],
                trade['side'],
                trade['position_side'],
                trade['type'],
                Decimal(str(trade['quantity'])),
                Decimal(str(trade['price'])) if trade['price'] else None,
                Decimal(str(trade['avg_price'])) if trade['avg_price'] else None,
                Decimal(str(trade['executed_qty'])) if trade['executed_qty'] else None,
                Decimal(str(trade['cum_quote'])) if trade['cum_quote'] else None,
                trade['status'],
                bool(trade['reduce_only']),
                trade['time_in_force'],
                trade['client_order_id'],
                Decimal(str(trade['tp_trigger_price'])) if trade['tp_trigger_price'] else None,
                Decimal(str(trade['tp_price'])) if trade['tp_price'] else None,
                Decimal(str(trade['sl_trigger_price'])) if trade['sl_trigger_price'] else None,
                Decimal(str(trade['sl_price'])) if trade['sl_price'] else None,
                trade['create_time'],
                trade['update_time'],
                trade['transaction_id'],
                trade['created_at'],
                trade['updated_at']
            )
            
            pg_cursor.execute(query, params)
            migrated_count += 1
        except Exception as e:
            logger.error(f"迁移交易记录失败：order_id={trade['order_id']}, error={e}")
    
    pg_conn.commit()
    logger.info(f"交易记录迁移完成：迁移 {migrated_count}/{len(trades)} 条")
    
    return migrated_count


def migrate_positions(sqlite_conn, pg_conn):
    """迁移持仓记录"""
    logger.info("开始迁移持仓记录...")
    
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT * FROM positions")
    positions = sqlite_cursor.fetchall()
    
    pg_cursor = pg_conn.cursor()
    
    migrated_count = 0
    for position in positions:
        try:
            query = """
                INSERT INTO schema_bianace.positions (
                    symbol, position_side, position_amt, entry_price,
                    mark_price, unrealized_profit, leverage, liquidation_price,
                    margin_type, is_auto_add_margin, last_update_time,
                    recorded_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, position_side) DO UPDATE SET
                    position_amt = EXCLUDED.position_amt,
                    entry_price = EXCLUDED.entry_price,
                    mark_price = EXCLUDED.mark_price,
                    unrealized_profit = EXCLUDED.unrealized_profit,
                    leverage = EXCLUDED.leverage,
                    liquidation_price = EXCLUDED.liquidation_price,
                    margin_type = EXCLUDED.margin_type,
                    is_auto_add_margin = EXCLUDED.is_auto_add_margin,
                    last_update_time = EXCLUDED.last_update_time,
                    recorded_at = CURRENT_TIMESTAMP
            """
            
            params = (
                position['symbol'],
                position['position_side'],
                Decimal(str(position['position_amt'])),
                Decimal(str(position['entry_price'])),
                Decimal(str(position['mark_price'])) if position['mark_price'] else None,
                Decimal(str(position['unrealized_profit'])) if position['unrealized_profit'] else None,
                position['leverage'],
                Decimal(str(position['liquidation_price'])) if position['liquidation_price'] else None,
                position['margin_type'],
                bool(position['is_auto_add_margin']),
                position['last_update_time'],
                position['recorded_at']
            )
            
            pg_cursor.execute(query, params)
            migrated_count += 1
        except Exception as e:
            logger.error(f"迁移持仓记录失败：symbol={position['symbol']}, error={e}")
    
    pg_conn.commit()
    logger.info(f"持仓记录迁移完成：迁移 {migrated_count}/{len(positions)} 条")
    
    return migrated_count


def migrate_monitoring_logs(sqlite_conn, pg_conn):
    """迁移监控日志"""
    logger.info("开始迁移监控日志...")
    
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT * FROM monitoring_logs")
    logs = sqlite_cursor.fetchall()
    
    pg_cursor = pg_conn.cursor()
    
    migrated_count = 0
    for log in logs:
        try:
            query = """
                INSERT INTO schema_bianace.monitoring_logs (
                    check_time, symbol, position_side, position_amt,
                    entry_price, current_price, unrealized_profit,
                    unrealized_profit_rate, tp_reached, sl_reached,
                    liquidation_risk, action_taken, remark,
                    created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            params = (
                log['check_time'],
                log['symbol'],
                log['position_side'],
                Decimal(str(log['position_amt'])) if log['position_amt'] else None,
                Decimal(str(log['entry_price'])) if log['entry_price'] else None,
                Decimal(str(log['current_price'])) if log['current_price'] else None,
                Decimal(str(log['unrealized_profit'])) if log['unrealized_profit'] else None,
                Decimal(str(log['unrealized_profit_rate'])) if log['unrealized_profit_rate'] else None,
                bool(log['tp_reached']),
                bool(log['sl_reached']),
                log['liquidation_risk'],
                log['action_taken'],
                log['remark'],
                log['created_at']
            )
            
            pg_cursor.execute(query, params)
            migrated_count += 1
        except Exception as e:
            logger.error(f"迁移监控日志失败：symbol={log['symbol']}, error={e}")
    
    pg_conn.commit()
    logger.info(f"监控日志迁移完成：迁移 {migrated_count}/{len(logs)} 条")
    
    return migrated_count


def migrate_account_transfers(sqlite_conn, pg_conn):
    """迁移资金划转记录"""
    logger.info("开始迁移资金划转记录...")
    
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT * FROM account_transfers")
    transfers = sqlite_cursor.fetchall()
    
    pg_cursor = pg_conn.cursor()
    
    migrated_count = 0
    for transfer in transfers:
        try:
            query = """
                INSERT INTO schema_bianace.account_transfers (
                    tran_id, asset, amount, type, from_account, to_account,
                    status, create_time, remark, related_order_id,
                    created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            params = (
                transfer['tran_id'],
                transfer['asset'],
                Decimal(str(transfer['amount'])),
                transfer['type'],
                transfer['from_account'],
                transfer['to_account'],
                transfer['status'],
                transfer['create_time'],
                transfer['remark'],
                transfer['related_order_id'],
                transfer['created_at']
            )
            
            pg_cursor.execute(query, params)
            migrated_count += 1
        except Exception as e:
            logger.error(f"迁移资金划转记录失败：tran_id={transfer['tran_id']}, error={e}")
    
    pg_conn.commit()
    logger.info(f"资金划转记录迁移完成：迁移 {migrated_count}/{len(transfers)} 条")
    
    return migrated_count


def migrate_simple_earn_redemptions(sqlite_conn, pg_conn):
    """迁移理财赎回记录"""
    logger.info("开始迁移理财赎回记录...")
    
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT * FROM simple_earn_redemptions")
    redemptions = sqlite_cursor.fetchall()
    
    pg_cursor = pg_conn.cursor()
    
    migrated_count = 0
    for redemption in redemptions:
        try:
            query = """
                INSERT INTO schema_bianace.simple_earn_redemptions (
                    redeem_id, product_id, asset, amount, redeem_all,
                    dest_account, success, create_time, remark,
                    created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            params = (
                redemption['redeem_id'],
                redemption['product_id'],
                redemption['asset'],
                Decimal(str(redemption['amount'])),
                bool(redemption['redeem_all']),
                redemption['dest_account'],
                bool(redemption['success']),
                redemption['create_time'],
                redemption['remark'],
                redemption['created_at']
            )
            
            pg_cursor.execute(query, params)
            migrated_count += 1
        except Exception as e:
            logger.error(f"迁移理财赎回记录失败：redeem_id={redemption['redeem_id']}, error={e}")
    
    pg_conn.commit()
    logger.info(f"理财赎回记录迁移完成：迁移 {migrated_count}/{len(redemptions)} 条")
    
    return migrated_count


def migrate_account_balance_snapshot(sqlite_conn, pg_conn):
    """迁移账户余额快照"""
    logger.info("开始迁移账户余额快照...")
    
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT * FROM account_balance_snapshot")
    snapshots = sqlite_cursor.fetchall()
    
    pg_cursor = pg_conn.cursor()
    
    migrated_count = 0
    for snapshot in snapshots:
        try:
            query = """
                INSERT INTO schema_bianace.account_balance_snapshot (
                    snapshot_time, account_type, asset, wallet_balance,
                    available_balance, unrealized_profit, total_margin_balance,
                    created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            params = (
                snapshot['snapshot_time'],
                snapshot['account_type'],
                snapshot['asset'],
                Decimal(str(snapshot['wallet_balance'])) if snapshot['wallet_balance'] else None,
                Decimal(str(snapshot['available_balance'])) if snapshot['available_balance'] else None,
                Decimal(str(snapshot['unrealized_profit'])) if snapshot['unrealized_profit'] else None,
                Decimal(str(snapshot['total_margin_balance'])) if snapshot['total_margin_balance'] else None,
                snapshot['created_at']
            )
            
            pg_cursor.execute(query, params)
            migrated_count += 1
        except Exception as e:
            logger.error(f"迁移账户余额快照失败：snapshot_time={snapshot['snapshot_time']}, error={e}")
    
    pg_conn.commit()
    logger.info(f"账户余额快照迁移完成：迁移 {migrated_count}/{len(snapshots)} 条")
    
    return migrated_count


def migrate_closed_positions(sqlite_conn, pg_conn):
    """迁移平仓记录"""
    logger.info("开始迁移平仓记录...")
    
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT * FROM closed_positions")
    closed_positions = sqlite_cursor.fetchall()
    
    pg_cursor = pg_conn.cursor()
    
    migrated_count = 0
    for position in closed_positions:
        try:
            query = """
                INSERT INTO schema_bianace.closed_positions (
                    order_id, symbol, side, position_side,
                    open_price, close_price, quantity,
                    open_time, close_time, leverage,
                    gross_pnl, commission, net_pnl, pnl_rate,
                    close_reason, max_unrealized_profit, min_unrealized_profit,
                    duration_seconds, remark, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            params = (
                position['order_id'],
                position['symbol'],
                position['side'],
                position['position_side'],
                Decimal(str(position['open_price'])),
                Decimal(str(position['close_price'])),
                Decimal(str(position['quantity'])),
                position['open_time'],
                position['close_time'],
                position['leverage'],
                Decimal(str(position['gross_pnl'])),
                Decimal(str(position['commission'])) if position['commission'] else None,
                Decimal(str(position['net_pnl'])),
                Decimal(str(position['pnl_rate'])),
                position['close_reason'],
                Decimal(str(position['max_unrealized_profit'])) if position['max_unrealized_profit'] else None,
                Decimal(str(position['min_unrealized_profit'])) if position['min_unrealized_profit'] else None,
                position['duration_seconds'],
                position['remark'],
                position['created_at']
            )
            
            pg_cursor.execute(query, params)
            migrated_count += 1
        except Exception as e:
            logger.error(f"迁移平仓记录失败：order_id={position['order_id']}, error={e}")
    
    pg_conn.commit()
    logger.info(f"平仓记录迁移完成：迁移 {migrated_count}/{len(closed_positions)} 条")
    
    return migrated_count


def migrate_tp_sl_triggers(sqlite_conn, pg_conn):
    """迁移止盈止损触发记录"""
    logger.info("开始迁移止盈止损触发记录...")
    
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT * FROM tp_sl_triggers")
    triggers = sqlite_cursor.fetchall()
    
    pg_cursor = pg_conn.cursor()
    
    migrated_count = 0
    for trigger in triggers:
        try:
            query = """
                INSERT INTO schema_bianace.tp_sl_triggers (
                    order_id, symbol, position_side, trigger_type,
                    trigger_time, trigger_price, target_price,
                    position_qty, entry_price, unrealized_profit, pnl_rate,
                    remark, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            params = (
                trigger['order_id'],
                trigger['symbol'],
                trigger['position_side'],
                trigger['trigger_type'],
                trigger['trigger_time'],
                Decimal(str(trigger['trigger_price'])),
                Decimal(str(trigger['target_price'])) if trigger['target_price'] else None,
                Decimal(str(trigger['position_qty'])) if trigger['position_qty'] else None,
                Decimal(str(trigger['entry_price'])) if trigger['entry_price'] else None,
                Decimal(str(trigger['unrealized_profit'])) if trigger['unrealized_profit'] else None,
                Decimal(str(trigger['pnl_rate'])) if trigger['pnl_rate'] else None,
                trigger['remark'],
                trigger['created_at']
            )
            
            pg_cursor.execute(query, params)
            migrated_count += 1
        except Exception as e:
            logger.error(f"迁移止盈止损触发记录失败：order_id={trigger['order_id']}, error={e}")
    
    pg_conn.commit()
    logger.info(f"止盈止损触发记录迁移完成：迁移 {migrated_count}/{len(triggers)} 条")
    
    return migrated_count


def migrate_trade_statistics(sqlite_conn, pg_conn):
    """迁移交易统计数据"""
    logger.info("开始迁移交易统计数据...")
    
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT * FROM trade_statistics")
    statistics = sqlite_cursor.fetchall()
    
    pg_cursor = pg_conn.cursor()
    
    migrated_count = 0
    for stat in statistics:
        try:
            query = """
                INSERT INTO schema_bianace.trade_statistics (
                    period_type, period_start, period_end, symbol,
                    total_trades, winning_trades, losing_trades,
                    total_net_pnl, total_commission,
                    avg_pnl_rate, max_pnl_rate, min_pnl_rate,
                    win_rate, profit_loss_ratio,
                    max_consecutive_wins, max_consecutive_losses,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (period_type, period_start, period_end, symbol) DO UPDATE SET
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
                stat['period_type'],
                stat['period_start'],
                stat['period_end'],
                stat['symbol'],
                stat['total_trades'],
                stat['winning_trades'],
                stat['losing_trades'],
                Decimal(str(stat['total_net_pnl'])) if stat['total_net_pnl'] else None,
                Decimal(str(stat['total_commission'])) if stat['total_commission'] else None,
                Decimal(str(stat['avg_pnl_rate'])) if stat['avg_pnl_rate'] else None,
                Decimal(str(stat['max_pnl_rate'])) if stat['max_pnl_rate'] else None,
                Decimal(str(stat['min_pnl_rate'])) if stat['min_pnl_rate'] else None,
                Decimal(str(stat['win_rate'])) if stat['win_rate'] else None,
                Decimal(str(stat['profit_loss_ratio'])) if stat['profit_loss_ratio'] else None,
                stat['max_consecutive_wins'],
                stat['max_consecutive_losses'],
                stat['created_at'],
                stat['updated_at']
            )
            
            pg_cursor.execute(query, params)
            migrated_count += 1
        except Exception as e:
            logger.error(f"迁移交易统计数据失败：period={stat['period_type']}-{stat['period_start']}, error={e}")
    
    pg_conn.commit()
    logger.info(f"交易统计数据迁移完成：迁移 {migrated_count}/{len(statistics)} 条")
    
    return migrated_count


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("开始 SQLite 到 PostgreSQL 数据迁移")
    logger.info("=" * 60)
    
    sqlite_conn = None
    pg_conn = None
    
    try:
        # 连接数据库
        logger.info("连接 SQLite 数据库...")
        sqlite_conn = get_sqlite_connection()
        
        logger.info("连接 PostgreSQL 数据库...")
        pg_conn = get_postgres_connection()
        
        # 迁移数据
        total_migrated = 0
        
        total_migrated += migrate_trades(sqlite_conn, pg_conn)
        total_migrated += migrate_positions(sqlite_conn, pg_conn)
        total_migrated += migrate_monitoring_logs(sqlite_conn, pg_conn)
        total_migrated += migrate_account_transfers(sqlite_conn, pg_conn)
        total_migrated += migrate_simple_earn_redemptions(sqlite_conn, pg_conn)
        total_migrated += migrate_account_balance_snapshot(sqlite_conn, pg_conn)
        total_migrated += migrate_closed_positions(sqlite_conn, pg_conn)
        total_migrated += migrate_tp_sl_triggers(sqlite_conn, pg_conn)
        total_migrated += migrate_trade_statistics(sqlite_conn, pg_conn)
        
        logger.info("=" * 60)
        logger.info(f"数据迁移完成！共迁移 {total_migrated} 条记录")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"迁移失败：{e}")
        raise
    finally:
        if sqlite_conn:
            sqlite_conn.close()
        if pg_conn:
            pg_conn.close()


if __name__ == '__main__':
    main()
