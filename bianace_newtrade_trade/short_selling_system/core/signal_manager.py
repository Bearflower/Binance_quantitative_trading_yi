"""
信号生成与管理模块

负责：
- 生成交易信号
- 管理信号状态
- 信号过期处理
- 信号查询
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from enum import Enum
from pathlib import Path
import json

from .scoring_engine_v41 import ScoringResultV41, scoring_engine_v41
from utils.logger import logger


class SignalStatus(Enum):
    """信号状态枚举"""
    PENDING = "pending"      # 待确认
    CONFIRMED = "confirmed"  # 已确认
    EXECUTED = "executed"    # 已执行
    CANCELLED = "cancelled"  # 已取消
    EXPIRED = "expired"      # 已过期


class Signal:
    """交易信号类"""
    
    def __init__(
        self,
        symbol: str,
        scoring_result: ScoringResultV41,
        current_price: float,
        entry_min: float,
        entry_max: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: float,
        expire_hours: int = 1
    ):
        """
        初始化交易信号
        
        Args:
            symbol: 币种符号
            scoring_result: 评分结果
            current_price: 当前价格
            entry_min: 建议入场最低价
            entry_max: 建议入场最高价
            stop_loss: 止损价
            take_profit_1: 第一止盈价 (20% 收益)
            take_profit_2: 第二止盈价 (30% 收益)
            expire_hours: 有效期 (小时，默认 1 小时)
        """
        self.id = str(uuid.uuid4())
        self.symbol = symbol
        self.scoring_result = scoring_result
        self.current_price = current_price
        self.entry_min = entry_min
        self.entry_max = entry_max
        self.stop_loss = stop_loss
        self.take_profit_1 = take_profit_1
        self.take_profit_2 = take_profit_2
        self.created_at = datetime.now()
        self.expire_at = self.created_at + timedelta(hours=expire_hours)
        self.status = SignalStatus.PENDING
        self.confirmed_at: Optional[datetime] = None
        self.executed_at: Optional[datetime] = None
        self.notes: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'id': self.id,
            'symbol': self.symbol,
            'scoring_result': self.scoring_result.to_dict(),
            'current_price': self.current_price,
            'entry_min': self.entry_min,
            'entry_max': self.entry_max,
            'stop_loss': self.stop_loss,
            'take_profit_1': self.take_profit_1,
            'take_profit_2': self.take_profit_2,
            'created_at': self.created_at.isoformat(),
            'expire_at': self.expire_at.isoformat(),
            'status': self.status.value,
            'confirmed_at': self.confirmed_at.isoformat() if self.confirmed_at else None,
            'executed_at': self.executed_at.isoformat() if self.executed_at else None,
            'notes': self.notes
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Signal':
        """从字典创建"""
        signal = cls(
            symbol=data['symbol'],
            scoring_result=ScoringResultV41.from_dict(data['scoring_result']),
            current_price=data['current_price'],
            entry_min=data['entry_min'],
            entry_max=data['entry_max'],
            stop_loss=data['stop_loss'],
            take_profit_1=data['take_profit_1'],
            take_profit_2=data['take_profit_2'],
        )
        signal.id = data['id']
        signal.created_at = datetime.fromisoformat(data['created_at'])
        signal.expire_at = datetime.fromisoformat(data['expire_at'])
        signal.status = SignalStatus(data['status'])
        signal.confirmed_at = (
            datetime.fromisoformat(data['confirmed_at'])
            if data.get('confirmed_at') else None
        )
        signal.executed_at = (
            datetime.fromisoformat(data['executed_at'])
            if data.get('executed_at') else None
        )
        signal.notes = data.get('notes', '')
        return signal
    
    def is_expired(self) -> bool:
        """检查是否过期"""
        return datetime.now() > self.expire_at
    
    def time_remaining(self) -> timedelta:
        """获取剩余时间"""
        remaining = self.expire_at - datetime.now()
        return max(remaining, timedelta(0))
    
    def confirm(self):
        """确认信号"""
        self.status = SignalStatus.CONFIRMED
        self.confirmed_at = datetime.now()
        logger.info(f"✅ 信号 {self.id[:8]} 已确认")
    
    def execute(self):
        """执行信号"""
        self.status = SignalStatus.EXECUTED
        self.executed_at = datetime.now()
        logger.info(f"✅ 信号 {self.id[:8]} 已执行")
    
    def cancel(self, reason: str = ""):
        """取消信号"""
        self.status = SignalStatus.CANCELLED
        self.notes = reason
        logger.info(f"❌ 信号 {self.id[:8]} 已取消：{reason}")
    
    def expire(self):
        """过期信号"""
        self.status = SignalStatus.EXPIRED
        logger.info(f"⏰ 信号 {self.id[:8]} 已过期")
    
    def __str__(self) -> str:
        """字符串表示"""
        return (
            f"Signal({self.symbol}, "
            f"评分={self.scoring_result.total_score:.2f}, "
            f"状态={self.status.value}, "
            f"剩余={self.time_remaining()})"
        )


class SignalManager:
    """信号管理器 v3.1（增加冷却机制）"""
    
    def __init__(self, state_file: str = "data/signals.json"):
        """
        初始化信号管理器
        
        Args:
            state_file: 信号状态文件
        """
        self.state_file = Path(state_file)
        self.signals: Dict[str, Signal] = {}
        
        # 冷却记录：symbol -> 最后开仓时间
        self.cooldown_records: Dict[str, datetime] = {}
        
        # 加载状态
        self.load_state()
        
        logger.info("✅ 信号管理器 v3.1 初始化完成（带冷却机制）")
    
    def load_state(self) -> bool:
        """加载信号状态"""
        if not self.state_file.exists():
            logger.info("📂 信号状态文件不存在，创建新文件")
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            return True
        
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self.signals = {
                signal_id: Signal.from_dict(signal_data)
                for signal_id, signal_data in data.items()
            }
            
            logger.info(f"📂 加载信号状态成功，共 {len(self.signals)} 个信号")
            return True
            
        except Exception as e:
            logger.error(f"❌ 加载信号状态异常：{e}")
            return False
    
    def save_state(self) -> bool:
        """保存信号状态"""
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(
                    {k: v.to_dict() for k, v in self.signals.items()},
                    f,
                    indent=2,
                    ensure_ascii=False
                )
            
            logger.info("💾 保存信号状态成功")
            return True
            
        except Exception as e:
            logger.error(f"❌ 保存信号状态异常：{e}")
            return False
    
    def generate_signal(
        self,
        symbol: str,
        scoring_result: ScoringResultV41,
        current_price: float,
        klines: Optional[List[Dict[str, Any]]] = None,
        stop_loss_percent: float = 0.05,
        take_profit_1_percent: float = 0.20,
        take_profit_2_percent: float = 0.30,
        entry_range_percent: float = 0.02,
        expire_hours: int = 1
    ) -> Optional[Signal]:
        """
        生成交易信号
        
        Args:
            symbol: 币种符号
            scoring_result: 评分结果
            current_price: 当前价格
            klines: K线数据（可选，用于 ATR 计算）
            stop_loss_percent: 止损比例 (默认 5%)
            take_profit_1_percent: 第一止盈比例 (默认 20%)
            take_profit_2_percent: 第二止盈比例 (默认 30%)
            entry_range_percent: 入场区间比例 (默认 2%)
            expire_hours: 有效期 (小时，默认 1 小时)
            
        Returns:
            交易信号对象，不符合条件返回 None
        """
        # 检查是否应该开仓（V4.1 标准：总分 >= 6.5）
        if scoring_result.total_score < 6.5:
            logger.info(f"ℹ️ {symbol} 不符合开仓条件，不生成信号")
            return None
        
        # V4.1.1: 如果启用 ATR 且有 K 线数据，使用 ATR 计算止损止盈
        stop_loss = current_price * (1 + stop_loss_percent)
        take_profit_1 = current_price * (1 - take_profit_1_percent)
        take_profit_2 = current_price * (1 - take_profit_2_percent)
        
        if klines and len(klines) > 15:
            try:
                # 计算 ATR
                atr_period = 14
                true_ranges = []
                for i in range(1, len(klines)):
                    high = float(klines[i].get('high', 0))
                    low = float(klines[i].get('low', 0))
                    prev_close = float(klines[i-1].get('close', 0))
                    tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                    true_ranges.append(tr)
                
                if len(true_ranges) >= atr_period:
                    atr = sum(true_ranges[-atr_period:]) / atr_period
                    
                    if atr > 0:
                        from config.settings import settings
                        if settings.use_atr_sl_tp:
                            stop_loss = current_price + atr * settings.stop_loss_atr_multiplier
                            take_profit_1 = current_price - atr * settings.take_profit_atr_multiplier
                            take_profit_2 = take_profit_1  # V4.1.1 使用单一止盈位
                            
                            logger.info(
                                f"📊 V4.1.1 ATR 止损止盈：ATR={atr:.4f}, "
                                f"止损={stop_loss:.4f}, 止盈={take_profit_1:.4f}"
                            )
            except Exception as e:
                logger.warning(f"ATR 计算失败，使用传统百分比：{e}")
        
        # 计算关键价位
        entry_min = current_price * (1 - entry_range_percent / 2)
        entry_max = current_price * (1 + entry_range_percent / 2)
        
        # 创建信号
        signal = Signal(
            symbol=symbol,
            scoring_result=scoring_result,
            current_price=current_price,
            entry_min=entry_min,
            entry_max=entry_max,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            expire_hours=expire_hours
        )
        
        # 保存信号
        self.signals[signal.id] = signal
        self.save_state()
        
        logger.info(
            f"🎯 生成信号：{signal.id[:8]}, "
            f"币种={symbol}, "
            f"评分={scoring_result.total_score:.2f}, "
            f"价格={current_price:.2f}, "
            f"有效期={expire_hours}小时"
        )
        
        return signal
    
    def get_signal(self, signal_id: str) -> Optional[Signal]:
        """获取信号"""
        signal = self.signals.get(signal_id)
        
        if signal:
            # 检查是否过期
            if signal.is_expired() and signal.status == SignalStatus.PENDING:
                signal.expire()
                self.save_state()
        
        return signal
    
    def get_pending_signals(self) -> List[Signal]:
        """获取所有待确认信号"""
        pending = []
        for signal in self.signals.values():
            if signal.status == SignalStatus.PENDING:
                # 检查是否过期
                if signal.is_expired():
                    signal.expire()
                else:
                    pending.append(signal)
        return pending
    
    def get_signal_by_symbol(self, symbol: str) -> Optional[Signal]:
        """根据币种符号获取最新的待确认信号"""
        for signal in reversed(self.signals.values()):
            if signal.symbol == symbol and signal.status == SignalStatus.PENDING:
                if not signal.is_expired():
                    return signal
        return None
    
    def confirm_signal(self, signal_id: str) -> bool:
        """确认信号"""
        signal = self.get_signal(signal_id)
        
        if not signal:
            logger.error(f"❌ 信号不存在：{signal_id}")
            return False
        
        if signal.status != SignalStatus.PENDING:
            logger.error(f"❌ 信号状态不正确：{signal.status.value}")
            return False
        
        signal.confirm()
        self.save_state()
        return True
    
    def cancel_signal(self, signal_id: str, reason: str = "") -> bool:
        """取消信号"""
        signal = self.get_signal(signal_id)
        
        if not signal:
            logger.error(f"❌ 信号不存在：{signal_id}")
            return False
        
        signal.cancel(reason)
        self.save_state()
        return True
    
    def execute_signal(self, signal_id: str) -> bool:
        """执行信号"""
        signal = self.get_signal(signal_id)
        
        if not signal:
            logger.error(f"❌ 信号不存在：{signal_id}")
            return False
        
        if signal.status != SignalStatus.CONFIRMED:
            logger.error(f"❌ 信号未确认：{signal.status.value}")
            return False
        
        signal.execute()
        self.save_state()
        return True
    
    def cleanup_expired(self) -> int:
        """清理过期信号"""
        count = 0
        for signal in list(self.signals.values()):
            if signal.is_expired() and signal.status == SignalStatus.PENDING:
                signal.expire()
                count += 1
        
        if count > 0:
            self.save_state()
            logger.info(f"🧹 清理 {count} 个过期信号")
        
        return count
    
    def is_in_cooldown(self, symbol: str, cooldown_hours: int = 2) -> bool:
        """
        检查币种是否在冷却期内
        
        Args:
            symbol: 币种符号
            cooldown_hours: 冷却时间（小时，默认 2 小时）
            
        Returns:
            是否在冷却期内
        """
        if symbol not in self.cooldown_records:
            return False
        
        last_trade_time = self.cooldown_records[symbol]
        hours_since_trade = (datetime.now() - last_trade_time).total_seconds() / 3600
        
        if hours_since_trade < cooldown_hours:
            remaining = cooldown_hours - hours_since_trade
            logger.debug(f"⏰ {symbol} 仍在冷却期内，剩余 {remaining:.1f} 小时")
            return True
        
        logger.debug(f"✅ {symbol} 已出冷却期")
        return False
    
    def mark_as_traded(self, symbol: str):
        """
        标记币种为已交易（开始冷却）
        
        Args:
            symbol: 币种符号
        """
        self.cooldown_records[symbol] = datetime.now()
        logger.info(f"🔒 {symbol} 已标记为已交易，开始 {2} 小时冷却")
    
    def can_generate_signal(self, symbol: str, listing_hours: float) -> Tuple[bool, str]:
        """
        判断是否可以生成信号（v3.1 新规则）
        
        Args:
            symbol: 币种符号
            listing_hours: 上线时间（小时）
            
        Returns:
            (是否允许，原因)
        """
        # 检查时间窗口：新币上线 48 小时内
        if listing_hours > 48:
            return False, f"上线时间过长 ({listing_hours:.1f}小时 > 48 小时)"
        
        # 检查冷却期
        if self.is_in_cooldown(symbol, cooldown_hours=2):
            return False, "信号冷却期内（2 小时）"
        
        return True, "符合开仓条件"


# 全局信号管理器实例 v3.1
signal_manager = SignalManager()
