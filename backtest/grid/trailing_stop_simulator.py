"""
移动止盈（追踪止损）模拟器

用于回测场景中模拟移动止盈逻辑：
- 当总盈亏比率达到预设阈值时启动追踪
- 追踪历史最高价，动态调整止盈价
- 价格回撤触发止盈信号

设计原则：
- 所有金额计算使用 Decimal，确保精度
- 状态机模式管理追踪生命周期
- 纯模拟，不直接操作仓位或订单
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import structlog

logger = structlog.get_logger()


@dataclass
class TrailingStopState:
    """移动止盈状态数据类

    记录追踪止盈的当前状态，包括是否激活、峰值价格、止盈价格和激活时间。
    """

    activated: bool = False
    """是否已启动追踪止盈"""

    peak_price: Decimal = Decimal("0")
    """追踪期间的历史最高价"""

    stop_price: Decimal = Decimal("0")
    """当前追踪止盈触发价格"""

    activation_time: Optional[str] = None
    """追踪止盈激活时的时间戳（字符串格式）"""


class TrailingStopSimulator:
    """移动止盈模拟器

    模拟回测中的移动止盈逻辑，遵循三步状态机：
    1. 启动：总盈亏比率达到 profit_trigger 时激活追踪
    2. 更新：当前价格超过历史峰值时，更新峰值和止盈价
    3. 触发：当前价格跌破止盈价时，发出止盈信号并重置

    配置参数说明：
    - profit_trigger: 触发追踪止盈的最低盈亏比率（默认 0.15，即 15%）
    - trailing_percent: 止盈回撤比例，止盈价 = 峰值 × (1 - trailing_percent)（默认 0.05，即 5%）
    """

    def __init__(self, config: dict):
        """初始化移动止盈模拟器

        Args:
            config: 配置字典，需包含以下键：
                - trailing_stop.profit_trigger（可选，默认 0.15）
                - trailing_stop.trailing_percent（可选，默认 0.05）
        """
        trailing_config = config.get("trailing_stop", {})

        self.profit_trigger: Decimal = Decimal(
            str(trailing_config.get("profit_trigger", 0.15))
        )
        self.trailing_percent: Decimal = Decimal(
            str(trailing_config.get("trailing_percent", 0.05))
        )

        # 内部状态
        self._state: TrailingStopState = TrailingStopState()

        # 统计计数
        self.trigger_count: int = 0
        """触发止盈的总次数（累计，reset() 不会清零）"""

        logger.info(
            "移动止盈模拟器初始化完成",
            profit_trigger=str(self.profit_trigger),
            trailing_percent=str(self.trailing_percent),
        )

    # ---- 公开方法 ----

    def reset(self) -> None:
        """重置追踪止盈状态

        将状态恢复为初始未激活状态，不会影响 trigger_count 累计计数。
        通常在每轮独立回测开始时调用。
        """
        self._state = TrailingStopState()
        logger.debug("移动止盈状态已重置")

    def check(
        self,
        current_price: Decimal,
        total_pnl_ratio: Decimal,
        current_time: str,
    ) -> str:
        """检查并更新移动止盈状态

        根据当前价格和盈亏比率，执行状态机流转，返回事件类型。

        Args:
            current_price: 当前市场价格（Decimal）
            total_pnl_ratio: 总盈亏比率，公式为 (已实现盈亏 + 未实现盈亏) / 初始资金（Decimal）
            current_time: 当前时间戳字符串，用于记录激活时间

        Returns:
            事件类型字符串，取值为以下之一：
            - 'none': 无状态变化或未激活
            - 'started': 追踪止盈已启动
            - 'updated': 峰值和止盈价已更新
            - 'triggered': 止盈条件触发，状态已自动重置
        """
        # ---- Step 1：检查是否满足启动条件 ----
        if not self._state.activated:
            if total_pnl_ratio >= self.profit_trigger:
                self._state.activated = True
                self._state.peak_price = current_price
                self._state.stop_price = self._calc_stop_price(current_price)
                self._state.activation_time = current_time

                logger.info(
                    "追踪止盈已启动",
                    current_price=str(current_price),
                    peak_price=str(self._state.peak_price),
                    stop_price=str(self._state.stop_price),
                    total_pnl_ratio=str(total_pnl_ratio),
                    time=current_time,
                )
                return "started"
            return "none"

        # ---- Step 2：已激活，检查是否需要更新峰值 ----
        if current_price > self._state.peak_price:
            self._state.peak_price = current_price
            self._state.stop_price = self._calc_stop_price(current_price)

            logger.debug(
                "追踪止盈峰值已更新",
                new_peak=str(self._state.peak_price),
                new_stop=str(self._state.stop_price),
                time=current_time,
            )
            return "updated"

        # ---- Step 3：已激活，检查是否触发止盈 ----
        if current_price <= self._state.stop_price:
            self.trigger_count += 1

            logger.info(
                "追踪止盈已触发",
                current_price=str(current_price),
                stop_price=str(self._state.stop_price),
                peak_price=str(self._state.peak_price),
                trigger_count=self.trigger_count,
                time=current_time,
            )

            # 触发后自动重置状态
            self._state = TrailingStopState()
            return "triggered"

        return "none"

    # ---- 私有方法 ----

    def _calc_stop_price(self, peak_price: Decimal) -> Decimal:
        """计算追踪止盈价

        公式：stop_price = peak_price × (1 - trailing_percent)

        Args:
            peak_price: 当前峰值价格

        Returns:
            计算得到的止盈触发价格
        """
        return peak_price * (Decimal("1") - self.trailing_percent)

    # ---- 只读属性 ----

    @property
    def state(self) -> TrailingStopState:
        """获取当前追踪止盈状态（只读）"""
        return self._state