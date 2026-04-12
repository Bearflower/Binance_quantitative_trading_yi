#!/usr/bin/env python3
"""
v6.13 动态仓位调整回测（真实多周期共振策略）

数据源：data/multi_timeframe_data.json
- 6 个月历史 K 线数据（2025-10-04 至 2026-04-07）
- 3 个币种：BTCUSDT, ETHUSDT, BNBUSDT
- 3 个周期：1d, 4h, 1h

策略逻辑：
1. 日线 EMA21 判断趋势方向
2. 4 小时 K 线寻找入场时机
3. 1 小时 K 线精确入场点
4. 符合多周期共振才开仓
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
logger = logging.getLogger('v613_backtest_multi')


def calculate_ema(data: List[Dict], period: int = 21) -> List[Optional[Decimal]]:
    """计算 EMA"""
    if len(data) < period:
        return [None] * len(data)
    
    ema_values = []
    multiplier = Decimal(2) / (Decimal(period) + 1)
    
    # 第一个 EMA 使用 SMA
    first_sma = sum(Decimal(k['close']) for k in data[:period]) / period
    ema_values.append(first_sma)
    
    current_ema = first_sma
    
    for i in range(period, len(data)):
        close = Decimal(data[i]['close'])
        current_ema = (close - current_ema) * multiplier + current_ema
        ema_values.append(current_ema)
    
    # 前面填充 None
    ema_values = [None] * (period - 1) + ema_values
    
    return ema_values


def generate_multi_timeframe_signals(data: Dict[str, Dict[str, List[Dict]]]) -> List[Dict[str, Any]]:
    """
    生成多周期共振信号
    
    策略逻辑：
    1. 日线 EMA21 判断趋势（价格在 EMA21 上方为多头，下方为空头）
    2. 4 小时 K 线回调/反弹入场
    3. 1 小时 K 线确认入场时机
    
    Returns:
        信号列表
    """
    signals = []
    
    for symbol, timeframes in data.items():
        logger.info(f"分析 {symbol} 的多周期信号...")
        
        daily_klines = timeframes.get('1d', [])
        k4h_klines = timeframes.get('4h', [])
        k1h_klines = timeframes.get('1h', [])
        
        if not (daily_klines and k4h_klines and k1h_klines):
            logger.warning(f"  {symbol} 数据不完整，跳过")
            continue
        
        # 计算日线 EMA21
        daily_ema21 = calculate_ema(daily_klines, 21)
        
        # 遍历 4 小时 K 线
        for i in range(21, len(k4h_klines)):
            k4h_current = k4h_klines[i]
            k4h_prev = k4h_klines[i-1]
            
            # 获取对应的日线数据（4 小时 K 线对应最近的日线）
            k4h_time = datetime.fromisoformat(k4h_current['timestamp'].replace('Z', '+00:00'))
            daily_index = min(i // 6, len(daily_klines) - 1)  # 6 根 4 小时 K 线 = 1 根日线
            
            if daily_index >= len(daily_ema21) or daily_ema21[daily_index] is None:
                continue
            
            daily_close = Decimal(daily_klines[daily_index]['close'])
            daily_ema = daily_ema21[daily_index]
            
            # 判断日线趋势
            is_bullish = daily_close > daily_ema
            is_bearish = daily_close < daily_ema
            
            # 4 小时 K 线回调/反弹判断
            if is_bullish:
                # 多头趋势：4 小时 K 线回调后企稳
                if (Decimal(k4h_prev['close']) < Decimal(k4h_prev['open']) and  # 前一根阴线
                    Decimal(k4h_current['close']) > Decimal(k4h_current['open']) and  # 当前阳线
                    Decimal(k4h_current['close']) > Decimal(k4h_prev['high'])):  # 突破前高
                    
                    # 在 1 小时 K 线中找精确入场点
                    k1h_entry = find_1h_entry(k1h_klines, i, is_bullish)
                    
                    if k1h_entry:
                        signals.append({
                            'symbol': symbol,
                            'direction': '多',
                            'timestamp': k4h_current['timestamp'],
                            'entry_price': Decimal(k1h_entry['close']),
                            'signal_grade': 'A' if daily_close > daily_ema * Decimal('1.02') else 'B',
                            'timeframe': '4h'
                        })
            
            elif is_bearish:
                # 空头趋势：4 小时 K 线反弹后受阻
                if (Decimal(k4h_prev['close']) > Decimal(k4h_prev['open']) and  # 前一根阳线
                    Decimal(k4h_current['close']) < Decimal(k4h_current['open']) and  # 当前阴线
                    Decimal(k4h_current['close']) < Decimal(k4h_prev['low'])):  # 跌破前低
                    
                    # 在 1 小时 K 线中找精确入场点
                    k1h_entry = find_1h_entry(k1h_klines, i, False)
                    
                    if k1h_entry:
                        signals.append({
                            'symbol': symbol,
                            'direction': '空',
                            'timestamp': k4h_current['timestamp'],
                            'entry_price': Decimal(k1h_entry['close']),
                            'signal_grade': 'A' if daily_close < daily_ema * Decimal('0.98') else 'B',
                            'timeframe': '4h'
                        })
        
        logger.info(f"  {symbol} 生成 {len([s for s in signals if s['symbol'] == symbol])} 个信号")
    
    logger.info(f"总计生成 {len(signals)} 个信号")
    return signals


def find_1h_entry(k1h_klines: List[Dict], index_4h: int, is_bullish: bool) -> Optional[Dict]:
    """在 1 小时 K 线中找精确入场点"""
    # 简化：直接使用 4 小时 K 线对应时间的 1 小时 K 线
    k1h_index = min(index_4h * 4, len(k1h_klines) - 1)  # 4 根 1 小时 K 线 = 1 根 4 小时 K 线
    
    if k1h_index >= len(k1h_klines):
        return None
    
    return k1h_klines[k1h_index]


class V613MultiTimeframeBacktester:
    """v6.13 多周期回测器"""
    
    def __init__(self, initial_capital: Decimal = Decimal('500')):
        self.initial_capital = initial_capital
        self.position_adjuster = PositionAdjuster()
        
        logger.info("=" * 80)
        logger.info("v6.13 多周期回测器初始化完成")
        logger.info("=" * 80)
        logger.info(f"初始资金：{initial_capital}U")
        logger.info(f"安全垫比例：{self.position_adjuster.safety_ratio}")
        logger.info(f"最小保证金：{self.position_adjuster.min_position_margin}U")
        logger.info("=" * 80)
    
    def simulate_v612(self, signals: List[Dict], 
                     fixed_margin: Decimal = Decimal('14')) -> Dict[str, Any]:
        """模拟 v6.12 固定仓位策略"""
        logger.info("\n" + "=" * 80)
        logger.info("模拟 v6.12 固定仓位策略")
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
        
        for signal in signals:
            if position is None:
                # 开仓
                if current_capital >= fixed_margin:
                    position = {
                        'entry_price': signal['entry_price'],
                        'direction': signal['direction'],
                        'margin': fixed_margin,
                        'entry_time': signal['timestamp']
                    }
                    logger.info(f"开仓：{signal['timestamp']} {signal['symbol']} {signal['direction']} @ {signal['entry_price']} (保证金：{fixed_margin}U)")
            
            else:
                # 平仓（简化：持仓 6 根 4 小时 K 线后平仓，即 24 小时）
                # 实际策略应该使用止损止盈
                exit_price = signal['entry_price']
                entry_price = position['entry_price']
                
                # 计算盈亏
                if position['direction'] == '多':
                    pnl_rate = (exit_price - entry_price) / entry_price
                else:
                    pnl_rate = (entry_price - exit_price) / entry_price
                
                actual_pnl = position['margin'] * pnl_rate
                fee = abs(actual_pnl) * Decimal('0.0004') * 2
                
                # 更新资金
                current_capital += actual_pnl - fee
                total_pnl += actual_pnl
                total_fees += fee
                
                # 统计
                if actual_pnl > 0:
                    winning_trades += 1
                else:
                    losing_trades += 1
                
                # 更新峰值和回撤
                if current_capital > peak_capital:
                    peak_capital = current_capital
                drawdown = (peak_capital - current_capital) / peak_capital
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
                
                logger.info(f"平仓：{signal['timestamp']} (盈亏：{actual_pnl:+.2f}U, 余额：{current_capital:.2f}U)")
                
                trade_details.append({
                    'entry_time': position['entry_time'],
                    'exit_time': signal['timestamp'],
                    'symbol': signal['symbol'],
                    'direction': position['direction'],
                    'entry_price': float(entry_price),
                    'exit_price': float(exit_price),
                    'margin': float(position['margin']),
                    'pnl': float(actual_pnl),
                    'fee': float(fee),
                    'balance': float(current_capital)
                })
                
                position = None
        
        # 生成回测报告
        return {
            'strategy': 'v6.12 固定仓位',
            'initial_capital': float(self.initial_capital),
            'final_capital': float(current_capital),
            'total_trades': winning_trades + losing_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': winning_trades / (winning_trades + losing_trades) if (winning_trades + losing_trades) > 0 else 0,
            'total_pnl': float(total_pnl),
            'total_fees': float(total_fees),
            'total_return': float((current_capital - self.initial_capital) / self.initial_capital),
            'max_drawdown': float(max_drawdown),
            'trade_details': trade_details
        }
    
    def simulate_v613(self, signals: List[Dict]) -> Dict[str, Any]:
        """模拟 v6.13 动态仓位策略"""
        logger.info("\n" + "=" * 80)
        logger.info("模拟 v6.13 动态仓位调整策略")
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
        adjusted_trades = 0
        skipped_trades = 0
        
        for signal in signals:
            if position is None:
                # 开仓
                base_margin = Decimal('14')
                
                # v6.13: 动态仓位调整
                position_params = {
                    'symbol': signal['symbol'],
                    'margin': base_margin,
                    'quantity': base_margin * Decimal('5') / signal['entry_price'],
                    'notional_value': base_margin * Decimal('5'),
                    'leverage': 5
                }
                
                adjusted_position = self.position_adjuster.adjust_position(
                    position_params, 
                    current_capital
                )
                
                if adjusted_position is None:
                    logger.warning(f"跳过：{signal['timestamp']} 资金不足")
                    skipped_trades += 1
                    continue
                
                adj_info = adjusted_position.get('adjustment_info', {})
                required_margin = adjusted_position['margin']
                
                if adj_info.get('adjusted'):
                    adjusted_trades += 1
                    logger.info(f"开仓：{signal['timestamp']} {signal['symbol']} {signal['direction']} @ {signal['entry_price']} (动态调仓：{base_margin}U → {required_margin}U)")
                else:
                    logger.info(f"开仓：{signal['timestamp']} {signal['symbol']} {signal['direction']} @ {signal['entry_price']} (保证金：{required_margin}U)")
                
                position = {
                    'entry_price': signal['entry_price'],
                    'direction': signal['direction'],
                    'margin': required_margin,
                    'entry_time': signal['timestamp']
                }
            
            else:
                # 平仓
                exit_price = signal['entry_price']
                entry_price = position['entry_price']
                
                # 计算盈亏
                if position['direction'] == '多':
                    pnl_rate = (exit_price - entry_price) / entry_price
                else:
                    pnl_rate = (entry_price - exit_price) / entry_price
                
                actual_pnl = position['margin'] * pnl_rate
                fee = abs(actual_pnl) * Decimal('0.0004') * 2
                
                # 更新资金
                current_capital += actual_pnl - fee
                total_pnl += actual_pnl
                total_fees += fee
                
                # 统计
                if actual_pnl > 0:
                    winning_trades += 1
                else:
                    losing_trades += 1
                
                # 更新峰值和回撤
                if current_capital > peak_capital:
                    peak_capital = current_capital
                drawdown = (peak_capital - current_capital) / peak_capital
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
                
                logger.info(f"平仓：{signal['timestamp']} (盈亏：{actual_pnl:+.2f}U, 余额：{current_capital:.2f}U)")
                
                trade_details.append({
                    'entry_time': position['entry_time'],
                    'exit_time': signal['timestamp'],
                    'symbol': signal['symbol'],
                    'direction': position['direction'],
                    'entry_price': float(entry_price),
                    'exit_price': float(exit_price),
                    'margin': float(position['margin']),
                    'pnl': float(actual_pnl),
                    'fee': float(fee),
                    'balance': float(current_capital),
                    'adjusted': position.get('adjusted', False)
                })
                
                position = None
        
        # 生成回测报告
        return {
            'strategy': 'v6.13 动态仓位',
            'initial_capital': float(self.initial_capital),
            'final_capital': float(current_capital),
            'total_trades': winning_trades + losing_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': winning_trades / (winning_trades + losing_trades) if (winning_trades + losing_trades) > 0 else 0,
            'adjusted_trades': adjusted_trades,
            'skipped_trades': skipped_trades,
            'total_pnl': float(total_pnl),
            'total_fees': float(total_fees),
            'total_return': float((current_capital - self.initial_capital) / self.initial_capital),
            'max_drawdown': float(max_drawdown),
            'trade_details': trade_details
        }
    
    def compare_and_save(self, v612_result: Dict, v613_result: Dict):
        """对比并保存结果"""
        logger.info("\n" + "=" * 80)
        logger.info("策略对比报告")
        logger.info("=" * 80)
        
        print("\n" + "=" * 80)
        print("📊 v6.12 vs v6.13 策略对比报告")
        print("=" * 80)
        
        print(f"\n{'指标':<20} {'v6.12 固定仓位':<20} {'v6.13 动态仓位':<20} {'改善':<20}")
        print("-" * 80)
        
        metrics = [
            ('总交易数 (笔)', 'total_trades', '{:.0f}'),
            ('胜率 (%)', 'win_rate', '{:.1%}'),
            ('总盈亏 (U)', 'total_pnl', '{:.2f}'),
            ('总收益率 (%)', 'total_return', '{:.1%}'),
            ('最大回撤 (%)', 'max_drawdown', '{:.1%}'),
            ('最终资金 (U)', 'final_capital', '{:.2f}'),
        ]
        
        for name, key, fmt in metrics:
            v612_val = v612_result.get(key, 0)
            v613_val = v613_result.get(key, 0)
            
            if key in ['win_rate', 'total_return', 'max_drawdown']:
                improvement = f"{v613_val - v612_val:+.1%}"
            else:
                improvement = f"{v613_val - v612_val:+.2f}"
            
            print(f"{name:<20} {fmt.format(v612_val):<20} {fmt.format(v613_val):<20} {improvement:<20}")
        
        # v6.13 特有指标
        print(f"{'调整后交易数 (笔)':<20} {'-':<20} {v613_result['adjusted_trades']:<20} {'-':<20}")
        print(f"{'跳过交易数 (笔)':<20} {'-':<20} {v613_result['skipped_trades']:<20} {'-':<20}")
        
        print("\n" + "-" * 80)
        if v613_result['total_pnl'] > v612_result['total_pnl']:
            print("✅ v6.13 表现更优")
        elif v613_result['total_pnl'] < v612_result['total_pnl']:
            print("⚠️ v6.12 表现更优")
        else:
            print("➖ 两者表现相当")
        print("=" * 80)
        
        # 保存报告
        report = {
            'backtest_date': datetime.now().isoformat(),
            'data_source': 'data/multi_timeframe_data.json',
            'strategy_type': '多周期共振（日线+4h+1h）',
            'initial_capital': float(self.initial_capital),
            'v612_result': v612_result,
            'v613_result': v613_result,
        }
        
        report_file = f'data/backtest_v613_multi_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        logger.info(f"回测报告已保存：{report_file}")


def main():
    """主函数"""
    logger.info("开始 v6.13 多周期回测")
    
    # 1. 初始化回测器
    backtester = V613MultiTimeframeBacktester(initial_capital=Decimal('500'))
    
    # 2. 加载数据
    with open('data/multi_timeframe_data.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    logger.info(f"加载数据完成：{list(data.keys())}")
    
    # 3. 生成多周期信号
    signals = generate_multi_timeframe_signals(data)
    logger.info(f"生成 {len(signals)} 个交易信号")
    
    # 4. 模拟 v6.12 固定仓位
    v612_result = backtester.simulate_v612(signals, fixed_margin=Decimal('14'))
    
    # 5. 模拟 v6.13 动态仓位
    v613_result = backtester.simulate_v613(signals)
    
    # 6. 对比分析
    backtester.compare_and_save(v612_result, v613_result)
    
    logger.info("回测完成")


if __name__ == '__main__':
    main()
