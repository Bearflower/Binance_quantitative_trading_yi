"""
风险管理器
负责硬止损、移动止盈、紧急暂停等风险控制
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AccountInfo:
    """账户信息"""
    total_balance: float  # 总余额
    available_balance: float  # 可用余额
    total_unrealized_pnl: float  # 总未实现盈亏
    total_margin_balance: float  # 总保证金余额
    total_position_initial_margin: float  # 总持仓初始保证金
    total_open_order_initial_margin: float  # 总挂单初始保证金
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'total_balance': self.total_balance,
            'available_balance': self.available_balance,
            'total_unrealized_pnl': self.total_unrealized_pnl,
            'total_margin_balance': self.total_margin_balance,
            'total_position_initial_margin': self.total_position_initial_margin,
            'total_open_order_initial_margin': self.total_open_order_initial_margin
        }


@dataclass
class RiskStatus:
    """风险状态"""
    is_safe: bool  # 是否安全
    hard_stop_loss_triggered: bool  # 硬止损是否触发
    trailing_profit_triggered: bool  # 移动止盈是否触发
    emergency_pause_triggered: bool  # 紧急暂停是否触发
    current_pnl_percent: float  # 当前盈亏百分比
    message: str  # 状态描述
    timestamp: datetime  # 时间戳
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'is_safe': self.is_safe,
            'hard_stop_loss_triggered': self.hard_stop_loss_triggered,
            'trailing_profit_triggered': self.trailing_profit_triggered,
            'emergency_pause_triggered': self.emergency_pause_triggered,
            'current_pnl_percent': self.current_pnl_percent,
            'message': self.message,
            'timestamp': self.timestamp
        }


class RiskManager:
    """风险管理器"""
    
    def __init__(
        self,
        hard_stop_loss: float = -0.08,
        trailing_profit_start: float = 0.15,
        trailing_profit_retrace: float = 0.5,
        emergency_break_layers: int = 3,
        emergency_break_window: int = 300
    ):
        """
        初始化风险管理器
        
        Args:
            hard_stop_loss: 硬止损阈值（-8%）
            trailing_profit_start: 移动止盈启动阈值（15%）
            trailing_profit_retrace: 移动止盈回撤比例（50%）
            emergency_break_layers: 紧急暂停触发层数
            emergency_break_window: 紧急暂停时间窗口（秒）
        """
        self.hard_stop_loss = hard_stop_loss
        self.trailing_profit_start = trailing_profit_start
        self.trailing_profit_retrace = trailing_profit_retrace
        self.emergency_break_layers = emergency_break_layers
        self.emergency_break_window = emergency_break_window
        
        # 移动止盈状态
        self._peak_price: Optional[float] = None
        self._peak_pnl_percent: Optional[float] = None
        self._trailing_profit_active: bool = False
        self._current_stop_price: Optional[float] = None
        
        # 紧急暂停状态
        self._breakthrough_times: List[datetime] = []
        self._emergency_paused: bool = False
    
    def check_risk(
        self,
        current_pnl_percent: float,
        current_price: float,
        grid_params: Dict
    ) -> RiskStatus:
        """
        检查风险状态
        
        Args:
            current_pnl_percent: 当前盈亏百分比
            current_price: 当前价格
            grid_params: 网格参数
            
        Returns:
            风险状态
        """
        messages = []
        is_safe = True
        
        # 1. 检查硬止损
        hard_stop_triggered = self._check_hard_stop_loss(current_pnl_percent)
        if hard_stop_triggered:
            is_safe = False
            messages.append("触发硬止损！")
        
        # 2. 检查移动止盈
        trailing_triggered = self._check_trailing_profit(
            current_pnl_percent, current_price, grid_params
        )
        if trailing_triggered:
            messages.append("触发移动止盈！")
        
        # 3. 检查紧急暂停
        emergency_triggered = self._check_emergency_pause()
        if emergency_triggered:
            is_safe = False
            messages.append("触发紧急暂停！")
        
        # 创建状态
        status = RiskStatus(
            is_safe=is_safe,
            hard_stop_loss_triggered=hard_stop_triggered,
            trailing_profit_triggered=trailing_triggered,
            emergency_pause_triggered=emergency_triggered,
            current_pnl_percent=current_pnl_percent,
            message="; ".join(messages) if messages else "风险状态正常",
            timestamp=datetime.now()
        )
        
        if not is_safe:
            logger.warning(f"风险警告：{status.message}")
        
        return status
    
    def _check_hard_stop_loss(self, current_pnl_percent: float) -> bool:
        """
        检查硬止损
        
        Args:
            current_pnl_percent: 当前盈亏百分比
            
        Returns:
            是否触发
        """
        if current_pnl_percent <= self.hard_stop_loss:
            logger.error(
                f"硬止损触发！当前盈亏：{current_pnl_percent*100:.2f}%, "
                f"阈值：{self.hard_stop_loss*100:.2f}%"
            )
            return True
        return False
    
    def _check_trailing_profit(
        self,
        current_pnl_percent: float,
        current_price: float,
        grid_params: Dict
    ) -> bool:
        """
        检查移动止盈
        
        Args:
            current_pnl_percent: 当前盈亏百分比
            current_price: 当前价格
            grid_params: 网格参数
            
        Returns:
            是否触发
        """
        # 1. 检查是否启动移动止盈
        if current_pnl_percent >= self.trailing_profit_start and not self._trailing_profit_active:
            self._trailing_profit_active = True
            self._peak_price = current_price
            self._peak_pnl_percent = current_pnl_percent
            logger.info(f"启动移动止盈：盈亏={current_pnl_percent*100:.2f}%, 价格={current_price}")
        
        # 2. 如果已启动，更新最高价
        if self._trailing_profit_active:
            if current_price > (self._peak_price or 0):
                self._peak_price = current_price
                self._peak_pnl_percent = current_pnl_percent
                logger.debug(f"更新最高价：{self._peak_price}")
            
            # 3. 计算回撤
            if self._peak_price:
                retrace = (self._peak_price - current_price) / self._peak_price
                
                # 回撤超过阈值时触发
                if retrace >= self.trailing_profit_retrace:
                    logger.info(
                        f"移动止盈触发！最高价：{self._peak_price}, "
                        f"当前价：{current_price}, 回撤：{retrace*100:.2f}%"
                    )
                    return True
        
        return False
    
    def _check_emergency_pause(self) -> bool:
        """
        检查紧急暂停
        
        Returns:
            是否触发
        """
        return self._emergency_paused
    
    def record_breakthrough(self, layers: int = 1) -> None:
        """
        记录突破网格层数
        
        Args:
            layers: 突破层数
        """
        now = datetime.now()
        
        # 记录突破时间
        for _ in range(layers):
            self._breakthrough_times.append(now)
        
        # 清理旧记录（超出时间窗口）
        cutoff = datetime.now()
        window_seconds = self.emergency_break_window
        
        self._breakthrough_times = [
            t for t in self._breakthrough_times
            if (cutoff - t).total_seconds() < window_seconds
        ]
        
        # 检查是否触发紧急暂停
        if len(self._breakthrough_times) >= self.emergency_break_layers:
            self._emergency_paused = True
            logger.error(
                f"紧急暂停触发！{window_seconds}秒内突破{len(self._breakthrough_times)}层"
            )
    
    def update_trailing_stop_price(self, new_stop_price: float) -> None:
        """
        更新移动止盈价格
        
        Args:
            new_stop_price: 新的止盈价格
        """
        self._current_stop_price = new_stop_price
        logger.debug(f"更新移动止盈价格：{new_stop_price}")
    
    def get_trailing_profit_info(self) -> Dict:
        """
        获取移动止盈信息
        
        Returns:
            移动止盈信息字典
        """
        return {
            'active': self._trailing_profit_active,
            'peak_price': self._peak_price,
            'peak_pnl_percent': self._peak_pnl_percent,
            'current_stop_price': self._current_stop_price
        }
    
    def reset(self) -> None:
        """重置所有状态"""
        self._peak_price = None
        self._peak_pnl_percent = None
        self._trailing_profit_active = False
        self._current_stop_price = None
        self._breakthrough_times.clear()
        self._emergency_paused = False
        logger.info("风险管理状态已重置")
    
    def is_safe_to_trade(self) -> bool:
        """
        判断是否可以交易
        
        Returns:
            是否安全
        """
        return not self._emergency_paused
