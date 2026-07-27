"""
动态ATR过滤器和动态成交量过滤器
根据历史波动率分布和ADX趋势强度动态调整最低ATR%阈值和成交量阈值

核心设计：
1. 币种自适应：每个币种独立计算自身的历史波动率分布
2. 市场状态自适应：强趋势时放宽低波动限制，震荡市时收紧
3. 绝对下限保护：避免因历史数据中极端低波动导致下限过低
4. 与动态成交量过滤器对称设计
5. v6.16.8新增：支持币种差异化参数配置
"""
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from collections import deque
import numpy as np
import structlog

logger = structlog.get_logger()


class DynamicATRFilter:
    """动态ATR过滤器
    
    根据历史ATR%分布和ADX趋势强度动态调整最低ATR%阈值
    
    核心算法：
    1. 计算历史ATR%序列（过去30天/720小时）
    2. 确定基础低波动阈值（35%分位数）
    3. 根据ADX动态调整：
       - ADX > 25（强趋势）：系数0.8（适度放宽）
       - ADX > 20（中等趋势）：系数0.9
       - ADX ≤ 20（弱趋势/震荡）：系数1.0（保持严格）
    4. 加上绝对下限保护（0.6%）
    """
    
    def __init__(self, config: Dict):
        """
        初始化动态ATR过滤器
        
        Args:
            config: 配置字典，包含过滤器参数
        """
        self.config = config
        self.enabled = config.get('enabled', True)
        
        self.lookback_hours = config.get('lookback_hours', 720)
        self.low_percentile = config.get('percentile', 0.35)  # v6.17.1调整：20%→35%
        self.min_history_count = config.get('min_history_count', 100)
        self.absolute_min = config.get('absolute_min_atr_percent', 0.6)  # v6.17.1调整：0.3→0.6
        
        adx_coef = config.get('adx_coefficients', {})
        self.strong_trend_threshold = adx_coef.get('strong_trend', 25)
        self.medium_trend_threshold = adx_coef.get('medium_trend', 20)
        self.strong_coefficient = adx_coef.get('strong_coefficient', 0.8)  # v6.17.1调整：0.6→0.8
        self.medium_coefficient = adx_coef.get('medium_coefficient', 0.9)  # v6.17.1调整：0.7→0.9
        self.weak_coefficient = adx_coef.get('weak_coefficient', 1.0)  # v6.17.1调整：0.8→1.0
        
        self.symbol_overrides = config.get('symbol_overrides', {})
        
        self._atr_history: Dict[str, deque] = {}
        self._percentile_cache: Dict[str, Tuple[float, datetime]] = {}
        self._cache_ttl = config.get('cache_ttl_seconds', 3600)
        
        logger.info(
            "动态ATR过滤器初始化",
            enabled=self.enabled,
            lookback_hours=self.lookback_hours,
            low_percentile=self.low_percentile,
            absolute_min=self.absolute_min,
            adx_coefficients={
                'strong': (self.strong_trend_threshold, self.strong_coefficient),
                'medium': (self.medium_trend_threshold, self.medium_coefficient),
                'weak': self.weak_coefficient
            }
        )
    
    def initialize_history(
        self,
        symbol: str,
        atr_values: List[float],
        close_prices: List[float]
    ) -> int:
        """
        初始化历史ATR%数据
        
        Args:
            symbol: 交易对
            atr_values: ATR值列表（按时间顺序，最新在最后）
            close_prices: 收盘价列表（与ATR对应）
        
        Returns:
            初始化的数据条数
        """
        if symbol not in self._atr_history:
            self._atr_history[symbol] = deque(maxlen=self.lookback_hours)
        
        self._atr_history[symbol].clear()
        
        count = 0
        for atr, close in zip(atr_values, close_prices):
            if close > 0 and atr > 0:
                atr_percent = (atr / close) * 100
                self._atr_history[symbol].append(atr_percent)
                count += 1
        
        self._invalidate_cache(symbol)
        
        logger.info(
            f"{symbol} 历史ATR%数据初始化完成",
            symbol=symbol,
            count=count,
            history_size=len(self._atr_history[symbol])
        )
        
        return count
    
    def update_history(
        self,
        symbol: str,
        atr: float,
        close_price: float
    ) -> None:
        """
        更新历史ATR%数据
        
        Args:
            symbol: 交易对
            atr: 当前ATR值
            close_price: 当前收盘价
        """
        if symbol not in self._atr_history:
            self._atr_history[symbol] = deque(maxlen=self.lookback_hours)
        
        if close_price > 0 and atr > 0:
            atr_percent = (atr / close_price) * 100
            self._atr_history[symbol].append(atr_percent)
            self._invalidate_cache(symbol)
    
    def _invalidate_cache(self, symbol: str) -> None:
        """使缓存失效"""
        if symbol in self._percentile_cache:
            del self._percentile_cache[symbol]
    
    def _get_cached_percentile(self, symbol: str) -> Optional[float]:
        """获取缓存的分位数值"""
        if symbol in self._percentile_cache:
            cached_value, cached_time = self._percentile_cache[symbol]
            if (datetime.now() - cached_time).total_seconds() < self._cache_ttl:
                return cached_value
        return None
    
    def _set_cache(self, symbol: str, value: float) -> None:
        """设置缓存"""
        self._percentile_cache[symbol] = (value, datetime.now())
    
    def get_base_threshold(self, symbol: str) -> Tuple[float, str]:
        """
        获取基础低波动阈值（历史ATR%的分位数）
        
        Args:
            symbol: 交易对
        
        Returns:
            (基础阈值, 计算说明)
        """
        if symbol not in self._atr_history or len(self._atr_history[symbol]) < self.min_history_count:
            fallback = self._get_symbol_override(symbol, 'fallback_atr_percent', 0.6)
            return fallback, f"数据不足({len(self._atr_history.get(symbol, []))}<{self.min_history_count})，使用默认值{fallback}%"
        
        cached = self._get_cached_percentile(symbol)
        if cached is not None:
            return cached, f"缓存值（历史{len(self._atr_history[symbol])}条数据的{self.low_percentile*100:.0f}%分位数）"
        
        atr_percents = list(self._atr_history[symbol])
        base_threshold = float(np.percentile(atr_percents, self.low_percentile * 100))
        
        self._set_cache(symbol, base_threshold)
        
        return base_threshold, f"历史{len(atr_percents)}条数据的{self.low_percentile*100:.0f}%分位数"
    
    def _get_adx_coefficient(self, adx: float) -> float:
        """
        根据ADX获取调整系数
        
        Args:
            adx: ADX值
        
        Returns:
            调整系数
        """
        if adx > self.strong_trend_threshold:
            return self.strong_coefficient
        elif adx > self.medium_trend_threshold:
            return self.medium_coefficient
        else:
            return self.weak_coefficient
    
    def _get_symbol_override(self, symbol: str, key: str, default: float) -> float:
        """获取币种特殊配置"""
        if symbol in self.symbol_overrides:
            return self.symbol_overrides[symbol].get(key, default)
        return default
    
    def get_dynamic_min_atr_percent(
        self,
        symbol: str,
        adx: float
    ) -> Tuple[float, Dict]:
        """
        获取动态最低ATR%阈值
        
        根据历史ATR%分布和ADX趋势强度计算动态阈值
        
        Args:
            symbol: 交易对
            adx: 当前ADX值
        
        Returns:
            (最低ATR%阈值, 详细信息字典)
        """
        if not self.enabled:
            return 0.0, {'reason': '动态ATR过滤器未启用'}
        
        base_threshold, base_reason = self.get_base_threshold(symbol)
        
        coefficient = self._get_adx_coefficient(adx)
        
        adjusted = base_threshold * coefficient
        
        absolute_min = self._get_symbol_override(symbol, 'absolute_min_atr_percent', self.absolute_min)
        min_allowed = max(adjusted, absolute_min)
        
        trend_level = "强趋势" if adx > self.strong_trend_threshold else \
                      "中等趋势" if adx > self.medium_trend_threshold else "弱趋势/震荡"
        
        info = {
            'base_threshold': base_threshold,
            'base_reason': base_reason,
            'adx': adx,
            'trend_level': trend_level,
            'coefficient': coefficient,
            'adjusted_threshold': adjusted,
            'absolute_min': absolute_min,
            'final_threshold': min_allowed,
            'history_count': len(self._atr_history.get(symbol, []))
        }
        
        return min_allowed, info
    
    def should_filter(
        self,
        symbol: str,
        current_atr_percent: float,
        adx: float
    ) -> Tuple[bool, str]:
        """
        判断是否应该过滤（ATR%是否过低）
        
        Args:
            symbol: 交易对
            current_atr_percent: 当前ATR百分比
            adx: 当前ADX值
        
        Returns:
            (是否过滤, 过滤原因说明)
        """
        if not self.enabled:
            return False, "动态ATR过滤器未启用"
        
        min_allowed, info = self.get_dynamic_min_atr_percent(symbol, adx)
        
        if current_atr_percent < min_allowed:
            reason = (
                f"ATR% {current_atr_percent:.2f}% < 动态下限 {min_allowed:.2f}% "
                f"(基准={info['base_threshold']:.2f}%, ADX={adx:.1f}[{info['trend_level']}], "
                f"系数={info['coefficient']})"
            )
            return True, reason
        
        return False, f"ATR% {current_atr_percent:.2f}% >= 动态下限 {min_allowed:.2f}%"
    
    def get_statistics(self, symbol: str) -> Dict:
        """
        获取币种的ATR统计信息
        
        Args:
            symbol: 交易对
        
        Returns:
            统计信息字典
        """
        if symbol not in self._atr_history or len(self._atr_history[symbol]) == 0:
            return {
                'symbol': symbol,
                'history_count': 0,
                'percentile_20': None,
                'mean': None,
                'std': None,
                'min': None,
                'max': None
            }
        
        atr_percents = list(self._atr_history[symbol])
        
        return {
            'symbol': symbol,
            'history_count': len(atr_percents),
            'percentile_20': float(np.percentile(atr_percents, 20)),
            'percentile_35': float(np.percentile(atr_percents, 35)),
            'percentile_50': float(np.percentile(atr_percents, 50)),
            'percentile_80': float(np.percentile(atr_percents, 80)),
            'current_atr_pct': atr_percents[-1],
            'mean': float(np.mean(atr_percents)),
            'std': float(np.std(atr_percents)),
            'min': float(np.min(atr_percents)),
            'max': float(np.max(atr_percents))
        }
    
    def log_statistics(self, symbol: str) -> None:
        """记录统计信息到日志"""
        stats = self.get_statistics(symbol)
        
        if stats['history_count'] == 0:
            logger.info(f"{symbol} ATR历史数据为空")
            return
        
        logger.info(
            f"{symbol} ATR%统计信息",
            symbol=symbol,
            history_count=stats['history_count'],
            percentile_20=f"{stats['percentile_20']:.2f}%",
            percentile_50=f"{stats['percentile_50']:.2f}%",
            percentile_80=f"{stats['percentile_80']:.2f}%",
            mean=f"{stats['mean']:.2f}%",
            std=f"{stats['std']:.2f}%",
            range=f"{stats['min']:.2f}%-{stats['max']:.2f}%"
        )


class DynamicVolumeFilter:
    """动态成交量过滤器（v6.16.8新增）
    
    根据历史成交量分布和ADX趋势强度动态调整最低成交量比率阈值
    
    核心算法：
    1. 计算过去20小时平均成交量
    2. 根据信号等级（S/A/B/C）确定基础成交量倍数
    3. 根据ADX动态调整：
       - ADX > 25（强趋势）：允许成交量要求降低20%
       - ADX ≤ 25：保持标准要求
    4. 币种差异化配置支持
    """
    
    def __init__(self, symbol_config: Dict, config: Dict = None):
        """
        初始化动态成交量过滤器
        
        Args:
            symbol_config: 币种配置字典，包含成交量过滤器参数
            config: 全局动态成交量配置（v6.16.10）
        """
        self.symbol_config = symbol_config
        self.config = config or {}
        self.base_ratio = symbol_config.get(
            'vol_ratio_base', 
            self.config.get('default_vol_ratio_base', {'S': 1.4, 'A': 1.2, 'B': 0.0, 'C': 0.0})
        )
        self.lookback_hours = self.config.get('lookback_hours', 20)
        
        logger.info(
            "动态成交量过滤器初始化",
            base_ratio=self.base_ratio,
            lookback_hours=self.lookback_hours
        )
    
    def check(
        self,
        current_volume: float,
        volume_history_1h: List[float],
        adx_1d: float,
        grade: str
    ) -> Tuple[bool, str]:
        """
        检查成交量是否满足要求
        
        Args:
            current_volume: 当前成交量
            volume_history_1h: 过去N小时的成交量历史
            adx_1d: 日线ADX值
            grade: 信号等级（S/A/B/C）
        
        Returns:
            (是否通过, 拒绝原因说明)
        """
        min_ratio = self.base_ratio.get(grade, 0.0)
        if min_ratio == 0.0:
            return True, ""  # 不检查成交量
        
        # 计算过去20小时平均成交量
        if len(volume_history_1h) < self.lookback_hours:
            return True, ""  # 数据不足，暂不检查
        
        avg_volume = sum(volume_history_1h[-self.lookback_hours:]) / self.lookback_hours
        if avg_volume == 0:
            return True, ""
        
        vol_ratio = current_volume / avg_volume
        
        # ADX调节：强趋势时允许成交量要求降低（从配置读取）
        effective_min = min_ratio
        adx_threshold = self.config.get('adx_strong_threshold', 25)
        adx_coef = self.config.get('adx_coefficient', 0.8)
        if adx_1d > adx_threshold:
            effective_min = min_ratio * adx_coef
        
        if vol_ratio >= effective_min:
            return True, ""
        else:
            return False, f"成交量比率 {vol_ratio:.2f} < {effective_min:.2f} (等级{grade}, ADX={adx_1d:.1f})"
    
    def check_with_position_adjustment(
        self,
        current_volume: float,
        volume_history_1h: List[float],
        adx_1d: float,
        grade: str
    ) -> Tuple[bool, float, str]:
        """
        检查成交量并返回仓位调整系数
        
        Args:
            current_volume: 当前成交量
            volume_history_1h: 过去N小时的成交量历史
            adx_1d: 日线ADX值
            grade: 信号等级（S/A/B/C）
        
        Returns:
            (是否通过, 仓位调整系数, 说明)
        """
        min_ratio = self.base_ratio.get(grade, 0.0)
        if min_ratio == 0.0:
            return True, 1.0, "不检查成交量"
        
        # 计算过去20小时平均成交量
        if len(volume_history_1h) < self.lookback_hours:
            return True, 1.0, "数据不足，暂不检查"
        
        avg_volume = sum(volume_history_1h[-self.lookback_hours:]) / self.lookback_hours
        if avg_volume == 0:
            return True, 1.0, "平均成交量为0"
        
        vol_ratio = current_volume / avg_volume
        
        # ADX调节：强趋势时允许成交量要求降低（从配置读取）
        effective_min = min_ratio
        adx_threshold = self.config.get('adx_strong_threshold', 25)
        adx_coef = self.config.get('adx_coefficient', 0.8)
        if adx_1d > adx_threshold:
            effective_min = min_ratio * adx_coef
        
        if vol_ratio >= effective_min:
            return True, 1.0, f"成交量达标（{vol_ratio:.2f}倍 ≥ {effective_min:.2f}倍）"
        else:
            near_coef = self.config.get('near_threshold_coefficient', 0.8)
            if vol_ratio >= effective_min * near_coef:
                # 接近阈值，仓位减半
                return True, 0.5, f"成交量接近阈值，仓位减半（{vol_ratio:.2f}倍）"
            else:
                return False, 0.0, f"成交量不足（{vol_ratio:.2f}倍 < {effective_min * near_coef:.2f}倍）"
