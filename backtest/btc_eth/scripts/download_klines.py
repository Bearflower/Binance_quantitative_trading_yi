#!/usr/bin/env python3
"""
下载BTCUSDT多时间周期K线数据
下载最近6个月的K线数据并保存到CSV文件
"""
import asyncio
import pandas as pd
from datetime import datetime, timedelta
import aiohttp
import structlog
import os

logger = structlog.get_logger()


class BinanceKlineDownloader:
    """币安K线数据下载器"""

    def __init__(self):
        self.base_url = "https://fapi.binance.com"
        self.session: aiohttp.ClientSession = None

    async def __aenter__(self):
        """异步上下文管理器入口"""
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        if self.session:
            await self.session.close()

    async def download_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int
    ) -> list:
        """
        下载K线数据

        Args:
            symbol: 交易对
            interval: 时间周期
            start_time: 开始时间戳(毫秒)
            end_time: 结束时间戳(毫秒)

        Returns:
            K线数据列表
        """
        all_klines = []
        current_start = start_time

        # 根据时间周期计算每次请求的时间跨度
        interval_ms = {
            '1h': 60 * 60 * 1000,
            '4h': 4 * 60 * 60 * 1000,
            '1d': 24 * 60 * 60 * 1000
        }

        ms_per_request = interval_ms[interval] * 1000  # 每次请求1000根K线

        logger.info(
            f"开始下载{symbol} {interval}K线数据",
            start_time=datetime.fromtimestamp(start_time / 1000),
            end_time=datetime.fromtimestamp(end_time / 1000)
        )

        while current_start < end_time:
            # 计算本次请求的结束时间
            current_end = min(current_start + ms_per_request, end_time)

            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": current_start,
                "endTime": current_end,
                "limit": 1000
            }

            try:
                async with self.session.get(
                    f"{self.base_url}/fapi/v1/klines",
                    params=params
                ) as response:
                    if response.status != 200:
                        logger.error(f"请求失败: {response.status}")
                        break

                    data = await response.json()

                    if not data:
                        logger.warning("没有更多数据")
                        break

                    all_klines.extend(data)
                    logger.info(
                        f"已下载 {len(data)} 根K线",
                        total=len(all_klines),
                        current_end=datetime.fromtimestamp(current_end / 1000)
                    )

                    # 更新下一次请求的开始时间
                    current_start = data[-1][0] + interval_ms[interval]

                    # 避免请求过快
                    await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"下载失败: {e}")
                break

        logger.info(f"下载完成，共 {len(all_klines)} 根K线")
        return all_klines

    def save_to_csv(self, klines: list, interval: str, output_dir: str):
        """
        保存K线数据到CSV文件

        Args:
            klines: K线数据列表
            interval: 时间周期
            output_dir: 输出目录
        """
        if not klines:
            logger.warning("没有数据可保存")
            return

        # 转换为DataFrame
        df = pd.DataFrame(klines, columns=[
            'open_time', 'open_price', 'high_price', 'low_price',
            'close_price', 'volume', 'close_time', 'quote_volume',
            'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])

        # 转换时间戳
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')

        # 只保留需要的列
        df = df[['open_time', 'open_price', 'high_price', 'low_price', 'close_price', 'volume']]

        # 保存到CSV
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, f"btcusdt_{interval}.csv")
        df.to_csv(output_file, index=False)

        logger.info(
            f"数据已保存到 {output_file}",
            count=len(df),
            start=df['open_time'].iloc[0],
            end=df['open_time'].iloc[-1]
        )


async def main():
    """主函数"""
    # 计算时间范围(最近6个月)
    end_time = datetime.now()
    start_time = end_time - timedelta(days=180)

    start_timestamp = int(start_time.timestamp() * 1000)
    end_timestamp = int(end_time.timestamp() * 1000)

    logger.info(
        "开始下载BTCUSDT K线数据",
        start_time=start_time,
        end_time=end_time
    )

    # 输出目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(script_dir, "data")

    async with BinanceKlineDownloader() as downloader:
        # 下载多个时间周期的数据
        intervals = ['1h', '4h', '1d']

        for interval in intervals:
            logger.info(f"\n{'='*60}")
            logger.info(f"开始下载 {interval} 数据")
            logger.info(f"{'='*60}")

            klines = await downloader.download_klines(
                symbol="BTCUSDT",
                interval=interval,
                start_time=start_timestamp,
                end_time=end_timestamp
            )

            downloader.save_to_csv(klines, interval, output_dir)

            logger.info(f"{interval} 数据下载完成\n")

    logger.info("="*60)
    logger.info("所有数据下载完成！")
    logger.info("="*60)


if __name__ == "__main__":
    asyncio.run(main())
