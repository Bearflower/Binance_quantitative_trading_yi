#!/usr/bin/env python3
"""
从币安获取多时间框架 K 线数据（日线、4 小时、1 小时）

用法：
    python fetch_multi_timeframe_data.py --symbols BTCUSDT,ETHUSDT,BNBUSDT --days 180 --output multi_timeframe_data.json
"""

import argparse
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def fetch_binance_klines(
    symbol: str,
    interval: str,
    start_time: datetime,
    end_time: datetime,
    limit: int = 1000
) -> List[Dict[str, Any]]:
    """
    从币安获取历史 K 线数据
    
    Args:
        symbol: 交易对
        interval: K 线周期 (1d, 4h, 1h)
        start_time: 开始时间
        end_time: 结束时间
        limit: 每次请求的最大数量
    
    Returns:
        K 线数据列表
    """
    url = 'https://fapi.binance.com/fapi/v1/klines'
    all_klines = []
    current_start = start_time
    
    while current_start < end_time:
        params = {
            'symbol': symbol,
            'interval': interval,
            'startTime': int(current_start.timestamp() * 1000),
            'endTime': int(end_time.timestamp() * 1000),
            'limit': limit
        }
        
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if not data:
                break
            
            for k in data:
                kline = {
                    'timestamp': datetime.fromtimestamp(k[0] / 1000).isoformat(),
                    'open': str(k[1]),
                    'high': str(k[2]),
                    'low': str(k[3]),
                    'close': str(k[4]),
                    'volume': str(k[5]),
                    'quote_volume': str(k[7]),
                    'trades_count': k[8]
                }
                all_klines.append(kline)
            
            if len(data) < limit:
                break
            
            # 更新起始时间
            last_time = datetime.fromtimestamp(data[-1][0] / 1000)
            current_start = last_time + timedelta(minutes=1)
            
            # 避免 API 频率限制
            time.sleep(0.1)
            
        except requests.exceptions.RequestException as e:
            logger.error(f"获取 {symbol} {interval} 数据失败：{e}")
            time.sleep(1)
            continue
    
    logger.info(f"{symbol} {interval}: 获取 {len(all_klines)} 条 K 线")
    return all_klines


def fetch_multi_timeframe_data(
    symbols: List[str],
    days: int = 180
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """
    获取多时间框架数据（日线、4 小时、1 小时）
    
    Args:
        symbols: 币种列表
        days: 获取天数
    
    Returns:
        {symbol: {'1d': [...], '4h': [...], '1h': [...]}}
    """
    end_time = datetime.now()
    start_time = end_time - timedelta(days=days)
    
    logger.info("=" * 80)
    logger.info("开始获取多时间框架历史数据")
    logger.info(f"币种：{', '.join(symbols)}")
    logger.info(f"时间范围：{start_time} ~ {end_time}")
    logger.info(f"时间框架：日线 (1d)、4 小时 (4h)、1 小时 (1h)")
    logger.info("=" * 80)
    
    multi_timeframe_data = {}
    
    for symbol in symbols:
        logger.info(f"\n正在获取 {symbol} 数据...")
        
        # 获取日线数据
        logger.info(f"  获取日线 (1d)...")
        daily_klines = fetch_binance_klines(
            symbol=symbol,
            interval='1d',
            start_time=start_time,
            end_time=end_time,
            limit=1000
        )
        
        # 获取 4 小时数据
        logger.info(f"  获取 4 小时 (4h)...")
        k4h_klines = fetch_binance_klines(
            symbol=symbol,
            interval='4h',
            start_time=start_time,
            end_time=end_time,
            limit=1000
        )
        
        # 获取 1 小时数据
        logger.info(f"  获取 1 小时 (1h)...")
        k1h_klines = fetch_binance_klines(
            symbol=symbol,
            interval='1h',
            start_time=start_time,
            end_time=end_time,
            limit=1000
        )
        
        multi_timeframe_data[symbol] = {
            '1d': daily_klines,
            '4h': k4h_klines,
            '1h': k1h_klines
        }
        
        logger.info(f"✅ {symbol} 完成：日线{len(daily_klines)}条，4 小时{len(k4h_klines)}条，1 小时{len(k1h_klines)}条")
    
    # 统计信息
    total_klines = 0
    for symbol, data in multi_timeframe_data.items():
        for tf, klines in data.items():
            total_klines += len(klines)
    
    logger.info("\n" + "=" * 80)
    logger.info("多时间框架数据获取完成")
    logger.info(f"总 K 线数：{total_klines}")
    logger.info("=" * 80)
    
    return multi_timeframe_data


def save_to_json(data: Dict, output_file: str):
    """保存数据到 JSON 文件"""
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"数据已保存到：{output_path}")
    logger.info(f"文件大小：{output_path.stat().st_size / 1024 / 1024:.2f} MB")


def main():
    parser = argparse.ArgumentParser(description='从币安获取多时间框架 K 线数据')
    parser.add_argument(
        '--symbols',
        type=str,
        default='BTCUSDT,ETHUSDT,BNBUSDT',
        help='币种列表，逗号分隔 (默认：BTCUSDT,ETHUSDT,BNBUSDT)'
    )
    parser.add_argument(
        '--days',
        type=int,
        default=180,
        help='获取天数 (默认：180)'
    )
    parser.add_argument(
        '--output',
        type=str,
        default='multi_timeframe_data.json',
        help='输出文件名 (默认：multi_timeframe_data.json)'
    )
    
    args = parser.parse_args()
    
    # 解析币种列表
    symbols = [s.strip() for s in args.symbols.split(',')]
    
    # 获取数据
    multi_timeframe_data = fetch_multi_timeframe_data(
        symbols=symbols,
        days=args.days
    )
    
    # 保存数据
    save_to_json(multi_timeframe_data, args.output)
    
    # 打印样本数据
    logger.info("\n样本数据 (BTCUSDT 最新 K 线):")
    for tf in ['1d', '4h', '1h']:
        if multi_timeframe_data['BTCUSDT'][tf]:
            latest = multi_timeframe_data['BTCUSDT'][tf][-1]
            logger.info(f"  {tf}: {latest['timestamp']} O:{latest['open']} H:{latest['high']} L:{latest['low']} C:{latest['close']}")
    
    logger.info(f"\n✅ 完成！数据已保存到 {args.output}")
    logger.info("可以使用以下命令进行回测:")
    logger.info(f"  python scripts/run_backtest_v5.py --data {args.output} --capital 500")


if __name__ == '__main__':
    main()
