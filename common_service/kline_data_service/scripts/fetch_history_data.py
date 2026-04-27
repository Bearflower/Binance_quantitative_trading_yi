#!/usr/bin/env python3
"""
批量获取历史 K 线数据并存储到数据库

用法：
python3 fetch_history_data.py --symbol BTCUSDT --interval 1h --days 30
"""

import asyncio
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from shared.core.database import db_manager
from kline_data_service.core.binance_client import BinanceClient
from kline_data_service.core.collector import KlineCollector
from shared.utils.logger import get_logger

logger = get_logger(__name__)


async def fetch_history(
    symbol: str,
    interval: str,
    days: int = 30,
    limit: int = 1000
):
    """
    获取历史 K 线数据
    
    Args:
        symbol: 交易对
        interval: 时间间隔
        days: 获取多少天的数据
        limit: 每次请求的最大数量
    """
    logger.info(f"开始获取 {symbol} {interval} 的历史数据（{days}天）...")
    
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
    
    # 计算时间范围
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
    
    logger.info(f"时间范围：{datetime.fromtimestamp(start_time/1000)} 到 {datetime.fromtimestamp(end_time/1000)}")
    
    # 分批获取数据
    total_stored = 0
    current_start = start_time
    
    while current_start < end_time:
        # 计算本次获取的结束时间
        batch_end = min(current_start + (limit * 60 * 60 * 1000), end_time)  # 转换为毫秒
        
        logger.info(f"获取数据：{datetime.fromtimestamp(current_start/1000)} 到 {datetime.fromtimestamp(batch_end/1000)}")
        
        # 从币安获取 K 线数据
        klines = await collector.collect_klines(
            symbol,
            interval,
            start_time=current_start,
            end_time=batch_end
        )
        
        if not klines:
            logger.warning("未获取到数据")
            break
        
        logger.info(f"获取到 {len(klines)} 条数据")
        
        # 存储数据
        stored = await collector.store_klines(klines)
        logger.info(f"✅ 存储 {stored} 条数据")
        
        total_stored += stored
        
        # 更新起始时间
        if len(klines) < limit:
            break
        
        # 设置下一批的起始时间（从最后一条数据的收盘时间开始）
        current_start = klines[-1].close_time + 1
        
        # 避免请求过快
        await asyncio.sleep(0.2)
    
    # 关闭连接
    await binance_client.disconnect()
    await db_manager.disconnect()
    
    logger.info(f"✅ 历史数据采集完成！共存储 {total_stored} 条数据")
    
    return total_stored


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='批量获取历史 K 线数据')
    parser.add_argument('--symbol', type=str, required=True, help='交易对，如 BTCUSDT')
    parser.add_argument('--interval', type=str, required=True, help='时间间隔，如 1h, 1d, 15m')
    parser.add_argument('--days', type=int, default=30, help='获取多少天的数据（默认 30）')
    parser.add_argument('--limit', type=int, default=1000, help='每次请求的最大数量（默认 1000）')
    
    args = parser.parse_args()
    
    try:
        await fetch_history(
            symbol=args.symbol,
            interval=args.interval,
            days=args.days,
            limit=args.limit
        )
    except Exception as e:
        logger.error(f"❌ 获取历史数据失败：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
