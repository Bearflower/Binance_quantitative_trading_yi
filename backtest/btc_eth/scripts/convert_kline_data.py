#!/usr/bin/env python3
"""
K线数据格式转换脚本

将原始K线数据格式转换为回测格式：
- 原始格式：open_time, open_price, high_price, low_price, close_price, volume, close_time, quote_volume, trade_count, taker_buy_volume, taker_buy_quote_volume
- 回测格式：open_time, open_price, high_price, low_price, close_price, volume

作者：资深Python工程师
创建时间：2026-05-09
"""
import pandas as pd
import os
from pathlib import Path
from typing import List


def convert_kline_format(input_file: str, output_file: str) -> None:
    """
    转换单个K线数据文件格式

    Args:
        input_file: 输入文件路径
        output_file: 输出文件路径
    """
    try:
        # 读取原始数据
        df = pd.read_csv(input_file, header=None)

        # 检查数据列数
        if df.shape[1] < 6:
            print(f"警告：文件 {input_file} 列数不足，跳过")
            return

        # 提取需要的列
        # 原始格式：open_time, open_price, high_price, low_price, close_price, volume, ...
        df_converted = df.iloc[:, :6]
        df_converted.columns = ['open_time', 'open_price', 'high_price', 'low_price', 'close_price', 'volume']

        # 确保输出目录存在
        os.makedirs(os.path.dirname(output_file), exist_ok=True)

        # 保存转换后的数据
        df_converted.to_csv(output_file, index=False)

        print(f"成功转换：{input_file} -> {output_file} ({len(df_converted)}条数据)")

    except Exception as e:
        print(f"错误：转换文件 {input_file} 失败：{e}")


def main():
    """主函数"""
    # 定义币种和时间周期
    symbols = ['btcusdt', 'ethusdt', 'bnbusdt', 'xrpusdt', 'solusdt', 'trxusdt']
    intervals = ['1h', '4h', '1d']

    # 输入输出目录
    input_dir = '/tmp'
    output_dir = '/Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/data'

    print("=" * 60)
    print("K线数据格式转换")
    print("=" * 60)
    print(f"输入目录：{input_dir}")
    print(f"输出目录：{output_dir}")
    print(f"币种数量：{len(symbols)}")
    print(f"时间周期：{', '.join(intervals)}")
    print("=" * 60)

    # 转换所有文件
    success_count = 0
    fail_count = 0

    for symbol in symbols:
        for interval in intervals:
            input_file = os.path.join(input_dir, f"{symbol}_{interval}.csv")
            output_file = os.path.join(output_dir, f"{symbol}_{interval}.csv")

            if os.path.exists(input_file):
                try:
                    convert_kline_format(input_file, output_file)
                    success_count += 1
                except Exception as e:
                    print(f"转换失败：{input_file} - {e}")
                    fail_count += 1
            else:
                print(f"文件不存在：{input_file}")
                fail_count += 1

    print("=" * 60)
    print(f"转换完成：成功 {success_count} 个，失败 {fail_count} 个")
    print("=" * 60)


if __name__ == "__main__":
    main()
