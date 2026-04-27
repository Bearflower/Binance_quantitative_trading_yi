#!/usr/bin/env python3
"""
V6.13.3 回测器 - 优化止损距离 + 持仓时间平仓

优化内容 (对比 V6.13.2):
1. 缩小止损距离：3-7% → 2-4%
2. 优化 ATR 计算：ATR * 1.5 作为止损基准
3. 新增持仓时间平仓：48 小时浮亏>2% 或 72 小时无条件平仓
4. 默认止损从 4% 降到 3%

预期效果:
- 止损触发率提高
- 止盈触发率提高  
- 持仓时间缩短
- 资金周转率提升
- 减少"无辜亏损"

数据源：data/multi_timeframe_data.json
"""

import json
import logging
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
import sys

# 导入 v6.13 动态仓位调整器
sys.path.append('/Users/yl/vscode/bianace_btcethbnb_trade')
from services.position_adjuster import PositionAdjuster

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('v6133_backtest')


class V6133Backtester:
    """V6.13.3 优化版回测器"""
    
    def __init__(self, initial_capital: Decimal = Decimal('500')):
        """
        初始化回测器
        
        Args:
            initial_capital: 初始资金，默认 500U
        """
        self.initial_capital = initial_capital
        self.position_adjuster = PositionAdjuster()
        
        # 评分系统配置
        self.grade_config = {
            'S': {'min_score': 85, 'position_ratio': Decimal('0.50'), 'leverage': 5},
            'A': {'min_score': 75, 'position_ratio': Decimal('0.30'), 'leverage': 4},
            'B': {'min_score': 65, 'position_ratio': Decimal('0.15'), 'leverage': 3},
            'C': {'min_score': 55, 'position_ratio': Decimal('0.05'), 'leverage': 2},
        }
        
        # v6.13.3 优化配置
        self.stop_loss_config = {
            'min_stop_loss_pct': Decimal('0.02'),  # v6.13.3: 2% (从 3% 下调)
            'max_stop_loss_pct': Decimal('0.04'),  # v6.13.3: 4% (从 7% 下调)
            'default_stop_loss_pct': Decimal('0.03'),  # v6.13.3: 3% (从 4% 下调)
            'atr_multiplier': Decimal('1.5'),  # v6.13.3: ATR * 1.5
        }
        
        # 止盈配置（保持 1.5R/2.5R）
        self.take_profit_config = {
            'tp1_multiplier': Decimal('1.5'),
            'tp2_multiplier': Decimal('2.5'),
            'tp1_ratio': Decimal('0.3'),
            'tp2_ratio': Decimal('0.3'),
            'tp3_ratio': Decimal('0.4'),
        }
        
        # v6.13.3 持仓时间平仓配置
        self.time_close_config = {
            'max_hold_hours': 48,  # 最大持仓时间 48 小时
            'emergency_hold_hours': 72,  # 紧急平仓 72 小时
            'min_loss_threshold': Decimal('0.02'),  # 浮亏阈值 2%
        }
        
        # 手续费
        self.fee_rate = Decimal('0.0004')
        
        # 回测统计
        self.trades = []
        self.closed_trades = []
        self.capital_curve = []
        
    def run_backtest(self, data_file: str, start_date: str = None, end_date: str = None) -> Dict[str, Any]:
        """
        运行回测
        
        Args:
            data_file: 数据文件路径
            start_date: 开始日期
            end_date: 结束日期
        
        Returns:
            回测结果
        """
        logger.info("=" * 60)
        logger.info("V6.13.3 回测开始")
        logger.info("=" * 60)
        
        # 加载数据
        with open(data_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        logger.info(f"数据文件：{data_file}")
        logger.info(f"数据条数：{len(data)}")
        
        # 初始化资金
        capital = self.initial_capital
        positions = {}  # 当前持仓
        
        # 遍历数据
        for i, bar in enumerate(data):
            timestamp = bar.get('timestamp')
            
            # 检查日期范围
            if start_date and timestamp < start_date:
                continue
            if end_date and timestamp > end_date:
                break
            
            # 1. 检查持仓时间平仓
            self._check_time_close(positions, bar, timestamp)
            
            # 2. 检查止盈止损
            self._check_tp_sl(positions, bar, timestamp)
            
            # 3. 生成新信号（简化版，实际应该调用信号检测器）
            signals = self._generate_signals(bar)
            
            # 4. 执行开仓
            for signal in signals:
                if len(positions) >= 2:  # 最多 2 个持仓
                    continue
                
                symbol = signal['symbol']
                if symbol in positions:
                    continue
                
                # 计算止损（v6.13.3 优化）
                entry_price = Decimal(str(bar['close']))
                atr = Decimal(str(bar.get('atr14', '0')))
                
                if atr > 0:
                    # v6.13.3: 使用 ATR * 1.5 计算止损
                    stop_loss_pct = (atr * self.stop_loss_config['atr_multiplier']) / entry_price
                    stop_loss_pct = max(
                        self.stop_loss_config['min_stop_loss_pct'],
                        min(stop_loss_pct, self.stop_loss_config['max_stop_loss_pct'])
                    )
                else:
                    stop_loss_pct = self.stop_loss_config['default_stop_loss_pct']
                
                # 计算止损价
                if signal['direction'] == 'LONG':
                    stop_loss = entry_price * (1 - stop_loss_pct)
                else:
                    stop_loss = entry_price * (1 + stop_loss_pct)
                
                # 计算止盈（基于 R 值）
                r_value = abs(entry_price - stop_loss)
                tp1 = entry_price + r_value * self.take_profit_config['tp1_multiplier']
                tp2 = entry_price + r_value * self.take_profit_config['tp2_multiplier']
                
                # 记录持仓
                positions[symbol] = {
                    'symbol': symbol,
                    'direction': signal['direction'],
                    'entry_price': entry_price,
                    'stop_loss': stop_loss,
                    'tp1': tp1,
                    'tp2': tp2,
                    'quantity': Decimal('0.002'),  # 简化
                    'entry_time': timestamp,
                    'grade': signal.get('grade', 'A'),
                }
                
                logger.info(f"{timestamp} 开仓：{symbol} @ {entry_price}, 止损：{stop_loss}, TP1: {tp1}, TP2: {tp2}")
        
        # 平仓所有剩余持仓
        for symbol, position in list(positions.items()):
            self._close_position(position, Decimal(str(data[-1]['close'])), data[-1]['timestamp'], '回测结束')
        
        # 计算统计
        result = self._calculate_statistics()
        
        logger.info("=" * 60)
        logger.info("回测完成")
        logger.info("=" * 60)
        
        return result
    
    def _check_time_close(self, positions: Dict, bar: Dict, timestamp: str):
        """v6.13.3: 检查持仓时间平仓"""
        current_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        
        for symbol, position in list(positions.items()):
            entry_time = datetime.fromisoformat(position['entry_time'].replace('Z', '+00:00'))
            hold_hours = (current_time - entry_time).total_seconds() / 3600
            
            current_price = Decimal(str(bar['close']))
            entry_price = position['entry_price']
            
            # 计算浮亏率
            if position['direction'] == 'LONG':
                pnl_rate = (current_price - entry_price) / entry_price
            else:
                pnl_rate = (entry_price - current_price) / entry_price
            
            # 检查 1: 超过 72 小时，紧急平仓
            if hold_hours >= self.time_close_config['emergency_hold_hours']:
                logger.info(f"{timestamp} 时间平仓：{symbol} 持仓{hold_hours:.1f}小时 (>= 72 小时)")
                self._close_position(position, current_price, timestamp, f'时间平仓 ({hold_hours:.1f}小时)')
                del positions[symbol]
                continue
            
            # 检查 2: 超过 48 小时且浮亏>2%
            if hold_hours >= self.time_close_config['max_hold_hours'] and pnl_rate < -self.time_close_config['min_loss_threshold']:
                logger.info(f"{timestamp} 时间平仓：{symbol} 持仓{hold_hours:.1f}小时，浮亏{pnl_rate*100:.2f}%")
                self._close_position(position, current_price, timestamp, f'时间平仓 ({hold_hours:.1f}小时，浮亏{pnl_rate*100:.2f}%)')
                del positions[symbol]
    
    def _check_tp_sl(self, positions: Dict, bar: Dict, timestamp: str):
        """检查止盈止损"""
        for symbol, position in list(positions.items()):
            current_price = Decimal(str(bar['close']))
            entry_price = position['entry_price']
            
            # 检查止损
            if position['direction'] == 'LONG':
                if current_price <= position['stop_loss']:
                    logger.info(f"{timestamp} 止损：{symbol} @ {current_price}")
                    self._close_position(position, current_price, timestamp, '止损')
                    del positions[symbol]
                    continue
                
                # 检查止盈
                if current_price >= position['tp2']:
                    logger.info(f"{timestamp} 止盈 TP2: {symbol} @ {current_price}")
                    self._close_position(position, current_price, timestamp, '止盈 TP2')
                    del positions[symbol]
                elif current_price >= position['tp1']:
                    logger.info(f"{timestamp} 止盈 TP1: {symbol} @ {current_price}")
                    self._close_position(position, current_price, timestamp, '止盈 TP1')
                    del positions[symbol]
            
            else:  # SHORT
                if current_price >= position['stop_loss']:
                    logger.info(f"{timestamp} 止损：{symbol} @ {current_price}")
                    self._close_position(position, current_price, timestamp, '止损')
                    del positions[symbol]
                    continue
                
                if current_price <= position['tp2']:
                    logger.info(f"{timestamp} 止盈 TP2: {symbol} @ {current_price}")
                    self._close_position(position, current_price, timestamp, '止盈 TP2')
                    del positions[symbol]
                elif current_price <= position['tp1']:
                    logger.info(f"{timestamp} 止盈 TP1: {symbol} @ {current_price}")
                    self._close_position(position, current_price, timestamp, '止盈 TP1')
                    del positions[symbol]
    
    def _close_position(self, position: Dict, exit_price: Decimal, timestamp: str, reason: str):
        """平仓"""
        entry_price = position['entry_price']
        quantity = position['quantity']
        
        # 计算盈亏
        if position['direction'] == 'LONG':
            pnl = (exit_price - entry_price) * quantity
        else:
            pnl = (entry_price - exit_price) * quantity
        
        # 扣除手续费
        fee = (entry_price + exit_price) * quantity * self.fee_rate
        pnl -= fee
        
        # 记录交易
        trade = {
            'symbol': position['symbol'],
            'direction': position['direction'],
            'entry_price': entry_price,
            'exit_price': exit_price,
            'quantity': quantity,
            'pnl': pnl,
            'entry_time': position['entry_time'],
            'exit_time': timestamp,
            'close_reason': reason,
            'grade': position['grade'],
        }
        
        self.trades.append(trade)
        self.closed_trades.append(trade)
    
    def _generate_signals(self, bar: Dict) -> List[Dict]:
        """生成信号（简化版）"""
        # 实际应该调用信号检测器
        # 这里简化为随机信号
        return []
    
    def _calculate_statistics(self) -> Dict[str, Any]:
        """计算统计"""
        if not self.closed_trades:
            return {'total_trades': 0}
        
        total_pnl = sum(t['pnl'] for t in self.closed_trades)
        wins = [t for t in self.closed_trades if t['pnl'] > 0]
        losses = [t for t in self.closed_trades if t['pnl'] <= 0]
        
        win_rate = len(wins) / len(self.closed_trades) * 100
        
        # 计算平均持仓时间
        total_hold_hours = 0
        for trade in self.closed_trades:
            entry = datetime.fromisoformat(trade['entry_time'].replace('Z', '+00:00'))
            exit = datetime.fromisoformat(trade['exit_time'].replace('Z', '+00:00'))
            hold_hours = (exit - entry).total_seconds() / 3600
            total_hold_hours += hold_hours
        
        avg_hold_hours = total_hold_hours / len(self.closed_trades)
        
        # 统计平仓原因
        close_reasons = {}
        for trade in self.closed_trades:
            reason = trade['close_reason']
            close_reasons[reason] = close_reasons.get(reason, 0) + 1
        
        return {
            'total_trades': len(self.closed_trades),
            'total_pnl': float(total_pnl),
            'win_rate': win_rate,
            'wins': len(wins),
            'losses': len(losses),
            'avg_hold_hours': avg_hold_hours,
            'close_reasons': close_reasons,
            'final_capital': float(self.initial_capital + total_pnl),
        }


if __name__ == '__main__':
    # 运行回测
    backtester = V6133Backtester(initial_capital=Decimal('500'))
    
    data_file = '/Users/yl/vscode/bianace_btcethbnb_trade/data/multi_timeframe_data.json'
    
    try:
        result = backtester.run_backtest(data_file)
        
        print("\n" + "=" * 60)
        print("V6.13.3 回测结果")
        print("=" * 60)
        print(f"总交易数：{result['total_trades']}")
        print(f"总盈亏：{result['total_pnl']:.2f}U")
        print(f"胜率：{result['win_rate']:.2f}%")
        print(f"平均持仓时间：{result['avg_hold_hours']:.1f}小时")
        print(f"最终资金：{result['final_capital']:.2f}U")
        print(f"平仓原因统计：{result['close_reasons']}")
        print("=" * 60)
        
    except FileNotFoundError:
        print(f"❌ 数据文件不存在：{data_file}")
        print("请先运行数据生成脚本或使用真实数据")
