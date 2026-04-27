#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一回测器:
- 支持多版本参数配置
- 通过配置文件切换版本
- 保留所有版本的核心功能
"""

import pandas as pd
import yaml
from typing import Optional, Dict, List
from datetime import datetime


class Backtester:
    """统一回测器,支持版本切换"""

    # 版本参数预设
    VERSION_PARAMS = {
        'v22': {
            'drop_threshold': 0.12,
            'surge_price_ratio': 0.05,
            'min_volume_ratio': 1.5,
            'max_volume_ratio': 12.0,
            'volume_shrink_ratio': 0.6,
            'shrink_to_surge_days': 10,
        },
        'v23': {
            'drop_threshold': 0.10,
            'surge_price_ratio': 0.04,
            'min_volume_ratio': 1.3,
            'max_volume_ratio': 13.0,
            'volume_shrink_ratio': 0.7,
            'shrink_to_surge_days': 20,
        },
        'v24': {
            'drop_threshold': 0.08,
            'surge_price_ratio': 0.03,
            'min_volume_ratio': 1.2,
            'max_volume_ratio': 15.0,
            'volume_shrink_ratio': 0.8,
            'shrink_to_surge_days': 60,
        },
        'v25': {
            'drop_threshold': 0.09,
            'surge_price_ratio': 0.035,
            'min_volume_ratio': 1.3,
            'max_volume_ratio': 15.0,
            'volume_shrink_ratio': 0.8,
            'shrink_to_surge_days': 50,
        },
    }

    def __init__(self, config_path: str = 'config/config.yaml', version: str = 'v24'):
        """
        初始化回测器

        Args:
            config_path: 配置文件路径
            version: 版本号(v22, v23, v24, v25)
        """
        # 加载配置文件
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)
        except FileNotFoundError:
            # 如果配置文件不存在,使用默认配置
            self.config = {}

        # 设置版本
        self.version = version

        # 加载版本参数
        self._load_version_params()

        # 加载交易参数
        self._load_trading_params()

    def _load_version_params(self):
        """加载版本参数"""
        # 优先使用配置文件中的参数,其次使用预设参数
        version_config = self.config.get('versions', {}).get(self.version, {})

        # 获取版本预设参数
        default_params = self.VERSION_PARAMS.get(self.version, self.VERSION_PARAMS['v24'])

        # 合并参数(配置文件优先)
        self.drop_threshold = version_config.get('drop_threshold', default_params['drop_threshold'])
        self.surge_price_ratio = version_config.get('surge_price_ratio', default_params['surge_price_ratio'])
        self.min_volume_ratio = version_config.get('min_volume_ratio', default_params['min_volume_ratio'])
        self.max_volume_ratio = version_config.get('max_volume_ratio', default_params['max_volume_ratio'])
        self.volume_shrink_ratio = version_config.get('volume_shrink_ratio', default_params['volume_shrink_ratio'])
        self.shrink_to_surge_days = version_config.get('shrink_to_surge_days', default_params['shrink_to_surge_days'])

        # 方案B参数
        pattern_config = self.config.get('pattern', {})
        self.flat_days = pattern_config.get('flat_days', 0)
        self.flat_volume_threshold = pattern_config.get('flat_volume_threshold', 0.85)
        self.flat_price_range = pattern_config.get('flat_price_range', 0.08)
        self.use_scheme_b = self.flat_days == 0
        self.post_surge_check_days = pattern_config.get('post_surge_check_days', 5)
        self.post_surge_max_drop = pattern_config.get('post_surge_max_drop', 0.97)

    def _load_trading_params(self):
        """加载交易参数"""
        trading_config = self.config.get('trading', {})
        self.trailing_stop_ratio = trading_config.get('trailing_stop_ratio', 0.08)
        self.hard_stop_loss = trading_config.get('hard_stop_loss', 0.10)
        self.min_hold_days = trading_config.get('min_hold_days', 5)
        self.max_hold_days = trading_config.get('max_hold_days', 30)
        self.commission = trading_config.get('commission', 0.00025)
        self.stamp_tax = trading_config.get('stamp_tax', 0.001)
        self.slippage = trading_config.get('slippage', 0.001)
        self.min_avg_volume = trading_config.get('min_avg_volume', 20_000_000)
        self.volume_check_period = trading_config.get('volume_check_period', 20)

    def check_liquidity(self, df, retrace_idx):
        """
        检查股票流动性

        Args:
            df: K线数据
            retrace_idx: 回踩确认索引

        Returns:
            bool: 是否满足流动性要求
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

    def check_all_patterns(self, df: pd.DataFrame, code: str,
                          period_start: str, period_end: str) -> List[Dict]:
        """
        检测所有符合条件的形态

        Args:
            df: K线数据
            code: 股票代码
            period_start: 开始日期
            period_end: 结束日期

        Returns:
            形态列表
        """
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)

        period_start_dt = pd.to_datetime(period_start)
        period_end_dt = pd.to_datetime(period_end)

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
                all_patterns.append(pattern)
                retrace_date = pd.to_datetime(pattern['retrace_date'])
                retrace_idx_in_temp = temp_df[temp_df['date'] == retrace_date].index
                if len(retrace_idx_in_temp) > 0:
                    search_start_idx += retrace_idx_in_temp[0] + 1
                else:
                    search_start_idx += 20
            else:
                break

        return all_patterns

    def _detect_pattern_from_start(self, period_df: pd.DataFrame, code: str) -> Optional[Dict]:
        """
        从给定数据的起始位置检测形态

        Args:
            period_df: 时间段数据
            code: 股票代码

        Returns:
            检测到的形态信息,如果未检测到则返回None
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

        # 2. 检测缩量
        shrink_found = False
        shrink_idx = -1

        for i in range(drop_end_idx+1, min(drop_end_idx + self.shrink_to_surge_days, len(period_df))):
            vol_i = period_df['volume'].iloc[i]
            vol_avg_20 = period_df['volume'].iloc[max(0, i-20):i].mean()

            if vol_i <= vol_avg_20 * self.volume_shrink_ratio:
                shrink_found = True
                shrink_idx = i
                break

        if not shrink_found:
            return None

        # 3. 检测放量
        surge_found = False
        surge_idx = -1

        for j in range(shrink_idx+1, min(shrink_idx+15, len(period_df))):
            vol_j = period_df['volume'].iloc[j]
            vol_prev = period_df['volume'].iloc[j-1]
            close_j = period_df['close'].iloc[j]
            close_prev = period_df['close'].iloc[j-1]

            vol_ratio = vol_j / vol_prev if vol_prev > 0 else 0
            price_change = (close_j - close_prev) / close_prev

            if vol_ratio >= self.min_volume_ratio and price_change >= self.surge_price_ratio:
                if not (self.min_volume_ratio <= vol_ratio <= self.max_volume_ratio):
                    continue

                if self.flat_days > 0:
                    # 方案A: 检查缩量走平
                    is_flat = True
                    for k in range(j+1, min(j+1+self.flat_days, len(period_df))):
                        vol_k = period_df['volume'].iloc[k]
                        close_k = period_df['close'].iloc[k]

                        if vol_k >= vol_j * self.flat_volume_threshold:
                            is_flat = False
                            break

                        price_change_k = abs(close_k - close_j) / close_j
                        if price_change_k >= self.flat_price_range:
                            is_flat = False
                            break

                    if not is_flat:
                        continue

                    surge_found = True
                    surge_idx = j
                    break

                else:
                    # 方案B: 检查启动后观察期
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

            if low_i >= support_level * 0.98:
                retrace_found = True
                retrace_idx = i
                break

        if not retrace_found:
            return None

        if not self.check_liquidity(period_df, retrace_idx):
            return None

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
            'scheme': f'V{self.version[1:]}' if self.version.startswith('v') else self.version,
            'shrink_to_surge_days': self.shrink_to_surge_days,
            'drop_threshold': self.drop_threshold,
            'surge_price_ratio': self.surge_price_ratio,
            'min_volume_ratio': self.min_volume_ratio,
            'volume_shrink_ratio': self.volume_shrink_ratio
        }

    def check_pattern_single(self, df: pd.DataFrame, code: str,
                            period_start: str, period_end: str) -> Optional[Dict]:
        """
        检测单只股票是否符合形态(只返回第一个)

        Args:
            df: K线数据
            code: 股票代码
            period_start: 开始日期
            period_end: 结束日期

        Returns:
            第一个检测到的形态,如果未检测到则返回None
        """
        all_patterns = self.check_all_patterns(df, code, period_start, period_end)
        if all_patterns and len(all_patterns) > 0:
            return all_patterns[0]
        return None

    def simulate_trade(self, df: pd.DataFrame, pattern_info: Dict) -> Optional[Dict]:
        """
        模拟交易

        Args:
            df: K线数据
            pattern_info: 形态信息

        Returns:
            交易结果,如果无法交易则返回None
        """
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

        # 跳过高开超过5%的股票
        if open_change >= 0.05:
            return None

        # 计算买入价格
        if open_change > 0.03:
            buy_price = open_price * (1 + 0.01)
        else:
            buy_price = open_price

        buy_date = next_day['date']

        high_since_buy = buy_price
        close_since_buy = buy_price

        # 模拟持仓过程
        for i in range(retrace_idx + 2, len(df)):
            current_day = df.iloc[i]
            high = current_day['high']
            low = current_day['low']
            close = current_day['close']

            hold_days = i - retrace_idx - 1

            # 更新最高价
            if high > high_since_buy:
                high_since_buy = high

            # 检查硬止损
            if low <= buy_price * (1 - self.hard_stop_loss):
                sell_price = buy_price * (1 - self.hard_stop_loss)
                sell_date = current_day['date']
                sell_reason = 'hard_stop_loss'
                break

            # 检查移动止盈
            if close <= high_since_buy * (1 - self.trailing_stop_ratio):
                if hold_days >= self.min_hold_days:
                    sell_price = close
                    sell_date = current_day['date']
                    sell_reason = 'trailing_stop'
                    break

            # 检查最长持仓天数
            if hold_days >= self.max_hold_days:
                sell_price = close
                sell_date = current_day['date']
                sell_reason = 'max_hold_days'
                break

        # 如果没有卖出,使用最后一天收盘价
        if sell_price is None:
            if retrace_idx + 2 < len(df):
                last_day = df.iloc[-1]
                sell_price = last_day['close']
                sell_date = last_day['date']
                sell_reason = 'end_of_period'
            else:
                return None

        # 计算收益率
        gross_profit = (sell_price - buy_price) / buy_price

        # 扣除交易成本
        cost = self.commission * 2 + self.stamp_tax + self.slippage * 2
        net_profit = gross_profit - cost

        return {
            'code': code,
            'buy_date': buy_date,
            'buy_price': buy_price,
            'sell_date': sell_date,
            'sell_price': sell_price,
            'sell_reason': sell_reason,
            'hold_days': (sell_date - buy_date).days,
            'gross_profit': gross_profit,
            'net_profit': net_profit,
            'cost': cost,
            'high_since_buy': high_since_buy,
        }

    def get_version_info(self) -> Dict:
        """
        获取当前版本信息

        Returns:
            版本信息字典
        """
        return {
            'version': self.version,
            'drop_threshold': self.drop_threshold,
            'surge_price_ratio': self.surge_price_ratio,
            'min_volume_ratio': self.min_volume_ratio,
            'max_volume_ratio': self.max_volume_ratio,
            'volume_shrink_ratio': self.volume_shrink_ratio,
            'shrink_to_surge_days': self.shrink_to_surge_days,
        }
