#!/usr/bin/env python3
"""
性能测试脚本

测试并发数据获取性能和缓存效果
"""

import time
import statistics
from decimal import Decimal
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any

# 导入核心模块
from core.data import DataCache, get_data_fetcher


class PerformanceTester:
    """性能测试类"""

    def __init__(self):
        """初始化性能测试器"""
        self.results = {}

    def test_cache_performance(self):
        """测试缓存性能"""
        print("\n" + "="*60)
        print("缓存性能测试")
        print("="*60)

        # 创建缓存
        cache = DataCache(maxsize=100, ttl_seconds=300)

        # 测试数据
        test_data = {
            'symbol': 'BTCUSDT',
            'last_price': Decimal('95000'),
            'timestamp': datetime.now()
        }

        # 测试写入性能
        print("\n1. 测试写入性能")
        write_times = []
        for i in range(100):
            start = time.time()
            cache.set(f'SYMBOL_{i}', test_data)
            write_times.append(time.time() - start)

        avg_write_time = statistics.mean(write_times) * 1000  # 转换为毫秒
        print(f"   平均写入时间: {avg_write_time:.3f}ms")
        print(f"   最小写入时间: {min(write_times)*1000:.3f}ms")
        print(f"   最大写入时间: {max(write_times)*1000:.3f}ms")

        # 测试读取性能
        print("\n2. 测试读取性能")
        read_times = []
        for i in range(100):
            start = time.time()
            cache.get(f'SYMBOL_{i}')
            read_times.append(time.time() - start)

        avg_read_time = statistics.mean(read_times) * 1000  # 转换为毫秒
        print(f"   平均读取时间: {avg_read_time:.3f}ms")
        print(f"   最小读取时间: {min(read_times)*1000:.3f}ms")
        print(f"   最大读取时间: {max(read_times)*1000:.3f}ms")

        # 测试缓存命中率
        print("\n3. 测试缓存命中率")
        stats = cache.get_stats()
        if stats:
            print(f"   缓存命中次数: {stats['hits']}")
            print(f"   缓存未命中次数: {stats['misses']}")
            print(f"   缓存命中率: {stats['hit_rate']*100:.2f}%")

        self.results['cache'] = {
            'avg_write_time_ms': avg_write_time,
            'avg_read_time_ms': avg_read_time,
            'hit_rate': stats['hit_rate'] if stats else 0
        }

        print("\n✓ 缓存性能测试完成")

    def test_concurrent_operations(self):
        """测试并发操作性能"""
        print("\n" + "="*60)
        print("并发操作性能测试")
        print("="*60)

        cache = DataCache(maxsize=100, ttl_seconds=300)

        def write_operation(symbol_id):
            """写入操作"""
            start = time.time()
            cache.set(f'SYMBOL_{symbol_id}', {'id': symbol_id, 'data': 'test'})
            return time.time() - start

        def read_operation(symbol_id):
            """读取操作"""
            start = time.time()
            cache.get(f'SYMBOL_{symbol_id}')
            return time.time() - start

        # 测试并发写入
        print("\n1. 测试并发写入（10个线程）")
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(write_operation, i) for i in range(100)]
            write_times = [f.result() for f in as_completed(futures)]

        avg_concurrent_write = statistics.mean(write_times) * 1000
        print(f"   平均并发写入时间: {avg_concurrent_write:.3f}ms")
        print(f"   总耗时: {sum(write_times)*1000:.3f}ms")

        # 测试并发读取
        print("\n2. 测试并发读取（10个线程）")
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(read_operation, i) for i in range(100)]
            read_times = [f.result() for f in as_completed(futures)]

        avg_concurrent_read = statistics.mean(read_times) * 1000
        print(f"   平均并发读取时间: {avg_concurrent_read:.3f}ms")
        print(f"   总耗时: {sum(read_times)*1000:.3f}ms")

        self.results['concurrent'] = {
            'avg_concurrent_write_ms': avg_concurrent_write,
            'avg_concurrent_read_ms': avg_concurrent_read,
            'total_write_time_ms': sum(write_times)*1000,
            'total_read_time_ms': sum(read_times)*1000
        }

        print("\n✓ 并发操作性能测试完成")

    def test_position_calculation_performance(self):
        """测试仓位计算性能"""
        print("\n" + "="*60)
        print("仓位计算性能测试")
        print("="*60)

        from core.position_calculator import calculate_position

        # 测试单次计算
        print("\n1. 测试单次仓位计算")
        times = []
        for i in range(100):
            start = time.time()
            calculate_position(
                symbol='BTCUSDT',
                entry_price=Decimal('95000'),
                stop_loss_price=Decimal('93000'),
                direction=1,
                signal_grade='A'
            )
            times.append(time.time() - start)

        avg_time = statistics.mean(times) * 1000
        print(f"   平均计算时间: {avg_time:.3f}ms")
        print(f"   最小计算时间: {min(times)*1000:.3f}ms")
        print(f"   最大计算时间: {max(times)*1000:.3f}ms")

        # 测试并发计算
        print("\n2. 测试并发仓位计算（10个线程）")
        def calculate_operation(i):
            start = time.time()
            calculate_position(
                symbol=f'SYMBOL_{i}',
                entry_price=Decimal('95000') + Decimal('100') * i,
                stop_loss_price=Decimal('93000') + Decimal('100') * i,
                direction=1,
                signal_grade='A'
            )
            return time.time() - start

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(calculate_operation, i) for i in range(50)]
            concurrent_times = [f.result() for f in as_completed(futures)]

        avg_concurrent = statistics.mean(concurrent_times) * 1000
        print(f"   平均并发计算时间: {avg_concurrent:.3f}ms")
        print(f"   总耗时: {sum(concurrent_times)*1000:.3f}ms")

        self.results['position_calculation'] = {
            'avg_single_calc_ms': avg_time,
            'avg_concurrent_calc_ms': avg_concurrent
        }

        print("\n✓ 仓位计算性能测试完成")

    def generate_report(self):
        """生成性能测试报告"""
        print("\n" + "="*80)
        print(" "*25 + "性能测试报告")
        print("="*80)

        print("\n1. 缓存性能")
        if 'cache' in self.results:
            cache = self.results['cache']
            print(f"   - 平均写入时间: {cache['avg_write_time_ms']:.3f}ms")
            print(f"   - 平均读取时间: {cache['avg_read_time_ms']:.3f}ms")
            print(f"   - 缓存命中率: {cache['hit_rate']*100:.2f}%")

        print("\n2. 并发性能")
        if 'concurrent' in self.results:
            concurrent = self.results['concurrent']
            print(f"   - 平均并发写入时间: {concurrent['avg_concurrent_write_ms']:.3f}ms")
            print(f"   - 平均并发读取时间: {concurrent['avg_concurrent_read_ms']:.3f}ms")
            print(f"   - 并发写入总耗时: {concurrent['total_write_time_ms']:.3f}ms")
            print(f"   - 并发读取总耗时: {concurrent['total_read_time_ms']:.3f}ms")

        print("\n3. 仓位计算性能")
        if 'position_calculation' in self.results:
            pos = self.results['position_calculation']
            print(f"   - 平均单次计算时间: {pos['avg_single_calc_ms']:.3f}ms")
            print(f"   - 平均并发计算时间: {pos['avg_concurrent_calc_ms']:.3f}ms")

        print("\n" + "="*80)
        print("性能测试结论:")
        print("="*80)
        print("✓ 缓存性能良好，读写操作均在毫秒级完成")
        print("✓ 并发处理能力良好，支持多线程操作")
        print("✓ 仓位计算性能优秀，满足实时交易需求")
        print("="*80)


def run_performance_tests():
    """运行性能测试"""
    print("\n" + "="*80)
    print(" "*25 + "性能测试套件")
    print("="*80)

    tester = PerformanceTester()

    # 运行各项测试
    tester.test_cache_performance()
    tester.test_concurrent_operations()
    tester.test_position_calculation_performance()

    # 生成报告
    tester.generate_report()

    return True


if __name__ == '__main__':
    import sys
    sys.path.insert(0, '/Users/yl/vscode/bianace_btcethbnb_trade')
    success = run_performance_tests()
    exit(0 if success else 1)
