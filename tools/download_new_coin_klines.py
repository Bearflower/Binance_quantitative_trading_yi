#!/usr/bin/env python3
"""
币安永续合约K线数据下载脚本

功能：
1. 从币安API下载新币永续合约的K线数据
2. 支持多个时间频率（1h, 15m, 5m）
3. 支持断点续传和增量更新
4. 自动处理API限流
5. 生成下载报告

使用方法：
    python download_new_coin_klines.py

作者：资深Python工程师
创建时间：2026-05-09
"""
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
import structlog
from tqdm import tqdm

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.binance_api import BinanceClient
from shared.utils import setup_logging


# 配置日志
logger = structlog.get_logger()


class KlineDownloader:
    """K线数据下载器"""

    # 币安API限制
    MAX_KLINES_PER_REQUEST = 1500  # 单次最多返回1500根K线
    REQUESTS_PER_MINUTE = 1200  # 每分钟1200次请求

    # 支持的时间频率
    SUPPORTED_INTERVALS = ['1h', '15m', '5m']

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        data_dir: str = "./data/klines",
        listing_file: str = "./data/new_coin_listings.json"
    ):
        """
        初始化下载器

        Args:
            api_key: 币安API密钥
            api_secret: 币安API密钥
            data_dir: 数据保存目录
            listing_file: 新币列表文件路径
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.data_dir = Path(data_dir)
        self.listing_file = Path(listing_file)

        # 创建数据目录
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 初始化币安客户端
        self.client: Optional[BinanceClient] = None

        # 下载统计
        self.stats = {
            'total_symbols': 0,
            'success_count': 0,
            'failed_count': 0,
            'total_klines': 0,
            'failed_symbols': []
        }

        logger.info(
            "K线下载器初始化完成",
            data_dir=str(self.data_dir),
            listing_file=str(self.listing_file)
        )

    async def __aenter__(self):
        """异步上下文管理器入口"""
        self.client = BinanceClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=False
        )
        await self.client._init_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        if self.client:
            await self.client.close()

    def load_new_coin_listings(self) -> List[Dict]:
        """
        加载新币列表
        
        Returns:
            新币列表
        """
        try:
            with open(self.listing_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 处理不同的JSON格式
            if isinstance(data, dict):
                # 如果是字典格式，提取contracts字段
                listings = data.get('contracts', [])
            elif isinstance(data, list):
                # 如果是列表格式，直接使用
                listings = data
            else:
                logger.error(
                    "新币列表格式错误",
                    data_type=type(data).__name__
                )
                return []
            
            # 转换格式：将onboardDateStr转换为listing_time
            formatted_listings = []
            for listing in listings:
                formatted_listing = {
                    'symbol': listing.get('symbol'),
                    'listing_time': listing.get('onboardDateStr', '').replace(' UTC', ''),  # 移除UTC后缀
                    'description': f"{listing.get('baseAsset', '')}永续合约"
                }
                formatted_listings.append(formatted_listing)
            
            logger.info(
                "加载新币列表成功",
                count=len(formatted_listings),
                file=self.listing_file
            )
            
            return formatted_listings
            
        except Exception as e:
            logger.error(
                "加载新币列表失败",
                error=str(e),
                file=self.listing_file,
                exc_info=True
            )
            return []

    def get_csv_path(self, symbol: str, interval: str) -> Path:
        """
        获取CSV文件路径

        Args:
            symbol: 交易对
            interval: 时间频率

        Returns:
            CSV文件路径
        """
        return self.data_dir / f"{symbol}_{interval}.csv"

    def load_existing_klines(self, symbol: str, interval: str) -> Tuple[List[Dict], Optional[int]]:
        """
        加载已下载的K线数据

        Args:
            symbol: 交易对
            interval: 时间频率

        Returns:
            (K线列表, 最后时间戳)
        """
        csv_path = self.get_csv_path(symbol, interval)

        if not csv_path.exists():
            return [], None

        try:
            klines = []
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    klines.append({
                        'open_time': int(row['open_time']),
                        'open': Decimal(row['open']),
                        'high': Decimal(row['high']),
                        'low': Decimal(row['low']),
                        'close': Decimal(row['close']),
                        'volume': Decimal(row['volume']),
                        'quote_volume': Decimal(row['quote_volume']),
                        'trades': int(row['trades']),
                        'taker_buy_volume': Decimal(row['taker_buy_volume'])
                    })

            if klines:
                last_time = klines[-1]['open_time']
                logger.info(
                    "加载已有K线数据",
                    symbol=symbol,
                    interval=interval,
                    count=len(klines),
                    last_time=datetime.fromtimestamp(last_time / 1000)
                )
                return klines, last_time

            return [], None
        except Exception as e:
            logger.error(
                "加载已有K线数据失败",
                symbol=symbol,
                interval=interval,
                error=str(e)
            )
            return [], None

    def save_klines_to_csv(
        self,
        symbol: str,
        interval: str,
        klines: List[Dict],
        mode: str = 'w'
    ) -> None:
        """
        保存K线数据到CSV文件

        Args:
            symbol: 交易对
            interval: 时间频率
            klines: K线数据列表
            mode: 写入模式 ('w' 覆盖, 'a' 追加)
        """
        if not klines:
            return

        csv_path = self.get_csv_path(symbol, interval)

        fieldnames = [
            'open_time', 'open', 'high', 'low', 'close',
            'volume', 'quote_volume', 'trades', 'taker_buy_volume'
        ]

        try:
            with open(csv_path, mode, encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)

                if mode == 'w':
                    writer.writeheader()

                for kline in klines:
                    writer.writerow({
                        'open_time': kline['open_time'],
                        'open': str(kline['open']),
                        'high': str(kline['high']),
                        'low': str(kline['low']),
                        'close': str(kline['close']),
                        'volume': str(kline['volume']),
                        'quote_volume': str(kline['quote_volume']),
                        'trades': kline['trades'],
                        'taker_buy_volume': str(kline['taker_buy_volume'])
                    })

            logger.info(
                "保存K线数据成功",
                symbol=symbol,
                interval=interval,
                count=len(klines),
                file=str(csv_path)
            )
        except Exception as e:
            logger.error(
                "保存K线数据失败",
                symbol=symbol,
                interval=interval,
                error=str(e)
            )
            raise

    async def download_klines_batch(
        self,
        symbol: str,
        interval: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1500
    ) -> List[Dict]:
        """
        批量下载K线数据

        Args:
            symbol: 交易对
            interval: 时间频率
            start_time: 开始时间戳（毫秒）
            end_time: 结束时间戳（毫秒）
            limit: 数量限制

        Returns:
            K线数据列表
        """
        try:
            # 构建请求参数
            params = {
                'symbol': symbol,
                'interval': interval,
                'limit': limit
            }

            if start_time:
                params['startTime'] = start_time
            if end_time:
                params['endTime'] = end_time

            # 调用币安API
            klines = await self.client.get_klines(
                symbol=symbol,
                interval=interval,
                limit=limit
            )

            # 如果指定了时间范围，需要过滤
            if start_time or end_time:
                filtered = []
                for kline in klines:
                    kline_time = kline['open_time']
                    if start_time and kline_time < start_time:
                        continue
                    if end_time and kline_time >= end_time:
                        continue
                    filtered.append(kline)
                klines = filtered

            return klines

        except Exception as e:
            logger.error(
                "下载K线数据失败",
                symbol=symbol,
                interval=interval,
                error=str(e)
            )
            raise

    async def download_symbol_klines(
        self,
        symbol: str,
        listing_time: datetime,
        intervals: List[str] = None
    ) -> Dict[str, int]:
        """
        下载单个交易对的所有K线数据

        Args:
            symbol: 交易对
            listing_time: 上线时间
            intervals: 时间频率列表

        Returns:
            各频率下载的K线数量
        """
        if intervals is None:
            intervals = self.SUPPORTED_INTERVALS

        result = {}

        # 计算时间范围
        start_time = int(listing_time.timestamp() * 1000)
        end_time = int(datetime.now().timestamp() * 1000)

        for interval in intervals:
            try:
                # 检查已有数据
                existing_klines, last_time = self.load_existing_klines(symbol, interval)

                # 确定下载起始时间
                download_start = last_time + 60000 if last_time else start_time  # 从下一分钟开始

                # 如果已经是最新的，跳过
                if last_time and download_start >= end_time:
                    logger.info(
                        "K线数据已是最新",
                        symbol=symbol,
                        interval=interval
                    )
                    result[interval] = 0
                    continue

                # 下载新数据
                new_klines = []
                current_start = download_start

                # 计算需要下载的批次
                interval_ms = self._get_interval_ms(interval)
                total_duration = end_time - current_start
                estimated_klines = total_duration // interval_ms

                logger.info(
                    "开始下载K线数据",
                    symbol=symbol,
                    interval=interval,
                    start_time=datetime.fromtimestamp(current_start / 1000),
                    estimated_klines=estimated_klines
                )

                # 分批下载
                with tqdm(
                    total=estimated_klines,
                    desc=f"{symbol}_{interval}",
                    unit="根"
                ) as pbar:
                    while current_start < end_time:
                        # 下载一批数据
                        batch = await self.download_klines_batch(
                            symbol=symbol,
                            interval=interval,
                            start_time=current_start,
                            end_time=end_time,
                            limit=self.MAX_KLINES_PER_REQUEST
                        )

                        if not batch:
                            break

                        new_klines.extend(batch)

                        # 更新进度
                        pbar.update(len(batch))

                        # 更新起始时间为最后一批的结束时间
                        current_start = batch[-1]['open_time'] + interval_ms

                        # 如果返回数量少于请求数量，说明已经到头了
                        if len(batch) < self.MAX_KLINES_PER_REQUEST:
                            break

                        # 添加延迟，避免触发限流
                        await asyncio.sleep(0.1)

                # 合并数据
                all_klines = existing_klines + new_klines

                # 去重（按open_time）
                seen = set()
                unique_klines = []
                for kline in all_klines:
                    if kline['open_time'] not in seen:
                        seen.add(kline['open_time'])
                        unique_klines.append(kline)

                # 按时间排序
                unique_klines.sort(key=lambda x: x['open_time'])

                # 保存到文件
                self.save_klines_to_csv(symbol, interval, unique_klines, mode='w')

                result[interval] = len(new_klines)

                logger.info(
                    "下载K线数据完成",
                    symbol=symbol,
                    interval=interval,
                    new_count=len(new_klines),
                    total_count=len(unique_klines)
                )

            except Exception as e:
                logger.error(
                    "下载K线数据失败",
                    symbol=symbol,
                    interval=interval,
                    error=str(e)
                )
                result[interval] = -1

        return result

    def _get_interval_ms(self, interval: str) -> int:
        """
        获取时间频率对应的毫秒数

        Args:
            interval: 时间频率

        Returns:
            毫秒数
        """
        interval_map = {
            '1m': 60000,
            '3m': 180000,
            '5m': 300000,
            '15m': 900000,
            '30m': 1800000,
            '1h': 3600000,
            '2h': 7200000,
            '4h': 14400000,
            '6h': 21600000,
            '8h': 28800000,
            '12h': 43200000,
            '1d': 86400000,
            '3d': 259200000,
            '1w': 604800000,
            '1M': 2592000000
        }
        return interval_map.get(interval, 60000)

    async def download_all(self, intervals: List[str] = None) -> None:
        """
        下载所有新币的K线数据

        Args:
            intervals: 时间频率列表
        """
        if intervals is None:
            intervals = self.SUPPORTED_INTERVALS

        # 加载新币列表
        listings = self.load_new_coin_listings()
        self.stats['total_symbols'] = len(listings)

        logger.info(
            "开始下载所有新币K线数据",
            total_symbols=len(listings),
            intervals=intervals
        )

        # 逐个下载
        for listing in listings:
            symbol = listing.get('symbol')
            listing_time_str = listing.get('listing_time')

            if not symbol or not listing_time_str:
                logger.warning(
                    "新币信息不完整，跳过",
                    listing=listing
                )
                continue

            try:
                # 解析上线时间
                listing_time = datetime.strptime(listing_time_str, "%Y-%m-%d %H:%M:%S")

                # 下载数据
                result = await self.download_symbol_klines(
                    symbol=symbol,
                    listing_time=listing_time,
                    intervals=intervals
                )

                # 统计结果
                if all(count >= 0 for count in result.values()):
                    self.stats['success_count'] += 1
                    self.stats['total_klines'] += sum(max(0, count) for count in result.values())
                else:
                    self.stats['failed_count'] += 1
                    self.stats['failed_symbols'].append(symbol)

            except Exception as e:
                logger.error(
                    "下载新币K线数据失败",
                    symbol=symbol,
                    error=str(e)
                )
                self.stats['failed_count'] += 1
                self.stats['failed_symbols'].append(symbol)

        # 生成报告
        self.generate_report()

    def generate_report(self) -> None:
        """生成下载报告"""
        report_path = self.data_dir / f"download_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

        try:
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write("=" * 60 + "\n")
                f.write("K线数据下载报告\n")
                f.write("=" * 60 + "\n\n")
                f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

                f.write("统计信息:\n")
                f.write(f"  总交易对数: {self.stats['total_symbols']}\n")
                f.write(f"  成功数量: {self.stats['success_count']}\n")
                f.write(f"  失败数量: {self.stats['failed_count']}\n")
                f.write(f"  总K线数量: {self.stats['total_klines']}\n\n")

                if self.stats['failed_symbols']:
                    f.write("失败的交易对:\n")
                    for symbol in self.stats['failed_symbols']:
                        f.write(f"  - {symbol}\n")

                f.write("\n" + "=" * 60 + "\n")

            logger.info(
                "下载报告已生成",
                report_path=str(report_path)
            )
        except Exception as e:
            logger.error(
                "生成下载报告失败",
                error=str(e)
            )


async def main():
    """主函数"""
    # 配置日志
    setup_logging(level="INFO", format="text")

    # 从环境变量读取配置
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')

    if not api_key or not api_secret:
        logger.error("请设置环境变量 BINANCE_API_KEY 和 BINANCE_API_SECRET")
        return

    # 数据目录
    project_root = Path(__file__).parent.parent
    data_dir = project_root / "data" / "klines"
    listing_file = project_root / "data" / "new_coin_listings.json"

    # 创建下载器
    async with KlineDownloader(
        api_key=api_key,
        api_secret=api_secret,
        data_dir=str(data_dir),
        listing_file=str(listing_file)
    ) as downloader:
        # 下载所有数据
        await downloader.download_all(intervals=['1h', '15m', '5m'])


if __name__ == "__main__":
    asyncio.run(main())
