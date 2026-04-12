#!/usr/bin/env python3
"""
从币安获取历史 K 线数据并保存为 JSON 文件

用法：
    python fetch_history_data.py --symbols BTCUSDT,ETHUSDT,BNBUSDT --interval 1h --days 180 --output historical_data.json
"""

import argparse
import json
import logging
import sys
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
    interval: str = '1h',
    start_time: datetime = None,
    end_time: datetime = None,
    limit: int = 1000
) -> List[Dict[str, Any]]:
    """
    从币安获取历史 K 线数据
    
    Args:
        symbol: 交易对 (如 'BTCUSDT')
        interval: K 线周期 (1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w, 1M)
        start_time: 开始时间
        end_time: 结束时间
        limit: 每次请求的最大数量（最多 1000）
    
    Returns:
        K 线数据列表
    """
    url = 'https://fapi.binance.com/fapi/v1/klines'
    
    # 如果没有指定时间，默认获取最近的数据
    if not end_time:
        end_time = datetime.now()
    if not start_time:
        # 默认获取 1000 条数据
        start_time = end_time - timedelta(hours=limit)
    
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
            
            # 更新起始时间，获取下一批数据
            if len(data) < limit:
                break
            
            # 设置下一批数据的开始时间
            last_time = datetime.fromtimestamp(data[-1][0] / 1000)
            current_start = last_time + timedelta(minutes=1)
            
            logger.info(f"{symbol}: 已获取 {len(all_klines)} 条 K 线")
            
        except requests.exceptions.RequestException as e:
            logger.error(f"获取 {symbol} 数据失败：{e}")
            break
    
    logger.info(f"{symbol}: 总共获取 {len(all_klines)} 条 K 线")
    return all_klines


def fetch_multiple_symbols(
    symbols: List[str],
    interval: str = '1h',
    days: int = 180
) -> Dict[str, List[Dict[str, Any]]]:
    """
    获取多个币种的历史数据
    
    Args:
        symbols: 币种列表
        interval: K 线周期
        days: 获取天数
    
    Returns:
        {symbol: [kline_data]}
    """
    end_time = datetime.now()
    start_time = end_time - timedelta(days=days)
    
    logger.info("=" * 60)
    logger.info("开始获取历史数据")
    logger.info(f"币种：{', '.join(symbols)}")
    logger.info(f"周期：{interval}")
    logger.info(f"天数：{days}天")
    logger.info(f"时间范围：{start_time} ~ {end_time}")
    logger.info("=" * 60)
    
    historical_data = {}
    
    for symbol in symbols:
        logger.info(f"\n正在获取 {symbol} 数据...")
        klines = fetch_binance_klines(
            symbol=symbol,
            interval=interval,
            start_time=start_time,
            end_time=end_time,
            limit=1000
        )
        historical_data[symbol] = klines
        logger.info(f"✅ {symbol} 获取完成：{len(klines)} 条")
    
    # 统计信息
    total_klines = sum(len(data) for data in historical_data.values())
    logger.info("\n" + "=" * 60)
    logger.info("数据获取完成")
    logger.info(f"总 K 线数：{total_klines}")
    logger.info("=" * 60)
    
    return historical_data


def save_to_json(data: Dict[str, List[Dict[str, Any]]], output_file: str):
    """保存数据到 JSON 文件"""
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"数据已保存到：{output_path}")
    logger.info(f"文件大小：{output_path.stat().st_size / 1024:.2f} KB")


def main():
    parser = argparse.ArgumentParser(description='从币安获取历史 K 线数据')
    parser.add_argument(
        '--symbols',
        type=str,
        default='BTCUSDT,ETHUSDT,BNBUSDT',
        help='币种列表，逗号分隔 (默认：BTCUSDT,ETHUSDT,BNBUSDT)'
    )
    parser.add_argument(
        '--interval',
        type=str,
        default='1h',
        help='K 线周期 (默认：1h)'
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
        default='historical_data.json',
        help='输出文件名 (默认：historical_data.json)'
    )
    
    args = parser.parse_args()
    
    # 解析币种列表
    symbols = [s.strip() for s in args.symbols.split(',')]
    
    # 获取数据
    historical_data = fetch_multiple_symbols(
        symbols=symbols,
        interval=args.interval,
        days=args.days
    )
    
    # 保存数据
    save_to_json(historical_data, args.output)
    
    # 打印样本数据
    logger.info("\n样本数据 (BTCUSDT 前 3 条):")
    for i, kline in enumerate(historical_data['BTCUSDT'][:3], 1):
        logger.info(f"{i}. {kline['timestamp']} O:{kline['open']} H:{kline['high']} L:{kline['low']} C:{kline['close']}")
    
    logger.info(f"\n✅ 完成！数据已保存到 {args.output}")
    logger.info("可以使用以下命令进行回测:")
    logger.info(f"  python run_backtest.py --data {args.output} --capital 500")


if __name__ == '__main__':
    main()
