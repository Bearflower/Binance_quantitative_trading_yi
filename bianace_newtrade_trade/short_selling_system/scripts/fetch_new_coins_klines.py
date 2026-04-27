#!/usr/bin/env python3
"""
从服务器获取新上线永续合约的 1 小时 K 线数据

功能：
1. 获取最近 100 个新上线的永续合约
2. 获取每个合约的 1 小时 K 线数据（最多 500 根）
3. 保存为回测数据格式

使用方法:
    python fetch_new_coins_klines.py --count 100 --output data/new_coins_backtest.json
"""

import json
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class BinanceNewCoinsFetcher:
    """币安新上线永续合约数据获取器"""
    
    def __init__(self):
        """初始化获取器"""
        self.base_url = "https://fapi.binance.com"
        self.headers = {
            'User-Agent': 'Mozilla/5.0',
            'X-MBX-APIKEY': ''  # 可选，有 API Key 可以提高限流
        }
    
    def get_all_symbols(self) -> List[Dict]:
        """获取所有永续合约信息"""
        import requests
        
        url = f"{self.base_url}/fapi/v1/exchangeInfo"
        
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            symbols = data.get('symbols', [])
            
            # 过滤永续合约（USDT 结算，交易状态）
            perpetual_symbols = [
                s for s in symbols 
                if s.get('contractType') == 'PERPETUAL' 
                and s.get('quoteAsset') == 'USDT'
                and s.get('status') in ['TRADING', 'PRE_TRADING']
            ]
            
            logger.info(f"获取到 {len(perpetual_symbols)} 个永续合约")
            return perpetual_symbols
            
        except Exception as e:
            logger.error(f"获取合约信息失败：{e}")
            return []
    
    def get_new_listings(self, count: int = 100) -> List[Dict]:
        """
        获取新上线的永续合约
        
        Args:
            count: 获取数量（默认 100 个）
            
        Returns:
            按上线时间排序的合约列表（最新的在前）
        """
        all_symbols = self.get_all_symbols()
        
        # 按 listTime 排序（最新的在前）
        symbols_with_listtime = [
            s for s in all_symbols 
            if s.get('listTime')
        ]
        
        sorted_symbols = sorted(
            symbols_with_listtime, 
            key=lambda x: x['listTime'], 
            reverse=True
        )
        
        # 取前 N 个
        new_listings = sorted_symbols[:count]
        
        logger.info(f"获取到 {len(new_listings)} 个新上线永续合约")
        
        # 打印上线时间信息
        if new_listings:
            newest = datetime.fromtimestamp(new_listings[0]['listTime'] / 1000)
            oldest = datetime.fromtimestamp(new_listings[-1]['listTime'] / 1000)
            logger.info(f"时间范围：{oldest} ~ {newest}")
        
        return new_listings
    
    def get_klines(self, symbol: str, interval: str = '1h', limit: int = 500) -> List[Dict]:
        """
        获取 K 线数据
        
        Args:
            symbol: 币种符号
            interval: K 线周期（默认 1h）
            limit: 获取数量（默认 500）
            
        Returns:
            K 线数据列表
        """
        import requests
        
        url = f"{self.base_url}/fapi/v1/klines"
        params = {
            'symbol': symbol,
            'interval': interval,
            'limit': limit
        }
        
        try:
            response = requests.get(url, params=params, headers=self.headers, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            # 转换为字典格式
            klines = []
            for k in data:
                klines.append({
                    'timestamp': k[0],
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5])
                })
            
            return klines
            
        except Exception as e:
            logger.error(f"获取 {symbol} K 线失败：{e}")
            return []
    
    def fetch_all_data(self, count: int = 100, kline_limit: int = 500) -> Dict:
        """
        获取完整数据
        
        Args:
            count: 获取多少个新币
            kline_limit: 每个币获取多少根 K 线
            
        Returns:
            完整的回测数据
        """
        logger.info(f"开始获取 {count} 个新上线永续合约数据...")
        
        # 1. 获取新上线合约列表
        new_listings = self.get_new_listings(count)
        
        if not new_listings:
            logger.error("没有获取到新上线合约")
            return {}
        
        # 2. 逐个获取 K 线数据
        all_data = {}
        
        for i, symbol_info in enumerate(new_listings, 1):
            symbol = symbol_info['symbol']
            list_time = symbol_info.get('listTime', 0)
            
            logger.info(f"[{i}/{count}] 获取 {symbol} 数据...")
            
            # 获取 K 线
            klines = self.get_klines(symbol, interval='1h', limit=kline_limit)
            
            if not klines:
                logger.warning(f"⚠️  {symbol} 获取 K 线失败，跳过")
                continue
            
            logger.info(f"✅ {symbol} 获取 {len(klines)} 根 K 线")
            
            # 构建数据
            all_data[symbol] = {
                'symbol_info': {
                    'symbol': symbol,
                    'baseAsset': symbol_info.get('baseAsset', ''),
                    'quoteAsset': symbol_info.get('quoteAsset', 'USDT'),
                    'listTime': list_time,
                    'listDate': datetime.fromtimestamp(list_time / 1000).isoformat() if list_time else None,
                    'contractType': symbol_info.get('contractType', 'PERPETUAL')
                },
                '1h': klines
            }
            
            # 避免请求过快
            if i % 10 == 0:
                time.sleep(1)
        
        logger.info(f"✅ 数据获取完成，共 {len(all_data)} 个币种")
        
        return all_data


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='获取新上线永续合约 K 线数据')
    parser.add_argument('--count', type=int, default=100,
                        help='获取多少个新上线合约（默认 100）')
    parser.add_argument('--limit', type=int, default=500,
                        help='每个合约获取多少根 1 小时 K 线（默认 500）')
    parser.add_argument('--output', type=str, default='data/new_coins_backtest.json',
                        help='输出文件路径')
    
    args = parser.parse_args()
    
    # 创建获取器
    fetcher = BinanceNewCoinsFetcher()
    
    # 获取数据
    all_data = fetcher.fetch_all_data(
        count=args.count,
        kline_limit=args.limit
    )
    
    if not all_data:
        logger.error("❌ 没有获取到数据")
        return
    
    # 保存数据
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)
    
    logger.info(f"💾 数据已保存到：{output_path}")
    
    # 打印统计
    print("\n" + "=" * 80)
    print("数据获取统计")
    print("=" * 80)
    print(f"\n📊 币种数量：{len(all_data)} 个")
    
    # 统计 K 线数量
    total_klines = sum(len(data.get('1h', [])) for data in all_data.values())
    avg_klines = total_klines / len(all_data) if all_data else 0
    
    print(f"📈 K 线总数：{total_klines} 根")
    print(f"📉 平均每个币：{avg_klines:.1f} 根")
    
    # 按上线时间排序
    sorted_coins = sorted(
        all_data.items(),
        key=lambda x: x[1]['symbol_info'].get('listTime', 0),
        reverse=True
    )
    
    print(f"\n🕐 最新上线的 10 个币种:")
    for symbol, data in sorted_coins[:10]:
        list_date = data['symbol_info'].get('listDate', 'Unknown')
        kline_count = len(data.get('1h', []))
        print(f"  {symbol:15s} | 上线时间：{list_date[:16]:16s} | K 线：{kline_count:4d} 根")
    
    print("\n" + "=" * 80 + "\n")


if __name__ == '__main__':
    main()
