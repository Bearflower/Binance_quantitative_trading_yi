#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.5 回测器：
- 放宽回踩确认要求，从 97% 放宽到 94%
- 其他参数保持 V2.4 不变
"""

import pandas as pd
import yaml
from typing import Optional, Dict, List
from datetime import datetime


class BacktesterV25:
    """V2.5 回测器（放宽回踩确认要求）"""
    
    def __init__(self, config_path: str = 'config_v21_final.yaml'):
        """初始化回测器"""
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # V2.5 参数设置（基于 V2.4，仅放宽回踩要求）
        pattern_config = config.get('pattern', {})
        
        # 核心参数（与 V2.4 相同）
        self.drop_threshold = 0.08  # 跌幅 8%
        self.surge_price_ratio = 0.03  # 放量涨幅 3%
        self.min_volume_ratio = 1.2  # 量比 1.2
        self.max_volume_ratio = 15.0  # 量比 15
        self.volume_shrink_ratio = 0.8  # 缩量 80%
        self.shrink_to_surge_days = 60  # 时间窗口 60 天
        
        # V2.5 关键改进：放宽回踩确认要求
        self.flat_days = pattern_config.get('flat_days', 0)
        self.flat_volume_threshold = pattern_config.get('flat_volume_threshold', 0.85)
        self.flat_price_range = pattern_config.get('flat_price_range', 0.08)
        self.use_scheme_b = self.flat_days == 0
        self.post_surge_check_days = pattern_config.get('post_surge_check_days', 5)
        self.post_surge_max_drop = 0.94  # V2.5: 从 0.97 放宽到 0.94（允许回踩 6%）
        
        # 交易参数
        trading_config = config.get('trading', {})
        self.trailing_stop_ratio = trading_config.get('trailing_stop_ratio', 0.08)
        self.hard_stop_loss = trading_config.get('hard_stop_loss', 0.10)
        self.min_hold_days = trading_config.get('min_hold_days', 5)
        self.max_hold_days = trading_config.get('max_hold_days', 30)
        self.commission = trading_config.get('commission', 0.00025)
        self.stamp_tax = trading_config.get('stamp_tax', 0.001)
        self.slippage = trading_config.get('slippage', 0.001)
        self.min_avg_volume = trading_config.get('min_avg_volume', 20_000_000)
        self.volume_check_period = trading_config.get('volume_check_period', 20)
        self.entry_timing = trading_config.get('entry_timing', 'next_open')
        self.skip_high_open_threshold = trading_config.get('skip_high_open_threshold', 0.05)
        self.skip_limit_up_open = trading_config.get('skip_limit_up_open', True)
    
    def check_all_patterns(self, df: pd.DataFrame, code: str, 
                            period_start: str, period_end: str) -> List[Dict]:
        """检测所有符合条件的形态"""
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        
        period_start_dt = pd.to_datetime(period_start)
        period_end_dt = pd.to_datetime(period_end)
        
        period_df = df[(df['date'] >= period_start_dt) & (df['date'] <= period_end_dt)].copy()
        
        if len(period_df) < 60:
            return []
        
        patterns = []
        
        # 遍历所有可能的起始点
        for i in range(len(period_df) - 60):
            pattern = self._detect_pattern_from_start(period_df, i)
            if pattern:
                pattern['stock_code'] = code
                pattern['stock_name'] = ''
                patterns.append(pattern)
        
        return patterns
    
    def _detect_pattern_from_start(self, df: pd.DataFrame, start_idx: int) -> Optional[Dict]:
        """从指定位置开始检测形态"""
        # 1. 检测大跌
        drop_result = self._detect_big_drop(df, start_idx)
        if not drop_result:
            return None
        
        # 2. 检测缩量
        shrink_result = self._detect_volume_shrink(df, drop_result['end_idx'])
        if not shrink_result:
            return None
        
        # 3. 检测放量上涨
        surge_result = self._detect_volume_surge(df, shrink_result['end_idx'])
        if not surge_result:
            return None
        
        # 4. 检测回踩确认
        retrace_result = self._detect_retrace(df, surge_result['end_idx'], surge_result['surge_close'])
        if not retrace_result:
            return None
        
        return {
            '大跌日期': df.loc[drop_result['drop_date_idx'], 'date'],
            '大跌跌幅': drop_result['drop_ratio'],
            '缩量日期': df.loc[shrink_result['shrink_date_idx'], 'date'],
            '放量日期': df.loc[surge_result['surge_date_idx'], 'date'],
            '缩量到放量天数': (df.loc[surge_result['surge_date_idx'], 'date'] - 
                               df.loc[shrink_result['shrink_date_idx'], 'date']).days,
            '放量涨幅': surge_result['surge_ratio'],
            '放量量比': surge_result['volume_ratio'],
            '回踩日期': df.loc[retrace_result['retrace_date_idx'], 'date'],
            '回踩确认日期': df.loc[retrace_result['confirm_date_idx'], 'date'],
            '建议买入日期': df.loc[retrace_result['confirm_date_idx'] + 1, 'date'] 
                           if retrace_result['confirm_date_idx'] + 1 < len(df) else None,
            '支撑位': retrace_result['support_price'],
            '止损价': retrace_result['support_price'] * self.post_surge_max_drop,
            '放量收盘价': surge_result['surge_close']
        }
    
    def _detect_big_drop(self, df: pd.DataFrame, start_idx: int) -> Optional[Dict]:
        """检测大跌"""
        df = df.reset_index(drop=True)
        
        # 查找 start_idx 之后 30 天内的大跌
        for i in range(start_idx, min(start_idx + 30, len(df) - 20)):
            # 计算从最高点下跌的幅度
            window_high = df.loc[i:i+20, 'close'].max()
            current_close = df.loc[i, 'close']
            drop_ratio = (window_high - current_close) / window_high
            
            if drop_ratio >= self.drop_threshold:
                return {
                    'drop_date_idx': i,
                    'drop_ratio': drop_ratio,
                    'end_idx': i
                }
        
        return None
    
    def _detect_volume_shrink(self, df: pd.DataFrame, start_idx: int) -> Optional[Dict]:
        """检测缩量"""
        df = df.reset_index(drop=True)
        
        # 计算 20 日平均成交量
        avg_volume_20 = df.loc[max(0, start_idx-19):start_idx, 'volume'].mean()
        
        # 查找 start_idx 之后 60 天内的缩量
        for i in range(start_idx, min(start_idx + self.shrink_to_surge_days, len(df))):
            current_volume = df.loc[i, 'volume']
            volume_ratio = current_volume / avg_volume_20
            
            if volume_ratio <= (1 - self.volume_shrink_ratio):
                return {
                    'shrink_date_idx': i,
                    'volume_ratio': volume_ratio,
                    'end_idx': i
                }
        
        return None
    
    def _detect_volume_surge(self, df: pd.DataFrame, start_idx: int) -> Optional[Dict]:
        """检测放量上涨"""
        df = df.reset_index(drop=True)
        
        # 计算 20 日平均成交量
        avg_volume_20 = df.loc[max(0, start_idx-19):start_idx, 'volume'].mean()
        
        # 查找 start_idx 之后 60 天内的放量上涨
        for i in range(start_idx, min(start_idx + self.shrink_to_surge_days, len(df))):
            current_volume = df.loc[i, 'volume']
            current_close = df.loc[i, 'close']
            prev_close = df.loc[i-1, 'close'] if i > 0 else current_close
            
            volume_ratio = current_volume / avg_volume_20
            price_ratio = (current_close - prev_close) / prev_close
            
            if (price_ratio >= self.surge_price_ratio and 
                self.min_volume_ratio <= volume_ratio <= self.max_volume_ratio):
                return {
                    'surge_date_idx': i,
                    'surge_ratio': price_ratio,
                    'volume_ratio': volume_ratio,
                    'surge_close': current_close,
                    'end_idx': i
                }
        
        return None
    
    def _detect_retrace(self, df: pd.DataFrame, start_idx: int, surge_close: float) -> Optional[Dict]:
        """检测回踩确认"""
        df = df.reset_index(drop=True)
        
        # V2.5: 放宽回踩要求到 94%
        min_price_threshold = surge_close * self.post_surge_max_drop
        
        # 查找 start_idx 之后 10 天内的回踩
        min_price = float('inf')
        min_price_idx = -1
        
        for i in range(start_idx + 1, min(start_idx + 10, len(df))):
            low_price = df.loc[i, 'low']
            if low_price < min_price:
                min_price = low_price
                min_price_idx = i
            
            # 检查是否跌破支撑
            if low_price < min_price_threshold:
                return None
        
        # 找到回踩确认的点（价格企稳）
        for i in range(start_idx + 1, min(start_idx + 10, len(df))):
            close_price = df.loc[i, 'close']
            
            # 回踩不破支撑，且收盘上涨
            if min_price >= min_price_threshold and close_price > df.loc[i-1, 'close']:
                return {
                    'retrace_date_idx': min_price_idx,
                    'confirm_date_idx': i,
                    'support_price': min_price,
                    'min_price': min_price
                }
        
        return None
    
    def simulate_trade(self, df: pd.DataFrame, pattern: Dict) -> Dict:
        """模拟交易"""
        if not pattern.get('建议买入日期'):
            return None
        
        buy_date = pattern['建议买入日期']
        buy_idx = df[df['date'] == buy_date].index
        
        if len(buy_idx) == 0:
            return None
        
        buy_idx = buy_idx[0]
        buy_price = df.loc[buy_idx, 'close']
        
        # 计算后续收益
        future_df = df.iloc[buy_idx:]
        
        # 最高价和最低价
        max_price = future_df['high'].max()
        min_price = future_df['low'].min()
        
        # 计算最大收益和最大亏损
        max_return = (max_price - buy_price) / buy_price
        min_return = (min_price - buy_price) / buy_price
        
        # 止盈止损
        stop_profit_price = buy_price * (1 + self.trailing_stop_ratio)
        stop_loss_price = buy_price * (1 - self.hard_stop_loss)
        
        # 实际收益（考虑止盈止损）
        actual_return = 0
        exit_price = buy_price
        exit_date = None
        
        for i in range(buy_idx + 1, len(df)):
            low_price = df.loc[i, 'low']
            high_price = df.loc[i, 'high']
            close_price = df.loc[i, 'close']
            
            # 止损
            if low_price <= stop_loss_price:
                exit_price = stop_loss_price
                exit_date = df.loc[i, 'date']
                actual_return = (exit_price - buy_price) / buy_price
                break
            
            # 止盈
            if high_price >= stop_profit_price:
                exit_price = stop_profit_price
                exit_date = df.loc[i, 'date']
                actual_return = (exit_price - buy_price) / buy_price
                break
        
        return {
            '买入日期': buy_date,
            '买入价格': buy_price,
            '最高价': max_price,
            '最低价': min_price,
            '最大收益': max_return,
            '最大亏损': min_return,
            '实际收益': actual_return,
            '退出价格': exit_price,
            '退出日期': exit_date,
            '是否盈利': actual_return > 0
        }
