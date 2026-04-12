"""
形态检测核心逻辑
实现"大跌→缩量→放量→回踩"四步检测流程
"""

import pandas as pd
import numpy as np
from typing import Dict, Any, Optional, Tuple

from utils.logger import get_logger

logger = get_logger()


class PatternDetector:
    """形态检测器"""

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        """
        初始化形态检测器
        
        Args:
            params: 形态检测参数
        """
        self.params = self._get_default_params()
        if params:
            self.params.update(params)

    def _get_default_params(self) -> Dict[str, Any]:
        """获取默认参数"""
        return {
            'drop_period': 25,
            'drop_threshold': 0.20,
            'support_lookback': 5,
            'support_method': 'both',
            'consolidation_days': 5,
            'consolidation_range': 0.03,
            'volume_shrink_ratio': 0.6,
            'volume_shrink_period': 10,
            'shrink_before_surge_days': 10,
            'surge_volume_ratio': 1.5,
            'surge_price_ratio': 0.05,
            'surge_lookback': 15,
            'surge_condition': 'either',
            'exclude_long_upper_shadow': False,
            'retrace_ratio': 0.5,
            'retrace_volume_ratio': 0.6,
            'retrace_max_days': 10,
            'support_level_combine': 'lowest_or_ma',
            'support_ma_period': 20
        }

    def check_pattern(self, df: pd.DataFrame) -> Tuple[bool, Dict[str, Any]]:
        """
        检测股票形态是否符合策略
        
        Args:
            df: K 线数据，包含 open, high, low, close, volume 列
        
        Returns:
            (是否匹配，详情字典)
        """
        if df is None or len(df) < 60:
            return False, {'reason': '数据不足 60 天'}

        p = self.params

        result, detail = self._check_drop_support(df, p)
        if not result:
            return False, detail

        result, shrink_detail = self._check_volume_shrink(df, p)
        if not result:
            return False, {**detail, **shrink_detail}
        detail.update(shrink_detail)

        result, surge_detail = self._check_surge(df, p)
        if not result:
            return False, detail
        detail.update(surge_detail)

        result, retrace_detail = self._check_retrace(df, p, detail)
        if not result:
            return False, {**detail, **retrace_detail}
        detail.update(retrace_detail)

        return True, detail

    def _check_drop_support(self, df: pd.DataFrame, p: Dict) -> Tuple[bool, Dict]:
        """
        步骤 1：大跌后支撑检测
        
        Returns:
            (是否通过，详情)
        """
        drop_period = p['drop_period']
        drop_threshold = p['drop_threshold']

        recent_low = df['low'].iloc[-drop_period:].min()
        previous_high = df['high'].iloc[-2*drop_period:-drop_period].max()
        
        drop_rate = (previous_high - recent_low) / previous_high if previous_high > 0 else 0
        
        if drop_rate < drop_threshold:
            return False, {
                'reason': '跌幅不足',
                'drop_rate': drop_rate,
                'required': drop_threshold
            }

        support_method = p['support_method']
        support_confirmed = False
        support_level = recent_low

        if support_method in ['lowest', 'both']:
            current_low_min = df['low'].iloc[-p['support_lookback']:].min()
            prev_low_min = df['low'].iloc[-2*p['support_lookback']:-p['support_lookback']].min()
            
            if current_low_min > prev_low_min:
                support_confirmed = True
                support_level = current_low_min

        if support_method in ['consolidation', 'both']:
            consolidation_days = p['consolidation_days']
            consolidation_range = p['consolidation_range']
            
            if len(df) >= consolidation_days:
                recent_high = df['high'].iloc[-consolidation_days:].max()
                recent_low = df['low'].iloc[-consolidation_days:].min()
                recent_avg = df['close'].iloc[-consolidation_days:].mean()
                
                range_ratio = (recent_high - recent_low) / recent_avg
                if range_ratio < consolidation_range:
                    support_confirmed = True
                    support_level = min(support_level, recent_low)

        if not support_confirmed:
            return False, {
                'reason': '支撑位未确认',
                'support_level': support_level,
                'drop_rate': drop_rate
            }

        return True, {
            'support_level': support_level,
            'drop_rate': drop_rate
        }

    def _check_volume_shrink(self, df: pd.DataFrame, p: Dict) -> Tuple[bool, Dict]:
        """
        步骤 2：缩量检测
        
        Returns:
            (是否通过，详情)
        """
        vol_ma20 = df['volume'].rolling(20).mean()
        
        volume_shrink_period = p['volume_shrink_period']
        surge_lookback = p['surge_lookback']
        
        start_idx = -volume_shrink_period - surge_lookback
        end_idx = -surge_lookback
        
        vol_shrink_region = df['volume'].iloc[start_idx:end_idx]
        vol_ma20_region = vol_ma20.iloc[start_idx:end_idx]
        
        if len(vol_shrink_region) == 0 or len(vol_ma20_region) == 0:
            return False, {'reason': '缩量区间数据不足'}
        
        if (vol_ma20_region == 0).any():
            return False, {'reason': '均量为 0'}
        
        min_vol_ratio = (vol_shrink_region / vol_ma20_region).min()
        
        if min_vol_ratio > p['volume_shrink_ratio']:
            return False, {
                'reason': '缩量不充分',
                'min_vol_ratio': min_vol_ratio,
                'required': p['volume_shrink_ratio']
            }

        return True, {
            'min_vol_ratio': min_vol_ratio
        }

    def _check_surge(self, df: pd.DataFrame, p: Dict) -> Tuple[bool, Dict]:
        """
        步骤 3：放量大涨检测
        
        Returns:
            (是否通过，详情)
        """
        df = df.copy()
        df['pct_chg'] = df['close'].pct_change()
        vol_ma20 = df['volume'].rolling(20).mean()
        df['vol_ratio'] = df['volume'] / vol_ma20

        surge_lookback = p['surge_lookback']
        surge_condition = p['surge_condition']

        surge_idx = None
        surge_price = None
        surge_open = None
        surge_high = None

        for i in range(-1, -surge_lookback-1, -1):
            price_condition = False
            
            if surge_condition == 'price_up':
                price_condition = df['pct_chg'].iloc[i] > p['surge_price_ratio']
            elif surge_condition == 'position_up':
                if df['high'].iloc[i] != df['low'].iloc[i]:
                    position = (df['close'].iloc[i] - df['low'].iloc[i]) / (df['high'].iloc[i] - df['low'].iloc[i])
                    price_condition = df['pct_chg'].iloc[i] > 0.03 and position > 2/3
                else:
                    price_condition = df['pct_chg'].iloc[i] > 0.03
            elif surge_condition == 'either':
                if df['pct_chg'].iloc[i] > p['surge_price_ratio']:
                    price_condition = True
                elif df['pct_chg'].iloc[i] > 0.03:
                    if df['high'].iloc[i] != df['low'].iloc[i]:
                        position = (df['close'].iloc[i] - df['low'].iloc[i]) / (df['high'].iloc[i] - df['low'].iloc[i])
                        price_condition = position > 2/3
                    else:
                        price_condition = True
            
            volume_condition = df['vol_ratio'].iloc[i] > p['surge_volume_ratio']
            
            if price_condition and volume_condition:
                if p['exclude_long_upper_shadow']:
                    body = abs(df['close'].iloc[i] - df['open'].iloc[i])
                    upper_shadow = df['high'].iloc[i] - max(df['close'].iloc[i], df['open'].iloc[i])
                    if body > 0 and upper_shadow / body > 2:
                        continue
                
                surge_idx = i
                surge_price = df['close'].iloc[i]
                surge_open = df['open'].iloc[i]
                surge_high = df['high'].iloc[i]
                break

        if surge_idx is None:
            return False, {'reason': '未找到放量大涨日'}

        shrink_before_surge_days = p['shrink_before_surge_days']
        if surge_idx < -shrink_before_surge_days:
            return False, {
                'reason': '缩量不在放量日前',
                'surge_idx': surge_idx,
                'required_days': shrink_before_surge_days
            }

        return True, {
            'surge_idx': surge_idx,
            'surge_date': df['date'].iloc[surge_idx],
            'surge_price': surge_price,
            'surge_open': surge_open,
            'surge_high': surge_high,
            'surge_volume_ratio': df['vol_ratio'].iloc[surge_idx],
            'surge_pct': df['pct_chg'].iloc[surge_idx]
        }

    def _check_retrace(self, df: pd.DataFrame, p: Dict, detail: Dict) -> Tuple[bool, Dict]:
        """
        步骤 4：回踩确认检测
        
        Returns:
            (是否通过，详情)
        """
        surge_idx = detail['surge_idx']
        support_level = detail['support_level']

        after_surge = df.iloc[surge_idx+1:].copy()
        
        if len(after_surge) == 0:
            return False, {'reason': '放量日后无数据'}

        retrace_max_days = p['retrace_max_days']
        if len(after_surge) > retrace_max_days:
            after_surge = after_surge.iloc[:retrace_max_days]

        low_after_surge = after_surge['low'].min()

        retrace_ok = low_after_surge > support_level

        if p['retrace_ratio'] > 0:
            surge_price = detail['surge_price']
            surge_open = detail['surge_open']
            candle_body = abs(surge_price - surge_open)
            
            if candle_body > 0:
                retrace_limit = surge_price - candle_body * p['retrace_ratio']
                retrace_ok = retrace_ok and (low_after_surge > retrace_limit)

        if not retrace_ok:
            return False, {
                'reason': '回踩跌破支撑',
                'low_after_surge': low_after_surge,
                'support': support_level
            }

        retrace_vol_avg = after_surge['volume'].mean()
        surge_vol = df['volume'].iloc[surge_idx]
        
        if surge_vol > 0:
            retrace_vol_ratio = retrace_vol_avg / surge_vol
            if retrace_vol_ratio > p['retrace_volume_ratio']:
                return False, {
                    'reason': '回踩未缩量',
                    'retrace_vol_ratio': retrace_vol_ratio,
                    'required': p['retrace_volume_ratio']
                }
        else:
            retrace_vol_ratio = 0

        current_close = df['close'].iloc[-1]

        return True, {
            'low_after_surge': low_after_surge,
            'retrace_vol_ratio': retrace_vol_ratio,
            'current_close': current_close,
            **detail
        }
