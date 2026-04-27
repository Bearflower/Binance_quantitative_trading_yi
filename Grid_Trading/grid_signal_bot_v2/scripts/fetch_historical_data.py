"""
从服务器数据库获取历史K线数据
用于长期回测
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any
import json
import subprocess

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s'
)
logger = logging.getLogger(__name__)


class HistoricalDataFetcher:
    """历史数据获取器"""
    
    def __init__(
        self,
        server_ip: str = "43.156.242.184",
        server_user: str = "root"
    ):
        """
        初始化历史数据获取器
        
        Args:
            server_ip: 服务器IP
            server_user: 服务器用户名
        """
        self.server_ip = server_ip
        self.server_user = server_user
    
    def fetch_klines_from_db(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1h",
        days: int = 90
    ) -> List[Dict[str, Any]]:
        """
        从数据库获取K线数据
        
        Args:
            symbol: 交易对
            interval: 时间间隔
            days: 获取天数
            
        Returns:
            K线数据列表
        """
        logger.info(f"📊 从数据库获取 {symbol} {interval} 最近 {days} 天的K线数据...")
        
        # 计算时间范围
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        start_timestamp = int(start_time.timestamp() * 1000)
        end_timestamp = int(end_time.timestamp() * 1000)
        
        # SQL 查询
        sql_query = f"""
            SELECT 
                open_time,
                open_price,
                high_price,
                low_price,
                close_price,
                volume,
                close_time
            FROM klines
            WHERE symbol = '{symbol}'
              AND interval = '{interval}'
              AND open_time >= {start_timestamp}
              AND open_time <= {end_timestamp}
            ORDER BY open_time ASC;
        """
        
        # 通过 SSH 执行查询
        ssh_command = [
            "ssh",
            f"{self.server_user}@{self.server_ip}",
            f"docker exec common_service_postgres psql -U binance -d binance_data -t -A -F'|' -c \"{sql_query}\""
        ]
        
        try:
            logger.info(f"执行查询...")
            result = subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode != 0:
                logger.error(f"查询失败：{result.stderr}")
                logger.info("尝试使用本地数据...")
                return []
            
            # 解析结果
            klines = []
            lines = result.stdout.strip().split('\n')
            
            for line in lines:
                if not line:
                    continue
                
                parts = line.split('|')
                if len(parts) >= 7:
                    kline = {
                        'open_time': int(parts[0]),
                        'open_price': float(parts[1]),
                        'high_price': float(parts[2]),
                        'low_price': float(parts[3]),
                        'close_price': float(parts[4]),
                        'volume': float(parts[5]),
                        'close_time': int(parts[6])
                    }
                    klines.append(kline)
            
            logger.info(f"✅ 获取到 {len(klines)} 根K线数据")
            return klines
            
        except subprocess.TimeoutExpired:
            logger.error("查询超时")
            return []
        except Exception as e:
            logger.error(f"查询失败：{e}")
            return []
    
    def fetch_klines_from_api(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1h",
        days: int = 90
    ) -> List[Dict[str, Any]]:
        """
        从K线服务API获取历史数据（分批获取）
        
        Args:
            symbol: 交易对
            interval: 时间间隔
            days: 获取天数
            
        Returns:
            K线数据列表
        """
        logger.info(f"📊 从K线服务API获取 {symbol} {interval} 最近 {days} 天的K线数据...")
        
        import requests
        
        # K线服务地址
        kline_url = "http://43.156.242.184:8765/api/v1/klines/historical"
        
        # 计算时间范围
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        start_timestamp = int(start_time.timestamp() * 1000)
        end_timestamp = int(end_time.timestamp() * 1000)
        
        all_klines = []
        current_start = start_timestamp
        
        try:
            # 分批获取数据（每次最多1000根）
            while current_start < end_timestamp:
                params = {
                    "symbol": symbol,
                    "interval": interval,
                    "start_time": current_start,
                    "end_time": end_timestamp,
                    "limit": 1000
                }
                
                response = requests.get(kline_url, params=params, timeout=30)
                response.raise_for_status()
                
                result = response.json()
                
                if result.get("code") == 0:
                    klines = result.get("data", [])
                    if not klines:
                        break
                    
                    all_klines.extend(klines)
                    
                    # 更新起始时间
                    last_time = klines[-1].get('open_time', 0)
                    if last_time <= current_start:
                        break
                    current_start = last_time + 1
                    
                    logger.info(f"已获取 {len(all_klines)} 根K线...")
                else:
                    logger.error(f"获取失败：{result.get('message')}")
                    break
            
            logger.info(f"✅ 总共获取到 {len(all_klines)} 根K线数据")
            return all_klines
            
        except Exception as e:
            logger.error(f"获取失败：{e}")
            return []
    
    def save_to_file(
        self,
        klines: List[Dict[str, Any]],
        output_file: str
    ):
        """
        保存K线数据到文件
        
        Args:
            klines: K线数据列表
            output_file: 输出文件路径
        """
        if not klines:
            logger.error("没有数据可保存")
            return
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(klines, f, indent=2)
        
        logger.info(f"✅ 数据已保存到 {output_file}")
    
    def load_from_file(self, input_file: str) -> List[Dict[str, Any]]:
        """
        从文件加载K线数据
        
        Args:
            input_file: 输入文件路径
            
        Returns:
            K线数据列表
        """
        if not os.path.exists(input_file):
            logger.error(f"文件不存在：{input_file}")
            return []
        
        with open(input_file, 'r', encoding='utf-8') as f:
            klines = json.load(f)
        
        logger.info(f"✅ 从文件加载 {len(klines)} 根K线数据")
        return klines


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='从服务器获取历史K线数据')
    parser.add_argument('--symbol', type=str, default='BTCUSDT', help='交易对')
    parser.add_argument('--interval', type=str, default='1h', help='时间间隔')
    parser.add_argument('--days', type=int, default=90, help='获取天数')
    parser.add_argument('--output', type=str, default='historical_klines.json', help='输出文件')
    parser.add_argument('--method', type=str, default='api', choices=['db', 'api'], help='获取方式')
    
    args = parser.parse_args()
    
    # 创建获取器
    fetcher = HistoricalDataFetcher()
    
    # 获取数据
    if args.method == 'db':
        klines = fetcher.fetch_klines_from_db(
            symbol=args.symbol,
            interval=args.interval,
            days=args.days
        )
    else:
        klines = fetcher.fetch_klines_from_api(
            symbol=args.symbol,
            interval=args.interval,
            days=args.days
        )
    
    # 保存数据
    if klines:
        fetcher.save_to_file(klines, args.output)


if __name__ == "__main__":
    main()
