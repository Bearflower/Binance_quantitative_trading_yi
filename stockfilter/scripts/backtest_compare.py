#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
版本对比回测脚本:
- 支持对比不同版本的回测结果
- 生成对比报告
"""

import argparse
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.backtester import Backtester


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='版本对比回测脚本')
    parser.add_argument('--versions', type=str, nargs='+', default=['v24', 'v25'],
                       help='要对比的版本列表')
    parser.add_argument('--start-date', type=str, default='2019-01-01',
                       help='开始日期')
    parser.add_argument('--end-date', type=str, default='2026-04-27',
                       help='结束日期')
    parser.add_argument('--config', type=str, default='config/config.yaml',
                       help='配置文件路径')
    parser.add_argument('--output', type=str, default='backtest_results/compare/',
                       help='输出目录')

    args = parser.parse_args()

    print(f"开始版本对比回测...")
    print(f"对比版本: {', '.join(args.versions)}")
    print(f"时间范围: {args.start_date} ~ {args.end_date}")

    # 为每个版本创建回测器
    backtesters = {}
    for version in args.versions:
        backtesters[version] = Backtester(config_path=args.config, version=version)
        version_info = backtesters[version].get_version_info()
        print(f"{version} 参数: {version_info}")

    # TODO: 实现版本对比逻辑
    # 这里应该从compare_v24_v25.py等脚本中复制核心逻辑
    print("版本对比功能待实现...")

    print("版本对比完成!")


if __name__ == '__main__':
    main()
