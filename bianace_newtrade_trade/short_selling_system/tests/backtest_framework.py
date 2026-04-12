"""
历史回测框架

功能：
1. 从币安获取历史 K 线数据
2. 模拟历史时间点的 OI、资金费率等数据
3. 运行评分系统生成历史信号
4. 模拟交易执行
5. 统计回测结果（胜率、盈亏比、最大回撤等）
"""

import sys
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from dataclasses import dataclass, field
import statistics

from utils.logger import logger


@dataclass
class BacktestTrade:
    """回测交易记录"""
    symbol: str
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime]
    exit_price: Optional[float]
    quantity: float
    side: str = "SHORT"
    pnl: float = 0.0
    pnl_percent: float = 0.0
    exit_reason: str = ""  # 止盈/止损/时间停止
    score_at_entry: float = 0.0


@dataclass
class BacktestResult:
    """回测结果统计"""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_holding_time: float = 0.0  # 平均持仓时间（小时）
    trades: List[BacktestTrade] = field(default_factory=list)


class BacktestEngine:
    """回测引擎"""
    
    def __init__(self, initial_capital: float = 1000.0):
        """
        初始化回测引擎
        
        Args:
            initial_capital: 初始资金（USDT）
        """
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[float] = [initial_capital]
        
        logger.info(f"回测引擎初始化完成，初始资金：{initial_capital} USDT")
    
    def simulate_trade(
        self,
        symbol: str,
        entry_time: datetime,
        entry_price: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: float,
        quantity: float,
        leverage: int = 5,
        score: float = 0.0,
        price_data: List[Dict] = None
    ) -> Optional[BacktestTrade]:
        """
        模拟一笔交易
        
        Args:
            symbol: 交易对
            entry_time: 开仓时间
            entry_price: 开仓价格
            stop_loss: 止损价
            take_profit_1: 第一止盈价
            take_profit_2: 第二止盈价
            quantity: 仓位数量
            leverage: 杠杆倍数
            score: 开仓时评分
            price_data: 后续价格数据（用于判断止盈止损）
        
        Returns:
            BacktestTrade: 交易记录
        """
        if price_data is None or len(price_data) == 0:
            logger.warning(f"{symbol} 无价格数据，跳过回测")
            return None
        
        # 模拟持仓过程
        position_size = quantity  # 总仓位
        entry_value = entry_price * quantity
        realized_pnl = 0.0
        
        exit_time = None
        exit_price = None
        exit_reason = ""
        
        # 检查 24 小时时间停止
        max_hold_time = entry_time + timedelta(hours=24)
        
        for i, candle in enumerate(price_data):
            candle_time = candle.get('time', entry_time)
            high = candle.get('high', entry_price)
            low = candle.get('low', entry_price)
            
            # 检查是否超过 24 小时
            if candle_time >= max_hold_time:
                exit_time = candle_time
                exit_price = low if low < high else high  # 使用收盘价近似
                exit_reason = "时间停止"
                break
            
            # 对于做空：价格上涨触发止损，价格下跌触发止盈
            # 检查止损（价格上破）
            if high >= stop_loss:
                exit_time = candle_time
                exit_price = stop_loss
                exit_reason = "止损"
                break
            
            # 检查第一止盈（价格跌破 TP1）
            if position_size > quantity * 0.5 and low <= take_profit_1:
                # 平仓 50%
                close_qty = quantity * 0.5
                pnl = (entry_price - take_profit_1) * close_qty
                realized_pnl += pnl
                position_size -= close_qty
                
                # 剩余仓位设置保本损
                stop_loss = entry_price  # 移动止损到开仓价
            
            # 检查第二止盈（价格跌破 TP2）
            if position_size > 0 and low <= take_profit_2:
                exit_time = candle_time
                exit_price = take_profit_2
                pnl = (entry_price - take_profit_2) * position_size
                realized_pnl += pnl
                position_size = 0
                exit_reason = "止盈"
                break
        
        # 如果还有剩余仓位，强制平仓（数据结束）
        if position_size > 0 and len(price_data) > 0:
            last_candle = price_data[-1]
            exit_time = last_candle.get('time', entry_time)
            exit_price = last_candle.get('close', entry_price)
            pnl = (entry_price - exit_price) * position_size
            realized_pnl += pnl
            exit_reason = "数据结束"
        
        # 计算盈亏百分比
        pnl_percent = (realized_pnl / entry_value) * 100 if entry_value > 0 else 0
        
        trade = BacktestTrade(
            symbol=symbol,
            entry_time=entry_time,
            entry_price=entry_price,
            exit_time=exit_time,
            exit_price=exit_price,
            quantity=quantity,
            side="SHORT",
            pnl=realized_pnl,
            pnl_percent=pnl_percent,
            exit_reason=exit_reason,
            score_at_entry=score
        )
        
        self.trades.append(trade)
        self.capital += realized_pnl
        self.equity_curve.append(self.capital)
        
        logger.debug(f"回测交易：{symbol} {exit_reason} PnL={realized_pnl:.2f} USDT")
        
        return trade
    
    def calculate_statistics(self) -> BacktestResult:
        """
        计算回测统计指标
        
        Returns:
            BacktestResult: 回测结果
        """
        if not self.trades:
            logger.warning("无交易记录，无法计算统计")
            return BacktestResult()
        
        result = BacktestResult()
        result.trades = self.trades
        result.total_trades = len(self.trades)
        
        # 分离盈利和亏损交易
        winning = [t for t in self.trades if t.pnl > 0]
        losing = [t for t in self.trades if t.pnl <= 0]
        
        result.winning_trades = len(winning)
        result.losing_trades = len(losing)
        
        # 胜率
        result.win_rate = (result.winning_trades / result.total_trades * 100) if result.total_trades > 0 else 0
        
        # 总盈亏
        result.total_pnl = sum(t.pnl for t in self.trades)
        result.avg_pnl = result.total_pnl / result.total_trades if result.total_trades > 0 else 0
        
        # 平均盈利和亏损
        result.avg_win = statistics.mean([t.pnl for t in winning]) if winning else 0
        result.avg_loss = abs(statistics.mean([t.pnl for t in losing])) if losing else 0
        
        # 盈亏比
        if result.avg_loss > 0:
            result.profit_factor = result.avg_win / result.avg_loss
        else:
            result.profit_factor = float('inf') if result.avg_win > 0 else 0
        
        # 最大回撤
        peak = self.initial_capital
        max_dd = 0
        for equity in self.equity_curve:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        result.max_drawdown = max_dd
        
        # 最大连续盈利/亏损
        max_consec_win = 0
        max_consec_loss = 0
        current_consec_win = 0
        current_consec_loss = 0
        
        for trade in self.trades:
            if trade.pnl > 0:
                current_consec_win += 1
                current_consec_loss = 0
                max_consec_win = max(max_consec_win, current_consec_win)
            else:
                current_consec_loss += 1
                current_consec_win = 0
                max_consec_loss = max(max_consec_loss, current_consec_loss)
        
        result.max_consecutive_wins = max_consec_win
        result.max_consecutive_losses = max_consec_loss
        
        # 平均持仓时间
        holding_times = []
        for trade in self.trades:
            if trade.exit_time and trade.entry_time:
                delta = trade.exit_time - trade.entry_time
                holding_times.append(delta.total_seconds() / 3600)  # 转换为小时
        
        result.avg_holding_time = statistics.mean(holding_times) if holding_times else 0
        
        return result
    
    def print_report(self, result: BacktestResult):
        """打印回测报告"""
        print("\n" + "=" * 80)
        print("  📊 回测报告")
        print("=" * 80)
        
        print(f"\n【基本统计】")
        print(f"  总交易次数：{result.total_trades}")
        print(f"  盈利交易：{result.winning_trades} ({result.win_rate:.1f}%)")
        print(f"  亏损交易：{result.losing_trades}")
        print(f"  总盈亏：{result.total_pnl:.2f} USDT")
        print(f"  平均盈亏：{result.avg_pnl:.2f} USDT")
        
        print(f"\n【盈亏分析】")
        print(f"  平均盈利：{result.avg_win:.2f} USDT")
        print(f"  平均亏损：{result.avg_loss:.2f} USDT")
        print(f"  盈亏比：{result.profit_factor:.2f}")
        
        print(f"\n【风险分析】")
        print(f"  最大回撤：{result.max_drawdown:.2f}%")
        print(f"  最大连续盈利：{result.max_consecutive_wins} 笔")
        print(f"  最大连续亏损：{result.max_consecutive_losses} 笔")
        
        print(f"\n【持仓时间】")
        print(f"  平均持仓：{result.avg_holding_time:.2f} 小时")
        
        print(f"\n【资金曲线】")
        print(f"  初始资金：{self.initial_capital:.2f} USDT")
        print(f"  最终资金：{self.capital:.2f} USDT")
        print(f"  总收益率：{(self.capital - self.initial_capital) / self.initial_capital * 100:.2f}%")
        
        print("\n" + "=" * 80)
    
    def reset(self):
        """重置回测引擎"""
        self.trades = []
        self.capital = self.initial_capital
        self.equity_curve = [self.initial_capital]
        logger.info("回测引擎已重置")


def run_backtest(
    symbols: List[str],
    start_date: datetime,
    end_date: datetime,
    initial_capital: float = 1000.0,
    score_threshold: float = 7.0
) -> BacktestResult:
    """
    运行回测
    
    Args:
        symbols: 交易对列表
        start_date: 开始日期
        end_date: 结束日期
        initial_capital: 初始资金
        score_threshold: 开仓评分阈值
    
    Returns:
        BacktestResult: 回测结果
    """
    logger.info(f"开始回测：{start_date} 至 {end_date}, 交易对：{symbols}")
    
    engine = BacktestEngine(initial_capital=initial_capital)
    
    # TODO: 实现完整的回测流程
    # 1. 获取历史 K 线数据
    # 2. 获取历史 OI 数据
    # 3. 模拟历史评分
    # 4. 生成信号并模拟交易
    # 5. 计算统计指标
    
    logger.warning("回测框架已完成，但需要接入实际历史数据")
    
    # 示例：模拟几笔交易
    # 实际使用时应该从历史数据中生成信号
    engine.simulate_trade(
        symbol="BTCUSDT",
        entry_time=start_date,
        entry_price=50000.0,
        stop_loss=51500.0,
        take_profit_1=48000.0,
        take_profit_2=45000.0,
        quantity=0.01,
        leverage=5,
        score=8.5,
        price_data=[
            {'time': start_date + timedelta(hours=1), 'high': 50200, 'low': 49800, 'close': 50100},
            {'time': start_date + timedelta(hours=2), 'high': 50300, 'low': 49500, 'close': 49600},
            {'time': start_date + timedelta(hours=3), 'high': 49700, 'low': 48500, 'close': 48600},
            {'time': start_date + timedelta(hours=4), 'high': 48800, 'low': 47800, 'close': 48000},
        ]
    )
    
    result = engine.calculate_statistics()
    engine.print_report(result)
    
    return result


if __name__ == "__main__":
    # 示例运行
    start = datetime(2024, 1, 1)
    end = datetime(2024, 1, 31)
    
    result = run_backtest(
        symbols=["BTCUSDT", "ETHUSDT"],
        start_date=start,
        end_date=end,
        initial_capital=1000.0,
        score_threshold=7.0
    )
