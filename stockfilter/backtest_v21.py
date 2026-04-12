#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.1 回测脚本（带流动性过滤）
"""

import pandas as pd
import json
from pathlib import Path
from datetime import datetime
import sys

# 导入 V2.1 回测器
from backtester_scheme_ab import BacktesterWithRules_AB


def load_config(config_file='config_v21_final.yaml'):
    """加载配置文件"""
    with open(config_file, 'r', encoding='utf-8') as f:
        import yaml
        return yaml.safe_load(f)


def get_all_csv_files(directory: str):
    """获取目录下所有 CSV 文件"""
    csv_path = Path(directory)
    if not csv_path.exists():
        return []
    
    all_files = list(csv_path.glob('*.csv'))
    complete_files = []
    for f in all_files:
        try:
            df = pd.read_csv(f)
            if len(df) >= 1700:
                complete_files.append(f)
        except:
            pass
    
    return sorted(complete_files)


def batch_backtest(csv_directory: str, config: dict):
    """批量回测"""
    print("\n" + "=" * 80)
    print("V2.1 批量回测（带流动性过滤）")
    print("=" * 80)
    
    csv_files = get_all_csv_files(csv_directory)
    total = len(csv_files)
    
    print(f"找到 {total} 只股票的 CSV 文件")
    print()
    
    # 初始化回测器
    backtester = BacktesterWithRules_AB(config_path='config_v21_final.yaml')
    
    results = []
    matched_count = 0
    
    for idx, csv_file in enumerate(csv_files):
        code = csv_file.stem.split('_')[0]
        name = csv_file.stem
        
        print(f"[{idx+1}/{total}] {code} - 检测中...", end=" ")
        
        # 加载数据
        try:
            df = pd.read_csv(csv_file)
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
        except:
            print("❌ 数据加载失败")
            continue
        
        if len(df) < 60:
            print("❌ 数据不足")
            continue
        
        # 检测形态
        try:
            pattern_info = backtester.check_pattern_single(df, code, '2019-01-01', '2026-04-07')
            
            if pattern_info:
                # 模拟交易
                trade_result = backtester.simulate_trade(df, pattern_info)
                
                if trade_result:
                    result = {
                        'code': code,
                        'name': name,
                        'is_match': True,
                        # 交易数据
                        'buy_date': str(trade_result.get('buy_date', '')),
                        'sell_date': str(trade_result.get('sell_date', '')),
                        'buy_price': trade_result.get('buy_price', 0),
                        'sell_price': trade_result.get('sell_price', 0),
                        'profit_pct': trade_result.get('net_return', 0) * 100,
                        'hold_days': trade_result.get('holding_days', 0),
                        'exit_reason': trade_result.get('sell_reason', ''),
                        # 形态数据
                        'retrace_date': str(pattern_info.get('retrace_date', '')),
                        'surge_date': str(pattern_info.get('surge_date', '')),
                        'drop_start_date': str(pattern_info.get('drop_start_date', '')),
                        'drop_end_date': str(pattern_info.get('drop_end_date', '')),
                        'shrink_date': str(pattern_info.get('shrink_date', '')),
                        'support_level': pattern_info.get('support_level', 0),
                        'detail': pattern_info
                    }
                    print(f"✅ 满足形态 (收益：{result['profit_pct']:.2f}%)")
                    matched_count += 1
                else:
                    print("❌ 不满足（交易失败）")
            else:
                print("❌ 不满足")
            
            # 保存完整的结果数据
            if pattern_info and trade_result:
                results.append(result)  # 使用完整的 result 字典
            else:
                results.append({
                    'code': code,
                    'name': name,
                    'is_match': False
                })
        except Exception as e:
            print(f"❌ 异常：{e}")
            results.append({'code': code, 'name': name, 'is_match': False})
    
    print()
    print("=" * 80)
    print(f"回测完成：共检测 {len(results)} 只股票，{matched_count} 只满足形态")
    print("=" * 80)
    
    return results


def generate_report(results):
    """生成报告"""
    matched = [r for r in results if r.get('is_match')]
    
    if not matched:
        print("❌ 没有匹配的股票")
        return
    
    # 统计
    profits = [r['profit_pct'] for r in matched]
    avg_profit = sum(profits) / len(profits)
    max_profit = max(profits)
    min_profit = min(profits)
    profitable = len([p for p in profits if p > 0])
    win_rate = profitable / len(matched) * 100
    
    print("\n" + "=" * 80)
    print("V2.1 回测结果")
    print("=" * 80)
    print(f"检测股票：{len(results)} 只")
    print(f"满足形态：{len(matched)} 只")
    print(f"平均收益：{avg_profit:.2f}%")
    print(f"最高收益：{max_profit:.2f}%")
    print(f"最低收益：{min_profit:.2f}%")
    print(f"胜率：{win_rate:.1f}%")
    print("=" * 80)
    
    # 保存结果
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = Path(f'backtest_results/backtest_v21_{timestamp}.json')
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    
    print(f"\n✅ 结果已保存：{output_file}")


def main():
    """主函数"""
    print("=" * 80)
    print("股票形态策略 V2.1 回测")
    print("=" * 80)
    
    # 加载配置
    try:
        config = load_config()
        print("\n✅ 配置加载完成")
    except Exception as e:
        print(f"\n❌ 配置加载失败：{e}")
        sys.exit(1)
    
    # 回测
    csv_directory = 'data/backtest/baostocks_full'
    results = batch_backtest(csv_directory, config)
    
    # 生成报告
    generate_report(results)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断回测")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 回测异常：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
