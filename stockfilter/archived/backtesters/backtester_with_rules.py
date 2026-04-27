#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票形态回测器（带买卖点规则、移动止盈、硬止损）

基于调整方向文档 v2.0 实现：
- 精确买卖点规则（次日开盘买入）
- 移动止盈（回撤 8% 卖出）
- 硬止损（-10% 立即卖出）
- 时间止盈（30 天强制卖出）
"""

import json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import yaml


class BacktesterWithRules:
    """带买卖点规则的回测器"""
    
    def __init__(self, config_path: str = 'config.yaml'):
        """
        初始化回测器
        
        Args:
            config_path: 配置文件路径
        """
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        # 形态参数
        pattern_config = self.config['pattern']
        self.drop_threshold = pattern_config.get('drop_threshold', 0.15)
        self.volume_shrink_ratio = pattern_config.get('volume_shrink_ratio', 0.6)
        self.surge_price_ratio = pattern_config.get('surge_price_ratio', 0.07)
        self.min_volume_ratio = pattern_config.get('min_volume_ratio', 2.0)
        self.max_volume_ratio = pattern_config.get('max_volume_ratio', 8.0)
        self.flat_days = pattern_config.get('flat_days', 2)
        self.flat_volume_threshold = pattern_config.get('flat_volume_threshold', 0.7)
        self.flat_price_range = pattern_config.get('flat_price_range', 0.05)
        
        # 交易参数
        trading_config = self.config.get('trading', {})
        self.buy_next_day_open = trading_config.get('buy_next_day_open', True)  # 次日开盘买入
        self.skip_limit_up_open = trading_config.get('skip_limit_up_open', True)  # 跳过涨停开盘
        self.skip_high_open = trading_config.get('skip_high_open_threshold', 0.05)  # 高开>5% 跳过
        self.position_per_stock = trading_config.get('position_per_stock', 0.1)  # 单只仓位 10%
        self.max_positions = trading_config.get('max_positions', 5)  # 最大持仓 5 只
        
        # 止盈止损参数
        self.stop_loss_ratio = trading_config.get('stop_loss_ratio', 0.03)  # 硬止损 -3%
        self.trailing_stop_ratio = trading_config.get('trailing_stop_ratio', 0.08)  # 移动止盈回撤 8%
        self.hard_stop_loss = trading_config.get('hard_stop_loss', 0.10)  # 硬止损 -10%
        self.max_holding_days = trading_config.get('max_holding_days', 30)  # 最长持仓 30 天
        self.min_holding_days = trading_config.get('min_holding_days', 5)  # 最短持仓 5 天
        
        # 交易成本
        self.commission = trading_config.get('commission', 0.00025)  # 佣金双向 0.025%
        self.stamp_tax = trading_config.get('stamp_tax', 0.001)  # 印花税卖出 0.1%
        self.slippage = trading_config.get('slippage', 0.001)  # 滑点 0.1%
        
        # 大盘过滤
        self.enable_market_filter = trading_config.get('enable_market_filter', False)  # 是否启用大盘过滤
        self.market_index_path = trading_config.get('market_index_path', 'data/index/hs300_index.csv')  # 指数数据路径
        self.market_ma20_ratio = trading_config.get('market_ma20_ratio', 1.0)  # 指数/MA20 的阈值（>1 表示在均线上方）
        
        # 加载沪深 300 指数数据
        self.market_index_data = None
        if self.enable_market_filter:
            self._load_market_index()
        
        # 结果存储
        self.trades = []  # 交易记录
        self.portfolio_history = []  # 资产历史
    
    def _load_market_index(self):
        """加载沪深 300 指数数据"""
        try:
            index_path = Path(self.market_index_path)
            if index_path.exists():
                self.market_index_data = pd.read_csv(index_path)
                self.market_index_data['date'] = pd.to_datetime(self.market_index_data['date'])
                print(f"✅ 已加载沪深 300 指数数据：{len(self.market_index_data)} 条")
            else:
                print(f"⚠️  指数数据文件不存在：{index_path}，将不使用大盘过滤")
                self.enable_market_filter = False
        except Exception as e:
            print(f"❌ 加载指数数据失败：{e}，将不使用大盘过滤")
            self.enable_market_filter = False
    
    def check_market_condition(self, date: pd.Timestamp) -> bool:
        """
        检查大盘状态
        
        Args:
            date: 日期
        
        Returns:
            bool: True 表示大盘条件允许买入（指数>20 日均线），False 表示不允许
        """
        if not self.enable_market_filter or self.market_index_data is None:
            return True  # 不启用大盘过滤，始终允许
        
        try:
            # 找到最接近的日期
            idx = self.market_index_data[self.market_index_data['date'] <= date].index
            if len(idx) == 0:
                return True  # 找不到数据，允许买入
            
            latest_idx = idx[-1]
            row = self.market_index_data.loc[latest_idx]
            
            close = float(row['close'])
            ma20 = float(row['ma20'])
            
            # 检查是否在 20 日均线上方
            if pd.isna(ma20):
                return True  # MA20 不存在，允许买入
            
            ratio = close / ma20
            return ratio >= self.market_ma20_ratio
        except Exception as e:
            print(f"检查大盘状态失败：{e}")
            return True  # 出错时允许买入
    
    def check_pattern_single(self, df: pd.DataFrame, code: str, 
                            period_start: str, period_end: str) -> Optional[Dict]:
        """
        检测单只股票是否符合形态
        
        Returns:
            dict: 形态信息（如果符合），否则 None
        """
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        
        # 转换日期
        period_start_dt = pd.to_datetime(period_start)
        period_end_dt = pd.to_datetime(period_end)
        
        # 筛选时间段
        period_df = df[(df['date'] >= period_start_dt) & (df['date'] <= period_end_dt)].copy()
        
        if len(period_df) < 30:  # 至少需要 30 个交易日
            return None
        
        # 1. 检测大跌
        drop_found = False
        drop_start_idx = -1
        drop_end_idx = -1
        
        for i in range(20, len(period_df)):
            window_df = period_df.iloc[i-20:i+1]
            high_price = window_df['high'].max()
            low_price = window_df['low'].min()
            drop = (high_price - low_price) / high_price
            
            if drop >= self.drop_threshold:
                drop_found = True
                drop_start_idx = window_df[window_df['high'] == high_price].index[0]
                drop_end_idx = i
                break
        
        if not drop_found:
            return None
        
        # 2. 检测缩量（在放量日前 10 天内）
        shrink_found = False
        shrink_idx = -1
        
        for i in range(drop_end_idx+1, min(drop_end_idx+10, len(period_df))):
            vol_i = period_df['volume'].iloc[i]
            vol_avg_20 = period_df['volume'].iloc[max(0, i-20):i].mean()
            
            if vol_i <= vol_avg_20 * self.volume_shrink_ratio:
                shrink_found = True
                shrink_idx = i
                break
        
        if not shrink_found:
            return None
        
        # 3. 检测放量涨停 + 随后缩量走平
        surge_found = False
        surge_idx = -1
        
        for j in range(shrink_idx+1, min(shrink_idx+15, len(period_df))):
            vol_j = period_df['volume'].iloc[j]
            vol_prev = period_df['volume'].iloc[j-1]
            close_j = period_df['close'].iloc[j]
            close_prev = period_df['close'].iloc[j-1]
            
            vol_ratio = vol_j / vol_prev if vol_prev > 0 else 0
            price_change = (close_j - close_prev) / close_prev
            
            # 基础条件：放量 + 大涨
            if vol_ratio >= 1.5 and price_change >= self.surge_price_ratio:
                # 新增条件 1：涨幅≥7%
                if price_change < self.surge_price_ratio:
                    continue
                
                # 新增条件 2：量比 2~8 倍
                if not (self.min_volume_ratio <= vol_ratio <= self.max_volume_ratio):
                    continue
                
                # 新增条件 3：随后 flat_days 天内缩量走平
                is_flat = True
                for k in range(j+1, min(j+1+self.flat_days, len(period_df))):
                    vol_k = period_df['volume'].iloc[k]
                    close_k = period_df['close'].iloc[k]
                    
                    # 成交量要求
                    if vol_k >= vol_j * self.flat_volume_threshold:
                        is_flat = False
                        break
                    
                    # 价格波动要求
                    price_change_k = abs(close_k - close_j) / close_j
                    if price_change_k >= self.flat_price_range:
                        is_flat = False
                        break
                
                if not is_flat:
                    continue
                
                # 所有条件满足
                surge_found = True
                surge_idx = j
                break
        
        if not surge_found:
            return None
        
        # 4. 检测回踩（放量日后）
        retrace_found = False
        retrace_idx = -1
        support_level = period_df['low'].iloc[surge_idx]
        
        for i in range(surge_idx+1, min(surge_idx+10, len(period_df))):
            low_i = period_df['low'].iloc[i]
            close_i = period_df['close'].iloc[i]
            
            # 回踩不破支撑位
            if low_i >= support_level * 0.98:  # 允许 2% 的误差
                retrace_found = True
                retrace_idx = i
                break
        
        if not retrace_found:
            return None
        
        # 返回形态信息
        return {
            'code': code,
            'drop_start_date': period_df['date'].iloc[drop_start_idx],
            'drop_end_date': period_df['date'].iloc[drop_end_idx],
            'drop_change': (period_df['high'].iloc[drop_start_idx] - period_df['low'].iloc[drop_end_idx]) / period_df['high'].iloc[drop_start_idx],
            'shrink_date': period_df['date'].iloc[shrink_idx],
            'surge_date': period_df['date'].iloc[surge_idx],
            'surge_close': period_df['close'].iloc[surge_idx],
            'retrace_date': period_df['date'].iloc[retrace_idx],
            'retrace_close': period_df['close'].iloc[retrace_idx],
            'retrace_low': period_df['low'].iloc[retrace_idx],
            'support_level': support_level,
            'is_match': True
        }
    
    def simulate_trade(self, df: pd.DataFrame, pattern_info: Dict) -> Optional[Dict]:
        """
        模拟单次交易（包含买卖点规则、止盈止损）
        
        Args:
            df: 股票数据（至少包含回踩日后 30 天）
            pattern_info: 形态信息
        
        Returns:
            dict: 交易结果
        """
        code = pattern_info['code']
        retrace_date = pd.to_datetime(pattern_info['retrace_date'])
        buy_price = None
        sell_price = None
        sell_date = None
        sell_reason = None
        
        # 找到回踩日位置
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        
        retrace_idx_list = df[df['date'] == retrace_date].index
        if len(retrace_idx_list) == 0:
            return None
        retrace_idx = retrace_idx_list[0]
        
        # 检查次日开盘情况
        if retrace_idx + 1 >= len(df):
            return None
        
        next_day = df.iloc[retrace_idx + 1]
        open_price = next_day['open']
        prev_close = df.iloc[retrace_idx]['close']
        
        # 检查是否涨停开盘或高开过多
        if self.skip_limit_up_open:
            open_change = (open_price - prev_close) / prev_close
            if open_change >= 0.095:  # 涨停开盘
                return None
        
        if self.skip_high_open > 0:
            open_change = (open_price - prev_close) / prev_close
            if open_change > self.skip_high_open:  # 高开>5%
                return None
        
        # 大盘过滤检查
        if self.enable_market_filter:
            if not self.check_market_condition(next_day['date']):
                # 大盘状态不好，放弃买入
                return None
        
        # 买入
        buy_price = open_price * (1 + self.slippage)  # 考虑滑点
        buy_date = next_day['date']
        support_level = pattern_info['support_level']
        
        # 设置止损价
        stop_loss_price = buy_price * (1 - self.hard_stop_loss)
        
        # 开始模拟持仓
        highest_price = buy_price
        sell_triggered = False
        
        for i in range(retrace_idx + 2, len(df)):
            day_df = df.iloc[i]
            current_date = day_df['date']
            high_price = day_df['high']
            low_price = day_df['low']
            close_price = day_df['close']
            
            # 更新最高价
            if high_price > highest_price:
                highest_price = high_price
            
            # 计算持仓天数
            holding_days = (current_date - buy_date).days
            
            # 1. 检查硬止损
            if low_price <= stop_loss_price:
                sell_price = stop_loss_price * (1 - self.slippage)
                sell_date = current_date
                sell_reason = '硬止损'
                sell_triggered = True
                break
            
            # 2. 检查移动止盈（持仓至少 min_holding_days 天）
            if holding_days >= self.min_holding_days:
                trailing_stop_price = highest_price * (1 - self.trailing_stop_ratio)
                if low_price <= trailing_stop_price:
                    sell_price = trailing_stop_price * (1 - self.slippage)
                    sell_date = current_date
                    sell_reason = '移动止盈'
                    sell_triggered = True
                    break
            
            # 3. 检查时间止盈（最长持仓 max_holding_days 天）
            if holding_days >= self.max_holding_days:
                sell_price = close_price * (1 - self.slippage)
                sell_date = current_date
                sell_reason = '时间止盈'
                sell_triggered = True
                break
        
        if not sell_triggered:
            # 数据不足，无法完成交易
            return None
        
        # 计算收益（扣除交易成本）
        gross_return = (sell_price - buy_price) / buy_price
        total_cost = self.commission * 2 + self.stamp_tax + self.slippage * 2
        net_return = gross_return - total_cost
        
        return {
            'code': code,
            'buy_date': buy_date,
            'buy_price': buy_price,
            'sell_date': sell_date,
            'sell_price': sell_price,
            'sell_reason': sell_reason,
            'holding_days': (sell_date - buy_date).days,
            'gross_return': gross_return * 100,
            'net_return': net_return * 100,
            'highest_price': highest_price,
            'max_drawdown': (lowest_price_in_period(df, retrace_idx+2, i) - highest_price) / highest_price * 100 if 'lowest_price_in_period' in dir() else 0,
            'is_profitable': net_return > 0
        }
    
    def run_backtest(self, stocks_data: Dict[str, pd.DataFrame], 
                    period_start: str, period_end: str) -> List[Dict]:
        """
        运行完整回测
        
        Args:
            stocks_data: 股票数据字典 {code: DataFrame}
            period_start: 回测开始日期
            period_end: 回测结束日期
        
        Returns:
            list: 交易结果列表
        """
        print("=" * 80)
        print("开始回测（带买卖点规则）")
        print(f"回测时间段：{period_start} 到 {period_end}")
        print("=" * 80)
        print()
        
        total_stocks = len(stocks_data)
        matched_count = 0
        traded_count = 0
        
        results = []
        
        for idx, (code, df) in enumerate(stocks_data.items()):
            if (idx + 1) % 100 == 0:
                print(f"已处理：{idx+1}/{total_stocks}")
            
            # 检测形态
            pattern_info = self.check_pattern_single(df, code, period_start, period_end)
            
            if pattern_info:
                matched_count += 1
                
                # 模拟交易
                trade_result = self.simulate_trade(df, pattern_info)
                
                if trade_result:
                    traded_count += 1
                    results.append(trade_result)
                    print(f"✅ {code} - 买入：{trade_result['buy_date'].strftime('%Y-%m-%d')} - "
                          f"收益：{trade_result['net_return']:.2f}% ({trade_result['sell_reason']})")
                else:
                    print(f"⚠️  {code} - 形态匹配但无法完成交易（数据不足）")
        
        print()
        print("=" * 80)
        print(f"回测完成")
        print(f"检测股票数：{total_stocks}")
        print(f"匹配形态数：{matched_count}")
        print(f"完成交易数：{traded_count}")
        print("=" * 80)
        
        return results


def lowest_price_in_period(df: pd.DataFrame, start_idx: int, end_idx: int) -> float:
    """获取指定区间的最低价"""
    return df.iloc[start_idx:end_idx+1]['low'].min()


def analyze_trading_results(results: List[Dict]) -> Dict:
    """分析交易结果"""
    if len(results) == 0:
        return {}
    
    df_results = pd.DataFrame(results)
    
    # 基本统计
    total_trades = len(df_results)
    profitable_trades = len(df_results[df_results['is_profitable'] == True])
    loss_trades = total_trades - profitable_trades
    win_rate = profitable_trades / total_trades * 100
    
    avg_net_return = df_results['net_return'].mean()
    median_net_return = df_results['net_return'].median()
    max_profit = df_results['net_return'].max()
    max_loss = df_results['net_return'].min()
    
    avg_holding_days = df_results['holding_days'].mean()
    
    # 按卖出原因统计
    sell_reason_stats = df_results.groupby('sell_reason').agg({
        'net_return': ['mean', 'count'],
        'is_profitable': 'sum'
    }).round(2)
    
    return {
        'total_trades': total_trades,
        'profitable_trades': profitable_trades,
        'loss_trades': loss_trades,
        'win_rate': win_rate,
        'avg_net_return': avg_net_return,
        'median_net_return': median_net_return,
        'max_profit': max_profit,
        'max_loss': max_loss,
        'avg_holding_days': avg_holding_days,
        'sell_reason_stats': sell_reason_stats,
        'df_results': df_results
    }


def main():
    """主函数"""
    # 初始化回测器
    backtester = BacktesterWithRules('config.yaml')
    
    # 加载股票数据
    print("加载股票数据...")
    stocks_data = {}
    data_path = Path('data/backtest/baostocks')
    
    for csv_file in data_path.glob('*_data.csv'):
        code = csv_file.stem.replace('_data', '')
        try:
            df = pd.read_csv(csv_file)
            if len(df) > 30:  # 至少 30 个交易日
                stocks_data[code] = df
        except Exception as e:
            print(f"加载 {code} 失败：{e}")
    
    print(f"成功加载 {len(stocks_data)} 只股票数据")
    print()
    
    # 运行回测（当前时间范围）
    period_start = '2025-08-01'
    period_end = '2026-03-30'
    
    results = backtester.run_backtest(stocks_data, period_start, period_end)
    
    if len(results) > 0:
        # 分析结果
        stats = analyze_trading_results(results)
        
        print()
        print("=" * 80)
        print("交易结果统计")
        print("=" * 80)
        print()
        print(f"总交易数：{stats['total_trades']}")
        print(f"盈利交易：{stats['profitable_trades']} ({stats['win_rate']:.1f}%)")
        print(f"亏损交易：{stats['loss_trades']} ({100-stats['win_rate']:.1f}%)")
        print()
        print(f"平均净收益：{stats['avg_net_return']:.2f}%")
        print(f"中位数收益：{stats['median_net_return']:.2f}%")
        print(f"最高收益：{stats['max_profit']:.2f}%")
        print(f"最低收益：{stats['max_loss']:.2f}%")
        print()
        print(f"平均持仓天数：{stats['avg_holding_days']:.1f} 天")
        print()
        
        # 保存结果
        stats['df_results'].to_csv('trading_rules_backtest_results.csv', index=False, encoding='utf-8-sig')
        print("详细结果已保存到：trading_rules_backtest_results.csv")
    else:
        print("没有产生任何交易记录")


if __name__ == '__main__':
    main()
