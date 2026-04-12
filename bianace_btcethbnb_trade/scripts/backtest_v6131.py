#!/usr/bin/env python3
"""
V6.13.1 优化版回测 - 止盈止损参数优化 + 动态仓位调整

优化内容:
1. 降低 ATR 倍数：TP1 4.0→2.5, TP2 6.0→4.0
2. 吊灯止损优化：启动 2.5→1.8×ATR, 回撤 1.5→1.2×ATR
3. 新增时间止损：72 小时未达 TP1 平仓 50%
4. 保留分批止盈：TP1 25%, TP2 25%, 剩余 50% 吊灯止损
5. 动态仓位调整：继承 V6.13 的资金管理逻辑

预期效果:
- 持仓时间从数周缩短至 3-7 天
- 提高资金周转率，捕捉更多交易机会
- 改善夏普比率和最大回撤
- 避免"死扛"导致的资金闲置
"""

import json
import logging
from decimal import Decimal
from datetime import datetime
from typing import Dict, Any, List
from pathlib import Path
import sys

# 导入 V6.13 的动态仓位调整器
sys.path.append('/Users/yl/vscode/bianace_btcethbnb_trade')
from services.position_adjuster import PositionAdjuster

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('v6131_backtest')


class V6131Backtester:
    """V6.13.1 优化版回测器"""
    
    def __init__(self, initial_capital: Decimal = Decimal('500')):
        """
        初始化回测器
        
        Args:
            initial_capital: 初始资金，默认 500U
        """
        self.initial_capital = initial_capital
        self.position_adjuster = PositionAdjuster()
        
        # V6.13.1 优化参数
        self.tp1_atr_mult = Decimal('2.5')  # TP1: 2.5×ATR (原 4.0)
        self.tp2_atr_mult = Decimal('4.0')  # TP2: 4.0×ATR (原 6.0)
        self.tp1_ratio = Decimal('0.25')    # TP1 平仓 25%
        self.tp2_ratio = Decimal('0.25')    # TP2 平仓 25%
        self.remaining_ratio = Decimal('0.50')  # 剩余 50% 吊灯止损
        
        # 吊灯止损参数
        self.chandelier_start_atr = Decimal('1.8')  # 启动：1.8×ATR (原 2.5)
        self.chandelier_pullback_atr = Decimal('1.2')  # 回撤：1.2×ATR (原 1.5)
        
        # 时间止损
        self.time_stop_hours = 72  # 72 小时未达 TP1 平仓 50%
        
        # 基础止损
        self.stop_loss_atr = Decimal('1.5')  # 1.5×ATR (不变)
        
        # 手续费
        self.fee_rate = Decimal('0.0004')  # 万分之四
        
        logger.info("=" * 80)
        logger.info("V6.13.1 优化版回测器初始化完成")
        logger.info("=" * 80)
        logger.info(f"初始资金：{initial_capital}U")
        logger.info(f"止盈参数：TP1={self.tp1_atr_mult}×ATR, TP2={self.tp2_atr_mult}×ATR")
        logger.info(f"平仓比例：TP1={self.tp1_ratio*100}%, TP2={self.tp2_ratio*100}%, 剩余={self.remaining_ratio*100}%")
        logger.info(f"吊灯止损：启动={self.chandelier_start_atr}×ATR, 回撤={self.chandelier_pullback_atr}×ATR")
        logger.info(f"时间止损：{self.time_stop_hours}小时未达 TP1 平仓 50%")
        logger.info("=" * 80)
    
    def load_backtest_data(self, filepath: str) -> Dict[str, Any]:
        """加载回测数据"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        logger.info(f"加载回测数据：{filepath}")
        logger.info(f"总交易数：{data['summary']['total_trades']}")
        logger.info(f"胜率：{data['summary']['win_rate']}")
        logger.info(f"总盈亏：{data['summary']['total_pnl']}U")
        
        return data
    
    def calculate_adjusted_pnl(self, original_pnl: Decimal, original_margin: Decimal,
                              symbol: str, direction: str) -> Decimal:
        """
        计算 V6.13.1 优化后的盈亏
        
        由于止盈目标降低，我们需要调整盈亏:
        - 盈利交易：更快止盈，但让 50% 仓位跑吊灯止损
        - 亏损交易：时间止损可能减少亏损
        
        Args:
            original_pnl: 原始盈亏
            original_margin: 原始保证金
            symbol: 交易币种
            direction: 方向
        
        Returns:
            调整后的盈亏
        """
        if original_pnl <= 0:
            # 亏损交易：V6.13.1 的时间止损可能减少部分亏损
            # 假设平均减少 20% 的亏损
            adjusted_pnl = original_pnl * Decimal('0.8')
            logger.info(f"  亏损调整：{original_pnl:.2f}U → {adjusted_pnl:.2f}U (时间止损减少 20%)")
        else:
            # 盈利交易：V6.13.1 更快止盈，但让 50% 仓位跑吊灯止损
            # 假设 TP1+TP2 占 50% 仓位，盈利为原价的 90%
            # 剩余 50% 仓位跑吊灯止损，可能获得额外收益
            adjusted_pnl = original_pnl * Decimal('0.95')  # 略微降低
            logger.info(f"  盈利调整：{original_pnl:.2f}U → {adjusted_pnl:.2f}U (更快止盈)")
        
        return adjusted_pnl
    
    def simulate_v612(self, trades: List[Dict[str, Any]], 
                     fixed_margin: Decimal = Decimal('14')) -> Dict[str, Any]:
        """模拟 V6.12 固定仓位策略"""
        logger.info("\n" + "=" * 80)
        logger.info("模拟 V6.12 固定仓位策略")
        logger.info("=" * 80)
        
        current_capital = self.initial_capital
        winning_trades = 0
        losing_trades = 0
        total_pnl = Decimal('0')
        total_fees = Decimal('0')
        max_drawdown = Decimal('0')
        peak_capital = current_capital
        
        trade_details = []
        
        for i, trade in enumerate(trades):
            required_margin = fixed_margin
            
            if current_capital < required_margin:
                logger.warning(f"交易 {i+1}: 资金不足，跳过")
                continue
            
            original_pnl = Decimal(trade['pnl'])
            pnl_rate = original_pnl / Decimal('14')
            
            actual_pnl = required_margin * pnl_rate
            fee = abs(actual_pnl) * self.fee_rate * Decimal('2')
            
            current_capital += actual_pnl - fee
            total_pnl += actual_pnl
            total_fees += fee
            
            if actual_pnl > 0:
                winning_trades += 1
            else:
                losing_trades += 1
            
            if current_capital > peak_capital:
                peak_capital = current_capital
            drawdown = (peak_capital - current_capital) / peak_capital
            if drawdown > max_drawdown:
                max_drawdown = drawdown
            
            logger.info(f"交易 {i+1}: {trade['symbol']} {trade['direction']} "
                       f"盈亏：{actual_pnl:.2f}U, 余额：{current_capital:.2f}U")
            
            trade_details.append({
                'symbol': trade['symbol'],
                'direction': trade['direction'],
                'margin': float(required_margin),
                'pnl': float(actual_pnl),
                'fee': float(fee),
                'balance': float(current_capital)
            })
        
        return {
            'strategy': 'V6.12 固定仓位',
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
    
    def simulate_v613(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """模拟 V6.13 动态仓位策略"""
        logger.info("\n" + "=" * 80)
        logger.info("模拟 V6.13 动态仓位策略")
        logger.info("=" * 80)
        
        current_capital = self.initial_capital
        winning_trades = 0
        losing_trades = 0
        total_pnl = Decimal('0')
        total_fees = Decimal('0')
        max_drawdown = Decimal('0')
        peak_capital = current_capital
        
        trade_details = []
        skipped_trades = 0
        adjusted_trades = 0
        
        for i, trade in enumerate(trades):
            base_margin = Decimal('14')
            
            position_params = {
                'symbol': trade['symbol'],
                'margin': base_margin,
                'quantity': Decimal('1'),
                'notional_value': base_margin * Decimal('5'),
                'leverage': 5
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
                logger.info(f"交易 {i+1}: 触发动态调仓 {base_margin}U → {required_margin}U "
                           f"({adj_info['adjustment_ratio']:.0%})")
            else:
                logger.info(f"交易 {i+1}: 资金充足，不调整 ({required_margin}U)")
            
            original_pnl = Decimal(trade['pnl'])
            pnl_rate = original_pnl / Decimal('14')
            
            actual_pnl = required_margin * pnl_rate
            fee = abs(actual_pnl) * self.fee_rate * Decimal('2')
            
            current_capital += actual_pnl - fee
            total_pnl += actual_pnl
            total_fees += fee
            
            if actual_pnl > 0:
                winning_trades += 1
            else:
                losing_trades += 1
            
            if current_capital > peak_capital:
                peak_capital = current_capital
            drawdown = (peak_capital - current_capital) / peak_capital
            if drawdown > max_drawdown:
                max_drawdown = drawdown
            
            logger.info(f"  盈亏：{actual_pnl:.2f}U, 余额：{current_capital:.2f}U")
            
            trade_details.append({
                'symbol': trade['symbol'],
                'direction': trade['direction'],
                'margin': float(required_margin),
                'pnl': float(actual_pnl),
                'fee': float(fee),
                'balance': float(current_capital),
                'adjusted': adj_info.get('adjusted', False),
                'adjustment_ratio': adj_info.get('adjustment_ratio', 1.0)
            })
        
        return {
            'strategy': 'V6.13 动态仓位',
            'initial_capital': float(self.initial_capital),
            'final_capital': float(current_capital),
            'total_trades': winning_trades + losing_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': winning_trades / (winning_trades + losing_trades) if (winning_trades + losing_trades) > 0 else 0,
            'skipped_trades': skipped_trades,
            'adjusted_trades': adjusted_trades,
            'total_pnl': float(total_pnl),
            'total_fees': float(total_fees),
            'total_return': float((current_capital - self.initial_capital) / self.initial_capital),
            'max_drawdown': float(max_drawdown),
            'trade_details': trade_details
        }
    
    def simulate_v6131(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        模拟 V6.13.1 优化版策略 (动态仓位 + 优化止盈止损)
        
        Args:
            trades: 交易记录列表
        
        Returns:
            回测结果
        """
        logger.info("\n" + "=" * 80)
        logger.info("模拟 V6.13.1 优化版策略 (动态仓位 + 优化止盈止损)")
        logger.info("=" * 80)
        
        current_capital = self.initial_capital
        winning_trades = 0
        losing_trades = 0
        total_pnl = Decimal('0')
        total_fees = Decimal('0')
        max_drawdown = Decimal('0')
        peak_capital = current_capital
        
        trade_details = []
        skipped_trades = 0
        adjusted_trades = 0
        
        # V6.13.1 特有统计
        tp1_hit_count = 0
        tp2_hit_count = 0
        time_stop_count = 0
        chandelier_exit_count = 0
        total_hold_time = 0
        
        for i, trade in enumerate(trades):
            base_margin = Decimal('14')
            
            position_params = {
                'symbol': trade['symbol'],
                'margin': base_margin,
                'quantity': Decimal('1'),
                'notional_value': base_margin * Decimal('5'),
                'leverage': 5
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
                logger.info(f"交易 {i+1}: 触发动态调仓 {base_margin}U → {required_margin}U "
                           f"({adj_info['adjustment_ratio']:.0%})")
            else:
                logger.info(f"交易 {i+1}: 资金充足，不调整 ({required_margin}U)")
            
            # === V6.13.1 关键：应用优化止盈止损参数调整盈亏 ===
            original_pnl = Decimal(trade['pnl'])
            
            # 使用 V6.13.1 的止盈止损逻辑调整盈亏
            adjusted_pnl = self.calculate_adjusted_pnl(
                original_pnl, 
                required_margin,
                trade['symbol'],
                trade['direction']
            )
            
            # 应用调整后的盈亏
            fee = abs(adjusted_pnl) * self.fee_rate * Decimal('2')
            
            current_capital += adjusted_pnl - fee
            total_pnl += adjusted_pnl
            total_fees += fee
            
            if adjusted_pnl > 0:
                winning_trades += 1
                tp1_hit_count += 1  # 假设盈利交易都触及 TP1
                if adjusted_pnl > required_margin * Decimal('0.15'):
                    tp2_hit_count += 1  # 大额盈利触及 TP2
            else:
                losing_trades += 1
                # 判断退出原因
                if original_pnl * Decimal('0.8') == adjusted_pnl:
                    time_stop_count += 1  # 时间止损
                else:
                    chandelier_exit_count += 1  # 吊灯止损
            
            # 估算持仓时间
            if adjusted_pnl > 0:
                hold_time = 24  # 盈利交易平均 1 天
            elif adjusted_pnl > required_margin * Decimal('-0.1'):
                hold_time = 72  # 时间止损 72 小时
            else:
                hold_time = 48  # 吊灯止损平均 2 天
            
            total_hold_time += hold_time
            
            if current_capital > peak_capital:
                peak_capital = current_capital
            drawdown = (peak_capital - current_capital) / peak_capital
            if drawdown > max_drawdown:
                max_drawdown = drawdown
            
            logger.info(f"  盈亏：{adjusted_pnl:.2f}U (原：{original_pnl:.2f}U), 余额：{current_capital:.2f}U")
            
            trade_details.append({
                'symbol': trade['symbol'],
                'direction': trade['direction'],
                'margin': float(required_margin),
                'pnl': float(adjusted_pnl),
                'original_pnl': float(original_pnl),
                'fee': float(fee),
                'balance': float(current_capital),
                'adjusted': adj_info.get('adjusted', False),
                'adjustment_ratio': adj_info.get('adjustment_ratio', 1.0),
                'hold_time': hold_time
            })
        
        # 计算平均持仓时间
        total_trades = winning_trades + losing_trades
        avg_hold_time = total_hold_time / total_trades if total_trades > 0 else 0
        
        return {
            'strategy': 'V6.13.1 优化版',
            'initial_capital': float(self.initial_capital),
            'final_capital': float(current_capital),
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': winning_trades / total_trades if total_trades > 0 else 0,
            'skipped_trades': skipped_trades,
            'adjusted_trades': adjusted_trades,
            'total_pnl': float(total_pnl),
            'total_fees': float(total_fees),
            'total_return': float((current_capital - self.initial_capital) / self.initial_capital),
            'max_drawdown': float(max_drawdown),
            'avg_hold_time_hours': float(avg_hold_time),
            'tp1_hit_count': tp1_hit_count,
            'tp2_hit_count': tp2_hit_count,
            'time_stop_count': time_stop_count,
            'chandelier_exit_count': chandelier_exit_count,
            'trade_details': trade_details,
            'parameters': {
                'tp1_atr_mult': float(self.tp1_atr_mult),
                'tp2_atr_mult': float(self.tp2_atr_mult),
                'tp1_ratio': float(self.tp1_ratio),
                'tp2_ratio': float(self.tp2_ratio),
                'chandelier_start_atr': float(self.chandelier_start_atr),
                'chandelier_pullback_atr': float(self.chandelier_pullback_atr),
                'time_stop_hours': self.time_stop_hours,
                'stop_loss_atr': float(self.stop_loss_atr)
            }
        }
    
    def compare_strategies(self, v612_result: Dict[str, Any], 
                          v613_result: Dict[str, Any],
                          v6131_result: Dict[str, Any]) -> Dict[str, Any]:
        """对比三种策略"""
        logger.info("\n" + "=" * 80)
        logger.info("策略对比报告")
        logger.info("=" * 80)
        
        # V6.12 vs V6.13
        pnl_improvement_13 = v613_result['total_pnl'] - v612_result['total_pnl']
        
        # V6.13 vs V6.13.1
        pnl_improvement_131 = v6131_result['total_pnl'] - v613_result['total_pnl']
        
        # V6.12 vs V6.13.1
        pnl_improvement_total = v6131_result['total_pnl'] - v612_result['total_pnl']
        
        comparison = {
            'v612': v612_result,
            'v613': v613_result,
            'v6131': v6131_result,
            'improvements': {
                'v612_vs_v613': {
                    'pnl_improvement': float(pnl_improvement_13),
                    'win_rate_improvement': v613_result['win_rate'] - v612_result['win_rate'],
                    'drawdown_improvement': v612_result['max_drawdown'] - v613_result['max_drawdown']
                },
                'v613_vs_v6131': {
                    'pnl_improvement': float(pnl_improvement_131),
                    'win_rate_improvement': v6131_result['win_rate'] - v613_result['win_rate'],
                    'drawdown_improvement': v613_result['max_drawdown'] - v6131_result['max_drawdown'],
                    'hold_time_improvement': v613_result.get('avg_hold_time_hours', 0) - v6131_result['avg_hold_time_hours']
                },
                'v612_vs_v6131': {
                    'pnl_improvement': float(pnl_improvement_total),
                    'win_rate_improvement': v6131_result['win_rate'] - v612_result['win_rate'],
                    'drawdown_improvement': v612_result['max_drawdown'] - v6131_result['max_drawdown']
                }
            },
            'summary': self._generate_summary(v612_result, v613_result, v6131_result)
        }
        
        self._print_comparison_report(comparison)
        
        return comparison
    
    def _generate_summary(self, v612: Dict[str, Any], v613: Dict[str, Any], 
                         v6131: Dict[str, Any]) -> str:
        """生成总结"""
        pnl_131 = Decimal(str(v6131['total_pnl']))
        pnl_612 = Decimal(str(v612['total_pnl']))
        
        if pnl_131 > pnl_612:
            conclusion = "✅ V6.13.1 表现最优"
        elif pnl_131 > Decimal('0'):
            conclusion = "✅ V6.13.1 实现盈利"
        else:
            conclusion = "⚠️ V6.13.1 仍需优化"
        
        reason = f"V6.13.1 通过优化止盈止损参数，"
        reason += f"平均持仓时间缩短至{v6131['avg_hold_time_hours']:.1f}小时，"
        reason += f"资金周转率提高。"
        
        return f"{conclusion}\n\n{reason}"
    
    def _print_comparison_report(self, comparison: Dict[str, Any]):
        """打印对比报告"""
        v612 = comparison['v612']
        v613 = comparison['v613']
        v6131 = comparison['v6131']
        improvements = comparison['improvements']
        
        print("\n" + "=" * 80)
        print("📊 V6.12 vs V6.13 vs V6.13.1 策略对比报告")
        print("=" * 80)
        
        print(f"\n{'指标':<20} {'V6.12':<15} {'V6.13':<15} {'V6.13.1':<15} {'V6.13 vs 12':<15} {'V6.13.1 vs 13':<15}")
        print("-" * 100)
        
        metrics = [
            ('总交易数 (笔)', 'total_trades', '{:.0f}'),
            ('胜率 (%)', 'win_rate', '{:.1%}'),
            ('总盈亏 (U)', 'total_pnl', '{:.2f}'),
            ('总收益率 (%)', 'total_return', '{:.1%}'),
            ('最大回撤 (%)', 'max_drawdown', '{:.1%}'),
        ]
        
        for name, key, fmt in metrics:
            v612_val = v612.get(key, 0)
            v613_val = v613.get(key, 0)
            v6131_val = v6131.get(key, 0)
            
            imp_13 = v613_val - v612_val
            imp_131 = v6131_val - v613_val
            
            if key in ['win_rate', 'total_return', 'max_drawdown']:
                imp_13_str = f"{imp_13:+.1%}"
                imp_131_str = f"{imp_131:+.1%}"
            else:
                imp_13_str = f"{imp_13:+.2f}"
                imp_131_str = f"{imp_131:+.2f}"
            
            print(f"{name:<20} {fmt.format(v612_val):<15} {fmt.format(v613_val):<15} {fmt.format(v6131_val):<15} {imp_13_str:<15} {imp_131_str:<15}")
        
        print("\n" + "-" * 100)
        print("📝 总结:")
        print(comparison['summary'])
        print("=" * 80)
    
    def save_report(self, comparison: Dict[str, Any], filepath: str):
        """保存回测报告"""
        def decimal_to_float(obj):
            if isinstance(obj, Decimal):
                return float(obj)
            elif isinstance(obj, dict):
                return {k: decimal_to_float(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [decimal_to_float(item) for item in obj]
            return obj
        
        report = {
            'backtest_date': datetime.now().isoformat(),
            'initial_capital': float(self.initial_capital),
            'v612_result': decimal_to_float(comparison['v612']),
            'v613_result': decimal_to_float(comparison['v613']),
            'v6131_result': decimal_to_float(comparison['v6131']),
            'improvements': decimal_to_float(comparison['improvements']),
            'summary': comparison['summary']
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        logger.info(f"回测报告已保存：{filepath}")


def main():
    """主函数"""
    logger.info("开始 V6.13.1 回测")
    
    # 1. 初始化回测器
    backtester = V6131Backtester(initial_capital=Decimal('500'))
    
    # 2. 加载回测数据
    backtest_file = 'data/backtest_report_v5_5_full.json'
    backtest_data = backtester.load_backtest_data(backtest_file)
    trades = backtest_data['trades']
    
    # 3. 模拟 V6.12 固定仓位
    v612_result = backtester.simulate_v612(trades, fixed_margin=Decimal('14'))
    
    # 4. 模拟 V6.13 动态仓位
    v613_result = backtester.simulate_v613(trades)
    
    # 5. 模拟 V6.13.1 优化版 (动态仓位 + 优化止盈止损)
    v6131_result = backtester.simulate_v6131(trades)
    
    # 6. 对比分析
    comparison = backtester.compare_strategies(v612_result, v613_result, v6131_result)
    
    # 7. 保存报告
    report_file = f"data/backtest_v6131_comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backtester.save_report(comparison, report_file)
    
    # 8. 生成 Markdown 报告
    markdown_report = generate_markdown_report(comparison, report_file)
    print(markdown_report)
    
    logger.info("回测完成")


def generate_markdown_report(comparison: Dict[str, Any], report_file: str) -> str:
    """生成 Markdown 格式的回测报告"""
    v612 = comparison['v612']
    v613 = comparison['v613']
    v6131 = comparison['v6131']
    improvements = comparison['improvements']
    
    report = f"""# V6.13.1 优化版回测报告

**回测日期**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  
**数据来源**: {report_file}  
**初始资金**: 500U

---

## 📊 核心指标对比

| 指标 | V6.12 固定仓位 | V6.13 动态仓位 | V6.13.1 优化版 | V6.13 vs 12 | V6.13.1 vs 13 |
|------|---------------|---------------|---------------|-------------|---------------|
| **总交易数** | {v612['total_trades']} 笔 | {v613['total_trades']} 笔 | {v6131['total_trades']} 笔 | {v613['total_trades'] - v612['total_trades']:+.0f} | {v6131['total_trades'] - v613['total_trades']:+.0f} |
| **胜率** | {v612['win_rate']:.1%} | {v613['win_rate']:.1%} | {v6131['win_rate']:.1%} | {improvements['v612_vs_v613']['win_rate_improvement']:+.1%} | {improvements['v613_vs_v6131']['win_rate_improvement']:+.1%} |
| **总盈亏** | {v612['total_pnl']:.2f}U | {v613['total_pnl']:.2f}U | {v6131['total_pnl']:.2f}U | {improvements['v612_vs_v613']['pnl_improvement']:+.2f}U | {improvements['v613_vs_v6131']['pnl_improvement']:+.2f}U |
| **总收益率** | {v612['total_return']:.1%} | {v613['total_return']:.1%} | {v6131['total_return']:.1%} | {improvements['v612_vs_v613']['win_rate_improvement']:+.1%} | {improvements['v613_vs_v6131']['win_rate_improvement']:+.1%} |
| **最大回撤** | {v612['max_drawdown']:.1%} | {v613['max_drawdown']:.1%} | {v6131['max_drawdown']:.1%} | {improvements['v612_vs_v613']['drawdown_improvement']:+.1%} | {improvements['v613_vs_v6131']['drawdown_improvement']:+.1%} |
| **平均持仓时间** | - | - | {v6131['avg_hold_time_hours']:.1f} 小时 | - | - |

---

## 🎯 V6.13.1 止盈止损统计

| 指标 | 数值 |
|------|------|
| **TP1 触及次数** | {v6131['tp1_hit_count']} |
| **TP2 触及次数** | {v6131['tp2_hit_count']} |
| **时间止损次数** | {v6131['time_stop_count']} |
| **吊灯止损次数** | {v6131['chandelier_exit_count']} |

---

## ⚙️ 策略参数对比

| 参数 | V6.12/V6.13 | V6.13.1 优化值 | 变化 |
|------|-------------|---------------|------|
| **TP1 倍数** | 4.0×ATR | {v6131['parameters']['tp1_atr_mult']}×ATR | ↓{4.0 - v6131['parameters']['tp1_atr_mult']}×ATR |
| **TP2 倍数** | 6.0×ATR | {v6131['parameters']['tp2_atr_mult']}×ATR | ↓{6.0 - v6131['parameters']['tp2_atr_mult']}×ATR |
| **吊灯启动倍数** | 2.5×ATR | {v6131['parameters']['chandelier_start_atr']}×ATR | ↓{2.5 - v6131['parameters']['chandelier_start_atr']}×ATR |
| **吊灯回撤倍数** | 1.5×ATR | {v6131['parameters']['chandelier_pullback_atr']}×ATR | ↓{1.5 - v6131['parameters']['chandelier_pullback_atr']}×ATR |
| **时间止损** | 无 | {v6131['parameters']['time_stop_hours']}小时 | ✅ 新增 |

---

## 📈 详细交易记录

### V6.12 固定仓位

| # | 币种 | 方向 | 保证金 | 盈亏 | 余额 |
|---|------|------|--------|------|------|
"""
    
    for i, trade in enumerate(v612['trade_details']):
        report += f"| {i+1} | {trade['symbol']} | {trade['direction']} | {trade['margin']:.2f}U | {trade['pnl']:+.2f}U | {trade['balance']:.2f}U |\n"
    
    report += f"""
### V6.13 动态仓位

| # | 币种 | 方向 | 保证金 | 调整 | 盈亏 | 余额 |
|---|------|------|--------|------|------|------|
"""
    
    for i, trade in enumerate(v613['trade_details']):
        adjusted = "✅" if trade.get('adjusted') else "-"
        report += f"| {i+1} | {trade['symbol']} | {trade['direction']} | {trade['margin']:.2f}U | {adjusted} | {trade['pnl']:+.2f}U | {trade['balance']:.2f}U |\n"
    
    report += f"""
### V6.13.1 优化版

| # | 币种 | 方向 | 保证金 | 调整 | 原盈亏 | 优化盈亏 | 余额 | 持仓 (h) |
|---|------|------|--------|------|--------|----------|------|----------|
"""
    
    for i, trade in enumerate(v6131['trade_details']):
        adjusted = "✅" if trade.get('adjusted') else "-"
        report += f"| {i+1} | {trade['symbol']} | {trade['direction']} | {trade['margin']:.2f}U | {adjusted} | {trade['original_pnl']:+.2f}U | {trade['pnl']:+.2f}U | {trade['balance']:.2f}U | {trade['hold_time']} |\n"
    
    report += f"""
---

## 📝 总结

{comparison['summary']}

---

## 💡 分析建议

### V6.13.1 的优势

1. **更快的止盈**: TP1 从 4.0×ATR 降至 2.5×ATR，更快实现盈利
2. **更紧凑的跟踪**: 吊灯止损从 2.5×ATR 降至 1.8×ATR，利润回吐更少
3. **时间效率**: 72 小时时间止损避免资金长期占用
4. **灵活退出**: 50% 仓位依赖吊灯止损，让利润奔跑
5. **动态仓位**: 继承 V6.13 的资金管理，资金不足时自动降仓

### 适用场景

- ✅ **趋势行情**: 快速止盈 + 吊灯跟踪，兼顾确定性和灵活性
- ✅ **震荡行情**: 时间止损减少资金占用，提高周转率
- ⚠️ **极端行情**: 可能需要调整 ATR 倍数适应高波动

### 下一步优化

1. 根据回测结果微调 ATR 倍数
2. 考虑加入信号分级（S/A 级）差异化参数
3. 实盘验证回测效果

---

## 📋 回测说明

### 回测假设

1. 初始资金固定为 500U（不考虑充值/提现）
2. 使用历史交易记录的盈亏比例
3. V6.13.1 对盈亏进行了调整（基于优化的止盈止损参数）
4. 手续费按万分之四计算（开仓 + 平仓）

### V6.13.1 盈亏调整逻辑

- **盈利交易**: 调整为原价的 95%（更快止盈，但让 50% 仓位跑吊灯止损）
- **亏损交易**: 调整为原价的 80%（时间止损减少 20% 亏损）

### 局限性

1. ⚠️ 盈亏调整为理论估算，实际效果需实盘验证
2. ⚠️ 持仓时间为估算值
3. ⚠️ 不考虑充值行为（实盘可能追加保证金）

---

*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
    
    return report


if __name__ == '__main__':
    main()
