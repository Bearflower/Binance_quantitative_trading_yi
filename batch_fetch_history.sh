#!/bin/bash

# ============================================
# 批量补全历史K线数据
# ============================================

echo "============================================="
echo "开始批量补全历史K线数据"
echo "============================================="

# 定义需要补全的交易对和时间周期
SYMBOLS=("XRPUSDT" "TRXUSDT")
INTERVALS=("1h" "4h" "1d")
DAYS=7

# 循环补全每个交易对的每个时间周期
for symbol in "${SYMBOLS[@]}"; do
    for interval in "${INTERVALS[@]}"; do
        echo ""
        echo "============================================="
        echo "补全 $symbol $interval 历史数据（$DAYS 天）"
        echo "============================================="
        
        # 执行补全脚本
        docker exec trading_system-kline python -c "
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, '/app')

from shared.core.database import db_manager
from core.binance_client import BinanceClient
from core.collector import KlineCollector
from shared.utils.logger import get_logger

logger = get_logger(__name__)

async def fetch_history(symbol, interval, days):
    logger.info(f'开始获取 {symbol} {interval} 的历史数据（{days}天）...')
    
    # 连接数据库
    await db_manager.connect()
    logger.info('✅ 数据库连接成功')
    
    # 初始化客户端
    binance_client = BinanceClient()
    await binance_client.connect()
    logger.info('✅ 币安 API 连接成功')
    
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
    
    logger.info(f'时间范围：{datetime.fromtimestamp(start_time/1000)} 到 {datetime.fromtimestamp(end_time/1000)}')
    
    # 分批获取数据
    total_stored = 0
    current_start = start_time
    limit = 1000
    
    while current_start < end_time:
        # 计算本次获取的结束时间
        batch_end = min(current_start + (limit * 60 * 60 * 1000), end_time)
        
        logger.info(f'获取数据：{datetime.fromtimestamp(current_start/1000)} 到 {datetime.fromtimestamp(batch_end/1000)}')
        
        # 从币安获取 K 线数据
        klines = await collector.collect_klines(
            symbol,
            interval,
            start_time=current_start,
            end_time=batch_end
        )
        
        if not klines:
            logger.warning('未获取到数据')
            break
        
        logger.info(f'获取到 {len(klines)} 条数据')
        
        # 存储数据
        stored = await collector.store_klines(klines)
        logger.info(f'✅ 存储 {stored} 条数据')
        
        total_stored += stored
        
        # 更新起始时间
        if len(klines) < limit:
            break
        
        # 设置下一批的起始时间
        current_start = klines[-1].close_time + 1
        
        # 避免请求过快
        await asyncio.sleep(0.2)
    
    # 关闭连接
    await binance_client.disconnect()
    await db_manager.disconnect()
    
    logger.info(f'✅ 历史数据采集完成！共存储 {total_stored} 条数据')
    
    return total_stored

# 执行
asyncio.run(fetch_history('$symbol', '$interval', $DAYS))
"
        
        if [ $? -eq 0 ]; then
            echo "✅ $symbol $interval 历史数据补全成功"
        else
            echo "❌ $symbol $interval 历史数据补全失败"
        fi
        
        # 等待一段时间，避免请求过快
        sleep 2
    done
done

echo ""
echo "============================================="
echo "✅ 批量补全完成！"
echo "============================================="