"""
网格计算器
计算网格层级、价格、数量
支持动态网格参数计算，根据市场状态自动调整
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from typing import ClassVar, List, Optional, Dict, Tuple
import pandas as pd
import structlog

from shared.indicators import TechnicalIndicators


logger = structlog.get_logger()


class GridMode(Enum):
    """
    网格模式枚举

    Attributes:
        ARITHMETIC: 等差网格
        GEOMETRIC: 等比网格
    """
    ARITHMETIC = "等差"
    GEOMETRIC = "等比"


# 约束链绑定归属（V2.5.5 方案B，FR-10）
BINDING_STRUCT = 'STRUCT'        # 结构约束（ATR 波动结构）
BINDING_PROFIT = 'PROFIT'        # 利润率约束（每格利润率下限）
BINDING_CAPITAL = 'CAPITAL'      # 资本约束（每格张数下限）
BINDING_INFEASIBLE = 'INFEASIBLE'  # 输入非法，无法判定绑定约束


@dataclass
class CapitalFeasibility:
    """
    资本可行性解析结果（V2.5.5 方案B）

    Attributes:
        feasible: 是否可行（min(N_struct, N_profit, N_cap) >= N_min）
        grid_count: 最终网格数；不可行时返回 N_struct 仅供参考
        qty_per_grid: 每格张数；可行时按 N_final、不可行时按 N_min 计算
        binding: 绑定约束 STRUCT / PROFIT / CAPITAL；不可行取实际压制项；
                 输入非法时为 INFEASIBLE
        required_margin: 不可行时，保持杠杆满足 N_min 格所需的最低保证金（ceil，可达）
        required_leverage: 不可行时，保持保证金满足 N_min 格所需的最低杠杆（ceil，可达）
    """
    feasible: bool
    grid_count: int
    qty_per_grid: Decimal
    binding: str
    required_margin: Optional[Decimal] = None
    required_leverage: Optional[int] = None


@dataclass
class DynamicGridParams:
    """
    动态网格参数数据类

    Attributes:
        lower_boundary: 下边界价格
        upper_boundary: 上边界价格
        grid_count: 网格数量
        grid_mode: 网格模式
        stop_loss_low: 终止最低价
        stop_loss_high: 终止最高价
        stop_move_up_price: 停止上移价格（可选）
        stop_move_down_price: 停止下移价格（可选）
        profit_rate: 每格利润率
        grid_spacing: 网格间距
    """
    lower_boundary: Decimal
    upper_boundary: Decimal
    grid_count: int
    grid_mode: GridMode
    stop_loss_low: Decimal
    stop_loss_high: Decimal
    stop_move_up_price: Optional[Decimal] = None
    stop_move_down_price: Optional[Decimal] = None
    profit_rate: Decimal = Decimal('0')
    grid_spacing: Decimal = Decimal('0')

    # 数据验证层硬性下限（ClassVar 不参与 dataclass 字段）：
    # 与 config.yaml 中 grid.weak_trend_min_grid_count 的全局最小值保持一致
    MIN_GRID_COUNT_HARD_LIMIT: ClassVar[int] = 4

    def __post_init__(self):
        """参数验证"""
        if self.lower_boundary <= 0:
            raise ValueError(f"下边界必须大于0，实际为 {self.lower_boundary}")

        if self.upper_boundary <= self.lower_boundary:
            raise ValueError(f"上边界 {self.upper_boundary} 必须大于下边界 {self.lower_boundary}")

        # 数据验证层硬性下限：取各市场状态配置下限的全局最小值
        # （弱趋势 weak_trend_min_grid_count=4，见 config.yaml）。
        # 状态感知的业务范围（震荡[5,12]/弱趋势[4,10]）由
        # GridCalculator.calculate_dynamic_grid_params 按 config 夹逼（修正 D-3）。
        if self.grid_count < self.MIN_GRID_COUNT_HARD_LIMIT:
            raise ValueError(
                f"网格数量必须至少为{self.MIN_GRID_COUNT_HARD_LIMIT}，实际为 {self.grid_count}"
            )

        if not isinstance(self.grid_mode, GridMode):
            raise ValueError(f"网格模式必须是 GridMode 类型，实际为 {type(self.grid_mode).__name__}")


@dataclass
class GridLevel:
    """
    网格层级数据类

    Attributes:
        price: 网格价格
        side: 交易方向 (BUY/SELL/HOLD)
        quantity: 交易数量
        level: 层级编号
    """
    price: Decimal
    side: str  # BUY/SELL/HOLD
    quantity: Decimal
    level: int = 0

    def __post_init__(self):
        """参数验证"""
        if self.price <= 0:
            raise ValueError(f"价格必须大于0，实际为 {self.price}")

        if self.quantity <= 0:
            raise ValueError(f"数量必须大于0，实际为 {self.quantity}")

        if self.side not in ['BUY', 'SELL', 'HOLD']:
            raise ValueError(f"方向必须是 BUY/SELL/HOLD，实际为 {self.side}")


class GridCalculator:
    """
    网格计算器

    支持三种网格类型：
    - arithmetic: 等差网格，网格间距固定
    - geometric: 等比网格，网格间距按比例递增
    - dynamic: 动态网格，根据波动率自动调整间距
    """

    def __init__(self, config: dict):
        """
        初始化网格计算器

        Args:
            config: 配置字典，包含网格参数

        Raises:
            ValueError: 配置参数验证失败
        """
        if not isinstance(config, dict):
            raise ValueError(f"配置必须是字典类型，实际为 {type(config).__name__}")

        self.config = config
        grid_config = config.get('grid', {})

        # 网格类型
        self.grid_type = grid_config.get('type', 'arithmetic')
        if self.grid_type not in ['arithmetic', 'geometric', 'dynamic']:
            raise ValueError(f"不支持的网格类型: {self.grid_type}")

        # 网格数量
        self.grid_count = grid_config.get('count')
        if self.grid_count is None:
            raise ValueError("配置缺失：grid.count")
        if self.grid_count <= 0:
            raise ValueError(f"网格数量必须大于0，实际为 {self.grid_count}")

        # 网格间距
        spacing = grid_config.get('spacing')
        if spacing is None:
            raise ValueError("配置缺失：grid.spacing")
        self.grid_spacing = Decimal(str(spacing))
        if self.grid_spacing <= 0:
            raise ValueError(f"网格间距必须大于0，实际为 {self.grid_spacing}")

        # 网格比例
        spacing_ratio = grid_config.get('spacing_ratio')
        if spacing_ratio is None:
            raise ValueError("配置缺失：grid.spacing_ratio")
        self.grid_spacing_ratio = Decimal(str(spacing_ratio))
        if self.grid_spacing_ratio <= 1:
            raise ValueError(f"网格比例必须大于1，实际为 {self.grid_spacing_ratio}")

        # 基础数量
        base_quantity = grid_config.get('base_quantity')
        if base_quantity is None:
            raise ValueError("配置缺失：grid.base_quantity")
        self.base_quantity = Decimal(str(base_quantity))
        if self.base_quantity <= 0:
            raise ValueError(f"基础数量必须大于0，实际为 {self.base_quantity}")

        # 价格区间
        price_range = grid_config.get('price_range', {})
        min_price = price_range.get('min')
        max_price = price_range.get('max')

        self.price_range_min = Decimal(str(min_price)) if min_price else None
        self.price_range_max = Decimal(str(max_price)) if max_price else None

        if self.price_range_min and self.price_range_max:
            if self.price_range_min >= self.price_range_max:
                raise ValueError(
                    f"最低价格 {self.price_range_min} 必须小于最高价格 {self.price_range_max}"
                )

        logger.info(
            "网格计算器初始化完成",
            grid_type=self.grid_type,
            grid_count=self.grid_count,
            grid_spacing=float(self.grid_spacing),
            base_quantity=float(self.base_quantity)
        )

    def calculate_grid_levels(
        self,
        current_price: Decimal,
        volatility: Optional[Decimal] = None
    ) -> List[GridLevel]:
        """
        计算网格层级

        Args:
            current_price: 当前价格
            volatility: 波动率（动态网格使用）

        Returns:
            网格层级列表

        Raises:
            ValueError: 参数验证失败
        """
        if current_price <= 0:
            raise ValueError(f"当前价格必须大于0，实际为 {current_price}")

        # 根据网格类型计算
        if self.grid_type == 'arithmetic':
            levels = self._calculate_arithmetic_grid(current_price)
        elif self.grid_type == 'geometric':
            levels = self._calculate_geometric_grid(current_price)
        elif self.grid_type == 'dynamic':
            levels = self._calculate_dynamic_grid(current_price, volatility)
        else:
            raise ValueError(f"不支持的网格类型：{self.grid_type}")

        # 过滤价格范围
        if self.price_range_min or self.price_range_max:
            levels = self._filter_by_price_range(levels)

        logger.info(
            "计算网格层级完成",
            grid_type=self.grid_type,
            levels_count=len(levels),
            price_range=f"[{levels[0].price if levels else 0}, {levels[-1].price if levels else 0}]"
        )

        return levels

    def _calculate_arithmetic_grid(self, current_price: Decimal) -> List[GridLevel]:
        """
        计算等差网格

        网格间距固定，例如每100 USDT一个网格

        Args:
            current_price: 当前价格

        Returns:
            网格层级列表
        """
        levels = []
        half_count = self.grid_count // 2

        for i in range(-half_count, half_count + 1):
            price = current_price + self.grid_spacing * i

            # 跳过负数或零价格
            if price <= 0:
                continue

            # 确定交易方向
            if i < 0:
                side = 'BUY'  # 低于当前价格，挂买单
            elif i > 0:
                side = 'SELL'  # 高于当前价格，挂卖单
            else:
                side = 'HOLD'  # 当前价格，不挂单

            level = GridLevel(
                price=price,
                side=side,
                quantity=self.base_quantity,
                level=i + half_count
            )
            levels.append(level)

        return levels

    def _calculate_geometric_grid(self, current_price: Decimal) -> List[GridLevel]:
        """
        计算等比网格

        网格间距按比例递增，例如每个网格价格是前一个的1.01倍

        Args:
            current_price: 当前价格

        Returns:
            网格层级列表
        """
        levels = []
        half_count = self.grid_count // 2

        for i in range(-half_count, half_count + 1):
            # 计算价格：current_price * (ratio ** i)
            price = current_price * (self.grid_spacing_ratio ** i)

            # 确定交易方向
            if i < 0:
                side = 'BUY'
            elif i > 0:
                side = 'SELL'
            else:
                side = 'HOLD'

            level = GridLevel(
                price=price,
                side=side,
                quantity=self.base_quantity,
                level=i + half_count
            )
            levels.append(level)

        return levels

    def _calculate_dynamic_grid(
        self,
        current_price: Decimal,
        volatility: Optional[Decimal]
    ) -> List[GridLevel]:
        """
        计算动态网格

        根据波动率自动调整网格间距，波动率越大，网格间距越大

        Args:
            current_price: 当前价格
            volatility: 波动率（0-1之间的小数）

        Returns:
            网格层级列表
        """
        # 根据波动率调整网格间距
        if volatility and volatility > 0:
            # 波动率越大，网格间距越大
            adjusted_spacing = self.grid_spacing * (Decimal('1') + volatility)
        else:
            adjusted_spacing = self.grid_spacing

        # 临时修改间距
        original_spacing = self.grid_spacing
        self.grid_spacing = adjusted_spacing

        # 使用等差网格计算
        levels = self._calculate_arithmetic_grid(current_price)

        # 恢复原始间距
        self.grid_spacing = original_spacing

        logger.debug(
            "动态网格计算完成",
            original_spacing=float(original_spacing),
            adjusted_spacing=float(adjusted_spacing),
            volatility=float(volatility) if volatility else None
        )

        return levels

    def _filter_by_price_range(self, levels: List[GridLevel]) -> List[GridLevel]:
        """
        按价格范围过滤网格层级

        Args:
            levels: 网格层级列表

        Returns:
            过滤后的网格层级列表
        """
        filtered = []

        for level in levels:
            # 检查最低价格
            if self.price_range_min and level.price < self.price_range_min:
                continue

            # 检查最高价格
            if self.price_range_max and level.price > self.price_range_max:
                continue

            filtered.append(level)

        logger.debug(
            "价格范围过滤完成",
            original_count=len(levels),
            filtered_count=len(filtered),
            price_range=f"[{self.price_range_min}, {self.price_range_max}]"
        )

        return filtered

    def calculate_reverse_price(
        self,
        level: GridLevel,
        profit_margin: Optional[Decimal] = None
    ) -> Decimal:
        """
        计算反向挂单价格

        当一个网格订单成交后，需要在相反方向挂单以实现网格交易

        Args:
            level: 网格层级
            profit_margin: 利润边际（可选，默认使用网格间距）

        Returns:
            反向价格

        Raises:
            ValueError: 参数验证失败
        """
        if not isinstance(level, GridLevel):
            raise ValueError(f"level 必须是 GridLevel 类型，实际为 {type(level).__name__}")

        profit_margin = profit_margin or self.grid_spacing

        if level.side == 'BUY':
            # 买入后卖出价格 = 买入价格 + 利润边际
            reverse_price = level.price + profit_margin
        elif level.side == 'SELL':
            # 卖出后买入价格 = 卖出价格 - 利润边际
            reverse_price = level.price - profit_margin
        else:
            raise ValueError(f"无法为 HOLD 方向计算反向价格")

        # 确保价格大于0
        if reverse_price <= 0:
            raise ValueError(f"计算的反向价格 {reverse_price} 无效（小于等于0）")

        logger.debug(
            "计算反向价格",
            original_price=float(level.price),
            original_side=level.side,
            reverse_price=float(reverse_price),
            profit_margin=float(profit_margin)
        )

        return reverse_price

    def get_grid_info(self) -> dict:
        """
        获取网格配置信息

        Returns:
            网格配置信息字典
        """
        return {
            'grid_type': self.grid_type,
            'grid_count': self.grid_count,
            'grid_spacing': float(self.grid_spacing),
            'grid_spacing_ratio': float(self.grid_spacing_ratio),
            'base_quantity': float(self.base_quantity),
            'price_range': {
                'min': float(self.price_range_min) if self.price_range_min else None,
                'max': float(self.price_range_max) if self.price_range_max else None
            }
        }

    def calculate_dynamic_grid_params(
        self,
        current_price: Decimal,
        atr_smooth: Decimal,
        atr_baseline: Decimal,
        market_state: str,
        trend_strength: Decimal = Decimal('0'),
        stop_loss_buffer: Optional[int] = None
    ) -> DynamicGridParams:
        """
        计算动态网格参数

        根据市场状态和波动率自动计算最优网格参数：
        - 价格区间：根据市场状态和ATR计算
        - 网格数量：根据ATR基准和平滑ATR的比值计算
        - 网格模式：根据价格区间宽度自动选择等差或等比
        - 止盈止损：根据ATR和止损缓冲倍数计算

        Args:
            current_price: 当前价格
            atr_smooth: 平滑ATR
            atr_baseline: 基准ATR（90天均值）
            market_state: 市场状态（'震荡市场', '弱趋势'）
            trend_strength: 趋势强度系数 k (0-0.5)
            stop_loss_buffer: 止损缓冲倍数（可选，默认从配置读取）

        Returns:
            动态网格参数

        Raises:
            ValueError: 参数验证失败
        """
        if current_price <= 0:
            raise ValueError(f"当前价格必须大于0，实际为 {current_price}")

        if atr_smooth <= 0:
            raise ValueError(f"平滑ATR必须大于0，实际为 {atr_smooth}")

        if atr_baseline <= 0:
            raise ValueError(f"基准ATR必须大于0，实际为 {atr_baseline}")

        # V2.1 状态值
        valid_states = ['震荡市场', '弱趋势']
        if market_state not in valid_states:
            raise ValueError(f"不支持计算网格参数的市场状态: {market_state}，该状态应由 signal_bot 处理")

        # 从配置读取止损缓冲倍数（V2.2：弱趋势使用更大的止损缓冲）
        if stop_loss_buffer is None:
            if market_state == '弱趋势':
                stop_loss_buffer = self.config.get('grid', {}).get('weak_trend_stop_loss_buffer')
                if stop_loss_buffer is None:
                    stop_loss_buffer = self.config.get('grid', {}).get('stop_loss_buffer', 2.4)
            else:
                stop_loss_buffer = self.config.get('grid', {}).get('stop_loss_buffer')
                if stop_loss_buffer is None:
                    raise ValueError("配置缺失：grid.stop_loss_buffer")

        logger.info(
            "开始计算动态网格参数",
            current_price=float(current_price),
            atr_smooth=float(atr_smooth),
            atr_baseline=float(atr_baseline),
            market_state=market_state,
            trend_strength=float(trend_strength)
        )

        # 1. 计算价格区间
        lower_boundary, upper_boundary = self._calculate_price_range(
            current_price=current_price,
            atr_smooth=atr_smooth,
            market_state=market_state
        )

        # 2. 计算网格数量（市场状态感知）
        atr_ratio = atr_baseline / atr_smooth
        atr_multipliers = self.config.get('market', {}).get('atr_multipliers', {})
        # 从配置读取网格数量范围
        min_grid = self.config.get('grid', {}).get('min_grid_count', 5)
        max_grid = self.config.get('grid', {}).get('max_grid_count', 12)
        # 从配置读取基准网格数量
        base_grid_count = self.config.get('grid', {}).get('base_grid_count', 8)
        if market_state == '弱趋势':
            # 弱趋势使用独立的基准网格数（V2.2）
            weak_base_count = self.config.get('grid', {}).get('weak_trend_base_grid_count', 6)
            # 弱趋势使用不同的ATR系数
            atr_multiplier = Decimal(str(atr_multipliers.get('weak_trend', 6.0)))
            weak_reduction = float(self.config.get('grid', {}).get('weak_trend_grid_reduction_factor', 0.8))
            raw_count = round(float(atr_ratio) * weak_base_count * weak_reduction)

            # 弱趋势最小/最大网格数（D-3：必须直接按弱趋势限夹逼，
            # 旧实现先用震荡 5/12 夹一次，会把弱趋势 4 格静默抬回 5）
            weak_min = self.config.get('grid', {}).get('weak_trend_min_grid_count', 4)
            weak_max = self.config.get('grid', {}).get('weak_trend_max_grid_count', 10)
            grid_count = max(min(raw_count, weak_max), weak_min)
        else:
            # 震荡市场：标准参数
            atr_multiplier = Decimal(str(atr_multipliers.get('oscillation', 5.0)))
            raw_count = round(float(atr_ratio) * base_grid_count)
            grid_count = max(min(raw_count, max_grid), min_grid)

        # 3. 选择网格模式
        grid_mode = self._select_grid_mode(
            lower_boundary=lower_boundary,
            upper_boundary=upper_boundary,
            current_price=current_price
        )

        # 4. 计算止盈止损价格
        stop_loss_low = lower_boundary - Decimal(str(stop_loss_buffer)) * atr_smooth
        stop_loss_high = upper_boundary + Decimal(str(stop_loss_buffer)) * atr_smooth

        # 5. 计算停止上移/下移价格
        stop_move_up_price = None
        stop_move_down_price = None

        if market_state == '震荡市场' or market_state == '弱趋势':
            # 震荡/弱趋势：上下移都计算，使用止盈止损缓冲除以配置除数
            move_divisor = Decimal(str(
                self.config.get('grid', {}).get('oscillation_move_buffer_divisor', 2)
            ))
            move_buffer = Decimal(str(stop_loss_buffer)) / move_divisor
            stop_move_up_price = upper_boundary + move_buffer * atr_smooth
            stop_move_down_price = lower_boundary - move_buffer * atr_smooth
            logger.debug(
                "震荡/弱趋势：计算上下移价格",
                market_state=market_state,
                upper_boundary=float(upper_boundary),
                lower_boundary=float(lower_boundary),
                atr_smooth=float(atr_smooth),
                stop_move_up_price=float(stop_move_up_price),
                stop_move_down_price=float(stop_move_down_price)
            )

        # 未覆盖的状态：回退止盈止损价
        if stop_move_up_price is None:
            stop_move_up_price = stop_loss_high
        if stop_move_down_price is None:
            stop_move_down_price = stop_loss_low

        # 6. 计算每格利润率和网格间距
        profit_rate, grid_spacing = self._calculate_profit_rate(
            lower_boundary=lower_boundary,
            upper_boundary=upper_boundary,
            grid_count=grid_count,
            grid_mode=grid_mode,
            current_price=current_price
        )

        # 7. 验证网格数量范围（状态感知夹逼，修正 D-3）：
        # 弱趋势使用 weak_trend_min/max_grid_count（4/10），
        # 震荡使用 min/max_grid_count（5/12）。
        # 旧实现对弱趋势误用震荡的 5/12，把弱趋势 4 格静默抬回 5，
        # 导致 weak_trend_min_grid_count=4 配置失效。
        if market_state == '弱趋势':
            range_min_grid = self.config.get('grid', {}).get('weak_trend_min_grid_count', 4)
            range_max_grid = self.config.get('grid', {}).get('weak_trend_max_grid_count', 10)
        else:
            range_min_grid = min_grid
            range_max_grid = max_grid
        if grid_count < range_min_grid or grid_count > range_max_grid:
            logger.warning(
                "网格数量超出配置范围，已自动调整",
                market_state=market_state,
                original_grid_count=grid_count,
                min_grid_count=range_min_grid,
                max_grid_count=range_max_grid
            )
            grid_count = max(range_min_grid, min(range_max_grid, grid_count))

        # 8. 构建结果
        params = DynamicGridParams(
            lower_boundary=lower_boundary,
            upper_boundary=upper_boundary,
            grid_count=grid_count,
            grid_mode=grid_mode,
            stop_loss_low=stop_loss_low,
            stop_loss_high=stop_loss_high,
            stop_move_up_price=stop_move_up_price,
            stop_move_down_price=stop_move_down_price,
            profit_rate=profit_rate,
            grid_spacing=grid_spacing
        )

        logger.info(
            "动态网格参数计算完成",
            lower_boundary=float(lower_boundary),
            upper_boundary=float(upper_boundary),
            grid_count=grid_count,
            grid_mode=grid_mode.value,
            profit_rate=float(profit_rate) * 100
        )

        return params

    def _calculate_price_range(
        self,
        current_price: Decimal,
        atr_smooth: Decimal,
        market_state: str
    ) -> Tuple[Decimal, Decimal]:
        """
        计算价格区间

        根据市场状态计算价格区间：
        - 震荡市场：P ± oscillation_atr_multiplier×ATR
        - 弱趋势：P ± weak_trend_atr_multiplier×ATR

        Args:
            current_price: 当前价格
            atr_smooth: 平滑ATR
            market_state: 市场状态

        Returns:
            (下边界, 上边界)
        """
        # 从配置读取ATR倍数参数
        atr_multipliers = self.config.get('market', {}).get('atr_multipliers', {})

        if market_state == '震荡市场':
            atr_multiplier = Decimal(str(
                atr_multipliers.get('oscillation', 2.0)
            ))
            lower_boundary = current_price - atr_multiplier * atr_smooth
            upper_boundary = current_price + atr_multiplier * atr_smooth
        elif market_state == '弱趋势':
            atr_multiplier = Decimal(str(
                atr_multipliers.get('weak_trend', 2.4)
            ))
            lower_boundary = current_price - atr_multiplier * atr_smooth
            upper_boundary = current_price + atr_multiplier * atr_smooth
        else:
            # 默认使用震荡市场的参数
            atr_multiplier = Decimal(str(
                atr_multipliers.get('oscillation', 2.0)
            ))
            lower_boundary = current_price - atr_multiplier * atr_smooth
            upper_boundary = current_price + atr_multiplier * atr_smooth

        logger.debug(
            "价格区间计算完成",
            market_state=market_state,
            lower_boundary=float(lower_boundary),
            upper_boundary=float(upper_boundary)
        )

        return lower_boundary, upper_boundary

    def _calculate_grid_count(
        self,
        atr_baseline: Decimal,
        atr_smooth: Decimal
    ) -> int:
        """
        计算网格数量

        公式：N = round(base_grid_count × ATR基准 / ATR平滑)
        范围：[min_grid_count, max_grid_count]

        Args:
            atr_baseline: 基准ATR
            atr_smooth: 平滑ATR

        Returns:
            网格数量
        """
        # 从配置读取基础网格数量
        base_grid_count = self.config.get('grid', {}).get('base_grid_count')
        if base_grid_count is None:
            raise ValueError("配置缺失：grid.base_grid_count")

        # 计算基础网格数量
        base_count = base_grid_count * atr_baseline / atr_smooth

        # 四舍五入
        grid_count = int(round(float(base_count)))

        # 限制范围（从配置读取）
        min_grid_count = self.config.get('grid', {}).get('min_grid_count')
        if min_grid_count is None:
            raise ValueError("配置缺失：grid.min_grid_count")

        max_grid_count = self.config.get('grid', {}).get('max_grid_count')
        if max_grid_count is None:
            raise ValueError("配置缺失：grid.max_grid_count")

        grid_count = max(min_grid_count, min(max_grid_count, grid_count))

        logger.debug(
            "网格数量计算完成",
            atr_baseline=float(atr_baseline),
            atr_smooth=float(atr_smooth),
            base_count=float(base_count),
            grid_count=grid_count
        )

        return grid_count

    def _select_grid_mode(
        self,
        lower_boundary: Decimal,
        upper_boundary: Decimal,
        current_price: Decimal
    ) -> GridMode:
        """
        选择网格模式

        规则：如果 (上边界 - 下边界) / 当前价格 < amplitude_threshold，则使用等差网格，否则使用等比网格

        Args:
            lower_boundary: 下边界
            upper_boundary: 上边界
            current_price: 当前价格

        Returns:
            网格模式
        """
        # 计算价格区间宽度比例
        width_ratio = (upper_boundary - lower_boundary) / current_price

        # 从配置读取振幅阈值
        amplitude_threshold = self.config.get('grid', {}).get('amplitude_threshold')
        if amplitude_threshold is None:
            raise ValueError("配置缺失：grid.amplitude_threshold")
        amplitude_threshold = Decimal(str(amplitude_threshold))

        # 判断网格模式
        if width_ratio < amplitude_threshold:
            grid_mode = GridMode.ARITHMETIC
        else:
            grid_mode = GridMode.GEOMETRIC

        logger.debug(
            "网格模式选择完成",
            width_ratio=float(width_ratio),
            grid_mode=grid_mode.value
        )

        return grid_mode

    def _calculate_profit_rate(
        self,
        lower_boundary: Decimal,
        upper_boundary: Decimal,
        grid_count: int,
        grid_mode: GridMode,
        current_price: Decimal
    ) -> Tuple[Decimal, Decimal]:
        """
        计算每格利润率和网格间距

        Args:
            lower_boundary: 下边界
            upper_boundary: 上边界
            grid_count: 网格数量
            grid_mode: 网格模式
            current_price: 当前价格

        Returns:
            (利润率, 网格间距)
        """
        if grid_mode == GridMode.ARITHMETIC:
            # 等差网格：间距 = (上边界 - 下边界) / N
            grid_spacing = (upper_boundary - lower_boundary) / Decimal(str(grid_count))
            profit_rate = grid_spacing / current_price

        else:
            # 等比网格：比例 = (上边界 / 下边界)^(1/N)
            ratio = (upper_boundary / lower_boundary) ** (Decimal('1') / Decimal(str(grid_count)))
            profit_rate = ratio - Decimal('1')
            grid_spacing = current_price * profit_rate

        logger.debug(
            "利润率计算完成",
            grid_mode=grid_mode.value,
            profit_rate=float(profit_rate) * 100,
            grid_spacing=float(grid_spacing)
        )

        return profit_rate, grid_spacing

    def validate_profit_rate(
        self,
        params: DynamicGridParams,
        min_profit_rate: Optional[Decimal] = None,
        min_grid_count: Optional[int] = None
    ) -> Tuple[bool, Optional[int]]:
        """
        验证每格利润率是否满足要求

        币安要求每格利润率 > 1%，如果不满足则减少网格数量。
        降网格试探沿用 _calculate_profit_rate 的等差/等比精确口径，
        不使用 W/(P×r_min) 近似（等比临界场景可能差 1）。

        Args:
            params: 动态网格参数
            min_profit_rate: 最小利润率（可选，默认从配置读取）
            min_grid_count: 降网格下限（可选，默认读 grid.min_grid_count；
                            弱趋势应传 weak_trend_min_grid_count）

        Returns:
            (是否满足, 建议的网格数量)
        """
        # 从配置读取最小利润率
        if min_profit_rate is None:
            min_profit_rate = self.config.get('grid', {}).get('min_profit_rate')
            if min_profit_rate is None:
                raise ValueError("配置缺失：grid.min_profit_rate")
            min_profit_rate = Decimal(str(min_profit_rate))

        if params.profit_rate >= min_profit_rate:
            return True, None

        # 尝试减少网格数量（未显式传入时从配置读取震荡最小网格数量）
        if min_grid_count is None:
            min_grid_count = self.config.get('grid', {}).get('min_grid_count')
            if min_grid_count is None:
                raise ValueError("配置缺失：grid.min_grid_count")

        for new_count in range(params.grid_count - 1, min_grid_count - 1, -1):
            profit_rate, _ = self._calculate_profit_rate(
                lower_boundary=params.lower_boundary,
                upper_boundary=params.upper_boundary,
                grid_count=new_count,
                grid_mode=params.grid_mode,
                current_price=(params.lower_boundary + params.upper_boundary) / 2
            )

            if profit_rate >= min_profit_rate:
                logger.info(
                    "利润率验证失败，建议减少网格数量",
                    original_profit_rate=float(params.profit_rate) * 100,
                    suggested_grid_count=new_count,
                    new_profit_rate=float(profit_rate) * 100
                )
                return False, new_count

        logger.warning(
            "利润率验证失败，无法通过减少网格数量满足要求",
            profit_rate=float(params.profit_rate) * 100,
            min_profit_rate=float(min_profit_rate) * 100
        )

        return False, None

    def calculate_baseline_atr(self, klines: List[Dict]) -> Decimal:
        """
        计算基准ATR

        使用配置的滚动窗口计算ATR均值

        Args:
            klines: K线数据列表（至少atr_baseline_period根）

        Returns:
            基准ATR值

        Raises:
            ValueError: 数据不足
        """
        # 从配置读取ATR基准周期和ATR计算周期
        atr_baseline_period = self.config.get('market', {}).get('atr_baseline_period')
        if atr_baseline_period is None:
            raise ValueError("配置缺失：market.atr_baseline_period")

        atr_period = self.config.get('market', {}).get('atr_period')
        if atr_period is None:
            raise ValueError("配置缺失：market.atr_period")

        if not klines or len(klines) < atr_baseline_period:
            raise ValueError(f"K线数据不足，至少需要{atr_baseline_period}根K线，实际为 {len(klines) if klines else 0}")

        # 转换为DataFrame
        df = pd.DataFrame(klines)

        # 计算ATR序列
        atr_series = TechnicalIndicators.calculate_atr(df, period=atr_period)

        # 计算基准周期均值
        baseline_atr = Decimal(str(atr_series.tail(atr_baseline_period).mean()))

        logger.debug(
            "基准ATR计算完成",
            baseline_atr=float(baseline_atr),
            data_points=len(atr_series.tail(atr_baseline_period)),
            atr_period=atr_period,
            atr_baseline_period=atr_baseline_period
        )

        return baseline_atr

    def _resolve_min_quantity(self, min_quantity: Optional[Decimal]) -> Decimal:
        """解析每格最小张数 q_min：None 时读 trading.min_quantity（FR-06，禁止硬编码）"""
        if min_quantity is None:
            min_quantity = self.config.get('trading', {}).get('min_quantity', 1)
        return Decimal(str(min_quantity))

    @staticmethod
    def _ceil_to_int(value: Decimal) -> int:
        """Decimal 向上取整为 int（保证所需保证金/杠杆可达，FR-04）"""
        return int(value.to_integral_value(rounding=ROUND_CEILING))

    def max_grids_by_capital(
        self,
        price: Decimal,
        leverage: int,
        margin: Decimal,
        min_quantity: Optional[Decimal] = None
    ) -> int:
        """
        资本约束上限 C3：N_cap = floor(M×L/(P×q_min))

        输入非法（price/leverage/margin/q_min <= 0）时返回 0，不抛异常。

        Args:
            price: 当前价格 P
            leverage: 杠杆 L
            margin: 保证金 M
            min_quantity: 每格最小张数 q_min（None 时读 trading.min_quantity）

        Returns:
            当前保证金最多可支撑的网格数
        """
        q_min = self._resolve_min_quantity(min_quantity)
        if price <= 0 or leverage <= 0 or margin <= 0 or q_min <= 0:
            return 0
        n_cap = (margin * Decimal(str(leverage)) / (price * q_min)).to_integral_value(
            rounding=ROUND_FLOOR
        )
        return int(n_cap)

    def resolve_capital_feasibility(
        self,
        price: Decimal,
        leverage: int,
        margin: Decimal,
        n_struct: int,
        n_profit_max: int,
        n_min: int,
        min_quantity: Optional[Decimal] = None,
        enforce_capital: bool = True
    ) -> CapitalFeasibility:
        """
        解析约束链 C1/C2/C3 下的最终网格数与资本可行性（V2.5.5 方案B）

        N_final = min(N_struct, N_profit, N_cap)，其中
        N_cap = floor(M×L/(P×q_min))。可行要求 N_final >= N_min。
        绑定约束并列时按 STRUCT > PROFIT > CAPITAL 取归属（A-review tie-break）。

        不可行时反推可达出口：
        - M_required = ceil(N_min×P×q_min/L)（保持杠杆所需保证金）
        - L_required = ceil(N_min×P×q_min/M)（保持保证金所需杠杆）

        Args:
            price: 当前价格 P
            leverage: 杠杆 L
            margin: 本次可行性口径使用的保证金 M（FR-01：建议保证金）
            n_struct: 结构约束上限（ATR 比值，已夹状态上下限）
            n_profit_max: 利润率约束上限（精确口径，见 validate_profit_rate）
            n_min: 市场状态对应的最小网格数（震荡 5 / 弱趋势 4）
            min_quantity: 每格最小张数 q_min（None 时读 trading.min_quantity）
            enforce_capital: 是否启用 C3 资本下压；False 时仅供灰度整体回退
                             （capital_constraint.enabled=false）复用同一条计算链

        Returns:
            CapitalFeasibility；输入非法时返回 feasible=False/binding=INFEASIBLE，
            不抛异常（4.6 边界，避免中断推送）
        """
        q_min = self._resolve_min_quantity(min_quantity)
        reference_count = max(int(n_struct), 0)

        # 非法输入防御：记录告警并返回不可行，不中断推送
        if price <= 0 or leverage <= 0 or margin <= 0 or q_min <= 0 or n_min <= 0:
            logger.warning(
                "资本可行性输入非法，判定不可行",
                price=float(price),
                leverage=leverage,
                margin=float(margin),
                min_quantity=float(q_min),
                n_min=n_min
            )
            return CapitalFeasibility(
                feasible=False,
                grid_count=reference_count,
                qty_per_grid=Decimal('0'),
                binding=BINDING_INFEASIBLE
            )

        # 约束候选（顺序即并列时的 tie-break 优先级：STRUCT > PROFIT > CAPITAL）
        candidates = [(BINDING_STRUCT, int(n_struct)), (BINDING_PROFIT, int(n_profit_max))]
        if enforce_capital:
            candidates.append((BINDING_CAPITAL, self.max_grids_by_capital(
                price=price, leverage=leverage, margin=margin, min_quantity=q_min
            )))
        binding, n_final = min(candidates, key=lambda item: item[1])

        if n_final >= n_min:
            qty_per_grid = margin * Decimal(str(leverage)) / (
                price * Decimal(str(n_final))
            )
            return CapitalFeasibility(
                feasible=True,
                grid_count=n_final,
                qty_per_grid=qty_per_grid,
                binding=binding
            )

        # 不可行：按 N_min 反推可达的保证金/杠杆出口（ceil 保证可达）
        qty_at_min = margin * Decimal(str(leverage)) / (price * Decimal(str(n_min)))
        required_margin = Decimal(str(self._ceil_to_int(
            Decimal(str(n_min)) * price * q_min / Decimal(str(leverage))
        )))
        required_leverage = self._ceil_to_int(
            Decimal(str(n_min)) * price * q_min / margin
        )
        logger.warning(
            "资本可行性判定不可行",
            margin=float(margin),
            leverage=leverage,
            n_struct=n_struct,
            n_profit_max=n_profit_max,
            n_min=n_min,
            binding=binding,
            required_margin=float(required_margin),
            required_leverage=required_leverage
        )
        return CapitalFeasibility(
            feasible=False,
            grid_count=reference_count,
            qty_per_grid=qty_at_min,
            binding=binding,
            required_margin=required_margin,
            required_leverage=required_leverage
        )

