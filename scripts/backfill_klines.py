"""
K线历史数据回填脚本
补充 SOLUSDT、XRPUSDT、TRXUSDT 缺失的 10 天历史数据（2026-07-07 ~ 2026-07-17）

在 K线服务容器内执行：
  docker exec trading_system-kline python /app/scripts/backfill_klines.py
"""
import asyncio
import sys
import os
from datetime import datetime, timedelta, timezone

# 添加项目根目录到路径
sys.path.insert(0, '/app')
sys.path.insert(0, '/app/services/kline_service')

from shared.core.database import db_manager
from shared.utils.logger import get_logger
from core.binance_client import BinanceClient
from core.collector import KlineCollector

logger = get_logger("backfill_klines")

# 回填配置
SYMBOLS = ["SOLUSDT", "XRPUSDT", "TRXUSDT"]
INTERVALS = ["1h", "4h", "1d"]
# 缺失数据时间范围：2026-07-07 00:00:00 UTC ~ 2026-07-17 00:00:00 UTC
START_DATE = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
END_DATE = datetime(2026, 7, 17, 0, 0, 0, tzinfo=timezone.utc)


async def backfill():
    """执行历史数据回填"""
    logger.info("=" * 60)
    logger.info("开始 K线历史数据回填")
    logger.info(f"目标币种: {SYMBOLS}")
    logger.info(f"时间周期: {INTERVALS}")
    logger.info(f"时间范围: {START_DATE} ~ {END_DATE}")
    logger.info("=" * 60)

    # 连接数据库
    await db_manager.connect()
    logger.info("数据库连接成功")

    # 初始化币安客户端
    binance_client = BinanceClient()
    await binance_client.connect()
    logger.info("币安API客户端已连接")

    # 初始化采集器
    collector = KlineCollector(
        binance_client=binance_client,
        db=db_manager,
        symbols=SYMBOLS,
        intervals=INTERVALS,
    )

    start_ms = int(START_DATE.timestamp() * 1000)
    end_ms = int(END_DATE.timestamp() * 1000)

    total_stored = 0

    for symbol in SYMBOLS:
        for interval in INTERVALS:
            logger.info(f"--- 采集 {symbol} {interval} ---")

            # 确保表存在
            await collector.ensure_table(symbol, interval)

            # 采集K线数据
            klines = await collector.collect_klines(
                symbol=symbol,
                interval=interval,
                start_time=start_ms,
                end_time=end_ms,
                limit=1000,  # 最大1000条，10天的1h数据仅240条，完全够用
            )

            if not klines:
                logger.warning(f"{symbol} {interval}: 未获取到数据")
                continue

            logger.info(f"{symbol} {interval}: 获取到 {len(klines)} 条K线")

            # 存储到数据库
            stored = await collector.store_klines(klines)
            total_stored += stored
            logger.info(f"{symbol} {interval}: 存储 {stored} 条（跳过重复）")

    # 关闭连接
    await binance_client.disconnect()
    await db_manager.disconnect()

    logger.info("=" * 60)
    logger.info(f"历史数据回填完成！共存储 {total_stored} 条K线数据")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(backfill())