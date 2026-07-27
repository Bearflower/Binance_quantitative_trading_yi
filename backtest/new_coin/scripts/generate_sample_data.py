"""
生成示例K线数据用于测试回测框架
"""
import csv
import os
from datetime import datetime, timedelta
import random


def generate_sample_klines(symbol: str, output_dir: str, days: int = 30):
    """
    生成示例K线数据
    
    Args:
        symbol: 交易对
        output_dir: 输出目录
        days: 天数
    """
    # 确保目录存在
    os.makedirs(output_dir, exist_ok=True)
    
    # 输出文件路径
    output_path = os.path.join(output_dir, f"{symbol}_1h.csv")
    
    # 生成K线数据
    klines = []
    start_time = datetime(2025, 1, 1, 0, 0, 0)
    
    # 初始价格
    if symbol == "BTCUSDT":
        price = 50000.0
    elif symbol == "ETHUSDT":
        price = 3000.0
    else:
        price = 100.0
    
    # 生成每小时的K线
    for i in range(days * 24):
        open_time = start_time + timedelta(hours=i)
        close_time = open_time + timedelta(hours=1) - timedelta(milliseconds=1)
        
        # 随机波动
        change_percent = random.uniform(-0.05, 0.05)
        open_price = price
        close_price = price * (1 + change_percent)
        high_price = max(open_price, close_price) * (1 + random.uniform(0, 0.02))
        low_price = min(open_price, close_price) * (1 - random.uniform(0, 0.02))
        
        # 成交量
        volume = random.uniform(100, 1000)
        quote_volume = volume * price
        
        # 更新价格
        price = close_price
        
        # 添加到列表
        klines.append({
            'open_time': int(open_time.timestamp() * 1000),
            'open': round(open_price, 2),
            'high': round(high_price, 2),
            'low': round(low_price, 2),
            'close': round(close_price, 2),
            'volume': round(volume, 4),
            'quote_asset_volume': round(quote_volume, 2),
            'close_time': int(close_time.timestamp() * 1000)
        })
    
    # 写入CSV文件
    with open(output_path, 'w', encoding='utf-8', newline='') as f:
        fieldnames = ['open_time', 'open', 'high', 'low', 'close', 'volume', 'quote_asset_volume', 'close_time']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(klines)
    
    print(f"生成示例K线数据: {output_path}, 共 {len(klines)} 根K线")


def main():
    """主函数"""
    output_dir = "backtest/new_coin/data/klines"
    
    # 生成示例数据
    generate_sample_klines("BTCUSDT", output_dir, days=30)
    generate_sample_klines("ETHUSDT", output_dir, days=30)
    generate_sample_klines("SOLUSDT", output_dir, days=30)
    
    print("\n示例数据生成完成！")


if __name__ == '__main__':
    main()
