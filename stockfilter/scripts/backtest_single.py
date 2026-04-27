#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单只股票回测脚本:
- 支持指定股票代码
- 支持指定版本参数
- 支持指定时间范围
"""

import argparse
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.backtester import Backtester


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='单只股票回测脚本')
    parser.add_argument('--code', type=str, required=True,
                       help='股票代码(如: 603529)')
    parser.add_argument('--version', type=str, default='v24',
                       choices=['v22', 'v23', 'v24', 'v25'],
                       help='回测版本')
    parser.add_argument('--start-date', type=str, default='2019-01-01',
                       help='开始日期')
    parser.add_argument('--end-date', type=str, default='2026-04-27',
                       help='结束日期')
    parser.add_argument('--config', type=str, default='config/config.yaml',
                       help='配置文件路径')

    args = parser.parse_args()

    print(f"开始单只股票回测...")
    print(f"股票代码: {args.code}")
    print(f"版本: {args.version}")
    print(f"时间范围: {args.start_date} ~ {args.end_date}")

    # 创建回测器
    backtester = Backtester(config_path=args.config, version=args.version)

    # 打印版本信息
    version_info = backtester.get_version_info()
    print(f"版本参数: {version_info}")

    # TODO: 实现单只股票回测逻辑
    # 这里应该从check_603529_v24_final.py等脚本中复制核心逻辑
    print("单只股票回测功能待实现...")

    print("单只股票回测完成!")


if __name__ == '__main__':
    main()
