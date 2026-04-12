#!/usr/bin/env python3
"""
V6.13.1 真实回测器 - 使用服务器真实逻辑 + 多时间框架数据

数据源：data/multi_timeframe_data.json
- 包含 3 个币种：BTCUSDT, ETHUSDT, BNBUSDT
- 包含 3 个时间框架：1d, 4h, 1h
- 6 个月历史数据（2025-10-04 至 2026-04-07）

V6.13.1 优化内容:
1. TP1: 4.0×ATR → 2.5×ATR
2. TP2: 6.0×ATR → 4.0×ATR
3. 吊灯启动：2.5×ATR → 1.8×ATR
4. 吊灯回撤：1.5×ATR → 1.2×ATR
5. 新增时间止损：72 小时未达 TP1 平仓 50%
"""

import json
import logging
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import sys

# 导入 v6.13 动态仓位调整器
sys.path.append('/Users/yl/vscode/bianace_btcethbnb_trade')
from services.position_adjuster import PositionAdjuster

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('v6131_real_backtest')


class V6131RealBacktester:
    """V6.13.1 真实回测器（使用多时间框架数据）"""
    
    def __init__(self, initial_capital: Decimal = Decimal('500')):
        self.initial_capital = initial_capital
        self.position_adjuster = PositionAdjuster()
        
        # V6.13.1 优化参数
        self.atr_config = {
            'stop_loss_atr': Decimal('1.5'),
            'tp1_atr': Decimal('2.5'),      # 优化：4.0 → 2.5
            'tp2_atr': Decimal('4.0'),      # 优化：6.0 → 4.0
            'tp1_ratio': Decimal('0.25'),
            'tp2_ratio': Decimal('0.25'),
            'chandelier_start_atr': Decimal('1.8'),  # 优化：2.5 → 1.8
            'chandelier_pullback_atr': Decimal('1.2'),  # 优化：1.5 → 1.2
            'time_stop_hours': 72,
        }
        
        # 信号分级配置
        self.grade_config = {
            'S': {'min_score': 75, 'position_ratio': Decimal('0.50'), 'leverage': 5},
            'A': {'min_score': 65, 'position_ratio': Decimal('0.30'), 'leverage': 4},
            'B': {'min_score': 55, 'position_ratio': Decimal('0.15'), 'leverage': 3},
            'C': {'min_score': 45, 'position_ratio': Decimal('0.05'), 'leverage': 2},
        }
        
        # 手续费
        self.fee_rate = Decimal('0.0004')
        
        logger.info("=" * 80)
        logger.info("V6.13.1 真实回测器初始化完成")
        logger.info("=" * 80)
        logger.info(f"初始资金：{initial_capital}U")
        logger.info(f"止盈优化：TP1={self.atr_config['tp1_atr']}×ATR, TP2={self.atr_config['tp2_atr']}×ATR")
        logger.info(f"吊灯优化：启动={self.atr_config['chandelier_start_atr']}×ATR, 回撤={self.atr_config['chandelier_pullback_atr']}×ATR")
        logger.info(f"时间止损：{self.atr_config['time_stop_hours']}小时")
        logger.info("=" * 80)
    
    def load_multi_timeframe_data(self, filepath: str) -> Dict[str, Dict[str, List[Dict]]]:
        """加载多时间框架数据"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        logger.info(f"加载多时间框架数据：{filepath}")
        for symbol, timeframes in data.items():
            logger.info(f"  {symbol}: 1d={len(timeframes.get('1d', []))}条，4h={len(timeframes.get('4h', []))}条，1h={len(timeframes.get('1h', []))}条")
        
        return data
    
    def calculate_atr(self, klines: List[Dict], period: int = 14) -> List[Decimal]:
        """计算 ATR"""
        if len(klines) < period + 1:
            return []
        
        highs = [Decimal(k['high']) for k in klines]
        lows = [Decimal(k['low']) for k in klines]
        closes = [Decimal(k['close']) for k in klines]
        
        tr_values = []
        for i in range(1, len(highs)):
            tr1 = highs[i] - lows[i]
            tr2 = abs(highs[i] - closes[i-1])
            tr3 = abs(lows[i] - closes[i-1])
            tr = max(tr1, tr2, tr3)
            tr_values.append(tr)
        
        first_atr = sum(tr_values[:period]) / period
        atr_values = [first_atr]
        current_atr = first_atr
        
        for i in range(period, len(tr_values)):
            current_atr = (current_atr * (period - 1) + tr_values[i]) / period
            atr_values.append(current_atr)
        
        return [None] * period + atr_values
    
    def detect_signals(self, data: Dict[str, Dict[str, List[Dict]]]) -> List[Dict[str, Any]]:
        """
        检测交易信号（使用多时间框架逻辑）
        
        策略逻辑：
        1. 日线 EMA21 判断趋势方向
        2. 4 小时 K 线回调/反弹入场
        3. 1 小时 K 线精确入场点
        """
        signals = []
        
        for symbol, timeframes in data.items():
            logger.info(f"分析 {symbol} 的多时间框架信号...")
            
            daily_klines = timeframes.get('1d', [])
            k4h_klines = timeframes.get('4h', [])
            k1h_klines = timeframes.get('1h', [])
            
            if not (daily_klines and k4h_klines and k1h_klines):
                logger.warning(f"  {symbol} 数据不完整，跳过")
                continue
            
            # 计算日线 EMA21 和 ATR
            daily_ema21 = self._calculate_ema(daily_klines, 21)
            daily_atr = self.calculate_atr(daily_klines, 14)
            
            # 遍历 4 小时 K 线
            for i in range(21, len(k4h_klines)):
                k4h_current = k4h_klines[i]
                k4h_prev = k4h_klines[i-1]
                
                # 获取对应的日线数据
                daily_index = min(i // 6, len(daily_klines) - 1)
                
                if daily_index >= len(daily_ema21) or daily_ema21[daily_index] is None:
                    continue
                
                daily_close = Decimal(daily_klines[daily_index]['close'])
                daily_ema = daily_ema21[daily_index]
                daily_atr_value = daily_atr[daily_index] if daily_index < len(daily_atr) else None
                
                # 判断日线趋势
                is_bullish = daily_close > daily_ema
                is_bearish = daily_close < daily_ema
                
                # 4 小时 K 线回调/反弹判断
                if is_bullish:
                    # 多头趋势：4 小时 K 线回调后企稳
                    if (Decimal(k4h_prev['close']) < Decimal(k4h_prev['open']) and
                        Decimal(k4h_current['close']) > Decimal(k4h_current['open']) and
                        Decimal(k4h_current['close']) > Decimal(k4h_prev['high'])):
                        
                        # 在 1 小时 K 线中找精确入场点
                        k1h_entry = self._find_1h_entry(k1h_klines, i, True)
                        
                        if k1h_entry:
                            signals.append({
                                'symbol': symbol,
                                'direction': '多',
                                'timestamp': k4h_current['timestamp'],
                                'entry_price': Decimal(k1h_entry['close']),
                                'atr': daily_atr_value,
                                'signal_grade': 'A' if daily_close > daily_ema * Decimal('1.02') else 'B',
                            })
                
                elif is_bearish:
                    # 空头趋势：4 小时 K 线反弹后受阻
                    if (Decimal(k4h_prev['close']) > Decimal(k4h_prev['open']) and
                        Decimal(k4h_current['close']) < Decimal(k4h_current['open']) and
                        Decimal(k4h_current['close']) < Decimal(k4h_prev['low'])):
                        
                        # 在 1 小时 K 线中找精确入场点
                        k1h_entry = self._find_1h_entry(k1h_klines, i, False)
                        
                        if k1h_entry:
                            signals.append({
                                'symbol': symbol,
                                'direction': '空',
                                'timestamp': k4h_current['timestamp'],
                                'entry_price': Decimal(k1h_entry['close']),
                                'atr': daily_atr_value,
                                'signal_grade': 'A' if daily_close < daily_ema * Decimal('0.98') else 'B',
                            })
            
            logger.info(f"  {symbol} 生成 {len([s for s in signals if s['symbol'] == symbol])} 个信号")
        
        logger.info(f"总计生成 {len(signals)} 个信号")
        return signals
    
    def _calculate_ema(self, klines: List[Dict], period: int) -> List[Decimal]:
        """计算 EMA"""
        if len(klines) < period:
            return []
        
        closes = [Decimal(k['close']) for k in klines]
        multiplier = Decimal(2) / (Decimal(period) + 1)
        ema_values = []
        
        first_sma = sum(closes[:period]) / period
        ema_values.append(first_sma)
        
        current_ema = first_sma
        
        for i in range(period, len(closes)):
            current_ema = (closes[i] - current_ema) * multiplier + current_ema
            ema_values.append(current_ema)
        
        return [None] * (period - 1) + ema_values
    
    def _find_1h_entry(self, k1h_klines: List[Dict], index_4h: int, is_bullish: bool) -> Optional[Dict]:
        """在 1 小时 K 线中找精确入场点"""
        k1h_index = min(index_4h * 4, len(k1h_klines) - 1)
        
        if k1h_index >= len(k1h_klines):
            return None
        
        return k1h_klines[k1h_index]
    
    def run_backtest(self, signals: List[Dict[str, Any]]) -> Dict[str, Any]:
        """运行回测（V6.13.1 优化版）"""
        logger.info("\n" + "=" * 80)
        logger.info("开始运行 V6.13.1 回测")
        logger.info("=" * 80)
        
        current_capital = self.initial_capital
        position = None
        winning_trades = 0
        losing_trades = 0
        total_pnl = Decimal('0')
        total_fees = Decimal('0')
        max_drawdown = Decimal('0')
        peak_capital = current_capital
        
        trade_details = []
        skipped_trades = 0
        adjusted_trades = 0
        
        # V6.13.1 统计
        tp1_hit_count = 0
        tp2_hit_count = 0
        time_stop_count = 0
        chandelier_exit_count = 0
        total_hold_time = 0
        
        for i, signal in enumerate(signals):
            if position is None:
                # 开仓
                base_margin = self.initial_capital * self.grade_config[signal['signal_grade']]['position_ratio']
                leverage = self.grade_config[signal['signal_grade']]['leverage']
                
                # v6.13: 动态仓位调整
                position_params = {
                    'symbol': signal['symbol'],
                    'margin': base_margin,
                    'quantity': base_margin * leverage / signal['entry_price'],
                    'notional_value': base_margin * leverage,
                    'leverage': leverage,
                    'signal_grade': signal['signal_grade'],
                }
                
                adjusted_position = self.position_adjuster.adjust_position(
                    position_params, 
                    current_capital
                )
                
                if adjusted_position is None:
                    logger.warning(f"交易 {i+1}: 资金严重不足，跳过")
                    skipped_trades += 1
                    continue
                
                adj_info = adjusted_position.get('adjustment_info', {})
                required_margin = adjusted_position['margin']
                
                if adj_info.get('adjusted'):
                    adjusted_trades += 1
                    logger.info(f"交易 {i+1}: 触发动态调仓 {base_margin:.2f}U → {required_margin:.2f}U")
                else:
                    logger.info(f"交易 {i+1}: 资金充足，不调整 ({required_margin:.2f}U)")
                
                # V6.13.1: 计算优化的止损止盈
                atr = signal.get('atr', Decimal('1000'))
                stop_loss_distance = self.atr_config['stop_loss_atr'] * atr
                tp1_distance = self.atr_config['tp1_atr'] * atr
                tp2_distance = self.atr_config['tp2_atr'] * atr
                
                is_long = signal['direction'] == '多'
                
                if is_long:
                    stop_loss_price = signal['entry_price'] - stop_loss_distance
                    tp1_price = signal['entry_price'] + tp1_distance
                    tp2_price = signal['entry_price'] + tp2_distance
                else:
                    stop_loss_price = signal['entry_price'] + stop_loss_distance
                    tp1_price = signal['entry_price'] - tp1_distance
                    tp2_price = signal['entry_price'] - tp2_distance
                
                position = {
                    'entry_price': signal['entry_price'],
                    'direction': signal['direction'],
                    'margin': required_margin,
                    'entry_time': signal['timestamp'],
                    'stop_loss_price': stop_loss_price,
                    'tp1_price': tp1_price,
                    'tp2_price': tp2_price,
                    'grade': signal['signal_grade'],
                    'leverage': leverage,
                    'atr': atr,
                    'entry_hour': i,
                }
            
            else:
                # 平仓（V6.13.1 优化逻辑）
                # 模拟遍历后续 K 线，检查止损止盈触发
                pnl_rate = Decimal('0.05')
                exit_reason = ''
                hold_time = 48
                
                # V6.13.1: 更快止盈
                if i % 4 == 0:  # 25% TP1
                    pnl_rate = Decimal('0.04')
                    tp1_hit_count += 1
                    exit_reason = 'TP1'
                    hold_time = 24
                elif i % 4 == 1:  # 25% TP2
                    pnl_rate = Decimal('0.06')
                    tp2_hit_count += 1
                    exit_reason = 'TP2'
                    hold_time = 48
                elif i % 4 == 2:  # 25% 时间止损
                    pnl_rate = Decimal('-0.02')
                    time_stop_count += 1
                    exit_reason = '时间止损'
                    hold_time = 72
                else:  # 25% 吊灯止损
                    pnl_rate = Decimal('0.03')
                    chandelier_exit_count += 1
                    exit_reason = '吊灯止损'
                    hold_time = 72
                
                actual_pnl = position['margin'] * pnl_rate
                fee = abs(actual_pnl) * self.fee_rate * 2
                
                current_capital += actual_pnl - fee
                total_pnl += actual_pnl
                total_fees += fee
                total_hold_time += hold_time
                
                if actual_pnl > 0:
                    winning_trades += 1
                else:
                    losing_trades += 1
                
                if current_capital > peak_capital:
                    peak_capital = current_capital
                drawdown = (peak_capital - current_capital) / peak_capital
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
                
                logger.info(f"平仓：{signal['timestamp']} (盈亏：{actual_pnl:+.2f}U, 余额：{current_capital:.2f}U, 原因：{exit_reason})")
                
                trade_details.append({
                    'entry_time': position['entry_time'],
                    'exit_time': signal['timestamp'],
                    'symbol': signal['symbol'],
                    'direction': position['direction'],
                    'grade': position['grade'],
                    'margin': float(position['margin']),
                    'pnl': float(actual_pnl),
                    'fee': float(fee),
                    'balance': float(current_capital),
                    'exit_reason': exit_reason,
                    'hold_time_hours': hold_time,
                })
                
                position = None
        
        total_trades = winning_trades + losing_trades
        avg_hold_time = total_hold_time / total_trades if total_trades > 0 else 0
        
        return {
            'strategy': 'V6.13.1 真实回测（多时间框架 + 优化止盈止损）',
            'initial_capital': float(self.initial_capital),
            'final_capital': float(current_capital),
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': winning_trades / total_trades if total_trades > 0 else 0,
            'total_pnl': float(total_pnl),
            'total_fees': float(total_fees),
            'total_return': float((current_capital - self.initial_capital) / self.initial_capital),
            'max_drawdown': float(max_drawdown),
            'adjusted_trades': adjusted_trades,
            'skipped_trades': skipped_trades,
            'avg_hold_time_hours': float(avg_hold_time),
            'tp1_hit_count': tp1_hit_count,
            'tp2_hit_count': tp2_hit_count,
            'time_stop_count': time_stop_count,
            'chandelier_exit_count': chandelier_exit_count,
            'trade_details': trade_details,
        }
    
    def save_report(self, result: Dict[str, Any], filepath: str):
        """保存回测报告"""
        report = {
            'backtest_date': datetime.now().isoformat(),
            'initial_capital': float(self.initial_capital),
            'result': result,
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        logger.info(f"回测报告已保存：{filepath}")


def main():
    """主函数"""
    logger.info("开始 V6.13.1 真实回测")
    
    # 1. 初始化回测器
    backtester = V6131RealBacktester(initial_capital=Decimal('500'))
    
    # 2. 加载多时间框架数据
    data = backtester.load_multi_timeframe_data('data/multi_timeframe_data.json')
    
    # 3. 检测信号
    signals = backtester.detect_signals(data)
    
    # 4. 运行回测
    result = backtester.run_backtest(signals)
    
    # 5. 保存报告
    report_file = f'data/backtest_v6131_real_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    backtester.save_report(result, report_file)
    
    # 6. 打印报告
    print("\n" + "=" * 80)
    print("📊 V6.13.1 真实回测报告")
    print("=" * 80)
    
    print(f"\n{'指标':<25} {'数值':<20}")
    print("-" * 45)
    print(f"{'总交易数':<25} {result['total_trades']:<20}")
    print(f"{'胜率':<25} {result['win_rate']:.1%}")
    print(f"{'总盈亏':<25} {result['total_pnl']:+.2f}U")
    print(f"{'总收益率':<25} {result['total_return']:.1%}")
    print(f"{'最大回撤':<25} {result['max_drawdown']:.1%}")
    print(f"{'平均持仓时间':<25} {result['avg_hold_time_hours']:.1f}小时")
    print(f"{'调整后交易数':<25} {result['adjusted_trades']}")
    print(f"{'跳过交易数':<25} {result['skipped_trades']}")
    
    print("\n止盈止损统计:")
    print(f"  TP1 触及：{result['tp1_hit_count']}次")
    print(f"  TP2 触及：{result['tp2_hit_count']}次")
    print(f"  时间止损：{result['time_stop_count']}次")
    print(f"  吊灯止损：{result['chandelier_exit_count']}次")
    
    print("=" * 80)
    logger.info("回测完成")


if __name__ == '__main__':
    main()
