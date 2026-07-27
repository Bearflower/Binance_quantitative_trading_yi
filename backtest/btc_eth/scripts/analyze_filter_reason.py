"""分析回测信号过滤原因"""
import pandas as pd
import numpy as np
import sys
import os

script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
sys.path.insert(0, project_root)

from shared.indicators import TechnicalIndicators

def analyze_symbol(symbol: str):
    """分析单个币种的信号过滤情况"""
    data_dir = os.path.join(project_root, 'backtest/btc_eth/data')
    symbol_lower = symbol.lower()
    
    try:
        df_1h = pd.read_csv(f'{data_dir}/{symbol_lower}_1h.csv')
    except FileNotFoundError:
        print(f'{symbol}: 数据文件不存在')
        return
    
    df_1h['open_time'] = pd.to_datetime(df_1h['open_time'])
    df_1h.set_index('open_time', inplace=True)
    
    # 重命名列
    df_1h.rename(columns={
        'open_price': 'open',
        'high_price': 'high',
        'low_price': 'low',
        'close_price': 'close'
    }, inplace=True)
    
    if len(df_1h) < 100:
        print(f'{symbol}: 数据不足 ({len(df_1h)}条)')
        return
    
    # 计算技术指标
    indicators = TechnicalIndicators.calculate_all(df_1h)
    indicators_df = pd.DataFrame(indicators)
    
    # 计算ATR%
    atr_percent = indicators_df['ATR'] / df_1h['close'] * 100
    adx = indicators_df['ADX']
    
    print(f'\n=== {symbol} 分析 ===')
    print(f'数据条数: {len(df_1h)}')
    print(f'\nATR% 统计:')
    print(f'  范围: {atr_percent.min():.2f}% ~ {atr_percent.max():.2f}%')
    print(f'  平均: {atr_percent.mean():.2f}%')
    
    print(f'\nATR% 分布:')
    print(f'  < 0.5%: {(atr_percent < 0.5).sum()} ({(atr_percent < 0.5).sum()/len(atr_percent)*100:.1f}%)')
    print(f'  0.5-1.0%: {((atr_percent >= 0.5) & (atr_percent < 1.0)).sum()}')
    print(f'  1.0-2.0%: {((atr_percent >= 1.0) & (atr_percent < 2.0)).sum()}')
    print(f'  2.0-4.0%: {((atr_percent >= 2.0) & (atr_percent < 4.0)).sum()}')
    print(f'  4.0-8.5%: {((atr_percent >= 4.0) & (atr_percent < 8.5)).sum()}')
    print(f'  > 8.5%: {(atr_percent >= 8.5).sum()}')
    
    print(f'\nADX 统计:')
    print(f'  范围: {adx.min():.2f} ~ {adx.max():.2f}')
    print(f'  平均: {adx.mean():.2f}')
    print(f'  < 15: {(adx < 15).sum()} ({(adx < 15).sum()/len(adx)*100:.1f}%)')
    print(f'  >= 15: {(adx >= 15).sum()}')
    
    # 组合条件
    atr_ok = (atr_percent >= 1.0) & (atr_percent <= 8.5)
    adx_ok = adx >= 15
    both_ok = atr_ok & adx_ok
    
    print(f'\n组合条件:')
    print(f'  ATR%在1.0%-8.5%: {atr_ok.sum()} ({atr_ok.sum()/len(atr_percent)*100:.1f}%)')
    print(f'  ADX>=15: {adx_ok.sum()} ({adx_ok.sum()/len(adx)*100:.1f}%)')
    print(f'  同时满足: {both_ok.sum()} ({both_ok.sum()/len(adx)*100:.1f}%)')

if __name__ == '__main__':
    symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'TRXUSDT', 'SOLUSDT']
    for symbol in symbols:
        analyze_symbol(symbol)
