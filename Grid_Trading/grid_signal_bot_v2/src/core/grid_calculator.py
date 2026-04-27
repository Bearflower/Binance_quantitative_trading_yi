"""
网格参数计算器
根据市场状态和波动率动态计算网格参数
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from src.core.market_analyzer import MarketState

logger = logging.getLogger(__name__)


@dataclass
class GridParameters:
    """网格参数数据结构"""
    upper_price: float  # 上边界
    lower_price: float  # 下边界
    grid_count: int  # 网格数量
    grid_type: str  # 网格类型：arithmetic(等差) / geometric(等比)
    grid_direction: str  # 网格方向：LONG/SHORT/NEUTRAL
    leverage: int  # 杠杆倍数
    total_investment: float  # 总投资金额
    
    stop_upper_price: Optional[float] = None  # 停止上移价格
    stop_lower_price: Optional[float] = None  # 停止下移价格
    terminate_upper_price: Optional[float] = None  # 终止最高价格
    terminate_lower_price: Optional[float] = None  # 终止最低价格
    
    grid_spacing: Optional[float] = None  # 网格间距
    profit_rate: Optional[float] = None  # 每格利润率
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'upper_price': self.upper_price,
            'lower_price': self.lower_price,
            'grid_count': self.grid_count,
            'grid_type': self.grid_type,
            'grid_direction': self.grid_direction,
            'leverage': self.leverage,
            'total_investment': self.total_investment,
            'stop_upper_price': self.stop_upper_price,
            'stop_lower_price': self.stop_lower_price,
            'terminate_upper_price': self.terminate_upper_price,
            'terminate_lower_price': self.terminate_lower_price,
            'grid_spacing': self.grid_spacing,
            'profit_rate': self.profit_rate
        }


class GridParameterCalculator:
    """网格参数计算器"""
    
    def __init__(
        self,
        base_grid_count: int = 30,
        min_grid_count: int = 5,
        max_grid_count: int = 50,
        min_profit_rate: float = 0.01,
        leverage: int = 10,
        total_investment: float = 500,
        ranging_upper: float = 3.0,
        ranging_lower: float = 3.0,
        uptrend_upper: float = 4.0,
        uptrend_lower: float = 1.5,
        downtrend_upper: float = 1.5,
        downtrend_lower: float = 4.0
    ):
        """
        初始化网格参数计算器
        
        Args:
            base_grid_count: 基准网格数量
            min_grid_count: 最小网格数量
            max_grid_count: 最大网格数量
            min_profit_rate: 每格最小利润率
            leverage: 杠杆倍数
            total_investment: 总投资金额
            ranging_upper: 震荡上边界倍数
            ranging_lower: 震荡下边界倍数
            uptrend_upper: 上升趋势上边界倍数
            uptrend_lower: 上升趋势下边界倍数
            downtrend_upper: 下降趋势上边界倍数
            downtrend_lower: 下降趋势下边界倍数
        """
        self.base_grid_count = base_grid_count
        self.min_grid_count = min_grid_count
        self.max_grid_count = max_grid_count
        self.min_profit_rate = min_profit_rate
        self.leverage = leverage
        self.total_investment = total_investment
        
        # 网格宽度倍数
        self.ranging_upper = ranging_upper
        self.ranging_lower = ranging_lower
        self.uptrend_upper = uptrend_upper
        self.uptrend_lower = uptrend_lower
        self.downtrend_upper = downtrend_upper
        self.downtrend_lower = downtrend_lower
    
    def calculate(
        self,
        current_price: float,
        atr_smooth: float,
        market_state: MarketState,
        trend_strength: float = 0.0
    ) -> GridParameters:
        """
        计算网格参数
        
        Args:
            current_price: 当前价格
            atr_smooth: 平滑 ATR 值
            market_state: 市场状态
            trend_strength: 趋势强度系数
            
        Returns:
            计算出的网格参数
        """
        logger.info(
            f"计算网格参数：价格={current_price}, ATR={atr_smooth:.2f}, "
            f"状态={market_state.value}"
        )
        
        # 1. 计算网格边界
        upper_price, lower_price = self._calculate_boundaries(
            current_price, atr_smooth, market_state
        )
        
        # 2. 计算网格数量
        grid_count = self._calculate_grid_count(atr_smooth)
        
        # 3. 确定网格方向
        grid_direction = self._determine_direction(market_state)
        
        # 4. 计算网格类型（等差或等比）
        grid_type = self._determine_grid_type(upper_price, lower_price)
        
        # 5. 计算停止/终止价格
        stop_upper, stop_lower = self._calculate_stop_prices(
            upper_price, lower_price, atr_smooth, market_state, trend_strength
        )
        terminate_upper, terminate_lower = self._calculate_terminate_prices(
            upper_price, lower_price, atr_smooth
        )
        
        # 6. 计算网格间距和利润率
        grid_spacing, profit_rate = self._calculate_spacing_and_profit(
            upper_price, lower_price, grid_count, grid_type, current_price
        )
        
        # 7. 验证利润率
        if profit_rate < self.min_profit_rate:
            logger.warning(
                f"每格利润率 {profit_rate*100:.2f}% < {self.min_profit_rate*100:.1f}%，"
                f"尝试减少网格数量"
            )
            grid_count = self._adjust_grid_count_for_profit(
                upper_price, lower_price, current_price, grid_type
            )
            grid_spacing, profit_rate = self._calculate_spacing_and_profit(
                upper_price, lower_price, grid_count, grid_type, current_price
            )
        
        # 创建参数对象
        params = GridParameters(
            upper_price=upper_price,
            lower_price=lower_price,
            grid_count=grid_count,
            grid_type=grid_type,
            grid_direction=grid_direction,
            leverage=self.leverage,
            total_investment=self.total_investment,
            stop_upper_price=stop_upper,
            stop_lower_price=stop_lower,
            terminate_upper_price=terminate_upper,
            terminate_lower_price=terminate_lower,
            grid_spacing=grid_spacing,
            profit_rate=profit_rate
        )
        
        logger.info(
            f"网格参数计算完成：区间=[{lower_price:.2f}, {upper_price:.2f}], "
            f"数量={grid_count}, 方向={grid_direction}, 利润率={profit_rate*100:.2f}%"
        )
        
        return params
    
    def _calculate_boundaries(
        self,
        current_price: float,
        atr_smooth: float,
        market_state: MarketState
    ) -> tuple:
        """计算网格边界"""
        if market_state == MarketState.RANGING:
            # 震荡：对称边界（使用配置文件的倍数）
            lower_price = current_price - self.ranging_lower * atr_smooth
            upper_price = current_price + self.ranging_upper * atr_smooth
            
        elif market_state == MarketState.UPTREND:
            # 上升趋势：浅下界，深上界（使用配置文件的倍数）
            lower_price = current_price - self.uptrend_lower * atr_smooth
            upper_price = current_price + self.uptrend_upper * atr_smooth
            
        elif market_state == MarketState.DOWNTREND:
            # 下降趋势：深下界，浅上界（使用配置文件的倍数）
            lower_price = current_price - self.downtrend_lower * atr_smooth
            upper_price = current_price + self.downtrend_upper * atr_smooth
            
        else:  # STRONG_TREND
            # 强趋势暂停：不创建网格，返回默认值
            lower_price = current_price - self.ranging_lower * atr_smooth
            upper_price = current_price + self.ranging_upper * atr_smooth
        
        # 确保价格为正
        lower_price = max(0.01, lower_price)
        upper_price = max(lower_price * 1.01, upper_price)
        
        return upper_price, lower_price
    
    def _calculate_grid_count(self, atr_smooth: float) -> int:
        """计算网格数量"""
        # 简化处理：使用基准网格数
        # 实际应用中可以根据基准 ATR 调整
        grid_count = self.base_grid_count
        
        # 限制在范围内
        grid_count = int(max(self.min_grid_count, min(self.max_grid_count, grid_count)))
        
        return grid_count
    
    def _determine_direction(self, market_state: MarketState) -> str:
        """确定网格方向"""
        if market_state == MarketState.UPTREND:
            return "LONG"
        elif market_state == MarketState.DOWNTREND:
            return "SHORT"
        else:
            return "NEUTRAL"
    
    def _determine_grid_type(self, upper_price: float, lower_price: float) -> str:
        """确定网格类型"""
        # 如果振幅 < 30%，使用等差网格
        amplitude = (upper_price - lower_price) / lower_price
        if amplitude < 0.3:
            return "arithmetic"
        else:
            return "geometric"
    
    def _calculate_stop_prices(
        self,
        upper_price: float,
        lower_price: float,
        atr_smooth: float,
        market_state: MarketState,
        trend_strength: float
    ) -> tuple:
        """计算停止上移/下移价格"""
        # 上移/下移步长
        step = trend_strength * atr_smooth
        
        # 停止上移价格：上边界 + 步长
        stop_upper = upper_price + step if market_state == MarketState.UPTREND else None
        
        # 停止下移价格：下边界 - 步长
        stop_lower = lower_price - step if market_state == MarketState.DOWNTREND else None
        
        return stop_upper, stop_lower
    
    def _calculate_terminate_prices(
        self,
        upper_price: float,
        lower_price: float,
        atr_smooth: float
    ) -> tuple:
        """计算终止最高/最低价格"""
        terminate_lower = lower_price - 2 * atr_smooth
        terminate_upper = upper_price + 2 * atr_smooth
        
        # 确保价格为正
        terminate_lower = max(0.01, terminate_lower)
        
        return terminate_upper, terminate_lower
    
    def _calculate_spacing_and_profit(
        self,
        upper_price: float,
        lower_price: float,
        grid_count: int,
        grid_type: str,
        current_price: float
    ) -> tuple:
        """计算网格间距和利润率"""
        if grid_type == "arithmetic":
            # 等差网格
            grid_spacing = (upper_price - lower_price) / grid_count
            profit_rate = grid_spacing / current_price
        else:
            # 等比网格
            ratio = (upper_price / lower_price) ** (1.0 / grid_count)
            grid_spacing = ratio - 1
            profit_rate = ratio - 1
        
        return grid_spacing, profit_rate
    
    def _adjust_grid_count_for_profit(
        self,
        upper_price: float,
        lower_price: float,
        current_price: float,
        grid_type: str
    ) -> int:
        """调整网格数量以满足最小利润率"""
        grid_count = self.base_grid_count
        
        while grid_count > self.min_grid_count:
            grid_spacing, profit_rate = self._calculate_spacing_and_profit(
                upper_price, lower_price, grid_count, grid_type, current_price
            )
            
            if profit_rate >= self.min_profit_rate:
                break
            
            grid_count -= 1
        
        return grid_count
