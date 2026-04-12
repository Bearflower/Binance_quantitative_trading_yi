"""
网格参数计算器
根据市场状态和波动率动态计算网格参数
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.strategy.market_state import MarketState

logger = logging.getLogger(__name__)


@dataclass
class GridParameters:
    """网格参数数据结构"""
    upper_price: float  # 上边界
    lower_price: float  # 下边界
    grid_count: int  # 网格数量
    grid_direction: str  # 网格方向：LONG/SHORT/NEUTRAL
    total_investment: float  # 总投资金额
    leverage: int  # 杠杆倍数
    grid_type: str = "arithmetic"  # 网格类型：arithmetic(等差) / geometric(等比)
    
    # 可选的停止/终止价格
    stop_upper_price: Optional[float] = None  # 停止上移价格
    stop_lower_price: Optional[float] = None  # 停止下移价格
    terminate_upper_price: Optional[float] = None  # 终止最高价格
    terminate_lower_price: Optional[float] = None  # 终止最低价格
    
    # 等比网格专用参数
    geometric_ratio: Optional[float] = None  # 等比比率（例如 1.005 表示 0.5% 间隔）
    grid_prices: Optional[List[float]] = None  # 每个网格的具体价格（等比网格用）
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'upper_price': self.upper_price,
            'lower_price': self.lower_price,
            'grid_count': self.grid_count,
            'grid_direction': self.grid_direction,
            'total_investment': self.total_investment,
            'leverage': self.leverage,
            'stop_upper_price': self.stop_upper_price,
            'stop_lower_price': self.stop_lower_price,
            'terminate_upper_price': self.terminate_upper_price,
            'terminate_lower_price': self.terminate_lower_price
        }
    
    def get_adjustable_params(self) -> Dict:
        """获取可调整的参数（用于 modify_grid/switch_grid）"""
        params = {}
        
        if self.upper_price is not None:
            params['upper_price'] = self.upper_price
        if self.lower_price is not None:
            params['lower_price'] = self.lower_price
        if self.grid_count is not None:
            params['grid_count'] = self.grid_count
        if self.stop_upper_price is not None:
            params['stop_upper_price'] = self.stop_upper_price
        if self.stop_lower_price is not None:
            params['stop_lower_price'] = self.stop_lower_price
        if self.terminate_upper_price is not None:
            params['terminate_upper_price'] = self.terminate_upper_price
        if self.terminate_lower_price is not None:
            params['terminate_lower_price'] = self.terminate_lower_price
        
        return params


@dataclass
class AdjustmentTrigger:
    """参数调整触发条件"""
    trigger_type: str  # ATR_CHANGE, STATE_CHANGE, EDGE_APPROACH, BREAKTHROUGH, PROFIT_CHANGE
    description: str
    severity: float  # 严重程度 (0-1)
    timestamp: datetime
    details: Dict


class GridParameterCalculator:
    """网格参数计算器 - 保守策略版本"""
    
    def __init__(
        self,
        base_grid_count: int = 30,
        min_grid_count: int = 20,
        max_grid_count: int = 50,
        base_atr_window: int = 90,
        atr_change_threshold: float = 0.35,  # 优化：从 20% 提高到 35%
        leverage: int = 10,
        default_investment: float = 100,
        conservative_mode: bool = True  # 新增：保守模式开关
    ):
        """
        初始化网格参数计算器
        
        Args:
            base_grid_count: 基准网格数量
            min_grid_count: 最小网格数量
            max_grid_count: 最大网格数量
            base_atr_window: 基准 ATR 计算窗口（天）
            atr_change_threshold: ATR 变化触发阈值（保守模式：35%）
            leverage: 杠杆倍数
            default_investment: 默认投资金额
            conservative_mode: 是否启用保守模式（减少调整频率）
        """
        self.base_grid_count = base_grid_count
        self.min_grid_count = min_grid_count
        self.max_grid_count = max_grid_count
        self.base_atr_window = base_atr_window
        self.atr_change_threshold = atr_change_threshold
        self.leverage = leverage
        self.default_investment = default_investment
        self.conservative_mode = conservative_mode
        
        # 保守模式参数
        if conservative_mode:
            self.price_deviation_threshold = 0.10  # 价格偏离 > 10% 才调整
            self.terminate_deviation_threshold = 0.15  # 终止价格偏离 > 15% 才调整
            self.state_confirm_count = 3  # 市场状态需要连续确认 3 次
            self.edge_approach_threshold = 1.5  # 价格接近边界 < 1.5×ATR 才触发
            self.min_trigger_severity = 0.7  # 最小触发严重性
        else:
            self.price_deviation_threshold = 0.05
            self.terminate_deviation_threshold = 0.08
            self.state_confirm_count = 1
            self.edge_approach_threshold = 0.5
            self.min_trigger_severity = 0.5
        
        self._last_atr: Optional[float] = None
        self._last_state: Optional[MarketState] = None
        self._last_adjustment_time: Optional[datetime] = None
        self._adjustment_count_today: int = 0
        self._state_change_counter: int = 0  # 新增：市场状态连续确认计数器
    
    def calculate(
        self,
        current_price: float,
        atr_smooth: float,
        state: MarketState,
        prev_grid_params: Optional[GridParameters] = None
    ) -> GridParameters:
        """
        计算网格参数
        
        Args:
            current_price: 当前价格
            atr_smooth: 平滑 ATR 值
            state: 市场状态
            prev_grid_params: 前一次网格参数（可选）
            
        Returns:
            计算出的网格参数
        """
        logger.info(f"计算网格参数：价格={current_price}, ATR={atr_smooth:.2f}, 状态={state.value}")
        
        # 1. 计算网格边界
        upper_price, lower_price = self._calculate_boundaries(
            current_price, atr_smooth, state
        )
        
        # 2. 计算网格数量
        grid_count = self._calculate_grid_count(atr_smooth)
        
        # 3. 确定网格方向
        grid_direction = self._determine_direction(state)
        
        # 4. 计算网格类型（等差或等比）
        grid_type = "geometric" if grid_count <= 5 else "arithmetic"  # 小网格数用等比
        
        # 5. 计算停止/终止价格
        stop_upper, stop_lower = self._calculate_stop_prices(
            upper_price, lower_price, atr_smooth, state
        )
        terminate_upper, terminate_lower = self._calculate_terminate_prices(
            upper_price, lower_price, atr_smooth
        )
        
        # 6. 如果是等比网格，计算等比比率和具体价格
        geometric_ratio = None
        grid_prices = None
        
        if grid_type == "geometric":
            # 等比比率 = (上界/下界)^(1/网格数)
            geometric_ratio = (upper_price / lower_price) ** (1.0 / grid_count)
            # 计算每个网格的价格
            grid_prices = [lower_price * (geometric_ratio ** i) for i in range(grid_count + 1)]
            logger.info(f"等比网格：比率={geometric_ratio:.6f} ({(geometric_ratio-1)*100:.3f}%)")
        
        # 创建参数对象
        params = GridParameters(
            upper_price=upper_price,
            lower_price=lower_price,
            grid_count=grid_count,
            grid_direction=grid_direction,
            total_investment=self.default_investment,
            leverage=self.leverage,
            grid_type=grid_type,
            geometric_ratio=geometric_ratio,
            grid_prices=grid_prices,
            stop_upper_price=stop_upper,
            stop_lower_price=stop_lower,
            terminate_upper_price=terminate_upper,
            terminate_lower_price=terminate_lower
        )
        
        logger.info(
            f"网格参数计算完成：区间=[{lower_price:.2f}, {upper_price:.2f}], "
            f"数量={grid_count}, 方向={grid_direction}, 类型={grid_type}"
        )
        
        return params
    
    def _calculate_boundaries(
        self,
        current_price: float,
        atr_smooth: float,
        state: MarketState
    ) -> Tuple[float, float]:
        """
        计算网格边界
        
        Args:
            current_price: 当前价格
            atr_smooth: 平滑 ATR
            state: 市场状态
            
        Returns:
            (upper_price, lower_price)
        """
        # 基准宽度 = 4 × ATR
        base_width = 4 * atr_smooth
        
        if state == MarketState.RANGING:
            # 震荡：对称边界
            lower_price = current_price - base_width / 2
            upper_price = current_price + base_width / 2
            
        elif state == MarketState.UPTREND:
            # 上升趋势：浅下界，深上界
            lower_price = current_price - 1.5 * atr_smooth
            upper_price = current_price + 4 * atr_smooth
            
        else:  # DOWNTREND
            # 下降趋势：深下界，浅上界
            lower_price = current_price - 4 * atr_smooth
            upper_price = current_price + 1.5 * atr_smooth
        
        # 边界保护：确保上边界 > 下边界 + 最小宽度
        min_width = 2 * atr_smooth
        if upper_price <= lower_price + min_width:
            logger.warning("边界过窄，自动调整")
            upper_price = lower_price + min_width * 1.1  # 增加 10% 缓冲
        
        # 确保价格为正
        lower_price = max(0.01, lower_price)
        upper_price = max(lower_price * 1.01, upper_price)  # 至少 1% 的区间
        
        return upper_price, lower_price
    
    def _calculate_grid_count(self, atr_smooth: float) -> int:
        """
        计算网格数量
        
        根据波动率自适应调整：
        - 波动率升高 → 网格数量减少 → 格子变宽
        - 波动率降低 → 网格数量增加 → 格子变密
        
        Args:
            atr_smooth: 平滑 ATR
            
        Returns:
            网格数量
        """
        # 估算基准 ATR（假设过去 90 天均值）
        # 实际应用中应该从历史数据计算
        base_atr = atr_smooth  # 简化处理，使用当前 ATR 作为基准
        
        # 网格数量 = 基准网格数 × (基准 ATR / 当前 ATR)
        if atr_smooth > 0:
            grid_count = self.base_grid_count * (base_atr / atr_smooth)
        else:
            grid_count = self.base_grid_count
        
        # 限制在 [min_grid_count, max_grid_count] 范围内
        grid_count = int(max(self.min_grid_count, min(self.max_grid_count, grid_count)))
        
        logger.debug(f"网格数量计算：ATR={atr_smooth:.2f}, 数量={grid_count}")
        
        return grid_count
    
    def _determine_direction(self, state: MarketState) -> str:
        """
        确定网格方向
        
        Args:
            state: 市场状态
            
        Returns:
            网格方向：LONG/SHORT/NEUTRAL
        """
        if state == MarketState.UPTREND:
            return "LONG"  # 只做多
        elif state == MarketState.DOWNTREND:
            return "SHORT"  # 只做空
        else:
            return "NEUTRAL"  # 双向网格
    
    def _calculate_stop_prices(
        self,
        upper_price: float,
        lower_price: float,
        atr_smooth: float,
        state: MarketState
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        计算停止上移/下移价格
        
        Args:
            upper_price: 上边界
            lower_price: 下边界
            atr_smooth: 平滑 ATR
            state: 市场状态
            
        Returns:
            (stop_upper_price, stop_lower_price)
        """
        # 趋势强度系数
        # 注意：这里需要传入 ADX 值，暂时简化处理
        k_trend = 0.3  # 默认值
        
        # 上移/下移步长
        step = k_trend * atr_smooth
        
        # 停止上移价格：上边界 + 步长
        stop_upper = upper_price + step if state == MarketState.UPTREND else None
        
        # 停止下移价格：下边界 - 步长
        stop_lower = lower_price - step if state == MarketState.DOWNTREND else None
        
        return stop_upper, stop_lower
    
    def _calculate_terminate_prices(
        self,
        upper_price: float,
        lower_price: float,
        atr_smooth: float
    ) -> Tuple[float, float]:
        """
        计算终止最高/最低价格（硬止盈/止损线）
        
        Args:
            upper_price: 上边界
            lower_price: 下边界
            atr_smooth: 平滑 ATR
            
        Returns:
            (terminate_upper_price, terminate_lower_price)
        """
        # 基础终止价格
        terminate_lower = lower_price - 2 * atr_smooth  # 硬止损线
        terminate_upper = upper_price + 2 * atr_smooth  # 硬止盈线
        
        # 确保价格为正
        terminate_lower = max(0.01, terminate_lower)
        
        return terminate_upper, terminate_lower
    
    def check_adjustment_triggers(
        self,
        current_price: float,
        atr_smooth: float,
        state: MarketState,
        current_params: GridParameters,
        adx: Optional[float] = None
    ) -> List[AdjustmentTrigger]:
        """
        检查是否需要调整参数
        
        Args:
            current_price: 当前价格
            atr_smooth: 平滑 ATR
            state: 市场状态
            current_params: 当前网格参数
            adx: ADX 值（可选）
            
        Returns:
            触发条件列表
        """
        triggers = []
        
        # 1. 检查 ATR 变化
        if self._last_atr is not None:
            atr_change = abs(atr_smooth - self._last_atr) / self._last_atr
            if atr_change > self.atr_change_threshold:
                triggers.append(AdjustmentTrigger(
                    trigger_type="ATR_CHANGE",
                    description=f"ATR 变化 {atr_change*100:.1f}% (阈值：{self.atr_change_threshold*100}%)",
                    severity=min(1.0, atr_change / self.atr_change_threshold),
                    timestamp=datetime.now(),
                    details={
                        'old_atr': self._last_atr,
                        'new_atr': atr_smooth,
                        'change_percent': atr_change
                    }
                ))
        
        # 2. 检查市场状态变化（优化：需要连续确认）
        if self._last_state is not None and state != self._last_state:
            self._state_change_counter += 1
            
            # 保守模式：需要连续 3 次确认才触发
            if self._state_change_counter >= self.state_confirm_count:
                triggers.append(AdjustmentTrigger(
                    trigger_type="STATE_CHANGE",
                    description=f"市场状态从 {self._last_state.value} 变为 {state.value} (连续{self._state_change_counter}次确认)",
                    severity=0.8,
                    timestamp=datetime.now(),
                    details={
                        'old_state': self._last_state.value,
                        'new_state': state.value,
                        'confirm_count': self._state_change_counter
                    }
                ))
        else:
            # 状态未变化，重置计数器
            self._state_change_counter = 0
        
        # 3. 检查价格是否接近边界（优化：阈值从 0.5×ATR 提高到 1.5×ATR）
        price_range = current_params.upper_price - current_params.lower_price
        if price_range > 0:
            distance_to_upper = current_params.upper_price - current_price
            distance_to_lower = current_price - current_params.lower_price
            
            threshold = self.edge_approach_threshold * atr_smooth  # 保守模式：1.5×ATR
            
            if distance_to_upper < threshold:
                triggers.append(AdjustmentTrigger(
                    trigger_type="EDGE_APPROACH",
                    description=f"价格接近上边界 (距离：{distance_to_upper:.2f})",
                    severity=0.6,
                    timestamp=datetime.now(),
                    details={
                        'edge': 'upper',
                        'distance': distance_to_upper
                    }
                ))
            
            if distance_to_lower < threshold:
                triggers.append(AdjustmentTrigger(
                    trigger_type="EDGE_APPROACH",
                    description=f"价格接近下边界 (距离：{distance_to_lower:.2f})",
                    severity=0.6,
                    timestamp=datetime.now(),
                    details={
                        'edge': 'lower',
                        'distance': distance_to_lower
                    }
                ))
        
        # 4. 新增：检查价格偏离网格中心（保守模式：> 10% 才触发）
        grid_center = (current_params.upper_price + current_params.lower_price) / 2
        price_deviation = abs(current_price - grid_center) / grid_center
        
        if price_deviation > self.price_deviation_threshold:
            severity = min(1.0, price_deviation / 0.20)  # 偏离 20% 时严重性为 1.0
            triggers.append(AdjustmentTrigger(
                trigger_type="PRICE_DEVIATION",
                description=f"价格偏离网格中心 {price_deviation*100:.1f}% (阈值：{self.price_deviation_threshold*100}%)",
                severity=severity,
                timestamp=datetime.now(),
                details={
                    'grid_center': grid_center,
                    'current_price': current_price,
                    'deviation_percent': price_deviation
                }
            ))
        
        # 5. 新增：检查终止价格偏离（保守模式：> 15% 才触发）
        terminate_triggers = self._check_terminate_price_deviation(
            current_params, current_price, atr_smooth
        )
        triggers.extend(terminate_triggers)
        
        # 更新最后记录
        self._last_atr = atr_smooth
        self._last_state = state
        
        return triggers
    
    def _check_terminate_price_deviation(
        self,
        current_params: GridParameters,
        current_price: float,
        atr_smooth: float
    ) -> List[AdjustmentTrigger]:
        """
        检查终止价格偏离度（保守模式：> 15% 才触发）
        
        Args:
            current_params: 当前网格参数
            current_price: 当前价格
            atr_smooth: 平滑 ATR
            
        Returns:
            触发条件列表
        """
        triggers = []
        
        # 计算合理的终止价格
        reasonable_stop_loss = current_price - 3 * atr_smooth
        reasonable_stop_profit = current_price + 3 * atr_smooth
        
        # 检查当前终止价格
        current_stop_loss = current_params.terminate_lower_price
        current_stop_profit = current_params.terminate_upper_price
        
        if current_stop_loss is None or current_stop_profit is None:
            return triggers
        
        # 计算偏离度
        stop_loss_deviation = abs(current_stop_loss - reasonable_stop_loss) / reasonable_stop_loss
        stop_profit_deviation = abs(current_stop_profit - reasonable_stop_profit) / reasonable_stop_profit
        
        # 保守模式：只有偏离 > 15% 才触发
        if stop_loss_deviation > self.terminate_deviation_threshold:
            severity = min(1.0, stop_loss_deviation / 0.30)  # 偏离 30% 时严重性为 1.0
            triggers.append(AdjustmentTrigger(
                trigger_type="TERMINATE_DEVIATION",
                description=f"止损线偏离 {stop_loss_deviation*100:.1f}% (阈值：{self.terminate_deviation_threshold*100}%)",
                severity=severity,
                timestamp=datetime.now(),
                details={
                    'type': 'stop_loss',
                    'current': current_stop_loss,
                    'reasonable': reasonable_stop_loss,
                    'deviation_percent': stop_loss_deviation
                }
            ))
        
        if stop_profit_deviation > self.terminate_deviation_threshold:
            severity = min(1.0, stop_profit_deviation / 0.30)
            triggers.append(AdjustmentTrigger(
                trigger_type="TERMINATE_DEVIATION",
                description=f"止盈线偏离 {stop_profit_deviation*100:.1f}% (阈值：{self.terminate_deviation_threshold*100}%)",
                severity=severity,
                timestamp=datetime.now(),
                details={
                    'type': 'stop_profit',
                    'current': current_stop_profit,
                    'reasonable': reasonable_stop_profit,
                    'deviation_percent': stop_profit_deviation
                }
            ))
        
        return triggers
    
    def should_adjust(
        self,
        triggers: List[AdjustmentTrigger],
        current_price: Optional[float] = None,
        current_params: Optional[GridParameters] = None,
        atr_smooth: Optional[float] = None
    ) -> bool:
        """
        判断是否应该调整参数（保守策略）
        
        Args:
            triggers: 触发条件列表
            current_price: 当前价格（可选，用于极端情况检查）
            current_params: 当前网格参数（可选）
            atr_smooth: 平滑 ATR（可选）
            
        Returns:
            是否调整
        """
        if not triggers:
            return False
        
        # 检查最小调整间隔
        if self._last_adjustment_time:
            time_since_last = (datetime.now() - self._last_adjustment_time).seconds
            min_interval = 4 * 3600  # 4 小时
            
            if time_since_last < min_interval:
                logger.info(f"距离上次调整仅 {time_since_last/3600:.1f} 小时，跳过调整")
                return False
        
        # 检查每日最大调整次数
        if self._adjustment_count_today >= 6:
            logger.info(f"今日已调整 {self._adjustment_count_today} 次，达到上限")
            return False
        
        # ===== 极端情况：立即调整 =====
        if current_price is not None and current_params is not None and atr_smooth is not None:
            extreme_adjustment = self._check_extreme_situation(
                current_price, current_params, atr_smooth
            )
            if extreme_adjustment:
                logger.warning("⚠️  检测到极端情况，立即调整！")
                return True
        
        # ===== 保守模式：严重性检查 =====
        # 只有严重性 > 0.7 的触发才调整
        if self.conservative_mode:
            serious_triggers = [t for t in triggers if t.severity > self.min_trigger_severity]
            if not serious_triggers:
                logger.info(f"触发条件严重性不足（最大严重性：{max(t.severity for t in triggers):.2f}），跳过调整")
                return False
        
        return True
    
    def _check_extreme_situation(
        self,
        current_price: float,
        current_params: GridParameters,
        atr_smooth: float
    ) -> bool:
        """
        检查极端情况（需要立即调整）
        
        Args:
            current_price: 当前价格
            current_params: 当前网格参数
            atr_smooth: 平滑 ATR
            
        Returns:
            是否为极端情况
        """
        # 1. 价格突破网格范围 > 10%
        grid_range = current_params.upper_price - current_params.lower_price
        if current_price > current_params.upper_price * 1.10:
            logger.warning(f"⚠️  极端：价格突破上边界 10%！当前价格：{current_price:.2f}，上边界：{current_params.upper_price:.2f}")
            return True
        if current_price < current_params.lower_price * 0.90:
            logger.warning(f"⚠️  极端：价格突破下边界 10%！当前价格：{current_price:.2f}，下边界：{current_params.lower_price:.2f}")
            return True
        
        # 2. ATR 剧烈变化 > 50%
        if self._last_atr is not None:
            atr_change = abs(atr_smooth - self._last_atr) / self._last_atr
            if atr_change > 0.50:
                logger.warning(f"⚠️  极端：ATR 变化 {atr_change*100:.1f}%！当前 ATR：{atr_smooth:.2f}，上次 ATR：{self._last_atr:.2f}")
                return True
        
        return False
    
    def record_adjustment(self) -> None:
        """记录调整"""
        self._last_adjustment_time = datetime.now()
        self._adjustment_count_today += 1
        logger.info(f"记录参数调整，今日累计：{self._adjustment_count_today} 次")
    
    def reset_daily_count(self) -> None:
        """重置每日计数（每日调用一次）"""
        self._adjustment_count_today = 0
        logger.info("重置每日调整计数")
