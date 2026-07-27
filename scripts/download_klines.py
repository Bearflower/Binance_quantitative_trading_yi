#!/usr/bin/env python3
"""
下载ETHUSDT永续合约K线数据
支持多时间周期，可指定时间范围
支持代理配置
"""
import pandas as pd
from datetime import datetime, timedelta
import requests
import structlog
import os
import sys
import time

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = structlog.get_logger()


class BinanceKlineDownloader:
    """币安K线数据下载器"""

    def __init__(self, proxy: str = None):
        """
        初始化下载器

        Args:
            proxy: 代理地址，例如 "http://127.0.0.1:7890"
        """
        self.base_url = "https://fapi.binance.com"
        self.proxy = proxy
        self.session = requests.Session()

        # 设置代理
        if proxy:
            self.session.proxies = {
                'http': proxy,
                'https': proxy
            }
            logger.info(f"使用代理: {proxy}")

    def download_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        max_retries: int = 3
    ) -> list:
        """
        下载K线数据

        Args:
            symbol: 交易对
            interval: 时间周期
            start_time: 开始时间戳(毫秒)
            end_time: 结束时间戳(毫秒)
            max_retries: 最大重试次数

        Returns:
            K线数据列表
        """
        all_klines = []
        current_start = start_time

        # 根据时间周期计算每次请求的时间跨度
        interval_ms = {
            '15m': 15 * 60 * 1000,
            '1h': 60 * 60 * 1000,
            '4h': 4 * 60 * 60 * 1000,
            '1d': 24 * 60 * 60 * 1000
        }

        if interval not in interval_ms:
            raise ValueError(f"不支持的时间周期: {interval}")

        ms_per_request = interval_ms[interval] * 1000  # 每次请求1000根K线

        logger.info(
            f"开始下载{symbol} {interval}K线数据",
            start_time=datetime.fromtimestamp(start_time / 1000),
            end_time=datetime.fromtimestamp(end_time / 1000)
        )

        request_count = 0
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

            # 重试机制
            for retry in range(max_retries):
                try:
                    response = self.session.get(
                        f"{self.base_url}/fapi/v1/klines",
                        params=params,
                        timeout=30,
                        verify=True
                    )

                    if response.status_code != 200:
                        logger.error(f"请求失败: {response.status_code}, {response.text}")
                        if retry < max_retries - 1:
                            wait_time = 2 ** retry  # 指数退避
                            logger.info(f"等待 {wait_time} 秒后重试...")
                            time.sleep(wait_time)
                            continue
                        break

                    data = response.json()

                    if not data:
                        logger.warning("没有更多数据")
                        break

                    all_klines.extend(data)
                    request_count += 1

                    logger.info(
                        f"已下载 {len(data)} 根K线",
                        total=len(all_klines),
                        current_end=datetime.fromtimestamp(current_end / 1000),
                        request_count=request_count
                    )

                    # 更新下一次请求的开始时间
                    current_start = data[-1][0] + interval_ms[interval]

                    # 避免请求过快（币安限制每分钟1200次请求）
                    time.sleep(0.1)
                    break

                except requests.exceptions.SSLError as e:
                    logger.error(f"SSL错误: {e}")
                    if retry < max_retries - 1:
                        wait_time = 2 ** retry
                        logger.info(f"等待 {wait_time} 秒后重试...")
                        time.sleep(wait_time)
                        continue
                    break

                except requests.exceptions.ConnectionError as e:
                    logger.error(f"连接错误: {e}")
                    if retry < max_retries - 1:
                        wait_time = 2 ** retry
                        logger.info(f"等待 {wait_time} 秒后重试...")
                        time.sleep(wait_time)
                        continue
                    break

                except Exception as e:
                    logger.error(f"下载失败: {e}", exc_info=True)
                    if retry < max_retries - 1:
                        wait_time = 2 ** retry
                        logger.info(f"等待 {wait_time} 秒后重试...")
                        time.sleep(wait_time)
                        continue
                    break

        logger.info(f"下载完成，共 {len(all_klines)} 根K线")
        return all_klines

    def save_to_csv(self, klines: list, symbol: str, interval: str, output_dir: str):
        """
        保存K线数据到CSV文件

        Args:
            klines: K线数据列表
            symbol: 交易对
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

        # 重命名列以符合标准命名
        df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']

        # 保存到CSV
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, f"{symbol.lower()}_{interval}.csv")
        df.to_csv(output_file, index=False)

        logger.info(
            f"数据已保存到 {output_file}",
            count=len(df),
            start=df['timestamp'].iloc[0],
            end=df['timestamp'].iloc[-1]
        )

        return output_file


def main():
    """主函数"""
    # 设置时间范围：2025-11-09 到 2026-05-09
    start_date = datetime(2025, 11, 9)
    end_date = datetime(2026, 5, 9)

    start_timestamp = int(start_date.timestamp() * 1000)
    end_timestamp = int(end_date.timestamp() * 1000)

    logger.info(
        "开始下载ETHUSDT永续合约K线数据",
        start_time=start_date,
        end_time=end_date
    )

    # 输出目录
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_dir = os.path.join(project_root, "data", "klines")

    # 交易对和时间周期
    symbol = "ETHUSDT"
    intervals = ['15m', '1h', '4h']

    # 代理配置（如果需要）
    # 如果在中国大陆，请取消注释并设置您的代理地址
    # proxy = "http://127.0.0.1:7890"  # 例如：Clash代理
    proxy = None  # 不使用代理

    downloader = BinanceKlineDownloader(proxy=proxy)

    for interval in intervals:
        logger.info(f"\n{'='*60}")
        logger.info(f"开始下载 {symbol} {interval} 数据")
        logger.info(f"{'='*60}")

        klines = downloader.download_klines(
            symbol=symbol,
            interval=interval,
            start_time=start_timestamp,
            end_time=end_timestamp
        )

        output_file = downloader.save_to_csv(klines, symbol, interval, output_dir)

        logger.info(f"{interval} 数据下载完成，保存到: {output_file}\n")

    logger.info("="*60)
    logger.info("所有数据下载完成！")
    logger.info(f"数据保存位置: {output_dir}")
    logger.info("="*60)


if __name__ == "__main__":
    main()
