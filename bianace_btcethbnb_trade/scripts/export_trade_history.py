#!/usr/bin/env python3
"""
从服务器数据库导出历史交易数据

使用方法:
1. 在服务器容器内执行：
   docker exec binance-trade-analyzer python3 /app/scripts/export_trade_history.py

2. 导出的数据会保存到：
   /root/binance-trade-analyzer/data/trade_history_export.json
"""

import json
import logging
from datetime import datetime
from decimal import Decimal
import sys
import os

# 添加路径
sys.path.append('/app')

from models.database import get_db_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('export_trade_history')


def export_trade_history():
    """导出历史交易记录"""
    logger.info("=" * 80)
    logger.info("开始导出历史交易数据")
    logger.info("=" * 80)
    
    trades = []
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # 查询所有交易记录
                query = """
                SELECT 
                    symbol,
                    direction,
                    entry_price,
                    close_price,
                    quantity,
                    pnl,
                    pnl_percent,
                    open_time,
                    close_time,
                    signal_grade,
                    leverage,
                    status,
                    order_id
                FROM trade_records
                WHERE status = 'CLOSED'
                ORDER BY open_time ASC
                """
                
                logger.info(f"执行查询：{query[:100]}...")
                cursor.execute(query)
                results = cursor.fetchall()
                
                logger.info(f"查询到 {len(results)} 笔交易记录")
                
                # 转换为字典列表
                for row in results:
                    trade = {
                        'symbol': row['symbol'],
                        'direction': row['direction'],
                        'entry_price': str(row['entry_price']),
                        'close_price': str(row['close_price']),
                        'quantity': str(row['quantity']),
                        'pnl': str(row['pnl']),
                        'pnl_percent': str(row['pnl_percent']) if row['pnl_percent'] else None,
                        'open_time': row['open_time'].isoformat() if row['open_time'] else None,
                        'close_time': row['close_time'].isoformat() if row['close_time'] else None,
                        'signal_grade': row['signal_grade'],
                        'leverage': str(row['leverage']),
                        'status': row['status'],
                        'order_id': str(row['order_id']) if row['order_id'] else None
                    }
                    trades.append(trade)
                
                logger.info(f"已转换 {len(trades)} 笔交易")
                
    except Exception as e:
        logger.error(f"导出失败：{str(e)}", exc_info=True)
        raise
    
    # 生成回测报告格式
    report = {
        'export_date': datetime.now().isoformat(),
        'source': 'PostgreSQL Database - trade_records table',
        'total_trades': len(trades),
        'summary': {
            'total_trades': len(trades),
            'winning_trades': sum(1 for t in trades if Decimal(t['pnl']) > 0),
            'losing_trades': sum(1 for t in trades if Decimal(t['pnl']) < 0),
        },
        'trades': trades
    }
    
    # 计算汇总统计
    if trades:
        total_pnl = sum(Decimal(t['pnl']) for t in trades)
        winning_trades = [t for t in trades if Decimal(t['pnl']) > 0]
        losing_trades = [t for t in trades if Decimal(t['pnl']) < 0]
        
        report['summary'].update({
            'total_pnl': str(total_pnl),
            'win_rate': str(len(winning_trades) / len(trades)) if trades else '0',
            'avg_pnl': str(total_pnl / len(trades)) if trades else '0',
            'max_win': str(max(Decimal(t['pnl']) for t in trades)) if trades else '0',
            'max_loss': str(min(Decimal(t['pnl']) for t in trades)) if trades else '0',
        })
    
    # 保存文件
    output_file = '/app/data/trade_history_export.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    logger.info("=" * 80)
    logger.info(f"导出完成！")
    logger.info(f"文件路径：{output_file}")
    logger.info(f"总交易数：{len(trades)}")
    logger.info(f"盈利交易：{report['summary']['winning_trades']}")
    logger.info(f"亏损交易：{report['summary']['losing_trades']}")
    logger.info(f"总盈亏：{report['summary'].get('total_pnl', 'N/A')}")
    logger.info(f"胜率：{report['summary'].get('win_rate', 'N/A')}")
    logger.info("=" * 80)
    
    return output_file


if __name__ == '__main__':
    export_trade_history()
