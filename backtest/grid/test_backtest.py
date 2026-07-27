#!/usr/bin/env python3
"""
回测框架测试脚本
验证回测环境和数据可用性
"""
import sys
import os
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from backtest.grid.data_loader import DataLoader
import yaml


def test_data_loader():
    """
    测试数据加载器
    """
    print("=" * 60)
    print("测试数据加载器")
    print("=" * 60)

    try:
        # 加载配置
        config_path = Path(__file__).parent / 'config.yaml'
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        # 初始化数据加载器
        loader = DataLoader(config)

        # 测试加载单个时间框架数据
        print("\n1. 测试加载1小时K线数据")
        klines_1h = loader.load_klines('1h')
        print(f"   加载成功: {len(klines_1h)} 根K线")
        if klines_1h:
            print(f"   时间范围: {klines_1h[0]['timestamp']} 至 {klines_1h[-1]['timestamp']}")
            print(f"   价格范围: {min(k['close'] for k in klines_1h):.2f} 至 {max(k['close'] for k in klines_1h):.2f}")

        # 测试加载多时间框架数据
        print("\n2. 测试加载多时间框架数据")
        tf_data = loader.load_multi_timeframe_data()
        for tf, data in tf_data.items():
            print(f"   {tf}: {len(data)} 根K线")

        # 测试数据验证
        print("\n3. 测试数据验证")
        is_valid = loader.validate_klines(klines_1h)
        print(f"   数据验证: {'通过' if is_valid else '失败'}")

        # 测试数据摘要
        print("\n4. 测试数据摘要")
        summary = loader.get_klines_summary(klines_1h)
        print(f"   数据点数: {summary['count']}")
        print(f"   时间范围: {summary['start_time']} 至 {summary['end_time']}")
        print(f"   价格范围: {summary['price_range']['min']:.2f} 至 {summary['price_range']['max']:.2f}")

        print("\n" + "=" * 60)
        print("数据加载器测试完成")
        print("=" * 60)

        return True

    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_backtest_engine():
    """
    测试回测引擎
    """
    print("\n" + "=" * 60)
    print("测试回测引擎")
    print("=" * 60)

    try:
        from backtest.grid.backtest_engine import BacktestEngine

        # 加载配置
        config_path = Path(__file__).parent / 'config.yaml'

        # 初始化回测引擎
        print("\n1. 初始化回测引擎")
        engine = BacktestEngine(str(config_path))
        print(f"   初始资金: {float(engine.initial_balance):.2f} USDT")
        print(f"   杠杆倍数: {engine.leverage}x")
        print(f"   保证金: {float(engine.margin):.2f} USDT")

        # 运行回测(只处理前100根K线作为测试)
        print("\n2. 运行回测(测试模式)")
        print("   注意: 仅处理前100根K线作为测试")

        # 加载数据
        tf_data = engine.data_loader.load_multi_timeframe_data()
        main_interval = engine.config.get('kline', {}).get('interval', '1h')
        klines = tf_data[main_interval][:100]  # 只取前100根

        print(f"   加载K线数据: {len(klines)} 根")

        # 简单测试
        print("\n3. 测试网格初始化")
        if klines:
            from decimal import Decimal
            current_kline = klines[50]  # 从第50根开始
            current_price = Decimal(str(current_kline['close']))
            engine._initialize_grid(
                current_time=current_kline['timestamp'],
                current_price=current_price,
                klines=klines[:51],
                tf_data={tf: data[:51] for tf, data in tf_data.items()}
            )
            print(f"   网格初始化成功")
            print(f"   网格数量: {len(engine.grid_levels)}")
            print(f"   活跃订单: {len(engine.active_orders)}")

        print("\n" + "=" * 60)
        print("回测引擎测试完成")
        print("=" * 60)

        return True

    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """
    主函数
    """
    print("\n" + "=" * 60)
    print("ETHUSDT网格交易策略回测框架测试")
    print("=" * 60)

    # 测试数据加载器
    success1 = test_data_loader()

    # 测试回测引擎
    success2 = test_backtest_engine()

    # 汇总结果
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    print(f"数据加载器测试: {'通过' if success1 else '失败'}")
    print(f"回测引擎测试: {'通过' if success2 else '失败'}")

    if success1 and success2:
        print("\n所有测试通过! 回测框架已准备就绪。")
        print("\n运行完整回测:")
        print("  cd /Users/yl/vscode/Binance_quantitative_trading")
        print("  python backtest/grid/run_backtest.py")
        return 0
    else:
        print("\n部分测试失败,请检查配置和数据。")
        return 1


if __name__ == '__main__':
    sys.exit(main())
