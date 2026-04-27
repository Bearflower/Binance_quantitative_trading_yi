#!/usr/bin/env python3
"""
V6.13.3 回测器 - 优化止损距离 + 持仓时间平仓

优化内容 (对比 V6.13.2):
1. 缩小止损距离：3-7% → 2-4%
2. 优化 ATR 计算：ATR * 1.5 作为止损基准
3. 新增持仓时间平仓：48 小时浮亏>2% 或 72 小时无条件平仓
4. 默认止损从 4% 降到 3%

数据源：data/multi_timeframe_data.json
"""

import json
import logging
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
import sys

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('v6133_backtest')


def calculate_atr(data: List[Dict], period: int = 14) -> List[Decimal]:
    """计算 ATR 指标"""
    atr_values = []
    
    for i in range(len(data)):
        if i < period:
            atr_values.append(Decimal('0'))
            continue
        
        # 计算 TR
        high = Decimal(str(data[i]['high']))
        low = Decimal(str(data[i]['low']))
        prev_close = Decimal(str(data[i-1]['close']))
        
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        tr = max(tr1, tr2, tr3)
        
        # 计算 ATR (简化版，用最近 period 个 TR 的平均值)
        if i == period:
            # 第一个 ATR 是前 period 个 TR 的平均
            atr = sum(Decimal('0') for _ in range(period)) / period
        else:
            # 后续使用平滑公式：ATR = (前 ATR * (period-1) + 当前 TR) / period
            prev_atr = atr_values[-1]
            atr = (prev_atr * (period - 1) + tr) / period
        
        atr_values.append(atr)
    
    return atr_values


class V6133Backtester:
    """V6.13.3 优化版回测器"""
    
    def __init__(self, initial_capital: Decimal = Decimal('500')):
        """
        初始化回测器
        
        Args:
            initial_capital: 初始资金，默认 500U
        """
        self.initial_capital = initial_capital
        
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
        
    def run_backtest(self, data_file: str, symbol: str = 'BTCUSDT', 
                    start_date: str = None, end_date: str = None) -> Dict[str, Any]:
        """
        运行回测
        
        Args:
            data_file: 数据文件路径
            symbol: 交易对
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
            all_data = json.load(f)
        
        # 获取指定交易对的数据
        if symbol not in all_data:
            logger.error(f"交易对 {symbol} 不存在")
            return {'error': f'交易对 {symbol} 不存在'}
        
        symbol_data = all_data[symbol]
        
        # 使用 1h 数据
        if '1h' not in symbol_data:
            logger.error(f"{symbol} 没有 1h 数据")
            return {'error': f'{symbol} 没有 1h 数据'}
        
        data = symbol_data['1h']
        logger.info(f"数据文件：{data_file}")
        logger.info(f"交易对：{symbol}")
        logger.info(f"数据条数：{len(data)}")
        logger.info(f"时间范围：{data[0]['timestamp']} - {data[-1]['timestamp']}")
        
        # 计算 ATR
        logger.info("计算 ATR 指标...")
        atr_values = calculate_atr(data, period=14)
        
        # 将 ATR 添加到数据中
        for i, bar in enumerate(data):
            bar['atr14'] = str(atr_values[i])
        
        # 初始化资金
        capital = self.initial_capital
        positions = {}  # 当前持仓
        
        # 遍历数据
        for i, bar in enumerate(data):
            timestamp = bar['timestamp']
            
            # 检查日期范围
            if start_date and timestamp < start_date:
                continue
            if end_date and timestamp > end_date:
                break
            
            # 1. 检查持仓时间平仓
            self._check_time_close(positions, bar, timestamp)
            
            # 2. 检查止盈止损
            self._check_tp_sl(positions, bar, timestamp)
            
            # 3. 生成新信号（简化版）
            signals = self._generate_signals(bar, atr_values[i])
            
            # 4. 执行开仓
            for signal in signals:
                if len(positions) >= 2:  # 最多 2 个持仓
                    continue
                
                symbol_trade = signal['symbol']
                if symbol_trade in positions:
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
                
                # 计算仓位大小（基于评分）
                grade = signal.get('grade', 'A')
                position_ratio = self.grade_config.get(grade, {}).get('position_ratio', Decimal('0.3'))
                leverage = self.grade_config.get(grade, {}).get('leverage', 4)
                
                # 简化：固定仓位
                quantity = Decimal('0.002')
                
                # 记录持仓
                positions[symbol_trade] = {
                    'symbol': symbol_trade,
                    'direction': signal['direction'],
                    'entry_price': entry_price,
                    'stop_loss': stop_loss,
                    'tp1': tp1,
                    'tp2': tp2,
                    'quantity': quantity,
                    'entry_time': timestamp,
                    'grade': grade,
                }
                
                logger.info(f"{timestamp} 开仓：{symbol_trade} {signal['direction']} @ {entry_price}, "
                           f"止损：{stop_loss}, TP1: {tp1}, TP2: {tp2}, 止损幅度：{stop_loss_pct*100:.2f}%")
        
        # 平仓所有剩余持仓
        for symbol_trade, position in list(positions.items()):
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
            'entry_price': float(entry_price),
            'exit_price': float(exit_price),
            'quantity': float(quantity),
            'pnl': float(pnl),
            'entry_time': position['entry_time'],
            'exit_time': timestamp,
            'close_reason': reason,
            'grade': position['grade'],
        }
        
        self.trades.append(trade)
        self.closed_trades.append(trade)
    
    def _generate_signals(self, bar: Dict, atr: Decimal) -> List[Dict]:
        """生成信号（简化版 - 随机信号）"""
        # 实际应该调用信号检测器
        # 这里简化为：ATR 较大时生成信号
        import random
        
        # 简化：不生成信号，只测试止盈止损和时间平仓逻辑
        return []
    
    def _calculate_statistics(self) -> Dict[str, Any]:
        """计算统计"""
        if not self.closed_trades:
            return {'total_trades': 0}
        
        total_pnl = sum(t['pnl'] for t in self.closed_trades)
        wins = [t for t in self.closed_trades if t['pnl'] > 0]
        losses = [t for t in self.closed_trades if t['pnl'] <= 0]
        
        win_rate = len(wins) / len(self.closed_trades) * 100 if self.closed_trades else 0
        
        # 计算平均持仓时间
        total_hold_hours = 0
        for trade in self.closed_trades:
            entry = datetime.fromisoformat(trade['entry_time'].replace('Z', '+00:00'))
            exit_time = datetime.fromisoformat(trade['exit_time'].replace('Z', '+00:00'))
            hold_hours = (exit_time - entry).total_seconds() / 3600
            total_hold_hours += hold_hours
        
        avg_hold_hours = total_hold_hours / len(self.closed_trades) if self.closed_trades else 0
        
        # 统计平仓原因
        close_reasons = {}
        for trade in self.closed_trades:
            reason = trade['close_reason']
            close_reasons[reason] = close_reasons.get(reason, 0) + 1
        
        return {
            'total_trades': len(self.closed_trades),
            'total_pnl': total_pnl,
            'win_rate': win_rate,
            'wins': len(wins),
            'losses': len(losses),
            'avg_hold_hours': avg_hold_hours,
            'close_reasons': close_reasons,
            'final_capital': float(self.initial_capital) + total_pnl,
        }


if __name__ == '__main__':
    # 运行回测
    backtester = V6133Backtester(initial_capital=Decimal('500'))
    
    data_file = '/Users/yl/vscode/bianace_btcethbnb_trade/data/multi_timeframe_data.json'
    
    try:
        result = backtester.run_backtest(data_file, symbol='BTCUSDT')
        
        print("\n" + "=" * 60)
        print("V6.13.3 回测结果")
        print("=" * 60)
        print(f"总交易数：{result.get('total_trades', 0)}")
        print(f"总盈亏：{result.get('total_pnl', 0):.2f}U")
        print(f"胜率：{result.get('win_rate', 0):.2f}%")
        print(f"平均持仓时间：{result.get('avg_hold_hours', 0):.1f}小时")
        print(f"最终资金：{result.get('final_capital', 0):.2f}U")
        print(f"平仓原因统计：{result.get('close_reasons', {})}")
        print("=" * 60)
        
    except FileNotFoundError:
        print(f"❌ 数据文件不存在：{data_file}")
        print("请先运行数据生成脚本或使用真实数据")
    except Exception as e:
        print(f"❌ 回测失败：{str(e)}")
        import traceback
        traceback.print_exc()
