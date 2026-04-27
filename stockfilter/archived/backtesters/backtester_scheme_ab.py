#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方案 A+B 回测器：
- 方案 A：放宽参数（缩量走平 1 天）
- 方案 B：取消缩量走平，改为启动后 N 日内不破支撑
"""

import pandas as pd
import yaml
from typing import Optional, Dict, List
from datetime import datetime


class BacktesterWithRules_AB:
    """方案 A+B 回测器"""
    
    def __init__(self, config_path: str = 'config.yaml'):
        """初始化回测器"""
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 加载形态参数
        pattern_config = config.get('pattern', {})
        self.drop_threshold = pattern_config.get('drop_threshold', 0.12)
        self.surge_price_ratio = pattern_config.get('limit_up_threshold', 0.05)
        self.min_volume_ratio = pattern_config.get('min_volume_ratio', 1.5)
        self.max_volume_ratio = pattern_config.get('max_volume_ratio', 12.0)
        self.flat_days = pattern_config.get('flat_days', 1)
        self.flat_volume_threshold = pattern_config.get('flat_volume_threshold', 0.85)
        self.flat_price_range = pattern_config.get('flat_price_range', 0.08)
        self.volume_shrink_ratio = pattern_config.get('volume_shrink_ratio', 0.6)
        
        # 方案 B 参数
        self.use_scheme_b = pattern_config.get('flat_days', 1) == 0
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
        
        # 大盘过滤
        self.enable_market_filter = trading_config.get('enable_market_filter', False)
        self.market_index_path = trading_config.get('market_index_path', '')
        self.market_ma20_ratio = trading_config.get('market_ma20_ratio', 1.0)
        self.market_index_data = None
        
        if self.enable_market_filter and self.market_index_path:
            self._load_market_index()
        
        # 流动性过滤（V2.1 新增）
        self.min_avg_volume = trading_config.get('min_avg_volume', 30_000_000)
        self.volume_check_period = trading_config.get('volume_check_period', 20)
    
    def _load_market_index(self):
        """加载大盘指数数据"""
        try:
            self.market_index_data = pd.read_csv(self.market_index_path)
            self.market_index_data['date'] = pd.to_datetime(self.market_index_data['date'])
            
            # 计算 MA20
            self.market_index_data['ma20'] = self.market_index_data['close'].rolling(window=20).mean()
        except Exception as e:
            print(f"加载指数数据失败：{e}")
            self.market_index_data = None
    
    def check_liquidity(self, df, retrace_idx):
        """
        检查股票流动性（V2.1 新增）
        
        Args:
            df: K 线数据 DataFrame
            retrace_idx: 回踩确认日的索引
        
        Returns:
            bool: 是否满足流动性要求
        """
        if retrace_idx < self.volume_check_period:
            return False
        
        # 获取回踩日前 lookback_days 天的数据
        start_idx = retrace_idx - self.volume_check_period
        end_idx = retrace_idx + 1
        
        recent_df = df.iloc[start_idx:end_idx].copy()
        
        # 使用 amount 列（成交额，单位：元）
        if 'amount' in recent_df.columns:
            avg_amount = recent_df['amount'].mean()
        else:
            # 如果没有 amount 列，用 volume * close 估算
            recent_amount = recent_df['volume'] * recent_df['close']
            avg_amount = recent_amount.mean()
        
        return avg_amount >= self.min_avg_volume
    
    def check_market_condition(self, date: pd.Timestamp) -> bool:
        """检查大盘状态"""
        if not self.enable_market_filter or self.market_index_data is None:
            return True
        
        idx = self.market_index_data[self.market_index_data['date'] <= date].index
        if len(idx) == 0:
            return True
        
        latest_idx = idx[-1]
        row = self.market_index_data.loc[latest_idx]
        
        close = float(row['close'])
        ma20 = float(row['ma20'])
        
        if pd.isna(ma20):
            return True
        
        ratio = close / ma20
        return ratio >= self.market_ma20_ratio
    
    def check_all_patterns(self, df: pd.DataFrame, code: str, 
                            period_start: str, period_end: str) -> List[Dict]:
        """
        检测所有符合条件的形态（不只是第一个）
        
        Returns:
            List[Dict]: 所有形态的列表
        """
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        
        # 转换日期
        period_start_dt = pd.to_datetime(period_start)
        period_end_dt = pd.to_datetime(period_end)
        
        # 筛选时间段
        period_df = df[(df['date'] >= period_start_dt) & (df['date'] <= period_end_dt)].copy()
        
        if len(period_df) < 30:
            return []
        
        all_patterns = []
        search_start_idx = 0  # 从第一个位置开始搜索
        
        # 循环检测，直到数据末尾
        while search_start_idx < len(period_df) - 30:
            # 从当前位置开始检测
            temp_df = period_df.iloc[search_start_idx:].copy()
            temp_df = temp_df.reset_index(drop=True)
            
            if len(temp_df) < 30:
                break
            
            # 使用原有方法检测
            pattern = self._detect_pattern_from_start(temp_df, code)
            
            if pattern:
                all_patterns.append(pattern)
                # 从回踩日之后继续搜索下一个形态
                retrace_date = pd.to_datetime(pattern['retrace_date'])
                retrace_idx_in_temp = temp_df[temp_df['date'] == retrace_date].index
                if len(retrace_idx_in_temp) > 0:
                    search_start_idx += retrace_idx_in_temp[0] + 1
                else:
                    search_start_idx += 20  # 默认跳过 20 天
            else:
                break  # 没有更多形态
        
        return all_patterns
    
    def _detect_pattern_from_start(self, period_df: pd.DataFrame, code: str) -> Optional[Dict]:
        """
        从给定数据的起始位置检测形态（内部方法）
        这是 check_pattern_single 的核心逻辑，但不 break，而是返回单个形态
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
        
        # 3. 检测放量涨停
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
            if vol_ratio >= self.min_volume_ratio and price_change >= self.surge_price_ratio:
                # 量比要求
                if not (self.min_volume_ratio <= vol_ratio <= self.max_volume_ratio):
                    continue
                
                # 方案 A：缩量走平
                if self.flat_days > 0:
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
                    
                    surge_found = True
                    surge_idx = j
                    break
                
                # 方案 B：取消缩量走平，直接检测启动后 N 天内不破支撑
                else:
                    # 检测启动后 post_surge_check_days 天内是否不破支撑
                    support_level = close_j * self.post_surge_max_drop
                    is_valid = True
                    
                    for k in range(j+1, min(j+1+self.post_surge_check_days, len(period_df))):
                        low_k = period_df['low'].iloc[k]
                        if low_k < support_level:
                            is_valid = False
                            break
                    
                    if is_valid:
                        surge_found = True
                        surge_idx = j
                        break
        
        if not surge_found:
            return None
        
        # 4. 检测回踩确认
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
        
        # V2.1 新增：检查流动性（回踩日前 20 天日均成交额）
        if not self.check_liquidity(period_df, retrace_idx):
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
            'is_match': True,
            'scheme': 'A+B' if self.use_scheme_b else 'A'
        }
    
    def check_pattern_single(self, df: pd.DataFrame, code: str, 
                            period_start: str, period_end: str) -> Optional[Dict]:
        """
        检测单只股票是否符合形态（方案 A+B）
        （保留原有方法，只检测第一个形态）
        
        Returns:
            dict: 形态信息（如果符合），否则 None
        """
        # 调用新方法，但只返回第一个形态
        all_patterns = self.check_all_patterns(df, code, period_start, period_end)
        if all_patterns and len(all_patterns) > 0:
            return all_patterns[0]
        return None
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
        
        # 3. 检测放量涨停
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
            if vol_ratio >= self.min_volume_ratio and price_change >= self.surge_price_ratio:
                # 量比要求
                if not (self.min_volume_ratio <= vol_ratio <= self.max_volume_ratio):
                    continue
                
                # 方案 A：缩量走平
                if self.flat_days > 0:
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
                    
                    surge_found = True
                    surge_idx = j
                    break
                
                # 方案 B：取消缩量走平，直接检测启动后 N 日内不破支撑
                else:
                    # 检测启动后 post_surge_check_days 天内是否不破支撑
                    support_level = close_j * self.post_surge_max_drop
                    is_valid = True
                    
                    for k in range(j+1, min(j+1+self.post_surge_check_days, len(period_df))):
                        low_k = period_df['low'].iloc[k]
                        if low_k < support_level:
                            is_valid = False
                            break
                    
                    if is_valid:
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
        
        # V2.1 新增：检查流动性（回踩日前 20 天日均成交额）
        if not self.check_liquidity(period_df, retrace_idx):
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
            'is_match': True,
            'scheme': 'A+B' if self.use_scheme_b else 'A'
        }
    
    def simulate_trade(self, df: pd.DataFrame, pattern_info: Dict) -> Optional[Dict]:
        """模拟交易（与原版相同）"""
        code = pattern_info['code']
        retrace_date = pd.to_datetime(pattern_info['retrace_date'])
        buy_price = None
        sell_price = None
        sell_date = None
        sell_reason = None
        
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        
        retrace_idx_list = df[df['date'] == retrace_date].index
        if len(retrace_idx_list) == 0:
            return None
        retrace_idx = retrace_idx_list[0]
        
        if retrace_idx + 1 >= len(df):
            return None
        
        next_day = df.iloc[retrace_idx + 1]
        open_price = next_day['open']
        prev_close = df.iloc[retrace_idx]['close']
        
        open_change = (open_price - prev_close) / prev_close
        
        if open_change >= 0.05:
            return None
        
        if open_change > 0.03:
            buy_price = open_price * (1 + 0.01)
        else:
            buy_price = open_price
        
        buy_date = next_day['date']
        
        # 检查大盘
        if not self.check_market_condition(buy_date):
            return None
        
        # 模拟卖出
        high_since_buy = buy_price
        close_since_buy = buy_price
        
        for i in range(retrace_idx + 2, min(retrace_idx + 2 + self.max_hold_days, len(df))):
            row = df.iloc[i]
            current_date = row['date']
            current_close = row['close']
            current_high = row['high']
            current_low = row['low']
            
            hold_days = (current_date - buy_date).days
            
            if current_high > high_since_buy:
                high_since_buy = current_high
            
            trailing_stop_price = high_since_buy * (1 - self.trailing_stop_ratio)
            hard_stop_price = buy_price * (1 - self.hard_stop_loss)
            
            if hold_days >= self.min_hold_days:
                if current_low <= trailing_stop_price:
                    sell_price = trailing_stop_price
                    sell_reason = '移动止盈'
                    sell_date = current_date
                    break
        
            if current_low <= hard_stop_price and hold_days >= self.min_hold_days:
                sell_price = hard_stop_price
                sell_reason = '硬止损'
                sell_date = current_date
                break
            
            if hold_days >= self.max_hold_days:
                sell_price = current_close
                sell_reason = '时间止盈'
                sell_date = current_date
                break
            
            close_since_buy = current_close
        
        if sell_price is None:
            sell_price = close_since_buy
            sell_reason = '未触发卖出'
            sell_date = df.iloc[min(retrace_idx + 2 + self.max_hold_days - 1, len(df)-1)]['date']
        
        gross_return = (sell_price - buy_price) / buy_price
        cost = self.commission * 2 + self.stamp_tax + self.slippage * 2
        net_return = gross_return - cost
        
        return {
            'code': code,
            'buy_date': buy_date,
            'sell_date': sell_date,
            'buy_price': buy_price,
            'sell_price': sell_price,
            'gross_return': gross_return,
            'net_return': net_return,
            'holding_days': (sell_date - buy_date).days,
            'sell_reason': sell_reason
        }
