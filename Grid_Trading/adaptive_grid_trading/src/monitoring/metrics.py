"""
性能监控模块
收集系统性能指标、策略盈亏统计等
"""

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PerformanceMetrics:
    """性能指标"""
    # 系统性能
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    network_latency: float = 0.0
    
    # 业务指标
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    average_profit: float = 0.0
    average_loss: float = 0.0
    max_drawdown: float = 0.0
    
    # API 调用
    api_calls: int = 0
    api_errors: int = 0
    api_success_rate: float = 100.0


class MetricsCollector:
    """性能指标收集器"""
    
    def __init__(self, max_history: int = 1000):
        """
        初始化指标收集器
        
        Args:
            max_history: 最大历史记录数
        """
        self.max_history = max_history
        
        # 指标历史
        self._metrics_history: Deque[PerformanceMetrics] = deque(maxlen=max_history)
        
        # 交易记录
        self._trades: Deque[Dict] = deque(maxlen=max_history)
        
        # API 调用统计
        self._api_calls = 0
        self._api_errors = 0
        
        # 盈亏跟踪
        self._total_pnl = 0.0
        self._peak_pnl = 0.0
        self._max_drawdown = 0.0
        
        # 启动时间
        self._start_time = datetime.now()
        
        # 计时器
        self._timers: Dict[str, float] = {}
    
    def record_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: float,
        pnl: float = 0.0
    ) -> None:
        """
        记录交易
        
        Args:
            symbol: 交易对
            side: 方向
            price: 价格
            quantity: 数量
            pnl: 盈亏
        """
        trade = {
            'timestamp': datetime.now(),
            'symbol': symbol,
            'side': side,
            'price': price,
            'quantity': quantity,
            'pnl': pnl
        }
        
        self._trades.append(trade)
        
        if pnl != 0:
            self._total_pnl += pnl
            
            # 更新峰值盈亏
            if self._total_pnl > self._peak_pnl:
                self._peak_pnl = self._total_pnl
            
            # 更新最大回撤
            drawdown = (self._peak_pnl - self._total_pnl)
            if drawdown > self._max_drawdown:
                self._max_drawdown = drawdown
        
        logger.debug(f"记录交易：{symbol} {side} @ {price}, pnl={pnl}")
    
    def record_api_call(self, success: bool = True) -> None:
        """
        记录 API 调用
        
        Args:
            success: 是否成功
        """
        self._api_calls += 1
        
        if not success:
            self._api_errors += 1
        
        logger.debug(f"API 调用：success={success}, total={self._api_calls}, errors={self._api_errors}")
    
    def start_timer(self, timer_name: str) -> None:
        """
        开始计时
        
        Args:
            timer_name: 计时器名称
        """
        self._timers[timer_name] = time.time()
    
    def stop_timer(self, timer_name: str) -> float:
        """
        停止计时
        
        Args:
            timer_name: 计时器名称
            
        Returns:
            耗时（秒）
        """
        if timer_name not in self._timers:
            return 0.0
        
        elapsed = time.time() - self._timers[timer_name]
        del self._timers[timer_name]
        
        logger.debug(f"计时器 {timer_name}: {elapsed:.3f}秒")
        
        return elapsed
    
    def get_current_metrics(self) -> PerformanceMetrics:
        """
        获取当前性能指标
        
        Returns:
            性能指标
        """
        metrics = PerformanceMetrics()
        
        # 计算交易统计
        metrics.total_trades = len([t for t in self._trades if t['pnl'] != 0])
        metrics.winning_trades = len([t for t in self._trades if t['pnl'] > 0])
        metrics.losing_trades = len([t for t in self._trades if t['pnl'] < 0])
        
        if metrics.total_trades > 0:
            metrics.win_rate = metrics.winning_trades / metrics.total_trades
            
            profits = [t['pnl'] for t in self._trades if t['pnl'] > 0]
            losses = [t['pnl'] for t in self._trades if t['pnl'] < 0]
            
            metrics.average_profit = sum(profits) / len(profits) if profits else 0
            metrics.average_loss = sum(losses) / len(losses) if losses else 0
        
        metrics.total_pnl = self._total_pnl
        metrics.max_drawdown = self._max_drawdown
        
        # API 统计
        metrics.api_calls = self._api_calls
        metrics.api_errors = self._api_errors
        
        if self._api_calls > 0:
            metrics.api_success_rate = (
                (self._api_calls - self._api_errors) / self._api_calls * 100
            )
        
        return metrics
    
    def get_trade_statistics(self, days: int = 7) -> Dict:
        """
        获取交易统计
        
        Args:
            days: 天数
            
        Returns:
            统计字典
        """
        cutoff = datetime.now() - timedelta(days=days)
        recent_trades = [t for t in self._trades if t['timestamp'] >= cutoff]
        
        if not recent_trades:
            return {'total_trades': 0}
        
        total_pnl = sum(t['pnl'] for t in recent_trades)
        winning_trades = [t for t in recent_trades if t['pnl'] > 0]
        losing_trades = [t for t in recent_trades if t['pnl'] < 0]
        
        return {
            'total_trades': len(recent_trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': len(winning_trades) / len(recent_trades) if recent_trades else 0,
            'total_pnl': total_pnl,
            'average_pnl': total_pnl / len(recent_trades) if recent_trades else 0,
            'average_profit': sum(t['pnl'] for t in winning_trades) / len(winning_trades) if winning_trades else 0,
            'average_loss': sum(t['pnl'] for t in losing_trades) / len(losing_trades) if losing_trades else 0,
            'largest_profit': max((t['pnl'] for t in recent_trades), default=0),
            'largest_loss': min((t['pnl'] for t in recent_trades), default=0)
        }
    
    def get_uptime(self) -> timedelta:
        """
        获取运行时长
        
        Returns:
            运行时长
        """
        return datetime.now() - self._start_time
    
    def reset(self) -> None:
        """重置所有统计"""
        self._trades.clear()
        self._api_calls = 0
        self._api_errors = 0
        self._total_pnl = 0.0
        self._peak_pnl = 0.0
        self._max_drawdown = 0.0
        self._start_time = datetime.now()
        
        logger.info("性能指标已重置")
