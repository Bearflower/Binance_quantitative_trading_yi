#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量回测脚本（使用 Baostock 获取的完整历史数据）

功能：
1. 从 data/backtest/baostocks_full/ 目录读取所有 CSV 文件
2. 对每只股票执行形态检测
3. 生成回测报告（Markdown + JSON + CSV）
"""

import pandas as pd
import json
import yaml
from pathlib import Path
from datetime import datetime
from typing import List, Dict
import sys

# 导入回测器
from backtester_with_rules import BacktesterWithRules


def load_config():
    """加载配置文件"""
    with open('config.yaml', 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def get_all_csv_files(directory: str) -> List[Path]:
    """获取目录下所有 CSV 文件（只返回>=1700 行的完整文件）"""
    csv_path = Path(directory)
    if not csv_path.exists():
        print(f"❌ 目录不存在：{csv_path}")
        return []
    
    all_files = list(csv_path.glob('*.csv'))
    # 过滤出完整的文件（>=1700 行）
    complete_files = []
    for f in all_files:
        try:
            df = pd.read_csv(f)
            if len(df) >= 1700:
                complete_files.append(f)
        except:
            pass
    
    return sorted(complete_files)


def load_stock_data(csv_file: Path) -> pd.DataFrame:
    """加载单只股票的 CSV 数据"""
    try:
        df = pd.read_csv(csv_file)
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        return df
    except Exception as e:
        print(f"❌ 加载失败 {csv_file.name}: {str(e)}")
        return None


def check_pattern_with_backtest(df: pd.DataFrame, code: str, name: str, 
                                 backtester: BacktesterWithRules) -> Dict:
    """
    对单只股票执行形态检测和回测
    
    Returns:
        dict: 检测结果
    """
    try:
        # 检测形态（使用完整数据范围 2019-2026 年）
        period_start = '2019-01-01'
        period_end = '2026-04-07'
        pattern_info = backtester.check_pattern_single(df, code, period_start, period_end)
        
        if pattern_info:
            # 形态匹配，执行模拟交易
            trade_result = backtester.simulate_trade(df, pattern_info)
            
            if trade_result:
                return {
                    'code': code,
                    'name': name,
                    'is_match': True,
                    'matched_date': pattern_info.get('matched_date', ''),
                    'buy_date': trade_result.get('buy_date', ''),
                    'sell_date': trade_result.get('sell_date', ''),
                    'buy_price': trade_result.get('buy_price', 0),
                    'sell_price': trade_result.get('sell_price', 0),
                    'profit_pct': trade_result.get('net_return', 0),
                    'hold_days': trade_result.get('holding_days', 0),
                    'exit_reason': trade_result.get('sell_reason', ''),
                    'detail': pattern_info
                }
        
        return {
            'code': code,
            'name': name,
            'is_match': False
        }
    except Exception as e:
        return {
            'code': code,
            'name': name,
            'is_match': False,
            'error': str(e)
        }


def batch_backtest_all_stocks(csv_directory: str, config: Dict) -> List[Dict]:
    """
    批量回测所有股票
    
    Args:
        csv_directory: CSV 文件目录
        config: 配置字典
    
    Returns:
        list: 回测结果列表
    """
    print("\n" + "=" * 80)
    print("开始批量回测")
    print("=" * 80)
    
    # 获取所有 CSV 文件
    csv_files = get_all_csv_files(csv_directory)
    total = len(csv_files)
    
    if total == 0:
        print("❌ 未找到任何 CSV 文件")
        return []
    
    print(f"找到 {total} 只股票的 CSV 文件")
    print()
    
    # 初始化回测器
    backtester = BacktesterWithRules(config_path='config.yaml')
    
    results = []
    matched_count = 0
    
    for idx, csv_file in enumerate(csv_files):
        # 从文件名提取股票代码
        code = csv_file.stem.split('_')[0]  # 假设文件名格式：603529_kline.csv
        name = csv_file.stem  # 临时用文件名作为名称
        
        print(f"[{idx+1}/{total}] {code} - 检测中...", end=" ")
        
        # 加载数据
        df = load_stock_data(csv_file)
        if df is None or len(df) < 60:
            print("❌ 数据不足")
            continue
        
        # 执行回测
        result = check_pattern_with_backtest(df, code, name, backtester)
        
        if result['is_match']:
            print(f"✅ 满足形态 (收益：{result.get('profit_pct', 0):.2f}%)")
            matched_count += 1
        else:
            print("❌ 不满足")
        
        results.append(result)
    
    print()
    print("=" * 80)
    print(f"回测完成：共检测 {len(results)} 只股票，{matched_count} 只满足形态")
    print("=" * 80)
    
    return results


def generate_report(results: List[Dict], output_dir: str = 'backtest_results'):
    """生成回测报告"""
    output_path = Path(output_dir)
    output_path.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # 1. 生成 Markdown 报告
    md_file = output_path / f'backtest_report_{timestamp}.md'
    generate_markdown_report(results, md_file)
    
    # 2. 生成 JSON 报告
    json_file = output_path / f'backtest_results_{timestamp}.json'
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"✅ JSON 报告：{json_file}")
    
    # 3. 生成 CSV 汇总
    csv_file = output_path / f'backtest_summary_{timestamp}.csv'
    generate_csv_summary(results, csv_file)
    
    # 4. 生成满足形态的股票列表
    matched_stocks = [r for r in results if r.get('is_match')]
    if matched_stocks:
        matched_file = output_path / f'matched_stocks_{timestamp}.csv'
        df_matched = pd.DataFrame(matched_stocks)
        df_matched.to_csv(matched_file, index=False, encoding='utf-8-sig')
        print(f"✅ 满足形态股票列表：{matched_file}")
    
    print(f"\n✅ 所有报告已保存到：{output_path}/")


def generate_markdown_report(results: List[Dict], output_file: Path):
    """生成 Markdown 格式回测报告"""
    matched_stocks = [r for r in results if r.get('is_match')]
    
    report = []
    report.append("# 股票形态批量回测报告")
    report.append(f"\n**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"\n**检测股票总数**: {len(results)}")
    report.append(f"\n**满足形态数量**: {len(matched_stocks)}")
    report.append(f"\n**不满足数量**: {len(results) - len(matched_stocks)}")
    report.append(f"\n**形态匹配率**: {len(matched_stocks)/len(results)*100:.2f}%")
    
    if matched_stocks:
        report.append(f"\n---")
        report.append(f"\n## ✅ 满足形态的股票详情 ({len(matched_stocks)}只)")
        
        # 按收益排序
        matched_stocks_sorted = sorted(matched_stocks, 
                                       key=lambda x: x.get('profit_pct', 0), 
                                       reverse=True)
        
        # 汇总统计
        profits = [s.get('profit_pct', 0) for s in matched_stocks_sorted]
        avg_profit = sum(profits) / len(profits)
        max_profit = max(profits)
        min_profit = min(profits)
        
        report.append(f"\n### 📊 收益统计")
        report.append(f"- 平均收益：{avg_profit:.2f}%")
        report.append(f"- 最高收益：{max_profit:.2f}%")
        report.append(f"- 最低收益：{min_profit:.2f}%")
        
        # 详细列表
        report.append(f"\n### 📋 股票列表")
        report.append(f"\n| 排名 | 代码 | 名称 | 匹配日期 | 买入价 | 卖出价 | 收益% | 持仓天数 | 卖出原因 |")
        report.append(f"|------|------|------|----------|--------|--------|-------|----------|----------|")
        
        for idx, stock in enumerate(matched_stocks_sorted, 1):
            report.append(f"| {idx} | {stock['code']} | {stock.get('name', '')} | "
                         f"{stock.get('matched_date', '')} | "
                         f"{stock.get('buy_price', 0):.2f} | "
                         f"{stock.get('sell_price', 0):.2f} | "
                         f"{stock.get('profit_pct', 0):.2f} | "
                         f"{stock.get('hold_days', 0)} | "
                         f"{stock.get('exit_reason', '')} |")
        
        # 前 10 名详情
        report.append(f"\n### 🏆 收益前 10 名详情")
        for idx, stock in enumerate(matched_stocks_sorted[:10], 1):
            report.append(f"\n#### {idx}. {stock['code']} ({stock.get('name', '')})")
            report.append(f"- 匹配日期：{stock.get('matched_date', 'N/A')}")
            report.append(f"- 买入日期：{stock.get('buy_date', 'N/A')}")
            report.append(f"- 卖出日期：{stock.get('sell_date', 'N/A')}")
            report.append(f"- 买入价格：{stock.get('buy_price', 0):.2f} 元")
            report.append(f"- 卖出价格：{stock.get('sell_price', 0):.2f} 元")
            report.append(f"- 收益率：{stock.get('profit_pct', 0):.2f}%")
            report.append(f"- 持仓天数：{stock.get('hold_days', 0)} 天")
            report.append(f"- 卖出原因：{stock.get('exit_reason', 'N/A')}")
    
    else:
        report.append(f"\n## ❌ 暂无满足形态的股票")
    
    # 保存报告
    report_text = '\n'.join(report)
    output_file.write_text(report_text, encoding='utf-8')
    print(f"✅ Markdown 报告：{output_file}")


def generate_csv_summary(results: List[Dict], output_file: Path):
    """生成 CSV 格式汇总"""
    df = pd.DataFrame(results)
    
    # 只保留关键字段
    columns_to_keep = ['code', 'name', 'is_match', 'matched_date', 'buy_date', 
                       'sell_date', 'buy_price', 'sell_price', 'profit_pct', 
                       'hold_days', 'exit_reason']
    
    available_columns = [col for col in columns_to_keep if col in df.columns]
    df = df[available_columns]
    
    df.to_csv(output_file, index=False, encoding='utf-8-sig')
    print(f"✅ CSV 汇总：{output_file}")


def main():
    """主函数"""
    print("=" * 80)
    print("📊 股票形态批量回测系统（Baostock 完整数据）")
    print("=" * 80)
    
    # 1. 加载配置
    try:
        config = load_config()
        print("\n✅ 配置加载完成（方案 A）")
    except Exception as e:
        print(f"\n❌ 配置加载失败：{str(e)}")
        sys.exit(1)
    
    # 2. 设置 CSV 目录（优先使用 baostocks_full，如果没有则使用 baostocks）
    csv_directory = 'data/backtest/baostocks_full'
    
    if not Path(csv_directory).exists() or len(list(Path(csv_directory).glob('*.csv'))) < 100:
        print("⚠️  baostocks_full 目录数据不足，使用 baostocks 目录...")
        csv_directory = 'data/backtest/baostocks'
    
    # 3. 检查目录是否存在
    if not Path(csv_directory).exists():
        print(f"\n❌ CSV 目录不存在：{csv_directory}")
        print("请确认数据获取脚本已完成执行")
        sys.exit(1)
    
    # 4. 执行批量回测
    results = batch_backtest_all_stocks(csv_directory, config)
    
    if not results:
        print("\n❌ 回测结果为空")
        sys.exit(1)
    
    # 5. 生成报告
    generate_report(results, output_dir='backtest_results')
    
    # 6. 输出汇总
    matched_count = sum(1 for r in results if r.get('is_match'))
    print("\n" + "=" * 80)
    print("📊 回测汇总")
    print("=" * 80)
    print(f"检测股票：{len(results)} 只")
    print(f"满足形态：{matched_count} 只")
    print(f"匹配率：{matched_count/len(results)*100:.2f}%")
    print("=" * 80)
    
    print("\n✅ 回测全部完成！")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断回测")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 回测异常：{str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
