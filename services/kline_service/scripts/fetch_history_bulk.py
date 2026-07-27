#!/usr/bin/env python3
"""
批量补全历史 K 线数据

用法：
python3 fetch_history_bulk.py --symbols BTCUSDT,ETHUSDT,BNBUSDT --days 30
"""

import asyncio
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent

# 添加共享模块路径
sys.path.insert(0, str(project_root))

from shared.core.database import db_manager
from core.binance_client import BinanceClient
from core.collector import KlineCollector
from shared.utils.logger import get_logger

logger = get_logger(__name__)

# 默认交易对列表
DEFAULT_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']

# 时间周期和对应的数据天数
INTERVAL_DAYS = {
    '1d': 90,    # 日线采集 90 天
    '4h': 30,    # 4 小时采集 30 天
    '1h': 15,    # 1 小时采集 15 天
    '15m': 7,    # 15 分钟采集 7 天
    '5m': 3,     # 5 分钟采集 3 天
    '1m': 1,     # 1 分钟采集 1 天
}


async def fetch_symbol_history(symbol: str, days: int = 30):
    """
    获取单个交易对的历史数据
    
    Args:
        symbol: 交易对
        days: 获取多少天的数据
    """
    logger.info(f"开始获取 {symbol} 的历史数据（{days}天）...")
    
    # 连接数据库
    await db_manager.connect()
    logger.info("✅ 数据库连接成功")
    
    # 初始化客户端
    binance_client = BinanceClient()
    await binance_client.connect()
    logger.info("✅ 币安 API 连接成功")
    
    # 初始化采集器
    collector = KlineCollector(
        binance_client=binance_client,
        db=db_manager,
        symbols=[],
        intervals=[]
    )
    
    end_time = int(datetime.now().timestamp() * 1000)
    total_stored = 0
    
    # 为每个周期采集数据
    for interval, interval_days in INTERVAL_DAYS.items():
        actual_days = min(days, interval_days)
        if actual_days <= 0:
            continue
            
        start_time = int((datetime.now() - timedelta(days=actual_days)).timestamp() * 1000)
        
        logger.info(f"采集 {symbol} {interval} 周期（{actual_days}天）...")
        
        # 分批获取数据（币安 API 每次最多 1000 条）
        batch_limit = 1000
        current_start = start_time
        interval_stored = 0
        
        while current_start < end_time:
            batch_end = min(current_start + (batch_limit * 60 * 60 * 1000), end_time)
            
            klines = await collector.collect_klines(
                symbol,
                interval,
                start_time=current_start,
                end_time=batch_end
            )
            
            if not klines:
                logger.warning(f"未获取到 {symbol} {interval} 的数据")
                break
            
            stored = await collector.store_klines(klines)
            interval_stored += stored
            logger.debug(f"  批次：{stored} 条")
            
            if len(klines) < batch_limit:
                break
            
            # 设置下一批的起始时间
            current_start = klines[-1].close_time + 1
            
            # 避免请求过快
            await asyncio.sleep(0.2)
        
        logger.info(f"✅ {symbol} {interval}: 存储 {interval_stored} 条")
        total_stored += interval_stored
    
    # 关闭连接
    await binance_client.disconnect()
    await db_manager.disconnect()
    
    logger.info(f"✅ {symbol} 历史数据采集完成！共存储 {total_stored} 条数据")
    
    return total_stored


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='批量补全历史 K 线数据')
    parser.add_argument('--symbols', type=str, default=','.join(DEFAULT_SYMBOLS), 
                        help=f'交易对列表，逗号分隔（默认：{",".join(DEFAULT_SYMBOLS)}）')
    parser.add_argument('--days', type=int, default=30, help='获取多少天的数据（默认 30）')
    
    args = parser.parse_args()
    
    symbols = [s.strip() for s in args.symbols.split(',')]
    days = args.days
    
    logger.info(f"开始批量补全历史数据")
    logger.info(f"交易对：{symbols}")
    logger.info(f"天数：{days}")
    
    total = 0
    for symbol in symbols:
        try:
            stored = await fetch_symbol_history(symbol, days)
            total += stored
            
            # 交易对之间休息 1 秒，避免请求过快
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"❌ {symbol} 数据采集失败：{e}")
            import traceback
            traceback.print_exc()
    
    logger.info(f"\n{'='*60}")
    logger.info(f"✅ 所有交易对历史数据采集完成！")
    logger.info(f"共存储 {total} 条数据")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
