#!/usr/bin/env python3
"""
性能测试脚本

对比并发数据获取前后的性能差异。

测试内容：
1. 串行获取数据性能
2. 并发获取数据性能
3. 缓存命中性能
4. 性能对比报告

版本: v1.0.0
创建时间: 2026-04-27
"""

import time
import logging
from datetime import datetime
from typing import Dict, Any

from core.data.fetcher import MarketDataFetcher

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class PerformanceTester:
    """性能测试器"""

    def __init__(self):
        """初始化性能测试器"""
        self.results = {}

    def test_serial_fetch(self, symbols: list, iterations: int = 3) -> Dict[str, Any]:
        """
        测试串行获取性能

        Args:
            symbols: 交易对列表
            iterations: 测试次数

        Returns:
            测试结果
        """
        logger.info("=" * 60)
        logger.info("开始测试串行获取性能")
        logger.info("=" * 60)

        times = []
        fetcher = MarketDataFetcher(
            cache_duration_hours=1,
            max_workers=1,
            enable_concurrent=False
        )

        for i in range(iterations):
            # 清除缓存
            fetcher.clear_cache()

            # 记录开始时间
            start_time = time.time()

            try:
                # 获取数据
                data = fetcher.fetch_market_data(symbols)

                # 记录结束时间
                end_time = time.time()
                elapsed = end_time - start_time

                times.append(elapsed)
                logger.info(f"第 {i+1} 次测试完成，耗时: {elapsed:.2f} 秒，获取 {len(data)} 个交易对")

            except Exception as e:
                logger.error(f"第 {i+1} 次测试失败: {str(e)}")
                times.append(None)

        # 计算平均时间
        valid_times = [t for t in times if t is not None]
        avg_time = sum(valid_times) / len(valid_times) if valid_times else 0

        result = {
            'mode': '串行',
            'iterations': iterations,
            'times': times,
            'avg_time': avg_time,
            'min_time': min(valid_times) if valid_times else 0,
            'max_time': max(valid_times) if valid_times else 0,
            'success_rate': len(valid_times) / iterations
        }

        self.results['serial'] = result
        return result

    def test_concurrent_fetch(self, symbols: list, iterations: int = 3) -> Dict[str, Any]:
        """
        测试并发获取性能

        Args:
            symbols: 交易对列表
            iterations: 测试次数

        Returns:
            测试结果
        """
        logger.info("=" * 60)
        logger.info("开始测试并发获取性能")
        logger.info("=" * 60)

        times = []
        fetcher = MarketDataFetcher(
            cache_duration_hours=1,
            max_workers=5,
            enable_concurrent=True
        )

        for i in range(iterations):
            # 清除缓存
            fetcher.clear_cache()

            # 记录开始时间
            start_time = time.time()

            try:
                # 获取数据
                data = fetcher.fetch_market_data(symbols)

                # 记录结束时间
                end_time = time.time()
                elapsed = end_time - start_time

                times.append(elapsed)
                logger.info(f"第 {i+1} 次测试完成，耗时: {elapsed:.2f} 秒，获取 {len(data)} 个交易对")

            except Exception as e:
                logger.error(f"第 {i+1} 次测试失败: {str(e)}")
                times.append(None)

        # 计算平均时间
        valid_times = [t for t in times if t is not None]
        avg_time = sum(valid_times) / len(valid_times) if valid_times else 0

        result = {
            'mode': '并发',
            'iterations': iterations,
            'times': times,
            'avg_time': avg_time,
            'min_time': min(valid_times) if valid_times else 0,
            'max_time': max(valid_times) if valid_times else 0,
            'success_rate': len(valid_times) / iterations
        }

        self.results['concurrent'] = result
        return result

    def test_cache_performance(self, symbols: list, iterations: int = 5) -> Dict[str, Any]:
        """
        测试缓存性能

        Args:
            symbols: 交易对列表
            iterations: 测试次数

        Returns:
            测试结果
        """
        logger.info("=" * 60)
        logger.info("开始测试缓存性能")
        logger.info("=" * 60)

        times = []
        fetcher = MarketDataFetcher(
            cache_duration_hours=1,
            max_workers=5,
            enable_concurrent=True
        )

        # 第一次获取（无缓存）
        start_time = time.time()
        try:
            data = fetcher.fetch_market_data(symbols)
            first_time = time.time() - start_time
            logger.info(f"首次获取（无缓存）耗时: {first_time:.2f} 秒")
        except Exception as e:
            logger.error(f"首次获取失败: {str(e)}")
            first_time = None

        # 后续获取（有缓存）
        for i in range(iterations):
            start_time = time.time()

            try:
                data = fetcher.fetch_market_data(symbols)
                elapsed = time.time() - start_time
                times.append(elapsed)
                logger.info(f"第 {i+1} 次缓存获取耗时: {elapsed:.4f} 秒")

            except Exception as e:
                logger.error(f"第 {i+1} 次缓存获取失败: {str(e)}")
                times.append(None)

        # 计算缓存命中平均时间
        valid_times = [t for t in times if t is not None]
        avg_cache_time = sum(valid_times) / len(valid_times) if valid_times else 0

        # 获取缓存统计
        cache_stats = fetcher.get_cache_stats()

        result = {
            'first_fetch_time': first_time,
            'cache_fetch_times': times,
            'avg_cache_time': avg_cache_time,
            'cache_stats': cache_stats,
            'speedup': first_time / avg_cache_time if first_time and avg_cache_time > 0 else 0
        }

        self.results['cache'] = result
        return result

    def generate_report(self) -> str:
        """
        生成性能测试报告

        Returns:
            报告文本
        """
        logger.info("=" * 60)
        logger.info("生成性能测试报告")
        logger.info("=" * 60)

        report = []
        report.append("\n" + "=" * 60)
        report.append("性能测试报告")
        report.append("=" * 60)
        report.append(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")

        # 串行 vs 并发对比
        if 'serial' in self.results and 'concurrent' in self.results:
            serial = self.results['serial']
            concurrent = self.results['concurrent']

            report.append("【串行 vs 并发性能对比】")
            report.append("-" * 60)
            report.append(f"串行平均耗时: {serial['avg_time']:.2f} 秒")
            report.append(f"并发平均耗时: {concurrent['avg_time']:.2f} 秒")

            if serial['avg_time'] > 0 and concurrent['avg_time'] > 0:
                speedup = serial['avg_time'] / concurrent['avg_time']
                improvement = (serial['avg_time'] - concurrent['avg_time']) / serial['avg_time'] * 100
                report.append(f"性能提升: {speedup:.2f}x ({improvement:.1f}%)")

            report.append(f"串行成功率: {serial['success_rate']*100:.1f}%")
            report.append(f"并发成功率: {concurrent['success_rate']*100:.1f}%")
            report.append("")

        # 缓存性能
        if 'cache' in self.results:
            cache = self.results['cache']

            report.append("【缓存性能测试】")
            report.append("-" * 60)
            if cache['first_fetch_time']:
                report.append(f"首次获取（无缓存）: {cache['first_fetch_time']:.2f} 秒")
            report.append(f"缓存命中平均耗时: {cache['avg_cache_time']:.4f} 秒")

            if cache['speedup'] > 0:
                report.append(f"缓存加速比: {cache['speedup']:.0f}x")

            if cache['cache_stats']:
                report.append(f"缓存命中率: {cache['cache_stats']['hit_rate']*100:.1f}%")
                report.append(f"缓存大小: {cache['cache_stats']['cache_size']}")
            report.append("")

        # 结论
        report.append("【测试结论】")
        report.append("-" * 60)

        if 'serial' in self.results and 'concurrent' in self.results:
            serial_time = self.results['serial']['avg_time']
            concurrent_time = self.results['concurrent']['avg_time']

            if serial_time > concurrent_time:
                speedup = serial_time / concurrent_time
                report.append(f"✅ 并发模式性能优于串行模式，提升 {speedup:.2f}x")
            else:
                report.append("⚠️ 并发模式性能未优于串行模式，可能受网络或服务器限制")

        if 'cache' in self.results and self.results['cache']['speedup'] > 10:
            report.append(f"✅ 缓存机制效果显著，加速比达到 {self.results['cache']['speedup']:.0f}x")

        report.append("")
        report.append("=" * 60)

        return "\n".join(report)


def main():
    """主函数"""
    logger.info("开始性能测试")

    # 创建测试器
    tester = PerformanceTester()

    # 测试交易对
    symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']

    # 测试串行获取
    logger.info("\n测试 1: 串行获取性能")
    tester.test_serial_fetch(symbols, iterations=3)

    # 等待一下
    time.sleep(2)

    # 测试并发获取
    logger.info("\n测试 2: 并发获取性能")
    tester.test_concurrent_fetch(symbols, iterations=3)

    # 等待一下
    time.sleep(2)

    # 测试缓存性能
    logger.info("\n测试 3: 缓存性能")
    tester.test_cache_performance(symbols, iterations=5)

    # 生成报告
    report = tester.generate_report()
    print(report)

    # 保存报告
    report_file = f"performance_test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)

    logger.info(f"\n报告已保存到: {report_file}")


if __name__ == '__main__':
    main()
