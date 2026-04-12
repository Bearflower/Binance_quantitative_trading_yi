#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.2 回测器（2020-2025 年，剔除 2019 年数据）

实施顺序：
1. 剔除 2019 年数据，使用 2020-2025 年基础信号池
2. 流动性过滤（日均成交额≥3000 万）
3. 高开过滤（次日开盘价 > 前日收盘价×1.05 或涨停 → 跳过）
4. 信号间隔控制（记录每只股票上次信号日期，间隔<60 天则跳过）
5. 年度限制（每年最多 2 次）
"""

import pandas as pd
import yaml
from typing import Optional, Dict, List
from datetime import datetime
from collections import defaultdict


class BacktesterV22:
    """V2.2 回测器（2020-2025 年）"""
    
    def __init__(self, config_path: str = 'config_v21_final.yaml'):
        """初始化回测器"""
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 加载形态参数
        pattern_config = config.get('pattern', {})
        self.drop_threshold = pattern_config.get('drop_threshold', 0.12)
        self.surge_price_ratio = pattern_config.get('limit_up_threshold', 0.05)
        self.min_volume_ratio = pattern_config.get('min_volume_ratio', 1.5)
        self.max_volume_ratio = pattern_config.get('max_volume_ratio', 12.0)
        self.flat_days = pattern_config.get('flat_days', 0)
        self.flat_volume_threshold = pattern_config.get('flat_volume_threshold', 0.85)
        self.flat_price_range = pattern_config.get('flat_price_range', 0.08)
        self.volume_shrink_ratio = pattern_config.get('volume_shrink_ratio', 0.6)
        
        # 方案 B 参数
        self.use_scheme_b = pattern_config.get('flat_days', 0) == 0
        self.post_surge_check_days = pattern_config.get('post_surge_check_days', 5)
        self.post_surge_max_drop = pattern_config.get('post_surge_max_drop', 0.97)
        
        # 交易参数
        trading_config = config.get('trading', {})
        self.trailing_stop_ratio = trading_config.get('trailing_stop_ratio', 0.08)
        self.hard_stop_loss = trading_config.get('hard_stop_loss', 0.10)
        self.min_hold_days = trading_config.get('min_hold_days', 5)
        self.max_hold_days = trading_config.get('max_hold_days', 30)
        self.commission = trading_config.get('commission', 0.00025)
        self.stamp_tax = trading_config.get('stamp_tax', 0.001)
        self.slippage = trading_config.get('slippage', 0.001)
        
        # 流动性过滤
        self.min_avg_volume = trading_config.get('min_avg_volume', 30_000_000)
        self.volume_check_period = trading_config.get('volume_check_period', 20)
        
        # 高开过滤参数
        self.skip_high_open_threshold = trading_config.get('skip_high_open_threshold', 0.05)
        self.skip_limit_up_open = trading_config.get('skip_limit_up_open', True)
        
        # 信号间隔控制（全局状态）
        self.stock_last_signal_date = defaultdict(lambda: None)  # {stock_code: last_buy_date}
        
        # 年度限制（全局状态）
        self.stock_yearly_count = defaultdict(lambda: defaultdict(int))  # {stock_code: {year: count}}
        
        # 涨跌停判断参数
        self.limit_up_ratio = 0.10  # 主板 10% 涨停
    
    def check_liquidity(self, df, retrace_idx):
        """
        检查股票流动性（步骤 1）
        """
        if retrace_idx < self.volume_check_period:
            return False
        
        start_idx = retrace_idx - self.volume_check_period
        end_idx = retrace_idx + 1
        recent_df = df.iloc[start_idx:end_idx].copy()
        
        if 'amount' in recent_df.columns:
            avg_amount = recent_df['amount'].mean()
        else:
            recent_amount = recent_df['volume'] * recent_df['close']
            avg_amount = recent_amount.mean()
        
        return avg_amount >= self.min_avg_volume
    
    def check_high_open(self, df, retrace_idx, buy_idx):
        """
        检查高开情况（步骤 2）
        
        Args:
            df: K 线数据 DataFrame
            retrace_idx: 回踩确认日的索引
            buy_idx: 买入日（次日）的索引
        
        Returns:
            bool: True=可以买入，False=高开过多跳过
        """
        if buy_idx >= len(df):
            return False
        
        retrace_day = df.iloc[retrace_idx]
        buy_day = df.iloc[buy_idx]
        
        retrace_close = retrace_day['close']
        buy_open = buy_day['open']
        
        # 计算高开幅度
        high_open_ratio = (buy_open - retrace_close) / retrace_close
        
        # 判断涨停（简化：开盘涨幅≥10% 视为涨停）
        is_limit_up = high_open_ratio >= self.limit_up_ratio
        
        # 高开>5%
        is_high_open = high_open_ratio > self.skip_high_open_threshold
        
        if is_limit_up and self.skip_limit_up_open:
            return False  # 涨停开盘，跳过
        
        if is_high_open:
            return False  # 高开>5%，跳过
        
        return True  # 可以买入
    
    def check_signal_interval(self, code: str, buy_date: pd.Timestamp) -> bool:
        """
        检查信号间隔（步骤 3）
        
        Args:
            code: 股票代码
            buy_date: 本次买入日期
        
        Returns:
            bool: True=允许交易，False=间隔不足跳过
        """
        last_signal_date = self.stock_last_signal_date[code]
        
        if last_signal_date is None:
            return True  # 第一次交易，允许
        
        # 计算间隔天数（自然日）
        days_since_last = (buy_date - pd.to_datetime(last_signal_date)).days
        
        if days_since_last < 60:
            return False  # 间隔不足 60 天，跳过
        
        return True  # 间隔足够，允许
    
    def check_yearly_limit(self, code: str, buy_date: pd.Timestamp) -> bool:
        """
        检查年度限制（步骤 4）
        
        Args:
            code: 股票代码
            buy_date: 本次买入日期
        
        Returns:
            bool: True=允许交易，False=已达年度上限
        """
        buy_year = buy_date.year
        
        if self.stock_yearly_count[code][buy_year] >= 2:
            return False  # 已达年度上限（2 次）
        
        return True  # 未达上限，允许
    
    def update_signal_record(self, code: str, buy_date: pd.Timestamp):
        """
        更新信号记录（交易后调用）
        """
        # 更新上次信号日期
        self.stock_last_signal_date[code] = buy_date
        
        # 更新年度计数
        buy_year = buy_date.year
        self.stock_yearly_count[code][buy_year] += 1
    
    def check_all_patterns(self, df: pd.DataFrame, code: str, 
                            period_start: str, period_end: str) -> List[Dict]:
        """
        检测所有符合条件的形态（2020-2025 年）
        注意：使用完整数据（包含 2019 年）来计算跌幅，但只统计 2020-2025 年的买入信号
        """
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        
        # V2.2: 使用完整数据计算跌幅，但只统计 2020-2025 年的买入信号
        # 需要 2019 年数据来计算 2020 年初的跌幅
        period_start_dt = pd.to_datetime('2019-01-01')  # 使用 2019 年开始的数据计算跌幅
        period_end_dt = pd.to_datetime('2025-12-31')
        
        # 筛选时间段（包含 2019 年用于计算）
        period_df = df[(df['date'] >= period_start_dt) & (df['date'] <= period_end_dt)].copy()
        
        if len(period_df) < 30:
            return []
        
        all_patterns = []
        search_start_idx = 0
        
        while search_start_idx < len(period_df) - 30:
            temp_df = period_df.iloc[search_start_idx:].copy()
            temp_df = temp_df.reset_index(drop=True)
            
            if len(temp_df) < 30:
                break
            
            pattern = self._detect_pattern_from_start(temp_df, code)
            
            if pattern:
                # V2.2: 应用所有过滤规则
                retrace_date = pd.to_datetime(pattern['retrace_date'])
                buy_date = retrace_date + pd.Timedelta(days=1)
                
                # 找到买入日在原始 DataFrame 中的索引
                buy_idx_in_period = period_df[period_df['date'] == buy_date].index
                if len(buy_idx_in_period) == 0:
                    search_start_idx += 20
                    continue
                
                buy_idx = buy_idx_in_period[0]
                retrace_idx_in_period = period_df[period_df['date'] == retrace_date].index[0]
                
                # 步骤 1: 流动性过滤
                if not self.check_liquidity(period_df, retrace_idx_in_period):
                    search_start_idx += 20
                    continue
                
                # 步骤 2: 高开过滤
                if not self.check_high_open(period_df, retrace_idx_in_period, buy_idx):
                    search_start_idx += 20
                    continue
                
                # 步骤 3: 信号间隔控制
                if not self.check_signal_interval(code, buy_date):
                    search_start_idx += 20
                    continue
                
                # 步骤 4: 年度限制
                if not self.check_yearly_limit(code, buy_date):
                    search_start_idx += 20
                    continue
                
                # 通过所有过滤，添加到结果
                all_patterns.append(pattern)
                
                # 更新信号记录
                self.update_signal_record(code, buy_date)
                
                # 从回踩日之后继续搜索
                search_start_idx += retrace_idx_in_period + 1
            else:
                break
        
        return all_patterns
    
    def _detect_pattern_from_start(self, period_df: pd.DataFrame, code: str) -> Optional[Dict]:
        """
        从给定数据的起始位置检测形态（内部方法）
        """
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
        
        # 2. 检测缩量见底
        shrink_found = False
        shrink_idx = -1
        
        avg_volume_20 = period_df.iloc[drop_end_idx:drop_end_idx+20]['volume'].mean() if drop_end_idx+20 <= len(period_df) else period_df.iloc[drop_end_idx:]['volume'].mean()
        
        for i in range(drop_end_idx, min(drop_end_idx + 10, len(period_df))):
            if period_df.iloc[i]['volume'] < avg_volume_20 * self.volume_shrink_ratio:
                shrink_found = True
                shrink_idx = i
                break
        
        if not shrink_found:
            return None
        
        # 3. 检测放量大涨
        surge_found = False
        surge_idx = -1
        
        for i in range(shrink_idx, min(shrink_idx + 15, len(period_df))):
            prev_close = period_df.iloc[i-1]['close'] if i > 0 else period_df.iloc[i]['close']
            price_change = (period_df.iloc[i]['close'] - prev_close) / prev_close
            volume_ratio = period_df.iloc[i]['volume'] / avg_volume_20 if avg_volume_20 > 0 else 0
            
            if price_change >= self.surge_price_ratio and volume_ratio >= self.min_volume_ratio and volume_ratio <= self.max_volume_ratio:
                surge_found = True
                surge_idx = i
                break
        
        if not surge_found:
            return None
        
        # 4. 检测回踩确认
        retrace_found = False
        retrace_idx = -1
        support_level = period_df.iloc[surge_idx]['close']
        
        surge_close = period_df.iloc[surge_idx]['close']
        surge_low = period_df.iloc[surge_idx]['low']
        mid_price = (surge_close + surge_low) / 2
        
        for i in range(surge_idx + 1, min(surge_idx + 10, len(period_df))):
            current_low = period_df.iloc[i]['low']
            current_close = period_df.iloc[i]['close']
            
            if current_low >= mid_price and current_close <= surge_close * 1.02:
                retrace_found = True
                retrace_idx = i
                break
        
        if not retrace_found:
            return None
        
        # 方案 B: 启动后观察 5 天不破支撑
        if self.use_scheme_b:
            post_surge_ok = True
            for i in range(surge_idx + 1, min(surge_idx + 1 + self.post_surge_check_days, len(period_df))):
                if period_df.iloc[i]['low'] < surge_low * self.post_surge_max_drop:
                    post_surge_ok = False
                    break
            
            if not post_surge_ok:
                return None
        
        # 构建形态信息
        return {
            'code': code,
            'drop_start_date': str(period_df.iloc[drop_start_idx]['date']),
            'drop_end_date': str(period_df.iloc[drop_end_idx]['date']),
            'shrink_date': str(period_df.iloc[shrink_idx]['date']),
            'surge_date': str(period_df.iloc[surge_idx]['date']),
            'retrace_date': str(period_df.iloc[retrace_idx]['date']),
            'support_level': support_level,
            'surge_low': surge_low,
        }
    
    def simulate_trade(self, df: pd.DataFrame, pattern_info: Dict) -> Optional[Dict]:
        """
        模拟交易
        """
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        
        retrace_date = pd.to_datetime(pattern_info['retrace_date'])
        buy_date = retrace_date + pd.Timedelta(days=1)
        
        # 找到买入日索引
        buy_idx = df[df['date'] == buy_date].index
        if len(buy_idx) == 0:
            return None
        
        buy_idx = buy_idx[0]
        buy_price = df.iloc[buy_idx]['open']
        
        # 应用滑点到买入价
        buy_price *= (1 + self.slippage)
        
        # 模拟持仓
        hold_days = 0
        peak_price = buy_price
        stop_loss_price = buy_price * (1 - self.hard_stop_loss)
        
        sell_date = None
        sell_price = None
        sell_reason = None
        
        for i in range(buy_idx + 1, len(df)):
            hold_days += 1
            current_low = df.iloc[i]['low']
            current_close = df.iloc[i]['close']
            
            # 更新最高价
            if current_close > peak_price:
                peak_price = current_close
            
            # 移动止盈
            trailing_stop = peak_price * (1 - self.trailing_stop_ratio)
            
            # 检查卖出条件
            if current_low <= trailing_stop:
                sell_date = df.iloc[i]['date']
                sell_price = trailing_stop
                sell_reason = '移动止盈'
                break
            
            # 硬止损
            if current_low <= stop_loss_price:
                sell_date = df.iloc[i]['date']
                sell_price = stop_loss_price
                sell_reason = '硬止损'
                break
            
            # 最短持仓
            if hold_days >= self.min_hold_days:
                # 检查是否达到最长持仓
                if hold_days >= self.max_hold_days:
                    sell_date = df.iloc[i]['date']
                    sell_price = current_close
                    sell_reason = '到期卖出'
                    break
        
        if sell_date is None:
            # 持有到最后一天
            sell_date = df.iloc[-1]['date']
            sell_price = df.iloc[-1]['close']
            sell_reason = '期末卖出'
        
        # 计算收益
        gross_return = (sell_price - buy_price) / buy_price
        total_cost = self.commission + self.stamp_tax + self.slippage
        net_return = gross_return - total_cost
        
        return {
            'buy_date': str(buy_date),
            'sell_date': str(sell_date),
            'buy_price': buy_price,
            'sell_price': sell_price,
            'net_return': net_return,
            'holding_days': hold_days,
            'sell_reason': sell_reason,
        }
    
    def reset_state(self):
        """重置全局状态（用于多次回测）"""
        self.stock_last_signal_date.clear()
        self.stock_yearly_count.clear()
